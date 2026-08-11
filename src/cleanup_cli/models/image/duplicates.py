"""Image-specific duplicate indexing, matching, and service composition."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, TypeVar

from cleanup_cli.models.abstractions import (
    ImageDirectoryScanner,
    IndexedFile,
    RecursiveDirectoryIndexer,
)
from cleanup_cli.models.deduplication import (
    DeduplicationOptions,
    DirectoryDeduplicator,
    Duplicate,
    ExhaustiveCandidateIndex,
    QualityAwareDuplicateDetector,
)
from cleanup_cli.models.image.signature_cache import ImageSignatureCache
from cleanup_cli.models.image.signatures import (
    PHASH_BITS,
    ImageSignature,
    ImageSignatureDistance,
    PHashValue,
    PillowImageSignatureAnalyzer,
)
from cleanup_cli.models.progress import ProgressObserver


SignatureT = TypeVar("SignatureT")
ImageSignaturePair: TypeAlias = tuple[Path, ImageSignature]


@dataclass(frozen=True, order=True, slots=True)
class ImageQuality:
    """Ranking criteria for choosing which duplicate image to retain."""

    pixels: int
    encoded_bytes: int


def image_quality_key(image: IndexedFile[SignatureT]) -> ImageQuality:
    """Rank an image by resolution and then encoded size."""

    resolution = (
        image.value.resolution
        if isinstance(image.value, ImageSignature)
        else (0, 0)
    )
    pixels = resolution[0] * resolution[1]
    if image.identity is not None:
        return ImageQuality(pixels, image.identity.size)
    try:
        encoded_bytes = image.path.stat().st_size
    except OSError:
        encoded_bytes = 0
    return ImageQuality(pixels, encoded_bytes)


class BandedPHashIndex:
    """Prune comparisons by splitting a pHash into exact-match bands."""

    def __init__(self, threshold: int = 0) -> None:
        self._exhaustive = (
            ExhaustiveCandidateIndex(threshold)
            if threshold == PHASH_BITS
            else None
        )
        if self._exhaustive is not None:
            self._bands: tuple[tuple[int, int], ...] = ()
            self._tables: list[dict[int, int | list[int]]] = []
            return

        band_count = threshold + 1
        edges = [
            (PHASH_BITS * band) // band_count
            for band in range(band_count + 1)
        ]
        self._bands = tuple(
            (low, (1 << (high - low)) - 1)
            for low, high in zip(edges, edges[1:])
        )
        self._tables = [{} for _ in range(band_count)]

    def candidates(self, value: PHashValue) -> Iterable[int]:
        if self._exhaustive is not None:
            return self._exhaustive.candidates(value)

        phash = _phash_value(value)
        if len(self._tables) == 1:
            return _bucket_values(self._tables[0].get(phash))

        ranks: set[int] = set()
        for table, (shift, mask) in zip(self._tables, self._bands):
            ranks.update(_bucket_values(table.get((phash >> shift) & mask)))
        return sorted(ranks)

    def add(self, value: PHashValue, rank: int) -> None:
        if self._exhaustive is not None:
            self._exhaustive.add(value, rank)
            return

        phash = _phash_value(value)
        for table, (shift, mask) in zip(self._tables, self._bands):
            _add_rank(table, (phash >> shift) & mask, rank)


def _phash_value(value: PHashValue) -> int:
    return value if isinstance(value, int) else value.phash


def _bucket_values(bucket: int | list[int] | None) -> tuple[int, ...] | list[int]:
    if bucket is None:
        return ()
    return (bucket,) if isinstance(bucket, int) else bucket


def _add_rank(table: dict[int, int | list[int]], band: int, rank: int) -> None:
    bucket = table.get(band)
    if bucket is None:
        table[band] = rank
    elif isinstance(bucket, int):
        table[band] = [bucket, rank]
    else:
        bucket.append(rank)


def create_image_indexer() -> RecursiveDirectoryIndexer[ImageSignature]:
    """Build the production image-signature indexer."""

    analyzer = PillowImageSignatureAnalyzer()
    return RecursiveDirectoryIndexer(
        analyzer,
        scanner=ImageDirectoryScanner(),
        memory_estimator=analyzer,
        cache=ImageSignatureCache(),
    )


def create_image_duplicate_detector() -> QualityAwareDuplicateDetector[ImageSignature]:
    """Build the production quality-aware image duplicate detector."""

    return QualityAwareDuplicateDetector(
        ImageSignatureDistance(),
        image_quality_key,
        BandedPHashIndex,
        maximum_threshold=PHASH_BITS,
    )


def create_image_deduplicator() -> DirectoryDeduplicator[ImageSignature]:
    """Build the production image deduplication service."""

    return DirectoryDeduplicator(
        create_image_indexer(),
        create_image_duplicate_detector(),
    )


def index_images(
    directory: str | Path,
    *,
    max_workers: int | None = None,
    memory_limit_mb: int | None = None,
    on_progress: ProgressObserver | None = None,
) -> list[ImageSignaturePair]:
    """Recursively hash decodable images in natural path order."""

    return [
        (image.path, image.value)
        for image in create_image_indexer().index(
            Path(directory),
            max_workers=max_workers,
            memory_limit_mb=memory_limit_mb,
            on_progress=on_progress,
        )
    ]


def find_duplicates(
    images: Sequence[tuple[Path, PHashValue]], threshold: int = 0
) -> list[Duplicate]:
    """Choose duplicates while retaining the highest-quality matching image."""

    indexed = [IndexedFile(path, signature) for path, signature in images]
    detector = QualityAwareDuplicateDetector(
        ImageSignatureDistance(),
        image_quality_key,
        BandedPHashIndex,
        maximum_threshold=PHASH_BITS,
    )
    return detector.find(indexed, threshold)


def deduplicate_directory(
    directory: str | Path,
    *,
    threshold: int = 0,
    delete: bool = False,
    max_workers: int | None = None,
    memory_limit_mb: int | None = None,
    on_result: Callable[[Duplicate], None] | None = None,
    on_progress: ProgressObserver | None = None,
) -> list[Duplicate]:
    """Find duplicates recursively and optionally delete lower-quality files."""

    options = DeduplicationOptions(
        threshold=threshold,
        delete=delete,
        max_workers=max_workers,
        memory_limit_mb=memory_limit_mb,
    )
    return create_image_deduplicator().deduplicate(
        Path(directory),
        options,
        on_result=on_result,
        on_progress=on_progress,
    )
