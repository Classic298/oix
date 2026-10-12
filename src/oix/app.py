"""Authenticate and verify events before dispatching them to app handlers."""

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any, get_args

from aiohttp import ClientError, web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .client import APIError, Client
from .models import AppChannelReply, Event, EventType, Scope

Handler = Callable[[Event, Client], Awaitable[AppChannelReply | None]]


class App:
    def __init__(
        self,
        *,
        manifest_id: str,
        name: str,
        api_key: str,
        private_key: str,
        scopes: list[Scope],
        description: str = "",
        allowed_private_origins: tuple[str, ...] = (),
    ):
        if (
            not 32 <= len(api_key) <= 4096
            or not api_key.isascii()
            or any(c.isspace() for c in api_key)
            or "," in api_key
        ):
            raise ValueError(
                "API key must be a single ASCII secret of 32–4096 characters"
            )
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("An Ed25519 private key is required")
        if (
            not 1 <= len(manifest_id) <= 100
            or not 1 <= len(name) <= 100
            or len(description) > 2000
        ):
            raise ValueError("Invalid app metadata")
        if not set(scopes) <= set(get_args(Scope)):
            raise ValueError("Invalid app scopes")
        self._api_key = api_key
        self._private_key = key
        self._allowed_private_origins = allowed_private_origins
        self._handlers: dict[str, Handler] = {}
        self.manifest = {
            "id": manifest_id,
            "name": name,
            "description": description,
            "public_key": key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode(),
            "requested_scopes": list(scopes),
        }

    def authenticate(self, authorization: str) -> None:
        if not hmac.compare_digest(
            authorization.encode(),
            ("Bearer " + self._api_key).encode(),
        ):
            raise PermissionError("Invalid app API key")

    def client(self, webui_url: str, app_id: str) -> Client:
        return Client(
            webui_url,
            app_id,
            self._private_key,
            allowed_private_origins=self._allowed_private_origins,
        )

    def on(self, event_type: EventType):
        if event_type not in get_args(EventType):
            raise ValueError(f"Unknown event type: {event_type}")

        def register(handler: Handler) -> Handler:
            if event_type in self._handlers:
                raise ValueError(f"Handler already registered for {event_type}")
            self._handlers[event_type] = handler
            return handler

        return register

    def run(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8000,
        path: str = "/events",
        manifest: bool = True,
    ) -> None:
        """Serve HTTP events until shutdown. Call after registering handlers."""
        server = web.Application(client_max_size=262144)

        async def discovery(request: web.Request) -> web.Response:
            return web.json_response(self.manifest)

        async def events(request: web.Request) -> web.Response:
            authorization = request.headers.get("authorization", "")
            try:
                self.authenticate(authorization)
            except PermissionError:
                raise web.HTTPUnauthorized(text="Invalid app API key") from None
            body = bytearray()
            async for chunk in request.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 262144:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=262144, actual_size=len(body)
                    )
            try:
                return web.json_response(
                    await self.handle_event(
                        json.loads(body), authorization=authorization
                    )
                )
            except PermissionError:
                raise web.HTTPForbidden(text="Invalid event context") from None
            except (ValueError, KeyError, TypeError, AttributeError):
                raise web.HTTPBadRequest(text="Invalid app event or reply") from None
            except APIError:
                raise web.HTTPForbidden(
                    text="WebUI rejected event verification"
                ) from None
            except (ClientError, TimeoutError):
                raise web.HTTPBadGateway(text="WebUI is unavailable") from None

        if manifest:
            server.router.add_get(path, discovery)
        server.router.add_post(path, events)
        web.run_app(server, host=host, port=port)

    async def handle_event(
        self, payload: dict[str, Any], *, authorization: str
    ) -> dict:
        """Verify and dispatch an event. Authenticate before buffering HTTP bodies."""
        self.authenticate(authorization)
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        if not isinstance(payload.get("id"), str) or not 1 <= len(payload["id"]) <= 100:
            raise ValueError("Invalid event ID")
        client = self.client(payload["webui_url"], payload["app_id"])
        if payload.get("type") == "connect":
            challenge = payload.get("data", {}).get("challenge")
            if not isinstance(challenge, str) or not 16 <= len(challenge) <= 128:
                raise ValueError("Invalid connection challenge")
            return {"proof": client._sign(challenge)}
        event = Event.model_validate(payload)
        if event.type.startswith("channel.message."):
            verified = await client.verify_event(payload["event_token"])
            if verified != {k: v for k, v in payload.items() if k != "event_token"}:
                raise PermissionError("Event context mismatch")
        else:
            verified = await client.authorize(payload["execution_token"])
            if (
                verified.get("authorized") is not True
                or verified["operation_id"] != event.id
                or verified["actor"]["id"] != event.actor.get("id")
                or verified["channel_id"] != event.channel.get("id")
                or verified["type"] != event.type
                or verified["data"] != event.data
            ):
                raise PermissionError("Execution context mismatch")
            # Source and extra channel metadata are unsigned; do not expose them.
            event.actor = verified["actor"]
            event.channel = {"id": verified["channel_id"]}
            event.message = None
        handler = self._handlers.get(event.type)
        reply = await handler(event, client) if handler else None
        if event.type.startswith("channel.message."):
            if reply is not None:
                raise ValueError(
                    "Message events use client.create_message(), not replies"
                )
            return {"received": True}
        if reply is None:
            reply = AppChannelReply()
        if not isinstance(reply, AppChannelReply):
            raise TypeError("Interaction handlers must return AppChannelReply or None")
        if reply.type == "deferred" and reply.operation_id != event.id:
            raise ValueError("Use the incoming event ID for deferred replies")
        if event.type == "channel.operation.status" and (
            reply.visibility != "private" or reply.type == "update"
        ):
            raise ValueError("Operation status replies must be private")
        result = reply.model_dump()
        if len(json.dumps(result).encode()) > 262144:
            raise ValueError("App reply exceeds 256 KiB")
        return result
