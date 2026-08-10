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
    DuplicateObserver,
)
from ..models.image_webp import (
    WebPDirectoryConverter,
    WebPDirectoryConversionResult,
    WebPOptions,
    WebPResultObserver,
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


@dataclass(frozen=True, slots=True)
class DeduplicationRequest:
    """Input passed from a view to the deduplication controller."""

    directory: Path
    options: DeduplicationOptions = field(default_factory=DeduplicationOptions)
    on_result: DuplicateObserver | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class DeduplicationResult:
    """View-independent result of a deduplication operation."""

    duplicates: tuple[Duplicate, ...]
    deleted: bool

    @property
    def total_saved_bytes(self) -> int:
        """Return deleted or potentially reclaimable duplicate bytes."""

        return sum(duplicate.saved_bytes for duplicate in self.duplicates)


class DeduplicationController(
    Controller[DeduplicationRequest, DeduplicationResult],
    Generic[SignatureT],
):
    """Coordinate deduplication model operations for any view."""

    def __init__(self, model: DirectoryDeduplicator[SignatureT]) -> None:
        self._model = model

    def execute(self, request: DeduplicationRequest) -> DeduplicationResult:
        duplicates = self._model.deduplicate(
            request.directory,
            request.options,
            on_result=request.on_result,
        )
        return DeduplicationResult(tuple(duplicates), request.options.delete)


@dataclass(frozen=True, slots=True)
class WebPConversionRequest:
    """Input passed from a view to the WebP conversion controller."""

    directory: Path
    options: WebPOptions = field(default_factory=WebPOptions)
    on_result: WebPResultObserver | None = field(
        default=None,
        compare=False,
        repr=False,
    )


class WebPConversionController(
    Controller[WebPConversionRequest, WebPDirectoryConversionResult],
    Generic[FrameT],
):
    """Coordinate WebP model operations for any view."""

    def __init__(self, model: WebPDirectoryConverter[FrameT]) -> None:
        self._model = model

    def execute(self, request: WebPConversionRequest) -> WebPDirectoryConversionResult:
        return self._model.convert(
            request.directory,
            request.options,
            on_result=request.on_result,
        )
