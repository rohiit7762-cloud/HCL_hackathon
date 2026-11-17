"""Application error hierarchy.

Errors carry a stable machine code and a safe, user-facing message. Internal
details must never be put in `message`; log them separately instead.
"""

from __future__ import annotations


class AppError(Exception):
    code: str = "internal_error"
    status_code: int = 500
    default_message: str = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(AppError):
    """Used for both 'missing' and 'not permitted' resources (invariant I12)."""

    code = "not_found"
    status_code = 404
    default_message = "The requested resource was not found."


class AuthenticationError(AppError):
    code = "unauthenticated"
    status_code = 401
    default_message = "Your session is missing or has expired. Please sign in again."


class PermissionDeniedError(AppError):
    """For role-gated operations such as ingestion. Never used for data sources (use 404)."""

    code = "forbidden"
    status_code = 403
    default_message = "You are not allowed to perform this operation."


class ValidationFailedError(AppError):
    code = "validation_failed"
    status_code = 422
    default_message = "The request or a model response could not be validated."


class ProviderUnavailableError(AppError):
    code = "provider_unavailable"
    status_code = 503
    default_message = "An upstream service is temporarily unavailable. Please try again."


class RateLimitedError(AppError):
    code = "rate_limited"
    status_code = 429
    default_message = "The service is busy right now. Please try again shortly."


class ToolTimeoutError(AppError):
    code = "timeout"
    status_code = 504
    default_message = "The request took too long to complete."


class CapabilityLimitError(AppError):
    """Raised when a provider cannot perform an operation (e.g. no vision support)."""

    code = "capability_limit"
    status_code = 501
    default_message = "This capability is not available with the current configuration."
