"""Application controllers connecting cleanup models to external views."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

from ..models.image_duplicates import (
    DeduplicationOptions,
    DirectoryDeduplicator,
    Duplicate,
)
from ..models.image_webp import (
    WebPConversion,
    WebPDirectoryConverter,
    WebPOptions,
    WebPSkip,
)


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT", covariant=True)
SignatureT = TypeVar("SignatureT")
FrameT = TypeVar("FrameT")


class Controller(ABC, Generic[RequestT, ResultT]):
    """Generic application boundary consumed by a view."""

    @abstractmethod
    def execute(self, request: RequestT) -> ResultT:
        """Handle one validated application request."""


@dataclass(frozen=True)
class DeduplicationRequest:
    """Input passed from a view to the deduplication controller."""

    directory: Path
    options: DeduplicationOptions = field(default_factory=DeduplicationOptions)


@dataclass(frozen=True)
class DeduplicationResult:
    """View-independent result of a deduplication operation."""

    duplicates: tuple[Duplicate, ...]
    deleted: bool


class DeduplicationController(
    Controller[DeduplicationRequest, DeduplicationResult],
    Generic[SignatureT],
):
    """Coordinate deduplication model operations for any view."""

    def __init__(self, model: DirectoryDeduplicator[SignatureT]) -> None:
        self._model = model

    def execute(self, request: DeduplicationRequest) -> DeduplicationResult:
        duplicates = self._model.deduplicate(request.directory, request.options)
        return DeduplicationResult(tuple(duplicates), request.options.delete)


@dataclass(frozen=True)
class WebPConversionRequest:
    """Input passed from a view to the WebP conversion controller."""

    directory: Path
    options: WebPOptions = field(default_factory=WebPOptions)


@dataclass(frozen=True)
class WebPConversionResult:
    """View-independent result of a WebP conversion operation."""

    conversions: tuple[WebPConversion, ...]
    skips: tuple[WebPSkip, ...]


class WebPConversionController(
    Controller[WebPConversionRequest, WebPConversionResult],
    Generic[FrameT],
):
    """Coordinate WebP model operations for any view."""

    def __init__(self, model: WebPDirectoryConverter[FrameT]) -> None:
        self._model = model

    def execute(self, request: WebPConversionRequest) -> WebPConversionResult:
        conversions, skips = self._model.convert(
            request.directory,
            quality=request.options.quality,
            replace=request.options.replace,
            max_workers=request.options.max_workers,
        )
        return WebPConversionResult(tuple(conversions), tuple(skips))
