"""Installation-bound app operations over an authenticated socket."""

import json
import re

from .models import Action, AppChannelMessage, Embed


class APIError(Exception):
    def __init__(
        self, status: int, *, method: str = "", path: str = "", detail: str = ""
    ):
        self.status = status
        self.method = method
        self.path = path
        self.detail = detail
        message = f"Open WebUI returned status {status}"
        if method or path:
            message += f" for {method} {path}".rstrip()
        try:
            reason = json.loads(detail).get("detail")
        except (ValueError, AttributeError):
            reason = None
        # Only server-defined messages belong in default logs, never arbitrary bodies.
        if isinstance(reason, str) and reason in {
            "App scope was not granted",
            "Invite the app into this channel first",
            "Invalid app credential",
            "Invalid or expired app context",
            "Apps are disabled",
            "App not found",
            "App is inactive",
            "Channels are disabled",
            "Channel unavailable",
            "Channel access denied",
            "App invocation denied",
            "Actor is unavailable",
            "User is inactive",
            "App message not found",
            "Message changed",
            "If-Match is required",
            "Invalid message reference",
            "App credentials changed",
        }:
            message += f": {reason}"
        super().__init__(message)


class ConnectionUnavailable(ConnectionError):
    """No connection was available; the operation was not dispatched."""


class ConnectionLost(ConnectionError):
    """The connection failed after dispatch; the outcome may be unknown."""


class Client:
    """Runtime client bound to an authenticated installation."""

    def __init__(self, installation_id, *, transport, app_id=None, sid=None):
        self.installation_id = installation_id
        self.app_id = app_id
        self._transport = transport
        self._sid = sid

    async def _request(self, operation, **arguments):
        return await self._transport.request(
            self.installation_id, operation, arguments, preferred_sid=self._sid
        )

    async def get_me(self):
        return await self._request("get_me")

    async def list_channels(self):
        return await self._request("list_channels")

    async def list_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        parent_id: str | None = None,
    ):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params = {"limit": limit}
        if before is not None:
            params["before"] = before
        if parent_id is not None:
            params["parent_id"] = parent_id
        return await self._request(
            "list_messages",
            channel_id=_path_id(channel_id),
            **params,
        )

    async def create_message(
        self,
        channel_id: str,
        *,
        content: str = "",
        actions: list[Action] | None = None,
        embeds: list[Embed] | None = None,
        parent_id: str | None = None,
        reply_to_id: str | None = None,
    ):
        message = AppChannelMessage(
            content=content,
            actions=actions if actions is not None else [],
            embeds=embeds if embeds is not None else [],
            parent_id=parent_id,
            reply_to_id=reply_to_id,
        )
        return await self._request(
            "create_message",
            channel_id=_path_id(channel_id),
            **message.model_dump(),
        )

    async def update_message(
        self,
        message_id: str,
        *,
        content: str = "",
        actions: list[Action] | None = None,
        embeds: list[Embed] | None = None,
        etag: str,
    ):
        if not isinstance(etag, str) or not re.fullmatch(r"[0-9]+", etag):
            raise ValueError("Use the unquoted etag returned by Open WebUI")
        message = AppChannelMessage(
            content=content,
            actions=actions if actions is not None else [],
            embeds=embeds if embeds is not None else [],
        )
        return await self._request(
            "update_message",
            message_id=_path_id(message_id),
            etag=etag,
            **message.model_dump(),
        )

    async def authorize(self, execution_token: str):
        """Recheck current user, app, scope and channel access before sensitive work."""
        return await self._request(
            "authorize",
            token=execution_token,
        )

    async def verify_event(self, event_token: str):
        return await self._request(
            "verify_event",
            token=event_token,
        )


def _path_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise ValueError("Invalid resource ID")
    return value
