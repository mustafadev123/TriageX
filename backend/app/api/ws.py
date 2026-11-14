"""WebSocket channel: pushes lightweight change notifications to every open dashboard.

Messages (server -> client), all JSON:
  {"type": "hello", "run_id": str, "version": int}
  {"type": "run.updated", "run_id": str, "version": int, "reason": str, "recompute_ms": float|null}
  {"type": "feed.event", "run_id": str, "event": FeedEvent}
Clients refetch GET /api/runs/{run_id} (or use the POST response) after run.updated.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("triagex.ws")
router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def broadcast(self, message: dict) -> None:
        if not self._clients:
            return
        results = await asyncio.gather(
            *(ws.send_json(message) for ws in list(self._clients)), return_exceptions=True
        )
        for ws, result in zip(list(self._clients), results, strict=False):
            if isinstance(result, Exception):
                self.disconnect(ws)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    manager: ConnectionManager = ws.app.state.ws_manager
    service = ws.app.state.demo_service
    await manager.connect(ws)
    try:
        run = service.active_run()
        await ws.send_json({"type": "hello", "run_id": run.run_id, "version": run.version})
        while True:
            message = await ws.receive_json()
            if isinstance(message, dict) and message.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:  # malformed client input should never take the server down
        log.warning("websocket closed after an error", exc_info=True)
    finally:
        manager.disconnect(ws)
