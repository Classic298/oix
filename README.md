# oix: Open WebUI SDK

A Python SDK for building apps for Open WebUI, with authenticated events and a signed API client.

Requires Python 3.11+ and Open WebUI with Apps support.

## Install

```sh
python -m pip install oix
```

## A minimal app

```python
from oix import App

app = App(
    manifest_id="hello",
    name="Hello",
    scopes=["channels:messages:read", "channels:messages:write"],
)


@app.on("channel.message.created")
async def hello(event):
    if event.content == "hello":
        await event.reply("Hello!")


if __name__ == "__main__":
    app.run()
```

`manifest_id` identifies your app in its manifest. WebUI assigns a separate `app_id` to each installation; incoming events and their clients already carry that ID.

Save the example as `app.py`. Generate an Ed25519 key and an API key, then start the app:

```sh
umask 077
openssl genpkey -algorithm ED25519 -out app-private-key.pem
export APP_PRIVATE_KEY_FILE="$PWD/app-private-key.pem"
export API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python app.py
```

The app listens on port `8000` at `/events`. Use `app.run(port=8001, path="/manifest")` to change these. Press Ctrl+C to stop it.

Keep both secrets in persistent deployment storage and reuse them across restarts. The SDK reads `API_KEY` and the PEM file at `APP_PRIVATE_KEY_FILE`. Explicit `api_key` and PEM `private_key` arguments override these defaults. Missing or invalid credentials fail at construction; startup never generates credentials.

In WebUI, enable Apps, add your public HTTPS event URL and the API key, approve the scopes, then connect. Invite the app into a channel. GET on the event URL provides the manifest; POST receives authenticated events.

Pass `manifest=False` to `app.run()` for a POST-only endpoint and enter the app details manually in WebUI. `app.manifest` supplies those details.

For localhost development, pass the exact WebUI origin in `allowed_private_origins=("http://127.0.0.1:8080",)`. Configure WebUI's `APP_ALLOWED_PRIVATE_ORIGINS` to allow your app origin. Production callbacks require HTTPS and public destinations by default.

## Events and replies

Register async handlers with `@app.on(...)` for:

- `channel.mention`: explicit app mentions; text is in `event.data["text"]`.
- `channel.action`: a saved message button; `event.data` contains `message_id`, `action_id`, and `context`.
- `channel.operation.status`: a status request for deferred work.
- `channel.message.created`, `channel.message.updated`, `channel.message.deleted`: ordinary channel events, including messages without mentions. Request `channels:messages:read` for these.

All callbacks receive one `event`. Its `event.client` is bound to the originating instance and installation, and is excluded from event serialization. Events are verified against the originating WebUI before your handler runs. For interactions, the actor's admin status reflects their current authorization. Ordinary message events do not authorize commands on behalf of their authors.

For mentions, actions and operation-status handlers, return a string or `AppChannelReply(content="...")` for an inline private reply, or set `visibility="channel"` to post publicly in the channel. `AppChannelReply` also supports `update`, `download`, `error`, and `deferred`. `Action(action_id="...", label="...", context={...})` buttons are supported only on saved channel messages. Button handlers must enforce any operation-specific rules themselves.

Return `AppChannelReply(type="deferred", operation_id=event.id)` for background work and implement a `channel.operation.status` handler. Keep status replies private. Your app manages workers, job storage, and restart recovery. Recheck `await event.client.authorize(event.execution_token)` before later sensitive work, and apply your app's own permissions.

Ordinary channel-event handlers return `None`. Use `await event.send("...")` to post in the current channel or `await event.reply("...")` to reference the source message. Both preserve its existing thread; replying to a top-level message stays top-level. Both require write access and accept `actions=[...]`. Deleted messages and events without a source message cannot be replied to.

`event.content` contains created/updated message content or the original mention text, unchanged. `event.channel_id`, `event.message_id` and `event.parent_id` use verified source information. The original `actor`, `channel`, `message`, `data` and token fields remain available.

Ordinary events are acknowledged after verification, then handlers run in background tasks. `max_concurrency=32` bounds verification and ordinary handlers together; overload returns HTTP 503 without queuing or retries. Acknowledged work does not survive a process crash. Delivery is best effort, can arrive out of order, and has no replay guarantee. Keep durable work in your own job storage.

Self-authored message events are ignored by default for the current installation. Pass `ignore_self_events=False` to receive them. Other apps and model messages are not filtered. A mention can produce both a channel event and a mention interaction.

Interactions have a 2.5-second processing budget, including at most two seconds for verification. Ordinary verification has eight seconds; acknowledged handlers have no HTTP deadline. Timeout cancellation does not undo external side effects. Return a deferred reply for longer interactions.

Route buttons by exact action ID:

```python
from oix import AppChannelReply


@app.action("cancel_publish")
async def cancel(event):
    return AppChannelReply(
        type="update", visibility="channel", content="Publishing cancelled."
    )
```

An exact action handler overrides the generic `channel.action` handler; only one runs. Register async handlers accepting one event. Duplicate registrations and unknown event names fail immediately.

## Scopes

Apps request scopes in their manifest. Admins choose which to grant and can change that selection in app settings. Channel access also requires an invitation to the channel.

- `channels:mentions:read`: receive explicit app mentions, without message history.
- `channels:messages:read`: receive message events, read history and thread replies, and receive mentions.
- `channels:messages:write`: send messages and edit the app's own messages, including public callback replies.
- `channels:read`: list joined channels and their details, without messages.

Write access does not grant read access. Private replies do not require write access.

## Runtime client

```python
message = await event.client.create_message(
    event.channel_id, content="Working on it...", parent_id=event.parent_id
)
await event.client.update_message(message["id"], content="Done.", etag=message["etag"])
```

Content and actions are replacement values when editing. Omitted actions become an empty list; include any buttons you want to retain. `AppChannelMessage` remains exported for independent validation.

Available methods: `get_me()`, `list_channels()`, `list_messages(channel_id, limit=50, before=None, parent_id=None)`, `create_message(...)`, `update_message(...)`, `authorize(execution_token)`, and `verify_event(event_token)`. Responses are WebUI JSON dictionaries/lists. `APIError.status` exposes HTTP failures, including `412` for stale edits. `method`, `path` and `detail` provide request context and at most 64 KiB of response detail, without authorization headers. Transport failures use aiohttp exceptions or `TimeoutError`. Message creation is never automatically retried.

One `App` can serve multiple independent WebUI instances. Each event supplies its WebUI origin and local installation ID; its client is bound to that pair. For proactive messages, save the destination URL and app ID alongside your subscription, then call `app.client(webui_url, app_id)` after restart. API-key holders are trusted to connect instances; a shared key does not provide tenant-specific business authorization.

## Existing web servers

To mount the app in FastAPI, install `oix[fastapi]` and include its router:

```python
from fastapi import FastAPI
from oix.fastapi import create_router

server = FastAPI()
server.include_router(create_router(app, path="/events"))
```

Run your existing server instead of calling `app.run()`. The router also accepts `manifest=False`. Its shutdown lifespan composes with your application's lifespan, including existing workers.

For another web framework, call `app.authenticate(authorization)` before reading a body, enforce a 1 MiB body limit while streaming, parse JSON, then `await app.handle_event(payload, authorization=authorization)`. Return the resulting JSON with compact UTF-8 serialization. Responses are limited to 256 KiB of actual JSON bytes and message content to 32,000 characters. Call `await app.close()` during shutdown: it stops admission, waits up to 30 seconds for accepted handlers, then cancels and awaits remaining work. The built-in server and FastAPI adapter do this automatically.

## Migrating from 0.3

Before:

```python
@app.on("channel.message.created")
async def hello(event, client):
    await client.create_message(
        event.channel["id"], AppChannelMessage(content="Hello!")
    )
```

After:

```python
@app.on("channel.message.created")
async def hello(event):
    await event.send("Hello!")
```

For explicit destinations use `event.client.create_message(channel_id, content="...")`. Change updates to `event.client.update_message(message_id, content="...", etag=etag)`. Handlers no longer receive a second argument. Ordinary handlers run after acknowledgement and must return `None`; interactions can return strings for private replies.

## Development

```sh
uv sync --extra fastapi
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
uv run twine check dist/*
```

