"""
EventFlux — X-Request-ID Middleware

Reads an existing `X-Request-ID` header from the incoming request or
generates a new UUID4 if absent. The value is then:
  1. Stored in `request.state.request_id` for use in route handlers.
  2. Echoed back in the `X-Request-ID` response header.

This enables end-to-end tracing from client → API → worker → database.
"""
from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
