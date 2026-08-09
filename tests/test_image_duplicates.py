from pathlib import Path
import warnings

import numpy as np
import pytest
from PIL import Image

from cleanup_cli import (
    deduplicate_directory,
    find_duplicates,
    hamming_distance,
    image_signature,
    index_images,
    perceptual_hash,
)
from cleanup_cli.models import image_duplicates
from cleanup_cli.models.abstractions import IndexedFile, file_identity
from cleanup_cli.models.image_duplicates import Duplicate


def _write_pgm(path: Path, pixels: np.ndarray) -> None:
    height, width = pixels.shape
    path.write_bytes(f"P5\n{width} {height}\n255\n".encode() + pixels.astype(np.uint8).tobytes())


def _write_ppm(path: Path, pixels: np.ndarray) -> None:
    height, width, _ = pixels.shape
    header = f"P6\n{width} {height}\n255\n".encode()
    path.write_bytes(header + pixels.astype(np.uint8).tobytes())


def _pattern(size: int) -> np.ndarray:
    y, x = np.mgrid[:size, :size]
    return ((x * 7 + y * 3 + (x > y) * 80) % 256).astype(np.uint8)


def test_perceptual_hash_is_stable_for_same_decoded_pixels(tmp_path: Path) -> None:
    first = tmp_path / "first.pgm"
    second = tmp_path / "second.pgm"
    pixels = _pattern(32)
    _write_pgm(first, pixels)
    _write_pgm(second, pixels)

    assert perceptual_hash(first) == perceptual_hash(second)


def test_numpy_dct_fallback_matches_scipy(monkeypatch: pytest.MonkeyPatch) -> None:
    pixels = _pattern(32).astype(np.float64)
    assert image_duplicates._scipy_dctn is not None
    scipy_coefficients = image_duplicates._dct_2d(pixels)
    scipy_hash = image_duplicates._phash(pixels)

    monkeypatch.setattr(image_duplicates, "_scipy_dctn", None)
    numpy_coefficients = image_duplicates._dct_2d(pixels)

    np.testing.assert_allclose(numpy_coefficients, scipy_coefficients, atol=1e-10)
    assert image_duplicates._phash(pixels) == scipy_hash


def test_perceptual_hash_survives_resizing(tmp_path: Path) -> None:
    small = tmp_path / "small.pgm"
    large = tmp_path / "large.pgm"
    pixels = _pattern(32)
    _write_pgm(small, pixels)
    _write_pgm(large, np.repeat(np.repeat(pixels, 2, axis=0), 2, axis=1))

    assert hamming_distance(perceptual_hash(small), perceptual_hash(large)) <= 4
    duplicates = find_duplicates(
        [(small, image_signature(small)), (large, image_signature(large))],
        threshold=4,
    )
    assert len(duplicates) == 1
    assert duplicates[0].removed == small
    assert duplicates[0].kept == large
    assert duplicates[0].distance <= 4


def test_signature_rejects_color_shift_at_strict_threshold(tmp_path: Path) -> None:
    base_path = tmp_path / "base.ppm"
    shifted_path = tmp_path / "shifted.ppm"
    y, x = np.mgrid[:64, :64]
    base = np.stack(
        ((x * 5 + y * 2) % 256, (x * 2 + y * 7) % 256, (x * 3) % 256),
        axis=-1,
    ).astype(np.uint8)
    shifted = base.copy()
    shifted[..., 0] = np.clip(shifted[..., 0].astype(int) + 70, 0, 255)
    _write_ppm(base_path, base)
    _write_ppm(shifted_path, shifted)

    images = [(base_path, image_signature(base_path)), (shifted_path, image_signature(shifted_path))]

    assert find_duplicates(images, threshold=0) == []


def test_signature_rejects_local_edit_at_strict_threshold(tmp_path: Path) -> None:
    base_path = tmp_path / "base.pgm"
    edited_path = tmp_path / "edited.pgm"
    base = _pattern(64)
    edited = base.copy()
    edited[20:32, 20:32] = 255 - edited[20:32, 20:32]
    _write_pgm(base_path, base)
    _write_pgm(edited_path, edited)

    images = [(base_path, image_signature(base_path)), (edited_path, image_signature(edited_path))]

    assert find_duplicates(images, threshold=0) == []


def test_hamming_distance_counts_differing_bits() -> None:
    assert hamming_distance(0b1010, 0b0011) == 2


def test_threshold_controls_duplicate_matching() -> None:
    images = [(Path("1.png"), 0b0000), (Path("2.png"), 0b0011)]

    assert find_duplicates(images, threshold=1) == []
    assert find_duplicates(images, threshold=2) == [
        Duplicate(Path("1.png"), Path("2.png"), 2)
    ]


@pytest.mark.parametrize("threshold", [-1, 65])
def test_rejects_invalid_threshold(threshold: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 64"):
        find_duplicates([], threshold)


def test_rejects_invalid_threshold_before_indexing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_index(_: Path) -> list[tuple[Path, int]]:
        raise AssertionError("directory should not be indexed")

    monkeypatch.setattr(
        "cleanup_cli.models.image_duplicates.index_images", unexpected_index
    )

    with pytest.raises(ValueError, match="between 0 and 64"):
        deduplicate_directory(tmp_path, threshold=65)


def test_keeps_last_sorted_image_without_transitive_matching() -> None:
    images = [
        (Path("1.png"), 0b000),
        (Path("2.png"), 0b001),
        (Path("3.png"), 0b011),
    ]

    assert find_duplicates(images, threshold=1) == [
        Duplicate(Path("2.png"), Path("3.png"), 1)
    ]


def test_quality_selection_prefers_resolution_then_size_then_last_path(
    tmp_path: Path,
) -> None:
    low_path = tmp_path / "1-low.png"
    large_path = tmp_path / "2-large.png"
    high_path = tmp_path / "3-high.png"
    equal_first = tmp_path / "4-same.png"
    equal_last = tmp_path / "5-same.png"
    for path, size in (
        (low_path, 10),
        (large_path, 100),
        (high_path, 20),
        (equal_first, 50),
        (equal_last, 50),
    ):
        path.write_bytes(b"x" * size)

    low = image_duplicates.ImageSignature(0, (0, 0, 0), (10, 10))
    high = image_duplicates.ImageSignature(0, (0, 0, 0), (20, 20))
    equal_resolution = image_duplicates.ImageSignature(0, (0, 0, 0), (30, 30))

    assert find_duplicates([(low_path, low), (high_path, high)]) == [
        Duplicate(low_path, high_path, 0)
    ]
    assert find_duplicates([(low_path, low), (large_path, low)]) == [
        Duplicate(low_path, large_path, 0)
    ]
    assert find_duplicates(
        [(equal_first, equal_resolution), (equal_last, equal_resolution)]
    ) == [Duplicate(equal_first, equal_last, 0)]


def test_indexes_recursively_in_natural_order_and_skips_non_images(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "album-2"
    directory.mkdir()
    later = directory / "photo-10.pgm"
    earlier = directory / "photo-2.pgm"
    _write_pgm(later, _pattern(32))
    _write_pgm(earlier, _pattern(32))
    (tmp_path / "notes.txt").write_text("not an image")

    assert [path for path, _ in index_images(tmp_path)] == [earlier, later]


def test_oversized_images_are_skipped_without_decompression_bomb_warning(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.pgm"
    # The header is sufficient for Pillow to detect the pixel count. No pixel
    # payload is needed because the file must be rejected before decoding.
    oversized.write_bytes(b"P5\n20001 10000\n255\n")

    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        assert index_images(tmp_path) == []

    assert not any(
        warning.category is Image.DecompressionBombWarning for warning in emitted
    )


def test_dry_run_keeps_files_and_delete_removes_only_earlier_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "photo-1.pgm"
    last = tmp_path / "photo-2.pgm"
    first.touch()
    last.touch()
    indexed = [
        IndexedFile(first, 123, file_identity(first)),
        IndexedFile(last, 123, file_identity(last)),
    ]
    monkeypatch.setattr(
        "cleanup_cli.models.image_duplicates.ImageIndexAdapter.index",
        lambda self, directory: indexed,
    )

    assert deduplicate_directory(tmp_path) == [Duplicate(first, last, 0)]
    assert first.exists() and last.exists()

    deduplicate_directory(tmp_path, delete=True)
    assert not first.exists()
    assert last.exists()