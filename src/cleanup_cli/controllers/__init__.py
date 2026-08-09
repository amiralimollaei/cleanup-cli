"""Application controller contracts and implementations."""

from .core import (
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
