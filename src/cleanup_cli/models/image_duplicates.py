"""Perceptual image hashing and deterministic duplicate removal."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Generic, Protocol, TypeVar

import av
import numpy as np
from numpy.typing import NDArray

from .abstractions import (
    DirectoryIndexer,
    DistanceMetric,
    FileIdentity,
    ImageDirectoryScanner,
    IndexedFile,
    RecursiveDirectoryIndexer,
    file_identity,
    quarantine_if_unchanged,
)


PHASH_SIZE = 32
PHASH_LOW_FREQUENCIES = 8
PHASH_BITS = PHASH_LOW_FREQUENCIES**2
SignatureT = TypeVar("SignatureT")


@dataclass(frozen=True)
class Duplicate:
    """A duplicate path and the later sorted path retained in its place."""

    removed: Path
    kept: Path
    distance: int
    removed_identity: FileIdentity | None = field(default=None, compare=False)


@dataclass(frozen=True)
class DeduplicationOptions:
    """Policy for matching and optionally removing duplicate files."""

    threshold: int = 0
    delete: bool = False

    def __post_init__(self) -> None:
        _validate_threshold(self.threshold)


@dataclass(frozen=True)
class ImageSignature:
    """Structural pHash plus average normalized RGB color."""

    phash: int
    average_rgb: tuple[int, int, int]


@lru_cache(maxsize=None)
def _dct_matrix(size: int) -> NDArray[np.float64]:
    """Return an orthonormal DCT-II transform matrix."""

    positions = np.arange(size, dtype=np.float64)
    frequencies = positions[:, np.newaxis]
    matrix = np.cos(np.pi * (positions + 0.5) * frequencies / size)
    matrix[0] *= np.sqrt(1.0 / size)
    matrix[1:] *= np.sqrt(2.0 / size)
    return matrix

@lru_cache(maxsize=None)
def _load_normalized(path: Path) -> tuple[NDArray[np.float64], NDArray[np.uint8]]:
    """Decode the first image frame into normalized grayscale and RGB arrays."""

    with av.open(str(path)) as container:
        frame = next(container.decode(video=0))
        grayscale = frame.reformat(
            width=PHASH_SIZE,
            height=PHASH_SIZE,
            format="gray",
        )
        rgb = frame.reformat(width=PHASH_SIZE, height=PHASH_SIZE, format="rgb24")
        return (
            grayscale.to_ndarray().astype(np.float64, copy=False),
            np.asarray(rgb.to_ndarray(), dtype=np.uint8),
        )


def _phash(pixels: NDArray[np.float64]) -> int:
    transform = _dct_matrix(PHASH_SIZE)
    coefficients = transform @ pixels @ transform.T
    low_frequencies = coefficients[
        :PHASH_LOW_FREQUENCIES, :PHASH_LOW_FREQUENCIES
    ].ravel()
    average = float(low_frequencies[1:].mean())

    result = 0
    for value in low_frequencies:
        result = (result << 1) | int(value > average)
    return result


def perceptual_hash(path: str | Path) -> int:
    """Calculate a 64-bit pHash for an image decoded with PyAV."""

    grayscale, _ = _load_normalized(Path(path))
    return _phash(grayscale)


def image_signature(path: str | Path) -> ImageSignature:
    """Calculate structural and color fingerprints from one image decode."""

    grayscale, rgb = _load_normalized(Path(path))
    channels = rgb.mean(axis=(0, 1))
    average_rgb = (
        int(round(channels[0])),
        int(round(channels[1])),
        int(round(channels[2])),
    )
    return ImageSignature(_phash(grayscale), average_rgb)


class PyAVImageSignatureAnalyzer:
    """Build image signatures using PyAV decoding and NumPy transforms."""

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
    ) -> list[Duplicate]:
        """Return files considered duplicates under *threshold*."""


class ReverseDuplicateDetector(DuplicateDetector[SignatureT]):
    """Keep the last match while avoiding non-transitive deletion chains."""

    def __init__(self, metric: DistanceMetric[SignatureT]) -> None:
        self._metric = metric

    def find(
        self,
        images: Sequence[IndexedFile[SignatureT]],
        threshold: int = 0,
    ) -> list[Duplicate]:
        _validate_threshold(threshold)

        kept: list[IndexedFile[SignatureT]] = []
        duplicates: list[Duplicate] = []
        for image in reversed(images):
            match: tuple[Path, int] | None = None
            for retained in kept:
                distance = self._metric.distance(image.value, retained.value)
                if distance <= threshold:
                    match = (retained.path, distance)
                    break

            if match is None:
                kept.append(image)
            else:
                duplicates.append(
                    Duplicate(image.path, match[0], match[1], image.identity)
                )

        duplicates.reverse()
        return duplicates


class FileRemover(Protocol):
    """Remove a file selected by a deduplication policy."""

    def remove(self, path: Path) -> None:
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
    ) -> list[Duplicate]:
        request = options or DeduplicationOptions()
        images = self._indexer.index(directory)
        duplicates = self._detector.find(images, request.threshold)

        if request.delete:
            for duplicate in duplicates:
                if isinstance(self._remover, LocalFileRemover):
                    self._remover.remove(
                        duplicate.removed, duplicate.removed_identity
                    )
                else:
                    self._remover.remove(duplicate.removed)
        return duplicates


class ImageIndexAdapter(DirectoryIndexer[int | ImageSignature]):
    """Expose the tuple-based image index API through the model contract."""

    def index(self, directory: Path) -> list[IndexedFile[int | ImageSignature]]:
        indexer = RecursiveDirectoryIndexer[int | ImageSignature](
            PyAVImageSignatureAnalyzer(),
            scanner=ImageDirectoryScanner(),
        )
        return indexer.index(directory)


def _validate_threshold(threshold: int) -> None:
    if not 0 <= threshold <= PHASH_BITS:
        raise ValueError(f"threshold must be between 0 and {PHASH_BITS}")


def index_images(directory: str | Path) -> list[tuple[Path, ImageSignature]]:
    """Recursively hash decodable images in natural path order."""

    indexer = RecursiveDirectoryIndexer(
        PyAVImageSignatureAnalyzer(),
        scanner=ImageDirectoryScanner(),
    )
    return [(image.path, image.value) for image in indexer.index(Path(directory))]


def find_duplicates(
    images: Sequence[tuple[Path, int | ImageSignature]], threshold: int = 0
) -> list[Duplicate]:
    """Choose duplicates while retaining the last naturally sorted match.

    The reverse scan compares each image only with later paths that will
    actually be retained. This avoids deleting through a non-transitive chain
    where A matches B and B matches C, but A does not match C.
    """

    indexed = [IndexedFile(path, signature) for path, signature in images]
    detector = ReverseDuplicateDetector(ImageSignatureDistance())
    return detector.find(indexed, threshold)


def deduplicate_directory(
    directory: str | Path, *, threshold: int = 0, delete: bool = False
) -> list[Duplicate]:
    """Find duplicates recursively and optionally delete earlier paths."""

    model = DirectoryDeduplicator(
        ImageIndexAdapter(),
        ReverseDuplicateDetector(ImageSignatureDistance()),
    )
    options = DeduplicationOptions(threshold=threshold, delete=delete)
    return model.deduplicate(Path(directory), options)
