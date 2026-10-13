import asyncio
import copy
import logging
from unittest.mock import AsyncMock

import pytest
from test_sdk import API_KEY, PEM, authorized, bound, mention

from oix import Action, App, Client, Event
from oix.app import _EventError


def make_app(**kwargs):
    return App(
        manifest_id="hello",
        name="Hello",
        scopes=[],
        api_key=API_KEY,
        private_key=PEM,
        **kwargs,
    )


def message(**changes):
    value = dict(
        id="event-1",
        type="channel.message.created",
        app_id="installed-1",
        installation_id="https://webui.example",
        channel={"id": "channel-1"},
        message={
            "id": "message-1",
            "content": "hello",
            "parent_id": None,
            "author": {"type": "user", "id": "alice"},
        },
    )
    value.update(changes)
    value["event_token"] = "signed"
    return value


def verified_messages(monkeypatch, *payloads):
    verified = [
        {k: copy.deepcopy(v) for k, v in p.items() if k != "event_token"}
        for p in payloads
    ]
    monkeypatch.setattr(Client, "verify_event", AsyncMock(side_effect=verified))


def test_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("APP_PRIVATE_KEY_FILE", raising=False)
    args = dict(manifest_id="hello", name="Hello", scopes=[])
    with pytest.raises(ValueError, match="API_KEY"):
        App(**args)
    monkeypatch.setenv("API_KEY", API_KEY)
    with pytest.raises(ValueError, match="APP_PRIVATE_KEY_FILE"):
        App(**args)
    path = tmp_path / "key.pem"
    path.write_text(PEM)
    monkeypatch.setenv("APP_PRIVATE_KEY_FILE", str(path))
    app = App(**args)
    assert app.manifest["public_key"] == make_app().manifest["public_key"]
    monkeypatch.setenv("API_KEY", "bad")
    monkeypatch.setenv("APP_PRIVATE_KEY_FILE", "/missing")
    make_app()  # Explicit credentials override the environment.
    with pytest.raises(ValueError, match="API_KEY"):
        App(**args, api_key="", private_key=PEM)
    with pytest.raises(ValueError, match="PEM"):
        App(**args, api_key=API_KEY, private_key="")
    with pytest.raises(ValueError, match="Cannot read"):
        App(**args, api_key=API_KEY)


@pytest.mark.parametrize(
    "api_key", ["short-key", "a" * 31, "a" * 32, "key,with,commas"]
)
def test_api_key_length_warns_without_blocking_authentication(api_key, caplog):
    with caplog.at_level(logging.WARNING, logger="oix.app"):
        app = App(
            manifest_id="hello",
            name="Hello",
            scopes=[],
            api_key=api_key,
            private_key=PEM,
        )
    app.authenticate("Bearer " + api_key)
    with pytest.raises(PermissionError):
        app.authenticate("Bearer wrong-key")
    assert ("shorter than 32 characters" in caplog.text) == (len(api_key) < 32)
    assert api_key not in caplog.text


@pytest.mark.parametrize(
    "api_key",
    [
        "",
        "with spaces",
        "line\nbreak",
        "tab\tkey",
        "nul\x00key",
        "delete\x7fkey",
        "non-ascii-🔑",
        "a" * 4097,
    ],
)
def test_unusable_api_keys_are_rejected(api_key):
    with pytest.raises(ValueError, match="API_KEY"):
        App(
            manifest_id="hello",
            name="Hello",
            scopes=[],
            api_key=api_key,
            private_key=PEM,
        )


def test_registration_validation():
    app = make_app()
    with pytest.raises(TypeError, match="async"):
        app.on("channel.mention")(lambda event: None)

    async def old(event, client):
        pass

    with pytest.raises(TypeError, match="one event"):
        app.on("channel.mention")(old)
    for name in ["", "x" * 101, 42]:
        with pytest.raises(ValueError):
            app.action(name)
    handler = AsyncMock()
    app.action("confirm")(handler)
    with pytest.raises(ValueError, match="already"):
        app.action("confirm")(handler)


@pytest.mark.parametrize("parent", [None, "root-message"])
async def test_bound_helpers_and_serialization(monkeypatch, parent):
    app = make_app()
    payload = message()
    payload["message"]["parent_id"] = parent
    verified_messages(monkeypatch, payload)
    calls = []

    async def create(client, channel_id, **kwargs):
        calls.append((client.installation_id, client.app_id, channel_id, kwargs))
        return {"id": "new", "etag": "1"}

    monkeypatch.setattr(Client, "create_message", create)

    @app.on("channel.message.created")
    async def handler(event):
        assert event.content == "hello" and event.message_id == "message-1"
        assert "client" not in event.model_dump_json()
        assert "parent_id" not in event.model_dump()  # Property, not a wire field.
        assert (await event.send("hello"))["id"] == "new"
        await event.reply("reply", actions=[Action(action_id="ok", label="OK")])

    await app.handle_event(payload, client=bound(payload))
    await app.close()
    assert calls[0][3] == dict(
        content="hello", actions=None, embeds=None, parent_id=parent
    )
    assert calls[1][3]["reply_to_id"] == "message-1"
    assert calls[1][3]["parent_id"] == parent
    assert calls[0][:3] == (payload["installation_id"], payload["app_id"], "channel-1")
    event = Event.model_validate(payload)
    with pytest.raises(RuntimeError, match="verified client"):
        _ = event.client
    event.type = "channel.message.deleted"
    assert event.content is None
    with pytest.raises(ValueError, match="source message"):
        await event.reply("no")
    event.type = "channel.action"
    event.data = {}
    assert event.message_id is None and event.parent_id is None
    with pytest.raises(ValueError, match="source message"):
        await event.reply("no")


async def test_mentions_use_signed_thread_and_string_reply(monkeypatch):
    app = make_app()
    payload = mention()
    payload["data"].update(message_id="original", parent_id="thread")
    monkeypatch.setattr(
        Client, "authorize", AsyncMock(return_value=copy.deepcopy(authorized(payload)))
    )
    send = AsyncMock(return_value={"id": "new"})
    monkeypatch.setattr(Client, "create_message", send)

    @app.on("channel.mention")
    async def handler(event):
        assert event.content == "hello"
        await event.reply("public")
        return "private"

    result = await app.handle_event(payload, client=bound(payload))
    assert result["content"] == "private" and result["visibility"] == "private"
    assert send.call_args.kwargs["parent_id"] == "thread"
    payload["data"]["parent_id"] = "forged"
    with pytest.raises(_EventError) as exc:
        await app.handle_event(payload, client=bound(payload))
    assert exc.value.status == 403
    send.assert_awaited_once()


async def test_exact_action_precedes_generic(monkeypatch):
    app = make_app()
    generic = AsyncMock(return_value="generic")
    app.on("channel.action")(generic)
    exact = AsyncMock(return_value="exact")
    app.action("confirm")(exact)
    p = mention()
    p["type"] = "channel.action"
    p["data"] = {"action_id": "confirm"}
    monkeypatch.setattr(Client, "authorize", AsyncMock(return_value=authorized(p)))
    assert (await app.handle_event(p, client=bound(p)))["content"] == "exact"
    generic.assert_not_called()
    exact.assert_awaited_once()
    p["data"] = {"action_id": "other"}
    monkeypatch.setattr(Client, "authorize", AsyncMock(return_value=authorized(p)))
    assert (await app.handle_event(p, client=bound(p)))["content"] == "generic"


async def test_ack_capacity_and_drain(monkeypatch):
    app = make_app(max_concurrency=1)
    p = message()
    verified_messages(monkeypatch, p, p)
    started, finish = asyncio.Event(), asyncio.Event()

    @app.on("channel.message.created")
    async def handler(event):
        started.set()
        await finish.wait()

    assert await app.handle_event(p, client=bound(p)) == {"received": True}
    await started.wait()
    with pytest.raises(_EventError) as exc:
        await app.handle_event(p, client=bound(p))
    assert exc.value.status == 503
    closing = asyncio.create_task(app.close())
    await asyncio.sleep(0)
    assert not closing.done()
    with pytest.raises(_EventError) as exc:
        await app.handle_event(p, client=bound(p))
    assert exc.value.status == 503
    finish.set()
    await closing
    await app.close()
    assert not app._tasks and app._active_messages == 0


async def test_verification_reserves_capacity_and_shutdown_cancels(monkeypatch):
    import oix.app as module

    app = make_app(max_concurrency=1)
    started = asyncio.Event()

    async def verify(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Client, "verify_event", verify)
    task = asyncio.create_task(app.handle_event(message(), client=bound(message())))
    await started.wait()
    with pytest.raises(_EventError) as exc:
        await app.handle_event(message(), client=bound(message()))
    assert exc.value.status == 503
    monkeypatch.setattr(module, "SHUTDOWN_TIMEOUT", 0.01)
    await app.close()
    assert task.cancelled() and app._active_messages == 0


async def test_shutdown_cancels_running_handler(monkeypatch):
    import oix.app as module

    app = make_app()
    p = message()
    verified_messages(monkeypatch, p)
    started, cancelled = asyncio.Event(), asyncio.Event()

    @app.on("channel.message.created")
    async def handler(event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await app.handle_event(p, client=bound(p))
    await started.wait()
    monkeypatch.setattr(module, "SHUTDOWN_TIMEOUT", 0.01)
    await app.close()
    assert cancelled.is_set() and not app._tasks


@pytest.mark.parametrize(
    "ignore,author,expected",
    [
        (True, {"type": "app", "id": "installed-1"}, 0),
        (False, {"type": "app", "id": "installed-1"}, 1),
        (True, {"type": "app", "id": "other"}, 1),
        (True, {"type": "model", "id": "installed-1"}, 1),
    ],
)
async def test_self_filter(monkeypatch, ignore, author, expected):
    app = make_app(ignore_self_events=ignore)
    p = message()
    p["message"]["author"] = author
    verified_messages(monkeypatch, p)
    handler = AsyncMock(return_value=None)
    app.on(p["type"])(handler)
    await app.handle_event(p, client=bound(p))
    await app.close()
    assert handler.await_count == expected


async def test_instances_remain_bound_during_concurrent_work(monkeypatch):
    app = make_app()
    p = message()
    other = message(app_id="second", installation_id="https://second.example")
    verified_messages(monkeypatch, p, other)
    release = asyncio.Event()
    seen = []

    @app.on(p["type"])
    async def handler(event):
        await release.wait()
        seen.append((event.client.installation_id, event.client.app_id))

    await asyncio.gather(
        app.handle_event(p, client=bound(p)),
        app.handle_event(other, client=bound(other)),
    )
    release.set()
    await app.close()
    assert set(seen) == {
        ("https://webui.example", "installed-1"),
        ("https://second.example", "second"),
    }


@pytest.mark.parametrize(
    "phase", ["message_verification", "interaction_verification", "handler"]
)
async def test_deadlines(monkeypatch, phase):
    import oix.app as module

    for constant in [
        "VERIFICATION_TIMEOUT",
        "MESSAGE_VERIFICATION_TIMEOUT",
        "INTERACTION_TIMEOUT",
    ]:
        monkeypatch.setattr(
            module, constant, 0.01 if constant != "INTERACTION_TIMEOUT" else 0.03
        )
    app = make_app()

    async def slow(*args):
        await asyncio.Event().wait()

    p = message() if phase == "message_verification" else mention()
    if phase == "message_verification":
        monkeypatch.setattr(Client, "verify_event", slow)
    else:
        monkeypatch.setattr(
            Client,
            "authorize",
            slow
            if phase == "interaction_verification"
            else AsyncMock(return_value=authorized(p)),
        )
        app.on(p["type"])(slow)
    with pytest.raises(_EventError) as exc:
        await app.handle_event(p, client=bound(p))
    assert exc.value.status == 504
    assert app._active_messages == 0


async def test_background_invalid_return_is_logged(monkeypatch, caplog):
    app = make_app()
    p = message()
    verified_messages(monkeypatch, p)
    app.on(p["type"])(AsyncMock(return_value="incorrect"))
    with caplog.at_level(logging.ERROR):
        assert await app.handle_event(p, client=bound(p)) == {"received": True}
        await app.close()
    assert "use event.send() or event.reply()" in caplog.text


async def test_unregistered_valid_event_only_acknowledged(monkeypatch):
    app = make_app()
    payload = message()
    verified_messages(monkeypatch, payload)
    assert await app.handle_event(payload, client=bound(payload)) == {"received": True}
    assert not app._tasks and app._active_messages == 0


async def test_client_validates_replacement_values(monkeypatch):
    client = bound(dict(installation_id="one", app_id="installed"))
    request = AsyncMock(return_value={"id": "message", "etag": "2"})
    monkeypatch.setattr(client, "_request", request)
    await client.update_message("message", content="done", etag="1")
    assert request.call_args.kwargs["actions"] == []
    request.reset_mock()
    with pytest.raises(ValueError):
        await client.create_message("channel", content="x" * 32001)
    request.assert_not_awaited()
