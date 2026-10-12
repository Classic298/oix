"""Mount authenticated manifest discovery and Socket.IO on a FastAPI router."""

from contextlib import asynccontextmanager

import socketio
from fastapi import APIRouter, HTTPException, Request, WebSocket

from .app import App


def create_router(
    app: App, *, path: str = "/events", manifest: bool = True
) -> APIRouter:
    socket = app._get_socket("asgi")
    endpoint = path.rstrip("/") + "/socket.io/"
    # FastAPI already matched this route, including any host router prefix.
    transport = socketio.ASGIApp(socket.server, socketio_path=None)

    @asynccontextmanager
    async def lifespan(server):
        try:
            await socket.start()
            yield
        finally:
            await app.close()

    router = APIRouter(lifespan=lifespan)
    if manifest:

        @router.get(path)
        async def discovery(request: Request):
            try:
                app.authenticate(request.headers.get("authorization", ""))
            except PermissionError:
                raise HTTPException(401, "Invalid app API key") from None
            return app.manifest

    @router.post(path)
    async def events(request: Request):
        try:
            app.authenticate(request.headers.get("authorization", ""))
        except PermissionError:
            raise HTTPException(401, "Invalid app API key") from None
        raise HTTPException(426, "Upgrade to oix 0.5: events use Socket.IO")

    @router.websocket(endpoint)
    async def connection(websocket: WebSocket):
        try:
            app.authenticate(websocket.headers.get("authorization", ""))
        except PermissionError:
            await websocket.close(code=1008)
            return
        await transport(websocket.scope, websocket.receive, websocket.send)

    return router
