"""Perceptual image hashing and deterministic duplicate removal."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Generic, Protocol, TypeAlias, TypeVar
import warnings

import numpy as np
from numpy.typing import NDArray
from PIL import Image

try:
    from scipy.fft import dctn as _scipy_dctn
except ImportError:  # pragma: no cover - exercised by forcing the fallback
    _scipy_dctn = None

from .abstractions import (
    DirectoryIndexer,
    DistanceMetric,
    FileIdentity,
    ImageDirectoryScanner,
    IndexedFile,
    MeasuredValueT,
    RecursiveDirectoryIndexer,
    file_identity,
    quarantine_if_unchanged,
)
from .image_memory import estimate_peak_bytes


PHASH_SIZE = 32
PHASH_LOW_FREQUENCIES = 8
PHASH_BITS = PHASH_LOW_FREQUENCIES**2
SignatureT = TypeVar("SignatureT")


@dataclass(frozen=True, slots=True)
class Duplicate:
    """A duplicate path and the higher-quality path retained in its place."""

    removed: Path
    kept: Path
    distance: int
    removed_identity: FileIdentity | None = field(default=None, compare=False)

    @property
    def saved_bytes(self) -> int:
        """Return the storage reclaimed when the duplicate is deleted."""

        if self.removed_identity is not None:
            return self.removed_identity.size
        try:
            return self.removed.stat().st_size
        except OSError:
            return 0


DuplicateObserver: TypeAlias = Callable[[Duplicate], None]


@dataclass(frozen=True, slots=True)
class DeduplicationOptions:
    """Policy for matching and optionally removing duplicate files."""

    threshold: int = 0
    delete: bool = False
    max_workers: int | None = None
    memory_limit_mb: int | None = None

    def __post_init__(self) -> None:
        _validate_threshold(self.threshold)
        if self.max_workers is not None and self.max_workers < 1:
            raise ValueError("max_workers must be greater than 0")
        if self.memory_limit_mb is not None and self.memory_limit_mb < 1:
            raise ValueError("memory_limit_mb must be greater than 0")


@dataclass(frozen=True, slots=True)
class ImageSignature:
    """Structural and color fingerprints plus the source pixel dimensions."""

    phash: int
    average_rgb: tuple[int, int, int]
    resolution: tuple[int, int] = (0, 0)


@dataclass(frozen=True, order=True, slots=True)
class ImageQuality:
    """Ranking criteria (resolution, then file size) for quality comparisons."""

    pixels: int
    size: int


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    """Normalized pixels and source metadata produced by one image decode."""

    grayscale: NDArray[np.float64]
    rgb: NDArray[np.uint8]
    resolution: tuple[int, int]


@lru_cache(maxsize=None)
def _dct_matrix(size: int) -> NDArray[np.float64]:
    """Return an orthonormal DCT-II matrix for the NumPy fallback."""

    positions = np.arange(size, dtype=np.float64)
    frequencies = positions[:, np.newaxis]
    matrix = np.cos(np.pi * (positions + 0.5) * frequencies / size)
    matrix[0] *= np.sqrt(1.0 / size)
    matrix[1:] *= np.sqrt(2.0 / size)
    return matrix


def _load_normalized(
    path: Path,
) -> NormalizedImage:
    """Decode the first image frame into normalized grayscale and RGB arrays."""

    # Pillow warns once an image exceeds its decompression-bomb threshold.
    # This application intentionally decodes untrusted directory contents, so
    # turn that warning into a normal unsupported-image error instead of
    # allowing a potentially enormous image to be expanded in memory.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            image.seek(0)
            resolution = image.size
            normalized = image.convert("RGB").resize(
                (PHASH_SIZE, PHASH_SIZE), Image.Resampling.LANCZOS
            )
            rgb = np.asarray(normalized, dtype=np.uint8)
            grayscale = np.asarray(normalized.convert("L"), dtype=np.float64)
            return NormalizedImage(grayscale, rgb, resolution)


def _dct_2d(pixels: NDArray[np.float64]) -> NDArray[np.float64]:
    """Apply an orthonormal 2D DCT, falling back to NumPy without SciPy."""

    if _scipy_dctn is not None:
        # Normalizing the result through NumPy both guarantees the public
        # helper's dtype and works around SciPy's overly broad dispatch type.
        return np.asarray(
            _scipy_dctn(pixels, type=2, norm="ortho"), dtype=np.float64
        )

    height_transform = _dct_matrix(pixels.shape[0])
    width_transform = _dct_matrix(pixels.shape[1])
    return height_transform @ pixels @ width_transform.T


def _phash(pixels: NDArray[np.float64]) -> int:
    coefficients = _dct_2d(pixels)
    low_frequencies = coefficients[
        :PHASH_LOW_FREQUENCIES, :PHASH_LOW_FREQUENCIES
    ].ravel()
    average = float(low_frequencies[1:].mean())

    result = 0
    for value in low_frequencies:
        result = (result << 1) | int(value > average)
    return result


def perceptual_hash(path: str | Path) -> int:
    """Calculate a 64-bit pHash for an image decoded with Pillow."""

    return _phash(_load_normalized(Path(path)).grayscale)


def image_signature(path: str | Path) -> ImageSignature:
    """Calculate structural and color fingerprints from one image decode."""

    normalized = _load_normalized(Path(path))
    grayscale, rgb = normalized.grayscale, normalized.rgb
    channels = rgb.mean(axis=(0, 1))
    average_rgb = (
        int(round(channels[0])),
        int(round(channels[1])),
        int(round(channels[2])),
    )
    return ImageSignature(_phash(grayscale), average_rgb, normalized.resolution)


class PillowImageSignatureAnalyzer:
    """Build image signatures using Pillow, NumPy, and SciPy transforms."""

    def estimate_memory(self, path: Path) -> int:
        """Estimate decode and normalization memory from the image header."""

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                return estimate_peak_bytes(image.size)

    def analyze(self, path: Path) -> ImageSignature:
        return image_signature(path)


def hamming_distance(left: int, right: int) -> int:
    """Return the number of differing bits in two pHashes."""

    return (left ^ right).bit_count()


def _signature_distance(
    left: int | ImageSignature, right: int | ImageSignature
) -> int:
    if isinstance(left, int) and isinstance(right, int):
        return hamming_distance(left, right)
    if not isinstance(left, ImageSignature) or not isinstance(right, ImageSignature):
        raise TypeError("cannot compare a pHash with an image signature")

    structure = hamming_distance(left.phash, right.phash)
    # Map the largest 8-bit channel difference onto the pHash's 0..64 scale.
    color = round(
        max(abs(a - b) for a, b in zip(left.average_rgb, right.average_rgb))
        * PHASH_BITS
        / 255
    )
    return max(structure, color)


def image_quality_key(
    image: IndexedFile[SignatureT],
) -> ImageQuality:
    """Rank an image by resolution, then by its encoded file size.

    The indexer already records the file identity, so using its size avoids a
    second stat call during a directory deduplication.  The input sequence is
    naturally sorted and Python's stable sort consequently makes its last
    path win when both values tie.
    """

    resolution = (
        image.value.resolution if isinstance(image.value, ImageSignature) else (0, 0)
    )
    pixels = resolution[0] * resolution[1]
    size = image.identity.size if image.identity is not None else 0
    if image.identity is None:
        try:
            size = image.path.stat().st_size
        except OSError:
            pass
    return ImageQuality(pixels, size)


class ImageSignatureDistance:
    """Compare legacy pHashes or full structural and color signatures."""

    def distance(
        self,
        left: int | ImageSignature,
        right: int | ImageSignature,
    ) -> int:
        return _signature_distance(left, right)


class DuplicateDetector(ABC, Generic[SignatureT]):
    """Abstract strategy for selecting duplicate indexed files."""

    @abstractmethod
    def find(
        self,
        images: Sequence[IndexedFile[SignatureT]],
        threshold: int = 0,
        *,
        on_duplicate: DuplicateObserver | None = None,
    ) -> list[Duplicate]:
        """Return files considered duplicates under *threshold*."""


class CandidateIndex(Protocol[MeasuredValueT]):
    """Narrow the retained values a new candidate must be compared against."""

    def candidates(self, value: MeasuredValueT) -> Iterable[int]:
        """Return retained-list indexes to test in ascending order."""
        ...

    def add(self, value: MeasuredValueT, rank: int) -> None:
        """Record that *value* was retained at *rank*."""
        ...


class ExhaustiveCandidateIndex:
    """Compare a candidate against every retained value."""

    def __init__(self, threshold: int = 0) -> None:
        self._retained = 0

    def candidates(self, value: object) -> Iterable[int]:
        return range(self._retained)

    def add(self, value: object, rank: int) -> None:
        self._retained += 1


class BandedPHashIndex:
    """Prune comparisons by splitting a pHash into exact-match bands.

    Two 64-bit hashes within *threshold* bits must share at least one of
    ``threshold + 1`` bands. At threshold zero, the whole hash is one band and
    this becomes direct hash grouping. The index is conservative and therefore
    produces the same retained set as an exhaustive scan. A threshold covering
    all 64 bits uses an exhaustive range directly because no pruning is
    possible.
    """

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

        bands = threshold + 1
        edges = [
            (PHASH_BITS * band) // bands for band in range(bands + 1)
        ]
        self._bands = tuple(
            (low, (1 << (high - low)) - 1)
            for low, high in zip(edges, edges[1:])
        )
        self._tables: list[dict[int, int | list[int]]] = [
            {} for _ in range(bands)
        ]

    @staticmethod
    def _phash(value: int | ImageSignature) -> int:
        return value.phash if isinstance(value, ImageSignature) else value

    def candidates(self, value: int | ImageSignature) -> Iterable[int]:
        if self._exhaustive is not None:
            return self._exhaustive.candidates(value)

        phash = self._phash(value)
        if len(self._tables) == 1:
            bucket = self._tables[0].get(phash)
            if bucket is None:
                return ()
            if isinstance(bucket, int):
                return (bucket,)
            return bucket

        first: int | list[int] | None = None
        ranks: set[int] | None = None
        for table, (shift, mask) in zip(self._tables, self._bands):
            band = (phash >> shift) & mask
            bucket = table.get(band)
            if bucket is None:
                continue
            if first is None:
                first = bucket
                continue
            if ranks is None:
                ranks = {first} if isinstance(first, int) else set(first)
            if isinstance(bucket, int):
                ranks.add(bucket)
            else:
                ranks.update(bucket)

        if first is None:
            return ()
        if ranks is None:
            return (first,) if isinstance(first, int) else first
        return sorted(ranks)

    def add(self, value: int | ImageSignature, rank: int) -> None:
        if self._exhaustive is not None:
            self._exhaustive.add(value, rank)
            return

        phash = self._phash(value)
        if len(self._tables) == 1:
            self._add_rank(self._tables[0], phash, rank)
            return

        for table, (shift, mask) in zip(self._tables, self._bands):
            self._add_rank(table, (phash >> shift) & mask, rank)

    @staticmethod
    def _add_rank(
        table: dict[int, int | list[int]], band: int, rank: int
    ) -> None:
        bucket = table.get(band)
        if bucket is None:
            table[band] = rank
        elif isinstance(bucket, int):
            table[band] = [bucket, rank]
        else:
            bucket.append(rank)


class QualityAwareDuplicateDetector(DuplicateDetector[SignatureT]):
    """Keep the best match while avoiding non-transitive deletion chains.

    Candidates are ranked from lowest to highest quality. Python's stable sort
    preserves path order when quality ties, so the last input path is retained
    as the final tie-breaker.
    """

    def __init__(
        self,
        metric: DistanceMetric[SignatureT],
        quality_key: Callable[[IndexedFile[SignatureT]], ImageQuality] | None = None,
        index_factory: Callable[[int], CandidateIndex[SignatureT]] | None = None,
    ) -> None:
        self._metric = metric
        self._quality_key = quality_key or (lambda _: ImageQuality(0, 0))
        self._index_factory = index_factory or ExhaustiveCandidateIndex

    def find(
        self,
        images: Sequence[IndexedFile[SignatureT]],
        threshold: int = 0,
        *,
        on_duplicate: DuplicateObserver | None = None,
    ) -> list[Duplicate]:
        _validate_threshold(threshold)

        ranked_images = sorted(images, key=self._quality_key)
        index = self._index_factory(threshold)
        kept: list[IndexedFile[SignatureT]] = []
        duplicates: list[Duplicate] = []
        for image in reversed(ranked_images):
            match: tuple[Path, int] | None = None
            for rank in index.candidates(image.value):
                retained = kept[rank]
                distance = self._metric.distance(image.value, retained.value)
                if distance <= threshold:
                    match = (retained.path, distance)
                    break

            if match is None:
                index.add(image.value, len(kept))
                kept.append(image)
            else:
                duplicate = Duplicate(
                    image.path, match[0], match[1], image.identity
                )
                duplicates.append(duplicate)
                if on_duplicate is not None:
                    on_duplicate(duplicate)

        if not duplicates:
            return duplicates

        removed_paths = {duplicate.removed for duplicate in duplicates}
        positions: dict[Path, int] = {}
        for position, image in enumerate(images):
            if image.path in removed_paths:
                positions.setdefault(image.path, position)
                if len(positions) == len(removed_paths):
                    break
        duplicates.sort(key=lambda duplicate: positions[duplicate.removed])
        return duplicates


class FileRemover(Protocol):
    """Remove a file selected by a deduplication policy."""

    def remove(self, path: Path, expected: FileIdentity | None = None) -> None:
        """Remove *path* from storage."""
        ...


class LocalFileRemover:
    """Remove files from the local filesystem."""

    def remove(self, path: Path, expected: FileIdentity | None = None) -> None:
        if expected is None:
            path.unlink()
            return
        try:
            quarantine = quarantine_if_unchanged(path, expected)
        except OSError as error:
            raise FileChangedError(str(error)) from error
        quarantine.unlink()
        quarantine.parent.rmdir()


class FileChangedError(OSError):
    """A destructive action was refused because its input became stale."""


class DirectoryDeduplicator(Generic[SignatureT]):
    """Coordinate indexing, duplicate detection, and optional removal."""

    def __init__(
        self,
        indexer: DirectoryIndexer[SignatureT],
        detector: DuplicateDetector[SignatureT],
        *,
        remover: FileRemover | None = None,
    ) -> None:
        self._indexer = indexer
        self._detector = detector
        self._remover = remover or LocalFileRemover()

    def deduplicate(
        self,
        directory: Path,
        options: DeduplicationOptions | None = None,
        *,
        on_result: DuplicateObserver | None = None,
    ) -> list[Duplicate]:
        request = options or DeduplicationOptions()
        images = self._indexer.index(
            directory,
            max_workers=request.max_workers,
            memory_limit_mb=request.memory_limit_mb,
        )
        reported: dict[Path, Duplicate] = {}

        def handle(duplicate: Duplicate) -> None:
            # Indexers normally provide the identity, but retaining it here
            # also makes totals correct for lightweight injected indexers.
            if duplicate.removed_identity is None:
                try:
                    duplicate = Duplicate(
                        duplicate.removed,
                        duplicate.kept,
                        duplicate.distance,
                        file_identity(duplicate.removed),
                    )
                except OSError:
                    pass
            reported[duplicate.removed] = duplicate
            if request.delete:
                self._remover.remove(duplicate.removed, duplicate.removed_identity)
            if on_result is not None:
                on_result(duplicate)

        if request.delete or on_result is not None:
            duplicates = self._detector.find(
                images,
                request.threshold,
                on_duplicate=handle,
            )
        else:
            # Preserve compatibility with injected detectors that implement
            # the original two-argument protocol.
            duplicates = self._detector.find(images, request.threshold)
        return [reported.get(duplicate.removed, duplicate) for duplicate in duplicates]


class ImageIndexAdapter(DirectoryIndexer[int | ImageSignature]):
    """Expose the tuple-based image index API through the model contract."""

    def index(
        self,
        directory: Path,
        *,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
    ) -> list[IndexedFile[int | ImageSignature]]:
        analyzer = PillowImageSignatureAnalyzer()
        indexer = RecursiveDirectoryIndexer[int | ImageSignature](
            analyzer,
            scanner=ImageDirectoryScanner(),
            memory_estimator=analyzer,
        )
        return indexer.index(
            directory,
            max_workers=max_workers,
            memory_limit_mb=memory_limit_mb,
        )


def _validate_threshold(threshold: int) -> None:
    if not 0 <= threshold <= PHASH_BITS:
        raise ValueError(f"threshold must be between 0 and {PHASH_BITS}")


def index_images(
    directory: str | Path,
    *,
    max_workers: int | None = None,
    memory_limit_mb: int | None = None,
) -> list[tuple[Path, ImageSignature]]:
    """Recursively hash decodable images in natural path order."""

    analyzer = PillowImageSignatureAnalyzer()
    indexer = RecursiveDirectoryIndexer(
        analyzer,
        scanner=ImageDirectoryScanner(),
        memory_estimator=analyzer,
    )
    return [
        (image.path, image.value)
        for image in indexer.index(
            Path(directory),
            max_workers=max_workers,
            memory_limit_mb=memory_limit_mb,
        )
    ]


def find_duplicates(
    images: Sequence[tuple[Path, int | ImageSignature]], threshold: int = 0
) -> list[Duplicate]:
    """Choose duplicates while retaining the highest-quality matching image.

    Images are ranked by quality and each candidate is compared only with
    higher-quality images that will actually be retained. The last naturally
    sorted path wins if resolution and file size both tie. This avoids
    deleting through a non-transitive chain where A matches B and B matches C,
    but A does not match C.
    """

    indexed = [IndexedFile(path, signature) for path, signature in images]
    detector = QualityAwareDuplicateDetector(
        ImageSignatureDistance(), image_quality_key, BandedPHashIndex
    )
    return detector.find(indexed, threshold)


def deduplicate_directory(
    directory: str | Path,
    *,
    threshold: int = 0,
    delete: bool = False,
    max_workers: int | None = None,
    memory_limit_mb: int | None = None,
    on_result: DuplicateObserver | None = None,
) -> list[Duplicate]:
    """Find duplicates recursively and optionally delete earlier paths."""

    model = DirectoryDeduplicator(
        ImageIndexAdapter(),
        QualityAwareDuplicateDetector(
            ImageSignatureDistance(), image_quality_key, BandedPHashIndex
        ),
    )
    options = DeduplicationOptions(
        threshold=threshold,
        delete=delete,
        max_workers=max_workers,
        memory_limit_mb=memory_limit_mb,
    )
    return model.deduplicate(Path(directory), options, on_result=on_result)
