"""Application controller contracts and implementations."""

from cleanup_cli.controllers.core import (
    Controller,
    DeduplicationController,
    DeduplicationRequest,
    DeduplicationResult,
    WebPConversionController,
    WebPConversionRequest,
)

__all__ = [
    "Controller",
    "DeduplicationController",
    "DeduplicationRequest",
    "DeduplicationResult",
    "WebPConversionController",
    "WebPConversionRequest",
]
