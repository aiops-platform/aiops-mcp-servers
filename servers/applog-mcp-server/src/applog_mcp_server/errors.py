"""错误码与异常。"""
from enum import StrEnum


class ErrorCode(StrEnum):
    INTERNAL_ERROR = "INTERNAL_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_REQUEST = "INVALID_REQUEST"
    TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    CONFIG_ERROR = "CONFIG_ERROR"


class AppError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)
