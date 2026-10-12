"""Authenticated Socket.IO installations and single-destination replica routing."""

import asyncio
import hashlib
import json
import logging
import time
import uuid

import jwt
import socketio
from socketio.packet import Packet

from .client import APIError, Client, ConnectionLost, ConnectionUnavailable

log = logging.getLogger(__name__)
NAMESPACE = "/apps"
MAX_PACKET = 1048576


def dumps(value, **kwargs):
    kwargs.update(ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, **kwargs)


class JSON:
    dumps = staticmethod(dumps)
    loads = staticmethod(json.loads)


class JSONPacket(Packet):
    def decode(self, encoded_packet):
        if (
            not isinstance(encoded_packet, str)
            or len(encoded_packet.encode()) > MAX_PACKET
        ):
            raise ValueError("Socket packet exceeds 1 MiB or is not JSON")
        if encoded_packet[:1] in ("5", "6"):
            raise ValueError("Binary app packets are not supported")
        return super().decode(encoded_packet)


class AppRedisManager(socketio.AsyncRedisManager):
    async def _publish(self, data):
        # Retrying an ambiguous Redis publish could execute a WebUI write twice.
        if not self.connected:
            self._redis_connect()
        return await self.redis.publish(self.channel, self.json.dumps(data))


class AppSocket:
    def __init__(self, app, *, mode, redis_url=None):
        self.app, self.mode = app, mode
        self.fingerprint = hashlib.sha256(app._api_key.encode()).hexdigest()
        identity = hashlib.sha256(
            (
                app.manifest["id"] + app.manifest["public_key"] + self.fingerprint
            ).encode()
        ).hexdigest()
        self.prefix = f"oix:{identity}"
        self.redis = None
        manager = None
        if redis_url:
            from redis.asyncio import Redis
            from redis.asyncio.retry import Retry
            from redis.backoff import NoBackoff

            self.redis = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry=Retry(NoBackoff(), 0),
            )
            manager = AppRedisManager(
                redis_url,
                channel=self.prefix,
                redis_options={
                    "socket_connect_timeout": 5,
                    "retry": Retry(NoBackoff(), 0),
                },
            )
        self.server = socketio.AsyncServer(
            async_mode=mode,
            client_manager=manager,
            transports=["websocket"],
            allow_upgrades=False,
            max_http_buffer_size=MAX_PACKET,
            ping_interval=25,
            ping_timeout=20,
            json=JSON,
            serializer=JSONPacket,
        )
        self.peers = {}
        self.pending = {}
        self._heartbeat = None
        self._calls = {}
        self.server.on("connect", self.connect, namespace=NAMESPACE)
        self.server.on("disconnect", self.disconnect, namespace=NAMESPACE)
        self.server.on("app:connect", self.enroll, namespace=NAMESPACE)
        self.server.on("app:ready", self.ready, namespace=NAMESPACE)
        self.server.on("app:event", self.event, namespace=NAMESPACE)

    async def start(self):
        if self.redis:
            await self.redis.ping()
            # Background workers can route before this replica has any local sockets.
            if not self.server.manager_initialized:
                self.server.manager_initialized = True
                self.server.manager.initialize()
            async with asyncio.timeout(5):
                while not (
                    self.server.manager.pubsub and self.server.manager.pubsub.subscribed
                ):
                    await asyncio.sleep(0.01)
        if self._heartbeat is None:
            self._heartbeat = asyncio.create_task(self.heartbeat())

    async def connect(self, sid, environ, auth):
        try:
            self.app.authenticate(environ.get("HTTP_AUTHORIZATION", ""))
            if self.app._closing:
                return False
            if self.redis:
                await self.redis.ping()
        except Exception:
            return False
        self.pending[sid] = asyncio.create_task(self.expire(sid))

    async def expire(self, sid):
        await asyncio.sleep(20)
        if not self.peers.get(sid, {}).get("ready"):
            await self.server.disconnect(sid, namespace=NAMESPACE)

    async def enroll(self, sid, data):
        try:
            if sid not in self.pending or sid in self.peers:
                raise ValueError
            if not isinstance(data, dict) or len(dumps(data).encode()) > 16384:
                raise ValueError
            app_id, challenge = data["app_id"], data["challenge"]
            if not isinstance(app_id, str) or not 1 <= len(app_id) <= 100:
                raise ValueError
            if not isinstance(challenge, str) or not 16 <= len(challenge) <= 128:
                raise ValueError
            token = data.get("connection_token")
            now = int(time.time())
            if token is not None:
                if not isinstance(token, str) or len(token) > 8192:
                    raise ValueError
                if jwt.get_unverified_header(token).get("typ") != "oix-connection+jwt":
                    raise ValueError
                claims = jwt.decode(
                    token,
                    self.app._private_key.public_key(),
                    algorithms=["EdDSA"],
                    audience="oix:connection",
                    options={"require": ["sub", "app_id", "manifest_id", "key", "iat"]},
                )
                if (
                    claims["app_id"] != app_id
                    or claims["manifest_id"] != self.app.manifest["id"]
                    or claims["key"] != self.fingerprint
                ):
                    raise ValueError
                installation_id = str(uuid.UUID(claims["sub"]))
            else:
                installation_id = str(uuid.uuid4())
                token = jwt.encode(
                    {
                        "sub": installation_id,
                        "app_id": app_id,
                        "manifest_id": self.app.manifest["id"],
                        "key": self.fingerprint,
                        "iat": now,
                        "aud": "oix:connection",
                    },
                    self.app._private_key,
                    algorithm="EdDSA",
                    headers={"typ": "oix-connection+jwt"},
                )
            proof = jwt.encode(
                {
                    "sub": installation_id,
                    "app_id": app_id,
                    "manifest_id": self.app.manifest["id"],
                    "challenge": challenge,
                    "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                    "iat": now,
                    "exp": now + 60,
                    "aud": "oix:connect",
                },
                self.app._private_key,
                algorithm="EdDSA",
                headers={"typ": "oix-connect+jwt"},
            )
            self.peers[sid] = {
                "installation_id": installation_id,
                "app_id": app_id,
                "ready": False,
            }
            return {
                "status": 200,
                "data": {
                    "installation_id": installation_id,
                    "connection_token": token,
                    "proof": proof,
                },
            }
        except (KeyError, TypeError, ValueError, jwt.PyJWTError):
            return {
                "status": 403,
                "error": "Invalid connection credential or handshake",
            }

    async def ready(self, sid, data):
        peer = self.peers.get(sid)
        if not peer or peer["ready"] or self.app._closing:
            return {"status": 403, "error": "Connection is not awaiting activation"}
        try:
            result = await self.server.call(
                "app:request",
                {"operation": "ping", "arguments": {}},
                to=sid,
                namespace=NAMESPACE,
                timeout=10,
            )
            if result != {"status": 200, "data": {"received": True}}:
                raise ValueError
            peer["ready"] = True
            await self.register(sid, peer)
            pending = self.pending.pop(sid, None)
            if pending:
                pending.cancel()
            return {"status": 200, "data": {"received": True}}
        except Exception:
            peer["ready"] = False
            return {"status": 503, "error": "Connection readiness check failed"}

    def route(self, sid, peer):
        return dumps({"sid": sid, "app_id": peer["app_id"]})

    async def register(self, sid, peer):
        if self.redis:
            key = self.prefix + ":routes:" + peer["installation_id"]
            async with self.redis.pipeline(transaction=False) as pipe:
                pipe.zadd(key, {self.route(sid, peer): time.time() + 45})
                pipe.expire(key, 90)
                await pipe.execute()

    async def heartbeat(self):
        while True:
            try:
                for sid, peer in list(self.peers.items()):
                    if peer["ready"]:
                        await self.register(sid, peer)
            except Exception:
                log.warning("App routing store unavailable")
                for sid in list(self.peers):
                    await self.server.disconnect(sid, namespace=NAMESPACE)
            await asyncio.sleep(15)

    async def disconnect(self, sid, reason=None):
        pending = self.pending.pop(sid, None)
        if pending and pending is not asyncio.current_task():
            pending.cancel()
        peer = self.peers.pop(sid, None)
        for task in self._calls.get(sid, set()).copy():
            task.cancel()
        if self.redis and peer:
            try:
                await self.redis.zrem(
                    self.prefix + ":routes:" + peer["installation_id"],
                    self.route(sid, peer),
                )
            except Exception:
                log.warning("Disconnected app route will expire")

    async def resolve(self, installation_id, preferred_sid=None):
        if self.redis:
            try:
                await self.redis.ping()
            except Exception:
                raise ConnectionUnavailable(
                    "App routing store is unavailable"
                ) from None
        for sid in [preferred_sid, *self.peers]:
            peer = self.peers.get(sid)
            if peer and peer["ready"] and peer["installation_id"] == installation_id:
                return sid
        if self.redis:
            key = self.prefix + ":routes:" + installation_id
            try:
                await self.redis.zremrangebyscore(key, "-inf", time.time())
                routes = await self.redis.zrevrangebyscore(
                    key, "+inf", time.time(), start=0, num=1
                )
                if routes:
                    return json.loads(routes[0])["sid"]
            except Exception:
                raise ConnectionUnavailable(
                    "App routing store is unavailable"
                ) from None
        raise ConnectionUnavailable("WebUI is disconnected; operation was not sent")

    async def request(
        self, installation_id, operation, arguments, *, preferred_sid=None
    ):
        packet = {"operation": operation, "arguments": arguments}
        if len(dumps(packet).encode()) > MAX_PACKET - 128:
            raise ValueError("App request exceeds 1 MiB")
        sid = await self.resolve(installation_id, preferred_sid)
        task = asyncio.create_task(
            self.server.call(
                "app:request", packet, to=sid, namespace=NAMESPACE, timeout=10
            )
        )
        self._calls.setdefault(sid, set()).add(task)
        try:
            result = await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise ConnectionLost(
                "WebUI disconnected after dispatch; outcome may be unknown"
            ) from None
        except Exception:
            raise ConnectionLost(
                "WebUI request failed after dispatch; outcome may be unknown"
            ) from None
        finally:
            self._calls[sid].discard(task)
            if not self._calls[sid]:
                self._calls.pop(sid)
        if (
            not isinstance(result, dict)
            or len(dumps(result).encode()) > MAX_PACKET - 128
        ):
            raise ConnectionLost("Invalid WebUI response; outcome may be unknown")
        status = result.get("status")
        if type(status) is not int:
            raise ConnectionLost("Invalid WebUI status; outcome may be unknown")
        if not 200 <= status < 300:
            raise APIError(
                status,
                method=operation,
                detail=json.dumps({"detail": result.get("error", "Request rejected")})
                .encode()[:65536]
                .decode(errors="replace"),
            )
        return result.get("data")

    async def event(self, sid, payload):
        from .app import _EventError

        peer = self.peers.get(sid)
        if not peer or not peer["ready"]:
            return {"status": 403, "error": "Connection is not authenticated"}
        try:
            if len(dumps(payload).encode()) > MAX_PACKET - 128:
                return {"status": 413, "error": "Event exceeds 1 MiB"}
            if self.redis:
                try:
                    await self.redis.ping()
                except Exception:
                    return {"status": 503, "error": "App routing store unavailable"}
            client = Client(
                peer["installation_id"], transport=self, app_id=peer["app_id"], sid=sid
            )
            result = await self.app.handle_event(payload, client=client)
            return {"status": 200, "data": result}
        except _EventError as exc:
            return {"status": exc.status, "error": exc.detail}
        except Exception as exc:
            # Preserve explicit HTTP errors without misclassifying handler bugs.
            status = getattr(exc, "status_code", 500)
            detail = (
                getattr(exc, "detail", "App handler failed")
                if status != 500
                else "App handler failed"
            )
            return {"status": status, "error": detail}

    async def close(self):
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        for sid in list(set(self.peers) | set(self.pending)):
            await self.server.disconnect(sid, namespace=NAMESPACE)
        await self.server.shutdown()
        manager_task = getattr(self.server.manager, "thread", None)
        if manager_task:
            manager_task.cancel()
            await asyncio.gather(manager_task, return_exceptions=True)
        if self.redis:
            if self.server.manager.pubsub:
                await self.server.manager.pubsub.aclose()
            if self.server.manager.redis:
                await self.server.manager.redis.aclose()
            await self.redis.aclose()
