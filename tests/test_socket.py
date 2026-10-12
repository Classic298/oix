import asyncio
import copy
import os
import socket
from contextlib import asynccontextmanager
from unittest.mock import patch

import aiohttp
import jwt
import pytest
import socketio
import uvicorn
from aiohttp import web
from fastapi import FastAPI
from test_sdk import API_KEY, AUTH, PEM

from oix import App, ConnectionUnavailable
from oix.fastapi import create_router

NS = "/apps"


def make_app(**kwargs):
    return App(
        manifest_id="socket-test",
        name="Test",
        api_key=API_KEY,
        private_key=PEM,
        scopes=["channels:messages:read", "channels:messages:write"],
        **kwargs,
    )


@asynccontextmanager
async def serve(app, adapter="aiohttp"):
    if adapter == "aiohttp":
        with patch("oix.app.web.run_app") as run:
            app.run()
        runner = web.AppRunner(run.call_args.args[0])
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            await runner.cleanup()
    else:
        markers = []

        @asynccontextmanager
        async def lifespan(host):
            markers.append("start")
            yield
            markers.append("stop")

        host = FastAPI(lifespan=lifespan)
        host.include_router(create_router(app))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        config = uvicorn.Config(
            host, log_level="error", lifespan="on", ws_max_size=1048576
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        try:
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await task
        assert markers == ["start", "stop"]
        assert app._closing


@asynccontextmanager
async def connect(url, *, token=None, app_id="installed", request=None):
    sio = socketio.AsyncClient(reconnection=False)

    async def handler(data):
        if data["operation"] == "ping":
            return {"status": 200, "data": {"received": True}}
        return await request(data)

    sio.on("app:request", handler, namespace=NS)
    await sio.connect(
        url,
        socketio_path="events/socket.io",
        headers={"Authorization": AUTH},
        namespaces=[NS],
        transports=["websocket"],
    )
    try:
        response = await sio.call(
            "app:connect",
            {
                "app_id": app_id,
                "challenge": "challenge-string-1234",
                "connection_token": token,
            },
            namespace=NS,
        )
        assert response["status"] == 200, response
        credentials = response["data"]
        proof = jwt.decode(
            credentials["proof"],
            make_app()._private_key.public_key(),
            algorithms=["EdDSA"],
            audience="oix:connect",
        )
        assert proof["sub"] == credentials["installation_id"]
        assert proof["app_id"] == app_id
        assert await sio.call("app:ready", {}, namespace=NS) == {
            "status": 200,
            "data": {"received": True},
        }
        yield sio, credentials
    finally:
        await sio.disconnect()


@pytest.mark.parametrize("adapter", ["aiohttp", "fastapi"])
async def test_socket_auth_bidirectional_dispatch_and_ack(adapter):
    app = make_app()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writes = []
    payload = {}

    async def runtime(packet):
        if packet["operation"] == "verify_event":
            return {
                "status": 200,
                "data": {k: v for k, v in payload.items() if k != "event_token"},
            }
        if packet["operation"] == "create_message":
            writes.append(packet["arguments"])
            return {"status": 200, "data": {"id": "reply", "etag": "123"}}
        raise AssertionError(packet)

    @app.on("channel.message.created")
    async def message(event):
        entered.set()
        await release.wait()
        await event.reply("🙂" * 32000)
        finished.set()

    async with serve(app, adapter) as url:
        async with aiohttp.ClientSession() as http:
            async with http.get(url + "/events") as response:
                assert response.status == 401
            async with http.get(
                url + "/events", headers={"Authorization": AUTH}
            ) as response:
                assert (await response.json())["id"] == "socket-test"
            async with http.post(
                url + "/events", headers={"Authorization": AUTH}
            ) as response:
                assert response.status == 426
        bad = socketio.AsyncClient(reconnection=False)
        with pytest.raises(socketio.exceptions.ConnectionError):
            await bad.connect(
                url,
                socketio_path="events/socket.io",
                transports=["websocket"],
                namespaces=[NS],
            )
        await bad.disconnect()
        async with connect(url, request=runtime) as (webui, credentials):
            payload.update(
                id="event",
                type="channel.message.created",
                app_id="installed",
                installation_id=credentials["installation_id"],
                channel={"id": "channel"},
                message={
                    "id": "source",
                    "parent_id": "thread",
                    "content": "🙂" * 32000,
                },
                event_token="signed",
            )
            ack = await webui.call("app:event", payload, namespace=NS)
            assert ack == {"status": 200, "data": {"received": True}}
            await asyncio.wait_for(entered.wait(), 1)
            assert not finished.is_set()
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            assert writes[0]["parent_id"] == "thread"
            assert writes[0]["reply_to_id"] == "source"
            assert writes[0]["content"] == "🙂" * 32000
            forged = copy.deepcopy(payload)
            forged["installation_id"] = "another"
            assert (await webui.call("app:event", forged, namespace=NS))[
                "status"
            ] == 403


async def test_reconnect_identity_survives_restart_and_cannot_be_claimed():
    app = make_app()
    async with serve(app) as url:
        async with connect(url) as (_, first):
            pass
    app = make_app()
    async with serve(app) as url:
        async with connect(url, token=first["connection_token"]) as (webui, restored):
            assert restored["installation_id"] == first["installation_id"]
            result = await webui.call(
                "app:connect",
                {"app_id": "installed", "challenge": "challenge-string-1234"},
                namespace=NS,
            )
            assert result["status"] == 403
        async with connect(url) as (_, other):
            assert other["installation_id"] != first["installation_id"]
        sio = socketio.AsyncClient(reconnection=False)
        await sio.connect(
            url,
            socketio_path="events/socket.io",
            headers={"Authorization": AUTH},
            namespaces=[NS],
            transports=["websocket"],
        )
        try:
            result = await sio.call(
                "app:connect",
                {
                    "app_id": "impostor",
                    "challenge": "challenge-string-1234",
                    "connection_token": first["connection_token"],
                },
                namespace=NS,
            )
            assert result["status"] == 403
        finally:
            await sio.disconnect()
    with pytest.raises(ConnectionUnavailable):
        await app.client(first["installation_id"]).get_me()


async def test_replicas_route_one_write_and_isolate_installations():
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set TEST_REDIS_URL to run replica routing tests")
    one, two = make_app(redis_url=redis_url), make_app(redis_url=redis_url)
    writes = []

    async def runtime(packet):
        writes.append(packet)
        return {"status": 200, "data": {"id": str(len(writes))}}

    async with serve(one) as url_one, serve(two) as url_two:
        async with connect(url_one, request=runtime) as (_, first):
            async with connect(
                url_one, token=first["connection_token"], request=runtime
            ):
                # Replica two routes through Redis to exactly one socket on replica one.
                result = await two.client(first["installation_id"]).create_message(
                    "channel", content="once"
                )
                assert result["id"] == "1"
                assert len(writes) == 1
                async with connect(url_two, request=runtime) as (_, second):
                    assert second["installation_id"] != first["installation_id"]
                    assert await one.client(second["installation_id"]).get_me() == {
                        "id": "2"
                    }
                    assert len(writes) == 2
        with pytest.raises(ConnectionUnavailable):
            await two.client(first["installation_id"]).create_message(
                "channel", content="offline"
            )


async def test_disconnect_after_dispatch_is_uncertain_and_never_retried():
    from oix import ConnectionLost

    app = make_app()
    received = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def runtime(packet):
        calls.append(packet)
        received.set()
        await release.wait()
        return {"status": 200, "data": {"id": "already-created"}}

    async with serve(app) as url:
        async with connect(url, request=runtime) as (webui, credentials):
            pending = asyncio.create_task(
                app.client(credentials["installation_id"]).create_message(
                    "channel", content="once"
                )
            )
            await asyncio.wait_for(received.wait(), 1)
            await webui.disconnect()
            with pytest.raises(ConnectionLost, match="unknown"):
                await pending
            release.set()
            assert len(calls) == 1


async def test_redis_publish_failure_does_not_retry():
    from unittest.mock import AsyncMock

    from oix.socket import AppRedisManager

    manager = AppRedisManager("redis://unused")
    manager.connected = True
    manager.redis = AsyncMock()
    manager.redis.publish.side_effect = ConnectionError("response lost")
    with pytest.raises(ConnectionError):
        await manager._publish({"method": "emit", "data": {"content": "once"}})
    manager.redis.publish.assert_awaited_once()


def test_packet_parser_rejects_binary_and_oversized_frames():
    from oix.socket import MAX_PACKET, JSONPacket

    for value in (
        '51-/apps,["app:event",{"_placeholder":true,"num":0}]',
        '61-/apps,1[{"_placeholder":true,"num":0}]',
        "2" + "x" * MAX_PACKET,
        b"raw",
    ):
        with pytest.raises(ValueError):
            JSONPacket(encoded_packet=value)


async def test_error_detail_is_bounded_and_not_logged():
    from oix import APIError

    app = make_app()

    async def runtime(packet):
        return {"status": 500, "error": "secret-" * 20000}

    async with serve(app) as url:
        async with connect(url, request=runtime) as (_, credentials):
            with pytest.raises(APIError) as error:
                await app.client(credentials["installation_id"]).get_me()
            assert len(error.value.detail.encode()) <= 65536
            assert "secret" not in str(error.value)


async def test_separate_processes_route_worker_calls(tmp_path):
    import subprocess
    import sys

    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set TEST_REDIS_URL to run replica routing tests")
    script = tmp_path / "replica.py"
    script.write_text("""
import os
from fastapi import FastAPI
from oix import App, ConnectionUnavailable
from oix.fastapi import create_router
import uvicorn
app = App(manifest_id='socket-test', name='Test', api_key=os.environ['API_KEY'],
          private_key=os.environ['TEST_PRIVATE_KEY'], scopes=[],
          redis_url=os.environ['TEST_REDIS_URL'])
server = FastAPI()
server.include_router(create_router(app))
@server.post('/send/{installation_id}')
async def send(installation_id: str):
    try:
        return await app.client(installation_id).create_message(
            'channel', content='once')
    except ConnectionUnavailable:
        return {'disconnected': True}
uvicorn.run(server, host='127.0.0.1', port=int(os.environ['TEST_PORT']),
            log_level='error')
""")
    processes = []
    env = {**os.environ, "API_KEY": API_KEY, "TEST_PRIVATE_KEY": PEM}
    calls = []

    async def runtime(packet):
        calls.append(packet)
        return {"status": 200, "data": {"id": str(len(calls))}}

    try:
        urls = []
        async with aiohttp.ClientSession() as http:
            for _ in range(2):
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                process = subprocess.Popen(
                    [sys.executable, str(script)], env={**env, "TEST_PORT": str(port)}
                )
                processes.append(process)
                url = f"http://127.0.0.1:{port}"
                urls.append(url)
                async with asyncio.timeout(10):
                    while True:
                        assert process.poll() is None
                        try:
                            async with http.get(url + "/events") as response:
                                if response.status == 401:
                                    break
                        except aiohttp.ClientError:
                            pass
                        await asyncio.sleep(0.02)
            async with connect(urls[0], request=runtime) as (_, first):
                async with connect(
                    urls[0], token=first["connection_token"], request=runtime
                ):
                    async with http.post(
                        urls[1] + "/send/" + first["installation_id"]
                    ) as response:
                        assert await response.json() == {"id": "1"}
                    assert len(calls) == 1
            from redis.asyncio import Redis

            observer = Redis.from_url(redis_url)
            try:
                key = (
                    make_app()._get_socket("aiohttp").prefix
                    + ":routes:"
                    + first["installation_id"]
                )
                async with asyncio.timeout(5):
                    # Wait for remote route removal after the client disconnects.
                    while await observer.zcard(key):
                        await asyncio.sleep(0.01)
            finally:
                await observer.aclose()
            async with http.post(
                urls[1] + "/send/" + first["installation_id"]
            ) as response:
                assert await response.json() == {"disconnected": True}
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            await asyncio.to_thread(process.wait, 10)


def test_fastapi_router_prefix_and_optional_manifest():
    from fastapi.testclient import TestClient

    app = make_app()
    host = FastAPI()
    host.include_router(create_router(app, manifest=False), prefix="/integration")
    with TestClient(host) as http:
        assert (
            http.get("/integration/events", headers={"Authorization": AUTH}).status_code
            == 405
        )
        with http.websocket_connect(
            "/integration/events/socket.io/?EIO=4&transport=websocket",
            headers={"Authorization": AUTH, "Upgrade": "websocket"},
        ) as ws:
            assert ws.receive_text().startswith("0{")
    assert app._closing


@pytest.mark.parametrize("adapter", ["aiohttp", "fastapi"])
async def test_oversized_frame_disconnects_before_dispatch(adapter):
    app = make_app()
    async with serve(app, adapter) as url:
        async with connect(url) as (webui, credentials):
            disconnected = asyncio.Event()

            async def on_disconnect(*args):
                disconnected.set()

            webui.on("disconnect", on_disconnect, namespace=NS)
            await webui.emit("app:event", {"content": "x" * 1048576}, namespace=NS)
            await asyncio.wait_for(disconnected.wait(), 3)
            assert not app._tasks and not app._requests
