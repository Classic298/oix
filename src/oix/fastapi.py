"""Optional FastAPI adapter. Mount GET discovery and POST events at any path."""

import json

import aiohttp
from fastapi import APIRouter, HTTPException, Request

from .app import App
from .client import APIError


def create_router(
    app: App, *, path: str = "/events", manifest: bool = True
) -> APIRouter:
    router = APIRouter()
    if manifest:

        @router.get(path)
        async def discovery():
            return app.manifest

    @router.post(path)
    async def events(request: Request):
        authorization = request.headers.get("authorization", "")
        try:
            app.authenticate(authorization)
        except PermissionError:
            raise HTTPException(401, "Invalid app API key") from None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, "Payload too large")
        try:
            return await app.handle_event(json.loads(body), authorization=authorization)
        except PermissionError:
            raise HTTPException(403, "Invalid event context") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, "Invalid app event or reply") from None
        except APIError:
            raise HTTPException(403, "WebUI rejected event verification") from None
        except (aiohttp.ClientError, TimeoutError):
            raise HTTPException(502, "WebUI is unavailable") from None

    return router
