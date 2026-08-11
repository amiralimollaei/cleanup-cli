"""Image decoding and perceptual signature calculation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TypeAlias
import warnings

import numpy as np
from numpy.typing import NDArray
from PIL import Image

try:
    from scipy.fft import dctn as _scipy_dctn
except ImportError:  # pragma: no cover - exercised by forcing the fallback
    _scipy_dctn = None

from cleanup_cli.models.image.memory import estimate_peak_bytes


PHASH_SIZE = 64
PHASH_LOW_FREQUENCIES = 16
PHASH_BITS = PHASH_LOW_FREQUENCIES**2


@dataclass(frozen=True, slots=True)
class ImageSignature:
    """Structural and color fingerprints plus the source pixel dimensions."""

    phash: int
    average_rgb: tuple[int, int, int]
    resolution: tuple[int, int] = (0, 0)


PHashValue: TypeAlias = int | ImageSignature


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


def _load_normalized(path: Path) -> NormalizedImage:
    """Decode the first image frame into normalized grayscale and RGB arrays."""

    # Directory contents are untrusted. Treat Pillow's decompression warning as
    # a normal unsupported-input error before allocating a huge raster.
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
    """Calculate a 256-bit pHash for an image decoded with Pillow."""

    return _phash(_load_normalized(Path(path)).grayscale)


def image_signature(path: str | Path) -> ImageSignature:
    """Calculate structural and color fingerprints from one image decode."""

    normalized = _load_normalized(Path(path))
    channels = normalized.rgb.mean(axis=(0, 1))
    average_rgb = tuple(int(round(channel)) for channel in channels)
    return ImageSignature(
        _phash(normalized.grayscale),
        (average_rgb[0], average_rgb[1], average_rgb[2]),
        normalized.resolution,
    )


class PillowImageSignatureAnalyzer:
    """Build image signatures using Pillow and a DCT implementation."""

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


class ImageSignatureDistance:
    """Compare raw pHashes or structural and color image signatures."""

    def distance(self, left: PHashValue, right: PHashValue) -> int:
        if isinstance(left, int) and isinstance(right, int):
            return hamming_distance(left, right)
        if not isinstance(left, ImageSignature) or not isinstance(
            right, ImageSignature
        ):
            raise TypeError("cannot compare a pHash with an image signature")

        structure = hamming_distance(left.phash, right.phash)
        color = round(
            max(
                abs(left_channel - right_channel)
                for left_channel, right_channel in zip(
                    left.average_rgb, right.average_rgb
                )
            )
            * PHASH_BITS
            / 255
        )
        return max(structure, color)
