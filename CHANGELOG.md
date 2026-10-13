# Changelog

## 0.7.0 - 2026-10-13

- Add structured embeds with titles, fields, color accents, authors, footers and thumbnails.
- Support embeds on message creation, updates, event helpers and interaction replies.
- Add link actions that open a URL without calling an app handler.
- Validate embed text limits and HTTP(S) URLs before delivery.

## [0.6.0] - 2026-10-13

- Add `AppChannelReply(type="ack")` to acknowledge an interaction without creating a response message.
- Remove deferred replies, the operation-status event, and the reply `operation_id` field. Apps publish background results through the existing message API.
- Keep execution-token authorization and the current Socket.IO transport. Upgrade WebUI and apps together after draining existing deferred jobs.

## [0.5.1] - 2026-10-13

- Allow socket packets up to 16 MiB for runtime requests and responses, including message history. Keep the 1 MiB event and 256 KiB interaction response limits.

## [0.5.0]

- Move event delivery and runtime operations to authenticated Socket.IO connections initiated by WebUI. Private WebUI instances need no inbound endpoint.
- Preserve installation identity across restarts using app-issued reconnect credentials stored by WebUI.
- Add optional Redis routing for multiple app replicas and multiple WebUI workers; target one connection per operation.
- Expose `event.installation_id` and `app.client(installation_id)` for durable destinations. Remove URL-based clients and origin settings.
- Distinguish disconnected operations from uncertain outcomes without retrying message creation.
- Existing installations require one reconnect after upgrading WebUI and the SDK together. Recreate URL-based subscriptions explicitly.

## [0.4.4] - 2026-10-12

- Accept HTTP and private WebUI origins after API-key authentication without extra configuration.
- Replace `allowed_private_origins` / `OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS` with an optional `allowed_origins` / `OPEN_WEBUI_ALLOWED_ORIGINS` allowlist that restricts all WebUI origins when set.

## [0.4.3] - 2026-10-12

- Require the app API key for manifest discovery in both HTTP adapters. WebUI must send the key when fetching app details.

## [0.4.2] - 2026-10-12

- Validate private-origin configuration at startup, with an environment default.
- Add standalone logging and actionable configuration, network and verification errors.
- Show recognized WebUI API errors without exposing arbitrary response bodies.
- Separate one-time credential creation from repeatable startup instructions.

## [0.4.1] - 2026-10-12

- Warn instead of rejecting API keys shorter than 32 characters.
- Allow commas in API keys; continue rejecting empty keys and invalid HTTP header characters.

## [0.4.0] - 2026-10-12

- Read persistent credentials from environment defaults.
- Use single-event handlers with a verified client, message properties, and thread-aware sends and replies.
- Accept normal keyword arguments for message creation and edits; retain payload models for independent use.
- Add exact action handlers and private string interaction replies.
- Acknowledge verified ordinary events before running bounded background handlers; ignore self-authored events by default.
- Drain accepted work on shutdown, with matching built-in HTTP and FastAPI behavior.
- Separate input, verification and handler errors; bound deadlines, event bodies and API error details.
- Preserve compact UTF-8 responses and support 1 MiB incoming events.

## [0.3.0] - 2026-10-12

- Rename the channel reply model to `AppChannelReply` and channel events to `channel.*`.
- Use `manifest_id` when creating an app, separate from WebUI's installation `app_id`.
- Rename signed fields to `execution_token` and `event_token`; verification requests use `token`.
- Reject unknown event names when registering handlers.
- Prefix channel mention and message scopes with `channels:`.

## [0.2.0] - 2026-10-12

- Built-in HTTP server with `app.run()` and a plain-Python quickstart.

## [0.1.0] - 2026-10-12

- Initial external app SDK: authenticated events, signed runtime client, Markdown replies and actions.
- Optional FastAPI adapter and manifest discovery.
- uv packaging, CI, GitHub releases and PyPI trusted publishing.
