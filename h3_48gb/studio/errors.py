"""One error type for every studio operation; `app.py` turns it into the §7 envelope."""
from __future__ import annotations


class StudioError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = dict(detail or {})

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}
