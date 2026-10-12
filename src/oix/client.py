"""Small async client with signed requests and checked network destinations."""

import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

import aiohttp
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import Action, AppChannelMessage


class APIError(Exception):
    def __init__(
        self, status: int, *, method: str = "", path: str = "", detail: str = ""
    ):
        self.status = status
        self.method = method
        self.path = path
        self.detail = detail
        message = f"Open WebUI returned HTTP {status}"
        if method or path:
            message += f" for {method} {path}"
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


class _DestinationError(ValueError):
    """A destination rejected by the SDK's network policy."""


def _origin(value: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port or (443 if url.scheme == "https" else 80)
    except ValueError:
        raise ValueError(
            "Expected an HTTP(S) origin with a valid hostname and port"
        ) from None
    if (
        len(value) > 2048
        or any(c.isspace() for c in value)
        or "\\" in value
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or url.scheme not in ("http", "https")
    ):
        raise ValueError("Expected an HTTP(S) origin without a path or credentials")
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname.lower()
    suffix = f":{port}" if port != (443 if url.scheme == "https" else 80) else ""
    return f"{url.scheme}://{host}{suffix}"


def _check_address(host: str) -> None:
    address = ipaddress.ip_address(host)
    if (
        not address.is_global
        or address.is_multicast
        or (
            isinstance(address, ipaddress.IPv6Address)
            and (
                address.ipv4_mapped
                or address.sixtofour
                or address.teredo
                or address in ipaddress.ip_network("64:ff9b::/96")
                or address in ipaddress.ip_network("64:ff9b:1::/48")
            )
        )
    ):
        raise _DestinationError(
            "WebUI resolves to a private or reserved address. For local development, "
            "allow its exact origin with allowed_private_origins or "
            "OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS."
        )


class _PublicResolver(aiohttp.resolver.ThreadedResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        for address in addresses:
            _check_address(address["host"])
        return addresses


class Client:
    """Bound to one installation; construct a new client for each event destination."""

    def __init__(
        self,
        webui_url: str,
        app_id: str,
        private_key: Ed25519PrivateKey,
        *,
        allowed_private_origins: tuple[str, ...] = (),
    ):
        self.webui_url = _origin(webui_url)
        if not isinstance(app_id, str) or not 1 <= len(app_id) <= 100:
            raise ValueError("Invalid app ID")
        self.app_id = app_id
        self._private_key = private_key
        self._allow_private = self.webui_url in {
            _origin(value) for value in allowed_private_origins
        }
        if not self._allow_private:
            if not self.webui_url.startswith("https://"):
                raise _DestinationError(
                    "WebUI requires HTTPS. For local HTTP development, allow its exact "
                    "origin with allowed_private_origins or "
                    "OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS."
                )
            host = urlsplit(self.webui_url).hostname
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                _check_address(host)

    def _sign(self, challenge: str | None = None) -> str:
        now = int(time.time())
        claims = {
            "sub": self.app_id,
            "aud": self.webui_url + "/api/v1/apps/runtime",
            "iat": now,
            "exp": now + 60,
        }
        if challenge is not None:
            claims["challenge"] = challenge
        return jwt.encode(
            claims,
            self._private_key,
            algorithm="EdDSA",
            headers={"typ": "webui-app-connect+jwt" if challenge else "webui-app+jwt"},
        )

    async def _request(
        self, method: str, path: str, *, json_body=None, params=None, etag=None
    ):
        headers = {"Authorization": f"Bearer {self._sign()}"}
        if etag is not None:
            if not re.fullmatch(r"[0-9]+", etag):
                raise ValueError("Use the unquoted etag returned by Open WebUI")
            headers["If-Match"] = f'"{etag}"'
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                resolver=None if self._allow_private else _PublicResolver(),
                use_dns_cache=False,
            ),
            timeout=aiohttp.ClientTimeout(total=10),
            trust_env=False,
        ) as session:
            async with session.request(
                method,
                self.webui_url + "/api/v1/apps/runtime" + path,
                headers=headers,
                json=json_body,
                params=params,
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    error_body = bytearray()
                    while len(error_body) < 65536:
                        chunk = await response.content.read(65536 - len(error_body))
                        if not chunk:
                            break
                        error_body.extend(chunk)
                    raise APIError(
                        response.status,
                        method=method,
                        path=path,
                        detail=error_body.decode("utf-8", errors="replace"),
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > 1048576:
                        raise ValueError("WebUI response exceeds 1 MiB")
                return json.loads(body)

    async def get_me(self):
        return await self._request("GET", "/me")

    async def list_channels(self):
        return await self._request("GET", "/channels")

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
        params = {"limit": str(limit)}
        if before is not None:
            params["before"] = before
        if parent_id is not None:
            params["parent_id"] = parent_id
        return await self._request(
            "GET",
            f"/channels/{_path_id(channel_id)}/messages",
            params=params,
        )

    async def create_message(
        self,
        channel_id: str,
        *,
        content: str = "",
        actions: list[Action] | None = None,
        parent_id: str | None = None,
        reply_to_id: str | None = None,
    ):
        message = AppChannelMessage(
            content=content,
            actions=actions if actions is not None else [],
            parent_id=parent_id,
            reply_to_id=reply_to_id,
        )
        return await self._request(
            "POST",
            f"/channels/{_path_id(channel_id)}/messages",
            json_body=message.model_dump(),
        )

    async def update_message(
        self,
        message_id: str,
        *,
        content: str = "",
        actions: list[Action] | None = None,
        etag: str,
    ):
        message = AppChannelMessage(
            content=content, actions=actions if actions is not None else []
        )
        return await self._request(
            "PATCH",
            f"/messages/{_path_id(message_id)}",
            json_body=message.model_dump(),
            etag=etag,
        )

    async def authorize(self, execution_token: str):
        """Recheck current user, app, scope and channel access before sensitive work."""
        return await self._request(
            "POST",
            "/authorize",
            json_body={"token": execution_token},
        )

    async def verify_event(self, event_token: str):
        return await self._request(
            "POST",
            "/events/verify",
            json_body={"token": event_token},
        )


def _path_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise ValueError("Invalid resource ID")
    return value
