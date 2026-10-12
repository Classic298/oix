"""Optional FastAPI adapter. Mount GET discovery and POST events at any path."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Request

from .app import MAX_EVENT_BYTES, App, _EventError


def create_router(
    app: App, *, path: str = "/events", manifest: bool = True
) -> APIRouter:
    @asynccontextmanager
    async def lifespan(server):
        try:
            yield
        finally:
            await app.close()

    router = APIRouter(lifespan=lifespan)
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
            if len(body) > MAX_EVENT_BYTES:
                raise HTTPException(413, "Payload too large")
        try:
            return await app._handle_body(body, authorization)
        except _EventError as exc:
            raise HTTPException(exc.status, exc.detail) from None

    return router
