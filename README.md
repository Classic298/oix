# oix: Open WebUI SDK

A Python SDK for building apps for Open WebUI, with authenticated events and an installation-bound client.

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

WebUI opens an outbound socket to your app. For a deployed app, forward an HTTPS URL such as `https://hello.example.com/events` to port `8000`, with WebSocket upgrades enabled. WebUI can stay on localhost or a private network. You do not need to expose it or configure a callback URL.

```text
Private WebUI ── opens connection ──> Public app (for example, Railway)
             <── events, replies and API calls use that same socket ──>
```

For two processes on your computer, use the [local development setup](#local-development).

### 1. Connect the app

1. Sign in to WebUI as an admin. Open **Settings**, then **General** under **Admin**. Under **Features**, enable **Apps** and **Channels**, then save.
2. Open **Apps** in the admin settings and click **Add app** (the plus button).
3. In **App URL**, enter your app's full endpoint, for example `https://hello.example.com/events`. In **API Key**, paste the value of `API_KEY` from your app's `.env`, without `export API_KEY=`. Keep the private-key file on the app server.
4. Click **Continue**. WebUI authenticates with that key and loads the app's name, public key and requested scopes automatically. Keep `channels:messages:read` and `channels:messages:write` selected for this example, then click **Continue** to open the connection form.
5. Check **Access**, then click **Connect**.

The App URL points to your Python app. The SDK serves its optional manifest with GET at that URL. Events and runtime calls use the `/events/socket.io/` WebSocket endpoint and the `/apps` namespace. Discovery and the socket handshake require `Authorization: Bearer <API_KEY>`. You do not need a separate `/manifest` route. Opening the URL in a browser without the key returns 401.

### 2. Invite it and send a message

1. Open a channel you own or manage, or create one. Apps cannot join direct messages.
2. Click the **Users** count in the channel header. Click **Add Member**, select **Hello** under **Apps**, then click **Add**.
3. Send `hello` in the channel. The app should reply `Hello!` as a channel message. No @mention is needed for this handler.

Invite the app separately into each channel where it should receive messages. To connect another WebUI instance, repeat these steps with the same app URL and API key. Each event's client automatically uses the instance that sent it.

### Local development

This example assumes WebUI runs at `http://127.0.0.1:8080` and the app at `http://127.0.0.1:8000`, both directly on the same computer.

Set this environment variable **on the WebUI process**, then restart WebUI:

```sh
APP_ALLOWED_PRIVATE_ORIGINS=http://127.0.0.1:8000
```

Start your app with:

```sh
. ./.env
python app.py
```

Open WebUI at `http://127.0.0.1:8080` and follow the connection steps above, using **App URL** `http://127.0.0.1:8000/events`.

WebUI's setting permits the local app endpoint. With Docker or separate machines, use an app address reachable from WebUI; `127.0.0.1` inside a container points to that container. The app does not connect back to a WebUI address.

### Configuration

Set `profile_image_url="https://example.com/doge.png"` in `App(...)` to supply a profile image. Use an HTTP(S) image URL that users' browsers can load without the app API key, or embed a PNG, JPEG, GIF or WebP as a base64 data URL (maximum 192 KiB including the prefix). WebUI uses it in messages, mentions, member lists and admin settings. For an existing installation, refresh its app details in WebUI and save to pick up image changes. Omitting it keeps the default icon.

Pass a local file directly and oix will embed it:

```python
from pathlib import Path
from oix import App

app = App(
    manifest_id="doge",
    name="Doge",
    scopes=["channels:messages:write"],
    profile_image_url=Path("doge.png"),
)
```

The file is read once when `App` is created. Relative paths use the process's working directory. PNG, JPEG, GIF and WebP are supported; the encoded data URL must fit within 192 KiB. Missing, unreadable or oversized files fail at startup.

`manifest_id` identifies your app in its manifest. WebUI assigns its local `app_id`; the app issues an independent `installation_id` during connection. Incoming events and their clients carry both.

The SDK reads `API_KEY` and the PEM file at `APP_PRIVATE_KEY_FILE`. Explicit `api_key` and PEM `private_key` arguments override these defaults. Missing or invalid credentials fail at construction; startup never generates credentials.

Use `app.run(port=8001, path="/events")` to change the listening port or endpoint. Pass `manifest=False` to disable manifest discovery, then choose **Enter details manually** in WebUI. `app.manifest` supplies the app details, including its public key and requested scopes.

Any WebUI with the API key can install the app. WebUI pins the app's public key and verifies its signed connection proof. The app issues a signed reconnect credential, which WebUI stores encrypted. Possessing the shared API key does not let another installation claim an existing installation ID. Keep the API key private and use HTTPS across untrusted networks.

### Multiple workers and replicas

One process needs no Redis. For multiple app replicas, install `oix[redis]` (or `oix[fastapi,redis]`) and pass `redis_url="redis://..."` to `App`. All replicas must share that Redis, manifest ID, API key and private key. Redis stores expiring socket routes, not installation credentials. A background worker can route an API call through another replica's connection. Each call targets one WebUI socket. If Redis is unavailable, routing fails closed.

Each WebUI worker opens its own outbound connection. WebSocket-only transport avoids polling affinity requirements. Configure the proxy's idle timeout above the 25-second ping interval and 20-second ping timeout. Use a shared database, `WEBUI_SECRET_KEY` and Redis for WebUI HA. Independent WebUI installations remain isolated.

WebUI reconnects automatically with jittered backoff, up to 30 seconds. Restarting the app requires only its persistent credentials and, when using multiple replicas, the shared Redis configuration. An app needs business storage only for its own jobs or subscriptions.

## Events and replies

Register async handlers with `@app.on(...)` for:

- `channel.mention`: explicit app mentions; text is in `event.data["text"]`.
- `channel.action`: a saved message button; `event.data` contains `message_id`, `action_id`, and `context`.
- `channel.message.created`, `channel.message.updated`, `channel.message.deleted`: ordinary channel events, including messages without mentions. Request `channels:messages:read` for these.

All callbacks receive one `event`. Its `event.client` is bound to the originating instance and installation, and is excluded from event serialization. Events are verified against the originating WebUI before your handler runs. For interactions, the actor's admin status reflects their current authorization. Ordinary message events do not authorize commands on behalf of their authors.

For mentions and actions, return a string or `AppChannelReply(content="...")` for an inline private reply, or set `visibility="channel"` to post publicly in the channel. `AppChannelReply` also supports `update`, `download`, `error`, and `ack`. `Action(action_id="...", label="...", context={...})` buttons are supported only on saved channel messages. Button handlers must enforce any operation-specific rules themselves.

Return `AppChannelReply(type="ack")` after accepting background work. This creates no message, loading placeholder, or polling request. Persist durable work before acknowledging it; your worker posts results through `client.create_message()`. Your app owns workers and restart recovery. Recheck `await client.authorize(event.execution_token)` before later sensitive work, and apply your app's own permissions. An acknowledgement does not schedule work automatically.

Ordinary channel-event handlers return `None`. Use `await event.send("...")` to post in the current channel or `await event.reply("...")` to reference the source message. Both preserve its existing thread; replying to a top-level message stays top-level. Both require write access and accept `actions=[...]`. Deleted messages and events without a source message cannot be replied to.

`event.content` contains created/updated message content or the original mention text, unchanged. `event.channel_id`, `event.message_id` and `event.parent_id` use verified source information. The original `actor`, `channel`, `message`, `data` and token fields remain available.

Ordinary events are acknowledged after verification, then handlers run in background tasks. `max_concurrency=32` bounds verification and ordinary handlers together; overload returns status 503 without queuing or retries. Acknowledged work does not survive a process crash. Delivery is best effort, can arrive out of order, and has no replay guarantee. Keep durable work in your own job storage.

Self-authored message events are ignored by default for the current installation. Pass `ignore_self_events=False` to receive them. Other apps and model messages are not filtered. A mention can produce both a channel event and a mention interaction.

Interactions have a 2.5-second processing budget, including at most two seconds for verification. Ordinary verification has eight seconds; acknowledged handlers have no acknowledgement deadline. Timeout cancellation does not undo external side effects. Persist longer work and return an acknowledgement before this deadline.

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

Available methods: `get_me()`, `list_channels()`, `list_messages(channel_id, limit=50, before=None, parent_id=None)`, `create_message(...)`, `update_message(...)`, `authorize(execution_token)`, and `verify_event(event_token)`. Responses are WebUI JSON dictionaries/lists. `APIError.status` exposes operation failures, including `412` for stale edits. `method`, `path` and `detail` provide request context and at most 64 KiB of response detail, without authorization headers. `ConnectionUnavailable` means no route was available and the operation was not sent. `ConnectionLost` means dispatch began and its outcome may be unknown. Message creation is never automatically retried.

One `App` can serve multiple independent WebUI instances. For proactive messages, save `event.installation_id` with your subscription, then use `app.client(installation_id)` after restart. The transport must be running and a connection to that installation must be available. API-key holders are trusted to install apps; a shared key does not provide tenant-specific business authorization.

## Existing web servers

To mount the app in FastAPI, install `oix[fastapi]` and include its router:

```python
from fastapi import FastAPI
from oix.fastapi import create_router

server = FastAPI()
server.include_router(create_router(app, path="/events"))
```

Run your existing server instead of calling `app.run()`. Set its WebSocket frame limit to 16 MiB; with Uvicorn, use `--ws-max-size 16777216` (or `ws_max_size=16 * 1024 * 1024` in `uvicorn.run`). The router also accepts `manifest=False`. Its shutdown lifespan composes with your application's lifespan, including existing workers.

Use the built-in aiohttp server or the FastAPI router. Both authenticate before accepting the socket, enforce 16 MiB JSON packets, and reject binary packets. Individual events remain limited to 1 MiB. Interaction responses are limited to 256 KiB of compact UTF-8 JSON; message content remains limited to 32,000 characters. History remains paginated. `await app.close()` stops admission, waits up to 30 seconds for accepted handlers, then cancels and awaits remaining work. Both adapters call it during shutdown.

## Troubleshooting

`app.run()` installs concise stderr logging for `oix` if your application has not already configured logging. FastAPI uses the host application's logging. To see intentionally ignored events or events without handlers, configure Python logging before startup:

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("oix").setLevel(logging.DEBUG)
```

Connection verifies the app's signing identity and a round trip in both directions before WebUI marks it connected. If a connection fails, check the endpoint, API key, pinned public key and proxy WebSocket support. WebUI's app list shows Connected, Connecting or Disconnected. An upgraded HTTP installation must be reconnected once in admin settings.

Events still check current installation, scope and channel access before executing. Self-authored ordinary events and events without registered handlers are acknowledged without running a handler. Removing membership or disabling an app blocks subsequent runtime operations, even on an already open socket.

`APIError` includes the operation, status and recognized WebUI error reason. Unknown response bodies remain available in its bounded `detail` attribute but are not included in default messages. Do not log response bodies or tokens indiscriminately. Handler programming errors retain their tracebacks.

## Development

```sh
uv sync --locked --extra fastapi --extra redis
uv run ruff check .
uv run ruff format --check .
TEST_REDIS_URL=redis://127.0.0.1:6379 uv run pytest
uv build
uv run twine check dist/*
```
## Embeds

Send a compact report card with `Embed` and `EmbedField`:

```python
from oix import Action, Embed, EmbedField

await event.reply(
    embeds=[
        Embed(
            title="Advisory reported",
            url="https://github.com/example/project/security/advisories/GHSA-example",
            description="Review the reported vulnerability.",
            color=0xDA3633,
            fields=[EmbedField(name="Severity", value="High", inline=True)],
        )
    ],
    actions=[
        Action(
            label="View Full Report",
            url="https://github.com/example/project/security/advisories/GHSA-example",
        )
    ],
)
```

`event.send()`, `event.reply()`, client message methods and `AppChannelReply` accept `embeds`. Content is optional for card-only messages. `Embed.add_field()` appends a validated field. Authors, footers and thumbnails accept `{"name": ...}`, `{"text": ...}` and `{"url": ...}` respectively.

Edits replace content, actions and embeds. Include cards and buttons you want to retain; omitted lists are cleared. Link actions require an HTTP(S) URL instead of an action ID and never invoke a handler. Acknowledgements cannot contain embeds.

Embeds follow Discord's text limits: 256-character titles and field names, 4,096-character descriptions, 1,024-character field values, 2,048-character footers, 25 fields per embed, ten embeds and 6,000 combined embed characters per message. Oversized input raises a validation error; the SDK does not truncate it.

Upgrade WebUI before apps start sending embeds. Version 0.7.0 requires WebUI's embed support for those messages; the socket transport and credentials are unchanged.
