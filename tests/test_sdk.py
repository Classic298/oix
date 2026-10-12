import copy
from unittest.mock import AsyncMock, Mock

import aiohttp
import httpx
import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from oix import Action, APIError, App, AppChannelMessage, AppReply, Client
from oix.client import _check_address, _PublicResolver
from oix.fastapi import create_router

KEY = Ed25519PrivateKey.generate()
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
API_KEY = "test-key-" * 8
AUTH = "Bearer " + API_KEY


@pytest.fixture
def app():
    return App(
        id="test",
        name="Test",
        api_key=API_KEY,
        private_key=PEM,
        scopes=["messages:read", "messages:write"],
    )


def mention():
    return {
        "id": "operation-1",
        "type": "mention",
        "app_id": "installed-1",
        "webui_url": "https://webui.example",
        "actor": {"id": "alice", "is_admin": True},
        "channel": {"id": "channel-1"},
        "data": {"text": "hello"},
        "execution_context": "context",
        "source": {"unsigned": "not exposed"},
    }


def authorized(event):
    return {
        "authorized": True,
        "operation_id": event["id"],
        "actor": {"id": event["actor"]["id"], "is_admin": False},
        "channel_id": event["channel"]["id"],
        "type": event["type"],
        "data": event["data"],
    }


async def test_connect_multiple_instances_and_restart(app):
    for url, app_id in [("https://one.example", "one"), ("https://two.example", "two")]:
        event = {
            "id": "connect-1",
            "type": "connect",
            "webui_url": url,
            "app_id": app_id,
            "data": {"challenge": "x" * 32},
        }
        reply = await app.handle_event(event, authorization=AUTH)
        claims = jwt.decode(
            reply["proof"],
            KEY.public_key(),
            algorithms=["EdDSA"],
            audience=url + "/api/v1/apps/runtime",
        )
        assert claims["sub"] == app_id and claims["challenge"] == "x" * 32
        assert claims["exp"] - claims["iat"] == 60
        assert (
            jwt.get_unverified_header(reply["proof"])["typ"] == "webui-app-connect+jwt"
        )
    restarted = App(id="test", name="Test", api_key=API_KEY, private_key=PEM, scopes=[])
    assert restarted.manifest["public_key"] == app.manifest["public_key"]
    assert (
        jwt.get_unverified_header(restarted.client(url, app_id)._sign())["typ"]
        == "webui-app+jwt"
    )


async def test_authorize_before_handler_and_use_current_actor(app, monkeypatch):
    payload = mention()
    verify = AsyncMock(return_value=authorized(payload))
    monkeypatch.setattr(Client, "authorize", verify)
    seen = []

    @app.on("mention")
    async def handler(event, client):
        seen.append(event)
        assert not event.actor["is_admin"]
        assert not hasattr(event, "source")
        return AppReply(content="Hello")

    result = await app.handle_event(payload, authorization=AUTH)
    assert result["content"] == "Hello"
    verify.assert_awaited_once_with("context")
    assert len(seen) == 1
    payload["data"] = {"text": "tampered"}
    with pytest.raises(PermissionError):
        await app.handle_event(payload, authorization=AUTH)
    assert len(seen) == 1


async def test_message_events_verified_and_not_command_authorized(app, monkeypatch):
    payload = {
        "id": "event-1",
        "type": "message.created",
        "app_id": "installed-1",
        "webui_url": "https://webui.example",
        "channel": {"id": "channel-1"},
        "message": {"id": "message-1", "content": "hello"},
    }
    verify = AsyncMock(return_value=copy.deepcopy(payload))
    monkeypatch.setattr(Client, "verify_event", verify)
    authorize = AsyncMock(side_effect=AssertionError("Must not authorize a command"))
    monkeypatch.setattr(Client, "authorize", authorize)
    seen = []

    @app.on("message.created")
    async def handler(event, client):
        seen.append(event.message)

    payload["event_context"] = "signed"
    assert await app.handle_event(payload, authorization=AUTH) == {"received": True}
    assert len(seen) == 1
    payload["message"]["content"] = "forged"
    with pytest.raises(PermissionError):
        await app.handle_event(payload, authorization=AUTH)
    assert len(seen) == 1


async def test_adapter_auth_body_limits_optional_discovery(app):
    server = FastAPI()
    server.include_router(create_router(app, path="/manifest"))
    server.include_router(create_router(app, path="/private", manifest=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        assert (await client.get("/manifest")).json() == app.manifest
        assert (await client.get("/private")).status_code == 405
        assert (
            await client.post("/manifest", content=b"x" * 300000)
        ).status_code == 401
        assert (
            await client.post(
                "/manifest", headers={"Authorization": AUTH}, content=b"x" * 300000
            )
        ).status_code == 413
        for payload in [
            [],
            {"type": "connect"},
            {"id": "x", "webui_url": "http://127.0.0.1", "app_id": "one"},
        ]:
            assert (
                await client.post(
                    "/manifest", headers={"Authorization": AUTH}, json=payload
                )
            ).status_code == 400
        assert (
            await client.post(
                "/manifest", headers={"Authorization": "Bearer wrong"}, json=mention()
            )
        ).status_code == 401


@pytest.mark.parametrize("manifest", [True, False])
async def test_builtin_server_auth_limits_and_verified_dispatch(
    app, monkeypatch, manifest
):
    run = Mock()
    monkeypatch.setattr(web, "run_app", run)
    app.run(host="127.0.0.1", port=8001, path="/hook", manifest=manifest)
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 8001}
    payload = mention()
    verify = AsyncMock(return_value=authorized(payload))
    monkeypatch.setattr(Client, "authorize", verify)
    handler = AsyncMock(return_value=AppReply(content="Hello"))
    app.on("mention")(handler)

    async with TestClient(TestServer(run.call_args.args[0])) as client:
        response = await client.get("/hook")
        if manifest:
            assert response.status == 200
            assert await response.json() == app.manifest
        else:
            assert response.status == 405
        assert (await client.get("/events")).status == 404
        assert (await client.post("/hook", data=b"x" * 300000)).status == 401
        headers = {"Authorization": AUTH}
        assert (
            await client.post("/hook", headers=headers, data=b"x" * 300000)
        ).status == 413

        async def chunks():
            for _ in range(5):
                yield b"x" * 65536

        assert (
            await client.post("/hook", headers=headers, data=chunks())
        ).status == 413
        for body in [b"not json", b"[]", b"{}"]:
            assert (
                await client.post("/hook", headers=headers, data=body)
            ).status == 400
        handler.assert_not_called()
        verify.assert_not_called()

        response = await client.post("/hook", headers=headers, json=payload)
        assert response.status == 200
        assert (await response.json())["content"] == "Hello"
        handler.assert_awaited_once()
        assert handler.call_args.args[0].actor["is_admin"] is False

        payload["data"] = {"text": "forged"}
        assert (await client.post("/hook", headers=headers, json=payload)).status == 403
        verify.side_effect = APIError(403)
        assert (await client.post("/hook", headers=headers, json=payload)).status == 403
        verify.side_effect = aiohttp.ClientConnectionError("unavailable")
        assert (await client.post("/hook", headers=headers, json=payload)).status == 502
        handler.assert_awaited_once()


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://127.0.0.1",
        "https://[::1]",
        "https://user:password@public.example",
        "https://public.example/path",
        "https://public.example?query=1",
        "https://public.example/#fragment",
        "https://public.example\\@127.0.0.1",
    ],
)
def test_block_unsafe_destinations(app, url):
    with pytest.raises(ValueError):
        app.client(url, "installed")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "224.0.0.1",
    ],
)
def test_block_nonpublic_addresses(address):
    with pytest.raises(ValueError):
        _check_address(address)


async def test_dns_results_checked_at_connection_resolution(monkeypatch):
    monkeypatch.setattr(
        aiohttp.resolver.ThreadedResolver,
        "resolve",
        AsyncMock(
            return_value=[{"host": "127.0.0.1"}],
        ),
    )
    resolver = _PublicResolver()
    with pytest.raises(ValueError):
        await resolver.resolve("rebound.example", 443)
    await resolver.close()


async def test_runtime_requests_signing_etags_and_no_redirects(app):
    calls = []
    origin = ""

    async def runtime(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token,
            KEY.public_key(),
            algorithms=["EdDSA"],
            audience=origin + "/api/v1/apps/runtime",
        )
        assert claims["sub"] == "installed"
        assert jwt.get_unverified_header(token)["typ"] == "webui-app+jwt"
        calls.append(
            (
                request.method,
                request.path,
                dict(request.query),
                request.headers.get("If-Match"),
            )
        )
        if request.path.endswith("/me"):
            raise web.HTTPFound("/api/v1/apps/runtime/channels")
        if request.path.endswith("/channels"):
            return web.Response(body=b" " * 1048577)
        if request.method in ("POST", "PATCH"):
            assert (await request.json())["content"] == "Hello"
            return web.json_response({"id": "message-1", "etag": "123"})
        return web.json_response({"messages": [], "next_cursor": None})

    server = web.Application()
    server.router.add_route("*", "/{path:.*}", runtime)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    client = Client(origin, "installed", KEY, allowed_private_origins=(origin,))
    try:
        await client.list_messages("channel-1", before="cursor", parent_id="thread")
        assert calls[-1][2] == {
            "limit": "50",
            "before": "cursor",
            "parent_id": "thread",
        }
        message = AppChannelMessage(content="Hello")
        sent = await client.create_message("channel-1", message)
        await client.update_message(sent["id"], message, etag=sent["etag"])
        assert calls[-1][-1] == '"123"'
        with pytest.raises(APIError) as error:
            await client.get_me()
        assert error.value.status == 302
        assert len(calls) == 4
        with pytest.raises(ValueError, match="1 MiB"):
            await client.list_channels()
        for bad_id in ["../me", "foo/bar", "%2e%2e", "foo?bar"]:
            with pytest.raises(ValueError):
                await client.create_message(bad_id, message)
    finally:
        await runner.cleanup()


def test_private_buttons_rejected():
    with pytest.raises(ValueError, match="Buttons require"):
        AppReply(actions=[Action(action_id="publish", label="Publish")])


@pytest.mark.parametrize("kind", ["action", "operation_status"])
async def test_action_and_deferred_status(app, monkeypatch, kind):
    payload = mention()
    payload["type"] = kind
    payload["data"] = (
        {"message_id": "saved-1", "action_id": "confirm", "context": {"value": 1}}
        if kind == "action"
        else {"operation_id": payload["id"]}
    )
    monkeypatch.setattr(
        Client, "authorize", AsyncMock(return_value=authorized(payload))
    )

    @app.on(kind)
    async def handle(event, client):
        assert event.data == payload["data"]
        return AppReply(type="deferred", operation_id=event.id)

    reply = await app.handle_event(payload, authorization=AUTH)
    assert reply["operation_id"] == payload["id"]
    assert reply["visibility"] == "private"


async def test_revoked_access_never_calls_handler(app, monkeypatch):
    monkeypatch.setattr(Client, "authorize", AsyncMock(side_effect=APIError(403)))
    handler = AsyncMock()
    app.on("mention")(handler)
    with pytest.raises(APIError):
        await app.handle_event(mention(), authorization=AUTH)
    handler.assert_not_called()
