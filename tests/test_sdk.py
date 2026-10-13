import asyncio
import copy
from typing import get_args
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from oix import Action, APIError, App, AppChannelReply, Client
from oix.app import _EventError
from oix.models import EventType, Scope

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
        manifest_id="test",
        name="Test",
        api_key=API_KEY,
        private_key=PEM,
        scopes=["channels:messages:read", "channels:messages:write"],
    )


def mention():
    return {
        "id": "operation-1",
        "type": "channel.mention",
        "app_id": "installed-1",
        "installation_id": "https://webui.example",
        "actor": {"id": "alice", "is_admin": True},
        "channel": {"id": "channel-1"},
        "data": {"text": "hello"},
        "execution_token": "context",
        "source": {"unsigned": "not exposed"},
    }


@pytest.mark.parametrize("scope", get_args(Scope))
def test_supported_scopes_are_requested_in_manifest(scope):
    app = App(
        manifest_id="test",
        name="Test",
        api_key=API_KEY,
        private_key=PEM,
        scopes=[scope],
    )
    assert app.manifest["requested_scopes"] == [scope]


@pytest.mark.parametrize(
    "scope",
    [
        "mentions:read",
        "messages:read",
        "messages:write",
        "channels:*",
        "channels:messages",
        "unknown",
    ],
)
def test_unknown_scopes_are_rejected(scope):
    with pytest.raises(ValueError, match="Invalid app scopes"):
        App(
            manifest_id="test",
            name="Test",
            api_key=API_KEY,
            private_key=PEM,
            scopes=[scope],
        )


@pytest.mark.parametrize(
    "kind", ["mention", "operation_status", "channel.mentoin", "", "connect"]
)
def test_unknown_event_names_fail_at_registration(app, kind):
    with pytest.raises(ValueError, match="Unknown event type"):
        app.on(kind)


@pytest.mark.parametrize("kind", get_args(EventType))
def test_supported_events_register_once(app, kind):
    handler = AsyncMock()
    assert app.on(kind)(handler) is handler
    with pytest.raises(ValueError, match="already registered"):
        app.on(kind)(handler)


async def test_runtime_verification_sends_token_field(app, monkeypatch):
    request = AsyncMock(return_value={"authorized": True})
    monkeypatch.setattr(Client, "_request", request)
    client = bound(dict(installation_id="one", app_id="installed"))
    await client.authorize("signed-execution-token")
    request.assert_awaited_with("authorize", token="signed-execution-token")
    await client.verify_event("signed-event-token")
    request.assert_awaited_with("verify_event", token="signed-event-token")


def authorized(event):
    return {
        "authorized": True,
        "operation_id": event["id"],
        "actor": {"id": event["actor"]["id"], "is_admin": False},
        "channel_id": event["channel"]["id"],
        "type": event["type"],
        "data": event["data"],
    }


async def test_authorize_before_handler_and_use_current_actor(app, monkeypatch):
    payload = mention()
    verify = AsyncMock(return_value=authorized(payload))
    monkeypatch.setattr(Client, "authorize", verify)
    seen = []

    @app.on("channel.mention")
    async def handler(event):
        seen.append(event)
        assert not event.actor["is_admin"]
        assert not hasattr(event, "source")

    result = await app.handle_event(payload, client=bound(payload))
    assert result == {"received": True}
    await asyncio.gather(*app._tasks)
    verify.assert_awaited_once_with("context")
    assert len(seen) == 1
    payload["data"] = {"text": "tampered"}
    with pytest.raises(_EventError):
        await app.handle_event(payload, client=bound(payload))
    assert len(seen) == 1


@pytest.mark.parametrize(
    "kind",
    ["channel.message.created", "channel.message.updated", "channel.message.deleted"],
)
async def test_message_events_verified_and_not_command_authorized(
    app, monkeypatch, kind
):
    payload = {
        "id": "event-1",
        "type": kind,
        "app_id": "installed-1",
        "installation_id": "https://webui.example",
        "channel": {"id": "channel-1"},
        "message": {"id": "message-1", "content": "hello"},
    }
    verify = AsyncMock(return_value=copy.deepcopy(payload))
    monkeypatch.setattr(Client, "verify_event", verify)
    authorize = AsyncMock(side_effect=AssertionError("Must not authorize a command"))
    monkeypatch.setattr(Client, "authorize", authorize)
    seen = []

    @app.on(kind)
    async def handler(event):
        seen.append(event.message)

    payload["event_token"] = "signed"
    assert await app.handle_event(payload, client=bound(payload)) == {"received": True}
    await asyncio.sleep(0)
    assert len(seen) == 1
    payload["message"]["content"] = "forged"
    with pytest.raises(_EventError):
        await app.handle_event(payload, client=bound(payload))
    assert len(seen) == 1


def test_private_buttons_rejected():
    with pytest.raises(ValueError, match="Buttons require"):
        AppChannelReply(actions=[Action(action_id="publish", label="Publish")])


@pytest.mark.parametrize("kind", ["channel.action"])
async def test_acknowledgement(app, monkeypatch, kind):
    payload = mention()
    payload["type"] = kind
    if kind == "channel.action":
        payload["data"] = {
            "message_id": "saved-1",
            "action_id": "confirm",
            "context": {"value": 1},
        }
    monkeypatch.setattr(
        Client, "authorize", AsyncMock(return_value=authorized(payload))
    )

    @app.on(kind)
    async def handle(event):
        assert event.data == payload["data"]
        return AppChannelReply(type="ack")

    assert await app.handle_event(payload, client=bound(payload)) == {"type": "ack"}


def test_removed_reply_and_event_contract(app):
    with pytest.raises(ValueError):
        AppChannelReply(type="deferred")
    with pytest.raises(ValueError):
        AppChannelReply(operation_id="old")
    with pytest.raises(ValueError):
        app.on("channel.operation.status")
    with pytest.raises(ValueError, match="Acknowledgements"):
        AppChannelReply(type="ack", content="Not a message")


async def test_revoked_access_never_calls_handler(app, monkeypatch):
    monkeypatch.setattr(Client, "authorize", AsyncMock(side_effect=APIError(403)))
    handler = AsyncMock()
    app.on("channel.mention")(handler)
    with pytest.raises(_EventError):
        await app.handle_event(mention(), client=bound(mention()))
    handler.assert_not_called()


def bound(payload):
    return Client(
        payload["installation_id"], app_id=payload["app_id"], transport=Mock()
    )
