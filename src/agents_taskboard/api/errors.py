"""The `{"error": {"code", "message"}}` error shape; some errors add fields (a 412 on a document
carries `current_version`)."""

from typing import Any

from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, extra: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def error_response(
    status: int, code: str, message: str, extra: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, **(extra or {})}}, status_code=status
    )
