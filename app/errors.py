"""Shared exception types the service layer raises and API routers translate to JSON.

Keeping validation errors typed (rather than raw ValueError/HTTPException) lets the
service layer stay framework-agnostic, which matters because both the FastAPI routers
and any future direct callers (tests, scripts) need to catch these the same way.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for all business-rule errors raised by the service layer."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class NotFoundError(ServiceError):
    """Raised when a referenced record (case, station, category...) doesn't exist."""


class ValidationError(ServiceError):
    """Raised when input fails a business rule (as opposed to a schema-level check)."""


class InvalidTransitionError(ServiceError):
    """Raised when a requested status change is not allowed from the current status."""
