"""Authenticate events and run verified app handlers."""

import asyncio
import hmac
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, get_args

from aiohttp import (
    ClientConnectorDNSError,
    ClientConnectorError,
    ClientError,
    ClientSSLError,
    web,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .client import APIError, Client, _DestinationError, _origin
from .models import AppChannelReply, Event, EventType, Scope

Handler = Callable[[Event], Awaitable[AppChannelReply | str | None]]
log = logging.getLogger(__name__)
MAX_EVENT_BYTES = 1048576
INTERACTION_TIMEOUT = 2.5
VERIFICATION_TIMEOUT = 2
MESSAGE_VERIFICATION_TIMEOUT = 8
SHUTDOWN_TIMEOUT = 30


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class _EventError(Exception):
    """An incoming request failure, distinct from an application's exception."""

    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


class App:
    def __init__(
        self,
        *,
        manifest_id: str,
        name: str,
        scopes: list[Scope],
        api_key: str | None = None,
        private_key: str | None = None,
        description: str = "",
        allowed_private_origins: tuple[str, ...] | None = None,
        ignore_self_events: bool = True,
        max_concurrency: int = 32,
    ):
        if api_key is None:
            api_key = os.environ.get("API_KEY", "")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("Set API_KEY or pass api_key when creating App")
        if len(api_key) > 4096 or any(not 33 <= ord(c) <= 126 for c in api_key):
            raise ValueError(
                "API_KEY must contain only printable ASCII characters without spaces "
                "(maximum 4096 characters)"
            )
        if len(api_key) < 32:
            log.warning(
                "API_KEY is shorter than 32 characters. "
                "A long, randomly generated key is recommended."
            )
        if private_key is None:
            filename = os.environ.get("APP_PRIVATE_KEY_FILE")
            if not filename:
                raise ValueError("Set APP_PRIVATE_KEY_FILE or pass private_key to App")
            try:
                private_key = Path(filename).read_text(encoding="utf-8")
            except FileNotFoundError:
                raise ValueError(
                    "Cannot read APP_PRIVATE_KEY_FILE: file does not exist. "
                    "Set it to the path of your persistent Ed25519 PEM file."
                ) from None
            except PermissionError:
                raise ValueError(
                    "Cannot read APP_PRIVATE_KEY_FILE: permission denied. "
                    "Grant the app process read access to the key file."
                ) from None
            except (OSError, UnicodeError):
                raise ValueError(
                    "Cannot read APP_PRIVATE_KEY_FILE: expected a UTF-8 PEM file."
                ) from None
        if not isinstance(private_key, str):
            raise ValueError(
                "private_key must contain an unencrypted Ed25519 PEM string"
            )
        try:
            key = serialization.load_pem_private_key(
                private_key.encode(), password=None
            )
        except (ValueError, TypeError):
            raise ValueError(
                "App private key must be a valid unencrypted Ed25519 PEM"
            ) from None
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("An Ed25519 private key is required")
        if (
            not 1 <= len(manifest_id) <= 100
            or not 1 <= len(name) <= 100
            or len(description) > 2000
        ):
            raise ValueError("Invalid app metadata")
        if len(scopes) > 4 or not set(scopes) <= set(get_args(Scope)):
            raise ValueError("Invalid app scopes")
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if allowed_private_origins is None:
            allowed_private_origins = tuple(
                value.strip()
                for value in os.environ.get(
                    "OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS", ""
                ).split(",")
                if value.strip()
            )
        try:
            if isinstance(allowed_private_origins, str):
                raise ValueError
            allowed_private_origins = tuple(
                _origin(value) for value in allowed_private_origins
            )
        except (ValueError, TypeError, AttributeError):
            raise ValueError(
                "allowed_private_origins / OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS must "
                "contain HTTP(S) origins without paths, credentials, "
                "queries or fragments."
            ) from None
        self._api_key = api_key
        self._private_key = key
        self._allowed_private_origins = allowed_private_origins
        self._ignore_self_events = ignore_self_events
        self._max_concurrency = max_concurrency
        self._active_messages = 0
        self._tasks: set[asyncio.Task] = set()
        self._requests: set[asyncio.Task] = set()
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self._handlers: dict[str, Handler] = {}
        self._actions: dict[str, Handler] = {}
        self.manifest = {
            "id": manifest_id,
            "name": name,
            "description": description,
            "public_key": key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode(),
            "requested_scopes": list(scopes),
        }

    def authenticate(self, authorization: str) -> None:
        if not hmac.compare_digest(
            authorization.encode(), ("Bearer " + self._api_key).encode()
        ):
            raise PermissionError("Invalid app API key")

    def client(self, webui_url: str, app_id: str) -> Client:
        return Client(
            webui_url,
            app_id,
            self._private_key,
            allowed_private_origins=self._allowed_private_origins,
        )

    def _register(self, handlers: dict[str, Handler], key: str):
        def register(handler: Handler) -> Handler:
            if key in handlers:
                raise ValueError(f"Handler already registered for {key}")
            if not (
                inspect.iscoroutinefunction(handler)
                or inspect.iscoroutinefunction(getattr(handler, "__call__", None))
            ):
                raise TypeError("Handlers must be async functions accepting one event")
            try:
                inspect.signature(handler).bind(object())
            except (ValueError, TypeError):
                raise TypeError(
                    "Handlers must accept one event without other required arguments"
                ) from None
            handlers[key] = handler
            return handler

        return register

    def on(self, event_type: EventType):
        if event_type not in get_args(EventType):
            raise ValueError(f"Unknown event type: {event_type}")
        return self._register(self._handlers, event_type)

    def action(self, action_id: str):
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 100:
            raise ValueError("action_id must contain 1-100 characters")
        return self._register(self._actions, action_id)

    def run(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8000,
        path: str = "/events",
        manifest: bool = True,
    ) -> None:
        """Serve HTTP events until shutdown. Call after registering handlers."""
        logger = logging.getLogger("oix")
        if not logger.hasHandlers():
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(levelname)s %(name)s: %(message)s")
            )
            logger.addHandler(handler)
            if logger.level == logging.NOTSET:
                logger.setLevel(logging.INFO)
        server = web.Application(client_max_size=MAX_EVENT_BYTES)

        async def discovery(request):
            return web.json_response(self.manifest, dumps=_json_dumps)

        async def events(request):
            authorization = request.headers.get("authorization", "")
            try:
                self.authenticate(authorization)
            except PermissionError:
                raise web.HTTPUnauthorized(text="Invalid app API key") from None
            body = bytearray()
            async for chunk in request.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_EVENT_BYTES:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=MAX_EVENT_BYTES, actual_size=len(body)
                    )
            try:
                result = await self._handle_body(body, authorization)
            except _EventError as exc:
                return web.json_response({"detail": exc.detail}, status=exc.status)
            return web.json_response(result, dumps=_json_dumps)

        async def startup(server):
            log.info("Serving app %s on %s:%s%s", self.manifest["id"], host, port, path)

        async def shutdown(server):
            await self.close()

        if manifest:
            server.router.add_get(path, discovery)
        server.router.add_post(path, events)
        server.on_startup.append(startup)
        server.on_shutdown.append(shutdown)
        web.run_app(server, host=host, port=port)

    async def _handle_body(self, body: bytes, authorization: str) -> dict:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise _EventError(400, "Invalid JSON") from None
        return await self.handle_event(payload, authorization=authorization)

    async def handle_event(
        self, payload: dict[str, Any], *, authorization: str
    ) -> dict:
        """Verify an event before invoking or scheduling its handler."""
        self.authenticate(authorization)
        if self._closing:
            raise _EventError(503, "App is shutting down")
        try:
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if (
                not isinstance(payload.get("id"), str)
                or not 1 <= len(payload["id"]) <= 100
            ):
                raise ValueError("Invalid event ID")
            if not isinstance(payload.get("webui_url"), str):
                raise ValueError("Missing WebUI origin")
            if (
                not isinstance(payload.get("app_id"), str)
                or not 1 <= len(payload["app_id"]) <= 100
            ):
                raise ValueError("Invalid installation ID")
            try:
                client = self.client(payload["webui_url"], payload["app_id"])
            except ValueError as exc:
                # Client validation messages exclude credentials and event bodies.
                log.warning("App destination rejected: %s", exc)
                raise _EventError(400, str(exc)) from None
            if payload.get("type") == "connect":
                challenge = payload.get("data", {}).get("challenge")
                if not isinstance(challenge, str) or not 16 <= len(challenge) <= 128:
                    raise ValueError("Invalid connection challenge")
                log.info(
                    "App identity proof issued: origin=%s app=%s; "
                    "callback reachability is not tested",
                    client.webui_url,
                    client.app_id,
                )
                return {"proof": client._sign(challenge)}
            event = Event.model_validate(payload)
            ordinary = event.type.startswith("channel.message.")
            token = payload["event_token" if ordinary else "execution_token"]
            if not isinstance(token, str) or not token:
                raise ValueError("Missing event token")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise _EventError(400, "Invalid app event") from None
        if ordinary and self._active_messages >= self._max_concurrency:
            raise _EventError(503, "App is at capacity")
        request_task = asyncio.current_task()
        self._requests.add(request_task)
        if ordinary:
            self._active_messages += 1
        transferred = False
        try:
            if ordinary:
                await self._verify(event, client, payload, token, ordinary=True)
                if self._closing:
                    raise _EventError(503, "App is shutting down")
                author = (event.message or {}).get("author", {})
                if (
                    self._ignore_self_events
                    and author.get("type") == "app"
                    and author.get("id") == event.app_id
                ):
                    log.debug(
                        "Ignoring self event: type=%s event=%s origin=%s app=%s",
                        event.type,
                        event.id,
                        event.webui_url,
                        event.app_id,
                    )
                    return {"received": True}
                handler = self._handlers.get(event.type)
                if handler:
                    task = asyncio.create_task(
                        self._invoke(event, handler),
                        name=f"oix:{event.type}:{event.id}",
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._message_done)
                    transferred = True
                else:
                    log.debug(
                        "No handler: type=%s event=%s origin=%s app=%s",
                        event.type,
                        event.id,
                        event.webui_url,
                        event.app_id,
                    )
                return {"received": True}
            budget = asyncio.timeout(INTERACTION_TIMEOUT)
            try:
                async with budget:
                    await self._verify(event, client, payload, token, ordinary=False)
                    handler = self._handlers.get(event.type)
                    if event.type == "channel.action":
                        handler = self._actions.get(
                            event.data.get("action_id"), handler
                        )
                    return await self._invoke(event, handler)
            except TimeoutError:
                if not budget.expired():
                    raise
                raise _EventError(
                    504, "App interaction timed out; execution outcome may be unknown"
                ) from None
        finally:
            self._requests.discard(request_task)
            if ordinary and not transferred:
                self._active_messages -= 1

    async def _verify(self, event, client, payload, token, *, ordinary):
        try:
            async with asyncio.timeout(
                MESSAGE_VERIFICATION_TIMEOUT if ordinary else VERIFICATION_TIMEOUT
            ):
                verified = await (
                    client.verify_event(token) if ordinary else client.authorize(token)
                )
        except (APIError, TimeoutError, ClientError, ValueError, TypeError) as exc:
            status = 502
            if isinstance(exc, APIError):
                status = 403 if exc.status in (401, 403, 404) else 502
                detail = f"WebUI event verification failed: {exc}"
            elif isinstance(exc, TimeoutError):
                status, detail = 504, "WebUI verification timed out"
            elif isinstance(exc, _DestinationError):
                detail = str(exc)
            elif isinstance(exc, ClientSSLError):
                detail = (
                    "WebUI TLS verification failed. Check its certificate and hostname."
                )
            elif isinstance(exc, ClientConnectorDNSError):
                detail = (
                    "WebUI hostname could not be resolved. "
                    "Check DNS from the app server."
                )
            elif isinstance(exc, ClientConnectorError):
                detail = (
                    "Could not connect to WebUI. "
                    "Check its address, port and network access."
                )
            elif isinstance(exc, ClientError):
                detail = (
                    "WebUI verification connection failed before receiving a response."
                )
            else:
                detail = "WebUI returned an invalid verification response."
            log.warning(
                "Event verification failed: type=%s event=%s "
                "origin=%s app=%s reason=%s",
                event.type,
                event.id,
                client.webui_url,
                client.app_id,
                detail,
            )
            raise _EventError(status, detail) from None
        try:
            if ordinary:
                if verified != {k: v for k, v in payload.items() if k != "event_token"}:
                    raise PermissionError
            else:
                if (
                    verified.get("authorized") is not True
                    or verified["operation_id"] != event.id
                    or verified["actor"]["id"] != event.actor.get("id")
                    or verified["channel_id"] != event.channel.get("id")
                    or verified["type"] != event.type
                    or verified["data"] != event.data
                ):
                    raise PermissionError
                event.actor = verified["actor"]
                event.channel = {"id": verified["channel_id"]}
                event.message = None
        except (PermissionError, KeyError, TypeError, AttributeError):
            log.warning(
                "Event context mismatch: type=%s event=%s origin=%s app=%s",
                event.type,
                event.id,
                client.webui_url,
                client.app_id,
            )
            raise _EventError(403, "Event context mismatch") from None
        event._client = client

    async def _invoke(self, event: Event, handler: Handler | None):
        try:
            reply = await handler(event) if handler else None
            if event.type.startswith("channel.message."):
                if reply is not None:
                    raise TypeError(
                        "Message handlers must return None; "
                        "use event.send() or event.reply()"
                    )
                return None
            if isinstance(reply, str):
                reply = AppChannelReply(content=reply)
            if reply is None:
                reply = AppChannelReply()
            if not isinstance(reply, AppChannelReply):
                raise TypeError(
                    "Interaction handlers must return AppChannelReply, str or None"
                )
            if reply.type == "deferred" and reply.operation_id != event.id:
                raise ValueError("Use the incoming event ID for deferred replies")
            if event.type == "channel.operation.status" and (
                reply.visibility != "private" or reply.type == "update"
            ):
                raise ValueError("Operation status replies must be private")
            result = reply.model_dump()
            if len(_json_dumps(result).encode()) > 262144:
                raise ValueError("App reply exceeds 256 KiB")
            return result
        except Exception:
            log.exception(
                "Handler failed: type=%s event=%s origin=%s app=%s",
                event.type,
                event.id,
                event.webui_url,
                event.app_id,
            )
            raise

    def _message_done(self, task: asyncio.Task):
        self._tasks.discard(task)
        self._active_messages -= 1
        if not task.cancelled():
            task.exception()  # Retrieve failures already logged by _invoke.

    async def close(self):
        """Stop admission, drain accepted work, then cancel after 30 seconds."""
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._drain(asyncio.current_task()))
        await asyncio.shield(self._close_task)

    async def _drain(self, caller):
        pending = (self._requests | self._tasks) - {caller}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
