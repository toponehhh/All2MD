from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from starlette.responses import JSONResponse

from .errors import UploadTooLargeError

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


class RequestSizeLimitMiddleware:
    """Enforce a streaming ASGI body limit, including chunked requests."""

    def __init__(self, app, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive: AsgiReceive, send: AsgiSend) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        received_bytes = 0
        exceeded = False
        replacement_sent = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal exceeded, received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    exceeded = True
                    raise UploadTooLargeError("Request body exceeds the configured upload limit")
            return message

        async def limited_send(message: dict[str, Any]) -> None:
            nonlocal replacement_sent
            if not exceeded:
                await send(message)
                return
            if replacement_sent:
                return
            replacement_sent = True
            request_id = scope.get("state", {}).get("request_id", uuid4().hex)
            response = JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "upload_too_large",
                        "message": "Request body exceeds the configured upload limit",
                        "request_id": request_id,
                    }
                },
                headers={
                    "X-Request-ID": request_id,
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)

        try:
            await self.app(scope, limited_receive, limited_send)
        except UploadTooLargeError:
            if not replacement_sent:
                exceeded = True
                await limited_send({"type": "http.response.start"})
