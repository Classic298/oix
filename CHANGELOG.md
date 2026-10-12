# Changelog

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
