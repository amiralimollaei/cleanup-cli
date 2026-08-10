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
from cleanup_cli.models.abstractions import (
    DirectoryIndexer,
    IndexedFile,
    file_identity,
)
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


@pytest.mark.parametrize("threshold", [0, 1, 4, 16, 63, 64])
def test_banded_index_matches_exhaustive_detection(threshold: int) -> None:
    rng = np.random.default_rng(12345)
    values = [int(value) for value in rng.integers(0, 2**64, size=200, dtype=np.uint64)]
    # Include exact and near duplicates alongside mostly unique hashes.
    values.extend((values[10], values[20] ^ 0b1111, values[30] ^ ((1 << 63) - 1)))
    images: list[IndexedFile[int | image_duplicates.ImageSignature]] = [
        IndexedFile(Path(f"{position}.png"), value)
        for position, value in enumerate(values)
    ]
    metric = image_duplicates.ImageSignatureDistance()

    exhaustive = image_duplicates.QualityAwareDuplicateDetector(metric)
    banded = image_duplicates.QualityAwareDuplicateDetector(
        metric, index_factory=image_duplicates.BandedPHashIndex
    )

    assert banded.find(images, threshold) == exhaustive.find(images, threshold)


def test_banded_index_preserves_full_signature_color_matching() -> None:
    signatures = [
        image_duplicates.ImageSignature(0, (0, 0, 0)),
        image_duplicates.ImageSignature(0, (1, 1, 1)),
        image_duplicates.ImageSignature(1, (200, 200, 200)),
        image_duplicates.ImageSignature(3, (201, 201, 201)),
    ]
    images: list[IndexedFile[int | image_duplicates.ImageSignature]] = [
        IndexedFile(Path(f"{position}.png"), signature)
        for position, signature in enumerate(signatures)
    ]
    metric = image_duplicates.ImageSignatureDistance()
    exhaustive = image_duplicates.QualityAwareDuplicateDetector(metric)
    banded = image_duplicates.QualityAwareDuplicateDetector(
        metric, index_factory=image_duplicates.BandedPHashIndex
    )

    assert banded.find(images, 2) == exhaustive.find(images, 2)


def test_zero_threshold_avoids_comparing_unique_hashes() -> None:
    class CountingDistance(image_duplicates.ImageSignatureDistance):
        def __init__(self) -> None:
            self.calls = 0

        def distance(
            self,
            left: int | image_duplicates.ImageSignature,
            right: int | image_duplicates.ImageSignature,
        ) -> int:
            self.calls += 1
            return super().distance(left, right)

    metric = CountingDistance()
    detector = image_duplicates.QualityAwareDuplicateDetector(
        metric, index_factory=image_duplicates.BandedPHashIndex
    )
    images: list[IndexedFile[int | image_duplicates.ImageSignature]] = [
        IndexedFile(Path(f"{value}.png"), value) for value in range(1_000)
    ]

    assert detector.find(images, 0) == []
    assert metric.calls == 0


def test_banded_index_promotes_only_colliding_buckets_to_lists() -> None:
    index = image_duplicates.BandedPHashIndex(threshold=0)
    for rank in range(1_000):
        index.add(rank, rank)

    assert all(
        isinstance(bucket, int) for bucket in index._tables[0].values()
    )

    index.add(500, 1_000)

    assert list(index.candidates(500)) == [500, 1_000]
    assert isinstance(index._tables[0][500], list)


def test_low_threshold_limits_comparisons_for_mostly_unique_hashes() -> None:
    class CountingDistance(image_duplicates.ImageSignatureDistance):
        def __init__(self) -> None:
            self.calls = 0

        def distance(
            self,
            left: int | image_duplicates.ImageSignature,
            right: int | image_duplicates.ImageSignature,
        ) -> int:
            self.calls += 1
            return super().distance(left, right)

    rng = np.random.default_rng(54321)
    values = rng.integers(0, 2**64, size=5_000, dtype=np.uint64)
    images: list[IndexedFile[int | image_duplicates.ImageSignature]] = [
        IndexedFile(Path(f"{rank}.png"), int(value))
        for rank, value in enumerate(values)
    ]
    metric = CountingDistance()
    detector = image_duplicates.QualityAwareDuplicateDetector(
        metric, index_factory=image_duplicates.BandedPHashIndex
    )

    assert detector.find(images, threshold=4) == []
    assert metric.calls < 50_000


def test_maximum_threshold_uses_exhaustive_candidate_range() -> None:
    index = image_duplicates.BandedPHashIndex(threshold=64)
    for rank in range(10):
        index.add(rank, rank)

    assert list(index.candidates(123)) == list(range(10))
    assert index._tables == []


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


def test_equal_resolution_png_is_kept_over_smaller_webp(tmp_path: Path) -> None:
    png_path = tmp_path / "original.png"
    webp_path = tmp_path / "converted.webp"
    pixels = np.stack((_pattern(64), np.rot90(_pattern(64)), _pattern(64)), axis=-1)
    image = Image.fromarray(pixels)
    image.save(png_path, compress_level=0)
    image.save(webp_path, lossless=True)

    assert png_path.stat().st_size > webp_path.stat().st_size

    duplicates = deduplicate_directory(tmp_path)

    assert [(duplicate.removed, duplicate.kept) for duplicate in duplicates] == [
        (webp_path, png_path)
    ]


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


def test_signature_memory_estimate_reads_only_image_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "photo.pgm"
    _write_pgm(source, _pattern(32))

    def fail_if_loaded(image: Image.Image) -> None:
        raise AssertionError("memory estimation must not decode pixels")

    monkeypatch.setattr(Image.Image, "load", fail_if_loaded)

    estimate = image_duplicates.PillowImageSignatureAnalyzer().estimate_memory(source)

    assert estimate > 32 * 32 * 4


@pytest.mark.parametrize("memory_limit_mb", [0, -1])
def test_rejects_invalid_deduplication_memory_limit(
    tmp_path: Path, memory_limit_mb: int
) -> None:
    with pytest.raises(ValueError, match="memory_limit_mb must be greater than 0"):
        deduplicate_directory(tmp_path, memory_limit_mb=memory_limit_mb)


def test_dry_run_keeps_files_and_delete_removes_only_earlier_match(
    tmp_path: Path,
) -> None:
    first = tmp_path / "photo-1.pgm"
    last = tmp_path / "photo-2.pgm"
    first.touch()
    last.touch()
    indexed = [
        IndexedFile(first, 123, file_identity(first)),
        IndexedFile(last, 123, file_identity(last)),
    ]

    class StaticIndexer(DirectoryIndexer[int]):
        def __init__(self, images: list[IndexedFile[int]]) -> None:
            self._images = images

        def index(
            self,
            directory: Path,
            *,
            max_workers: int | None = None,
            memory_limit_mb: int | None = None,
        ) -> list[IndexedFile[int]]:
            return self._images

    service = image_duplicates.DirectoryDeduplicator(
        StaticIndexer(indexed),
        image_duplicates.QualityAwareDuplicateDetector(
            image_duplicates.ImageSignatureDistance()
        ),
    )

    assert service.deduplicate(tmp_path) == [Duplicate(first, last, 0)]
    assert first.exists() and last.exists()

    service.deduplicate(
        tmp_path,
        image_duplicates.DeduplicationOptions(delete=True),
    )
    assert not first.exists()
    assert last.exists()


def test_reports_duplicate_after_successful_delete_and_totals_saved_bytes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "photo-1.pgm"
    last = tmp_path / "photo-2.pgm"
    first.write_bytes(b"duplicate-data")
    last.write_bytes(b"kept-data")
    indexed = [
        IndexedFile(first, 123, file_identity(first)),
        IndexedFile(last, 123, file_identity(last)),
    ]
    reported: list[Duplicate] = []

    class StaticIndexer(DirectoryIndexer[int]):
        def index(
            self,
            directory: Path,
            *,
            max_workers: int | None = None,
            memory_limit_mb: int | None = None,
        ) -> list[IndexedFile[int]]:
            return indexed

    from cleanup_cli.models.image_duplicates import (
        DirectoryDeduplicator,
        LocalFileRemover,
        QualityAwareDuplicateDetector,
    )

    service = DirectoryDeduplicator(
        StaticIndexer(),
        QualityAwareDuplicateDetector(image_duplicates.ImageSignatureDistance()),
        remover=LocalFileRemover(),
    )
    result = service.deduplicate(
        tmp_path,
        image_duplicates.DeduplicationOptions(delete=True),
        on_result=reported.append,
    )

    assert not first.exists()
    assert reported == result
    assert result[0].saved_bytes == len(b"duplicate-data")