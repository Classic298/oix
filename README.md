# oix: Open WebUI SDK

A Python SDK for building apps for Open WebUI, with authenticated events and a signed API client.

Requires Python 3.11+ and Open WebUI with Apps support.

## Install

```sh
python -m pip install oix
```

## A minimal app

```python
import os
from pathlib import Path

from oix import App, AppChannelReply, Client, Event

app = App(
    manifest_id="hello",
    name="Hello",
    api_key=os.environ["API_KEY"],
    private_key=Path(os.environ["APP_PRIVATE_KEY_FILE"]).read_text(),
    scopes=["mentions:read"],
)


@app.on("channel.mention")
async def hello(event: Event, client: Client) -> AppChannelReply:
    return AppChannelReply(content="Hello! Only you can see this reply.")


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

Keep both secrets in persistent deployment storage and reuse them across restarts.

In WebUI, enable Apps, add your public HTTPS event URL and the API key, approve the scopes, then connect. Invite the app into a channel. GET on the event URL provides the manifest; POST receives authenticated events.

Pass `manifest=False` to `app.run()` for a POST-only endpoint and enter the app details manually in WebUI. `app.manifest` supplies those details.

For localhost development, pass the exact WebUI origin in `allowed_private_origins=("http://127.0.0.1:8080",)`. Configure WebUI's `APP_ALLOWED_PRIVATE_ORIGINS` to allow your app origin. Production callbacks require HTTPS and public destinations by default.

## Events and replies

Register async handlers with `@app.on(...)` for:

- `channel.mention`: explicit app mentions; text is in `event.data["text"]`.
- `channel.action`: a saved message button; `event.data` contains `message_id`, `action_id`, and `context`.
- `channel.operation.status`: a status request for deferred work.
- `channel.message.created`, `channel.message.updated`, `channel.message.deleted`: ordinary channel events, including messages without mentions. Request `messages:read` for these.

All callbacks receive `(event, client)`. Events are verified against the originating WebUI before your handler runs. For interactions, the actor's admin status reflects their current authorization. Ordinary message events do not authorize commands on behalf of their authors.

Return `AppChannelReply(content="...")` for an inline private reply, or set `visibility="channel"` to post publicly in the channel. `AppChannelReply` also supports `update`, `download`, `error`, and `deferred`. `Action(action_id="...", label="...", context={...})` buttons are supported only on saved channel messages. Button handlers must enforce any operation-specific rules themselves.

Return `AppChannelReply(type="deferred", operation_id=event.id)` for background work and implement an `channel.operation.status` handler. Keep status replies private. Your app manages workers, job storage, and restart recovery. Recheck `await client.authorize(event.execution_token)` before later sensitive work, and apply your app's own permissions.

Ordinary channel-event handlers return `None`; use the runtime client to send a message. A mention can produce both a channel event and a mention interaction. Check `event.message["author"]` to avoid responding to your own messages. Delivery is best effort, can arrive out of order, and has no replay guarantee.

## Runtime client

```python
from oix import AppChannelMessage

message = await client.create_message(
    event.channel["id"], AppChannelMessage(content="Working on it...")
)
await client.update_message(
    message["id"], AppChannelMessage(content="Done."), etag=message["etag"]
)
```

Available methods: `get_me()`, `list_channels()`, `list_messages(channel_id, limit=50, before=None, parent_id=None)`, `create_message(...)`, `update_message(...)`, `authorize(execution_token)`, and `verify_event(event_token)`. Responses are WebUI JSON dictionaries/lists. `APIError.status` exposes HTTP failures, including `412` for stale edits. Transport failures use aiohttp exceptions or `TimeoutError`. Message creation is never automatically retried.

One `App` can serve multiple independent WebUI instances. Each event supplies its WebUI origin and local installation ID; its client is bound to that pair. For proactive messages, save the destination URL and app ID alongside your subscription, then call `app.client(webui_url, app_id)` after restart. API-key holders are trusted to connect instances; a shared key does not provide tenant-specific business authorization.

## Existing web servers

To mount the app in FastAPI, install `oix[fastapi]` and include its router:

```python
from fastapi import FastAPI
from oix.fastapi import create_router

server = FastAPI()
server.include_router(create_router(app, path="/events"))
```

Run your existing server instead of calling `app.run()`. The router also accepts `manifest=False`.

For another web framework, call `app.authenticate(authorization)` before reading a body, enforce a 256 KiB body limit, parse JSON, then `await app.handle_event(payload, authorization=authorization)`. Return the resulting JSON.

## Development

```sh
uv sync --extra fastapi
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
uv run twine check dist/*
```

