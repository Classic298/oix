# oix: Open WebUI SDK

A Python SDK for building apps for Open WebUI, with authenticated events and a signed API client.

Requires Python 3.11+ and Open WebUI with Apps support.

## Install

```sh
python -m pip install oix
```

## A minimal app

Save this as `app.py`:

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

### Create credentials once

Run this from your app directory. It creates an API key and a signing key, and refuses to overwrite existing credentials:

```sh
(
    set -eu
    umask 077
    if [ -e app-private-key.pem ] || [ -e .env ]; then
        echo "Credentials already exist. Reuse them; do not regenerate them." >&2
        exit 1
    fi
    set -C
    openssl genpkey -algorithm ED25519 > app-private-key.pem
    printf 'export API_KEY=%s\nexport APP_PRIVATE_KEY_FILE=./app-private-key.pem\n' \
        "$(openssl rand -hex 32)" > .env
)
```

Keep `.env` and the private-key file out of source control. Back them up securely. In production, persist these values through your deployment's secret settings and a mounted key file. Replacing either credential requires updating existing installations.

### Start the app

Load the saved credentials and run:

```sh
. ./.env
python app.py
```

The SDK reads process environment variables; it does not automatically load `.env`. Run from the app directory when using the relative file path above.

The app listens on port `8000` at `/events`. Keep it running while you connect it to WebUI and send messages. Press Ctrl+C to stop it; use the same startup commands and credentials next time.

## Connect to Open WebUI

Your app and WebUI must be able to reach each other. For a deployed app, forward an HTTPS URL such as `https://hello.example.com/events` to the app's port `8000`. Open WebUI through its externally reachable HTTPS address when connecting. For two processes on your computer, use the [local development setup](#local-development) below first.

### 1. Connect the app

1. Sign in to WebUI as an admin. Open **Settings**, then **General** under **Admin**. Under **Features**, enable **Apps** and **Channels**, then save.
2. Open **Apps** in the admin settings and click **Add app** (the plus button).
3. In **App URL**, enter your app's full endpoint, for example `https://hello.example.com/events`, and click **Continue**.
4. WebUI loads the app's name, public key and requested scopes automatically. Keep `channels:messages:read` and `channels:messages:write` selected for this example, then click **Back** to return to the connection form.
5. In **API Key**, paste the value of `API_KEY` from your app's `.env`, without `export API_KEY=`. This is the key you created above. Keep the private-key file on the app server.
6. Check **Who can use this app?**, then click **Connect**.

The App URL points to your Python app. The SDK serves its manifest with GET and receives events with POST at that same URL; you do not need a separate `/manifest` route.

### 2. Invite it and send a message

1. Open a channel you own or manage, or create one. Apps cannot join direct messages.
2. Click the **Users** count in the channel header. Under **Apps**, select **Hello** from **Choose an app** and click **Invite**.
3. Send `hello` in the channel. The app should reply `Hello!` as a channel message. No @mention is needed for this handler.

Invite the app separately into each channel where it should receive messages. To connect another WebUI instance, repeat these steps with the same app URL and API key. Each event's client automatically uses the instance that sent it.

### Local development

This example assumes WebUI runs at `http://127.0.0.1:8080` and the app at `http://127.0.0.1:8000`, both directly on the same computer.

Set this environment variable **on the WebUI process**, then restart WebUI:

```sh
APP_ALLOWED_PRIVATE_ORIGINS=http://127.0.0.1:8000,http://127.0.0.1:8080
```

Start your app with:

```sh
. ./.env
export OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS=http://127.0.0.1:8080
python app.py
```

Open WebUI at `http://127.0.0.1:8080` and follow the connection steps above, using **App URL** `http://127.0.0.1:8000/events`.

WebUI's setting permits the local app endpoint and its own HTTP origin during connection. The app's setting permits callbacks to local WebUI. Public HTTPS destinations need neither exception. With Docker or separate machines, replace these addresses with ones reachable from both processes; `127.0.0.1` inside a container points to that container.

### Configuration

`manifest_id` identifies your app in its manifest. WebUI assigns a separate `app_id` to each installation; incoming events and their clients already carry that ID.

The SDK reads `API_KEY` and the PEM file at `APP_PRIVATE_KEY_FILE`. Explicit `api_key` and PEM `private_key` arguments override these defaults. Missing or invalid credentials fail at construction; startup never generates credentials.

Use `app.run(port=8001, path="/events")` to change the listening port or endpoint. Pass `manifest=False` for a POST-only endpoint, then choose **Enter details manually** in WebUI. `app.manifest` supplies the app details, including its public key and requested scopes.

`OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS` accepts comma-separated origins. Explicit `allowed_private_origins=(...)` overrides the environment; an empty tuple disables all exceptions. Invalid configured origins fail at construction.

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

## Troubleshooting

`app.run()` installs concise stderr logging for `oix` if your application has not already configured logging. FastAPI uses the host application's logging. To see intentionally ignored events or events without handlers, configure Python logging before startup:

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("oix").setLevel(logging.DEBUG)
```

Connection verifies the app's signing identity, not callback reachability. If connection succeeds but handlers never run, check the app's verification logs: the app must reach the WebUI origin over HTTP(S), and WebUI must still permit the installation, scope and channel access. The logs distinguish blocked destinations, DNS, TLS, connection failures and timeouts. Ordinary self-authored events and events without registered handlers are acknowledged without running a handler.

`APIError` includes the HTTP method, path, status and recognized WebUI error reason. Unknown response bodies remain available in its bounded `detail` attribute but are not included in default messages. Do not log response bodies or tokens indiscriminately. Handler programming errors retain their tracebacks.

## Development

```sh
uv sync --extra fastapi
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
uv run twine check dist/*
```

