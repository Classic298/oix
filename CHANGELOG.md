# Changelog

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
