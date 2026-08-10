from pathlib import Path
from threading import Condition, Lock
from time import sleep

import numpy as np
import pytest
from PIL import Image

from cleanup_cli.models import image_webp
from cleanup_cli import convert_directory_to_webp
from cleanup_cli.models.image_webp import (
    DecodedImage,
    ImageInspection,
    PillowWebPCodec,
    WebPCodec,
    WebPDirectoryConverter,
    WebPOptions,
)


def _write_ppm(path: Path, size: tuple[int, int] = (128, 96)) -> None:
    width, height = size
    y, x = np.mgrid[:height, :width]
    pixels = np.stack(
        ((x * 7 + y * 3) % 256, (x * 2 + y * 9) % 256, (x * 5) % 256),
        axis=-1,
    ).astype(np.uint8)
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels.tobytes())


def _write_webp(path: Path, size: tuple[int, int] = (32, 24)) -> None:
    width, height = size
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., 0] = 200
    pixels[..., 3] = 255
    Image.fromarray(pixels, mode="RGBA").save(path, format="WEBP")


def test_converts_recursively_in_place_without_changing_dimensions(tmp_path: Path) -> None:
    nested = tmp_path / "album"
    nested.mkdir()
    source = nested / "photo.ppm"
    _write_ppm(source)
    original_size = source.stat().st_size

    result = convert_directory_to_webp(tmp_path, replace=True)
    conversions = result.conversions
    skips = result.skips

    destination = nested / "photo.webp"
    assert skips == ()
    assert len(conversions) == 1
    assert not source.exists()
    assert destination.stat().st_size < original_size
    with Image.open(destination) as image:
        assert image.format == "WEBP"
        assert image.size == (128, 96)


def test_skips_existing_webp_and_non_images(tmp_path: Path) -> None:
    webp = tmp_path / "existing.webp"
    _write_webp(webp)
    contents = webp.read_bytes()
    (tmp_path / "notes.txt").write_text("not an image")

    result = convert_directory_to_webp(tmp_path)
    assert result.conversions == ()
    assert result.skips == ()
    assert webp.read_bytes() == contents


def test_skips_webp_by_path_before_decoding(tmp_path: Path) -> None:
    class FailingCodec(WebPCodec[object]):
        def inspect(self, path: Path) -> ImageInspection:
            raise AssertionError("existing WebP should not be inspected")

        def decode(self, path: Path) -> DecodedImage[object]:
            raise AssertionError("existing WebP should not be decoded")

        def encode(self, frame: object, destination: Path, quality: int) -> None:
            raise AssertionError("existing WebP should not be encoded")

        def dimensions(self, path: Path) -> tuple[int, int]:
            raise AssertionError("existing WebP should not be inspected")

    webp = tmp_path / "already-converted.WEBP"
    webp.write_bytes(b"not even a decodable WebP")

    converter = WebPDirectoryConverter(FailingCodec())

    result = converter.convert(tmp_path, WebPOptions(replace=True))
    assert result.conversions == ()
    assert result.skips == ()


def test_pillow_inspection_does_not_load_raster_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "photo.ppm"
    _write_ppm(source, (17, 13))

    def fail_if_loaded(image: Image.Image) -> None:
        raise AssertionError("header inspection must not decode pixels")

    monkeypatch.setattr(Image.Image, "load", fail_if_loaded)

    inspection = PillowWebPCodec().inspect(source)

    assert inspection.dimensions == (17, 13)
    assert inspection.is_webp is False
    assert inspection.is_multi_frame is False
    assert inspection.estimated_peak_bytes > 17 * 13 * 4


def test_keeps_source_when_webp_is_not_smaller(tmp_path: Path) -> None:
    source = tmp_path / "tiny.ppm"
    source.write_bytes(b"P6\n1 1\n255\n\xff\x00\x00")

    result = convert_directory_to_webp(tmp_path)
    conversions = result.conversions
    skips = result.skips

    assert conversions == ()
    assert len(skips) == 1
    assert skips[0].reason == "WebP would not be smaller"
    assert source.exists()
    assert not source.with_suffix(".webp").exists()


def test_does_not_overwrite_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "photo.ppm"
    destination = tmp_path / "photo.webp"
    _write_ppm(source)
    destination.write_bytes(b"existing")

    result = convert_directory_to_webp(tmp_path)
    conversions = result.conversions
    skips = result.skips

    assert conversions == ()
    assert len(skips) == 1
    assert "destination exists" in skips[0].reason
    assert source.exists()
    assert destination.read_bytes() == b"existing"


def test_dry_run_never_replaces_source(tmp_path: Path) -> None:
    source = tmp_path / "photo.ppm"
    _write_ppm(source)
    original = source.read_bytes()

    result = convert_directory_to_webp(tmp_path)
    conversions = result.conversions
    skips = result.skips

    assert conversions == ()
    assert skips[0].reason == "replacement not enabled (use --replace)"
    assert source.read_bytes() == original
    assert not source.with_suffix(".webp").exists()


def test_does_not_follow_or_overwrite_dangling_destination_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "photo.ppm"
    destination = tmp_path / "photo.webp"
    _write_ppm(source)
    destination.symlink_to("missing.webp")

    result = convert_directory_to_webp(tmp_path, replace=True)
    conversions = result.conversions
    skips = result.skips

    assert conversions == ()
    assert "destination exists" in skips[0].reason
    assert source.exists()
    assert destination.is_symlink()


@pytest.mark.parametrize("quality", [-1, 101])
def test_rejects_invalid_quality(tmp_path: Path, quality: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        convert_directory_to_webp(tmp_path, quality=quality)


@pytest.mark.parametrize("max_workers", [0, -1])
def test_rejects_invalid_worker_count(tmp_path: Path, max_workers: int) -> None:
    with pytest.raises(ValueError, match="max_workers must be greater than 0"):
        convert_directory_to_webp(tmp_path, max_workers=max_workers)


@pytest.mark.parametrize("memory_limit_mb", [0, -1])
def test_rejects_invalid_memory_limit(
    tmp_path: Path, memory_limit_mb: int
) -> None:
    with pytest.raises(ValueError, match="memory_limit_mb must be greater than 0"):
        convert_directory_to_webp(tmp_path, memory_limit_mb=memory_limit_mb)


def test_automatic_memory_limit_uses_conservative_available_memory_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gibibyte = 1024 * 1024 * 1024
    monkeypatch.setattr(image_webp, "_available_memory_bytes", lambda: 2 * gibibyte)

    assert image_webp._automatic_memory_limit() == gibibyte // 2


def test_skips_image_that_cannot_fit_budget_without_decoding(tmp_path: Path) -> None:
    class OversizedCodec(WebPCodec[object]):
        def inspect(self, path: Path) -> ImageInspection:
            return ImageInspection((1, 1), False, False, 2 * 1024 * 1024)

        def decode(self, path: Path) -> DecodedImage[object]:
            raise AssertionError("oversized image must not be decoded")

        def encode(self, frame: object, destination: Path, quality: int) -> None:
            raise AssertionError("oversized image must not be encoded")

        def dimensions(self, path: Path) -> tuple[int, int]:
            raise AssertionError("oversized image must not be validated")

    source = tmp_path / "oversized.jpg"
    source.write_bytes(b"x" * 100)

    result = WebPDirectoryConverter(OversizedCodec()).convert(
        tmp_path,
        WebPOptions(replace=True, memory_limit_mb=1),
    )

    assert result.conversions == ()
    assert len(result.skips) == 1
    assert result.skips[0].path == source
    assert "estimated conversion memory" in result.skips[0].reason
    assert "exceeds limit" in result.skips[0].reason
    assert source.exists()


def test_limits_aggregate_conversion_memory_and_keeps_safe_parallelism(
    tmp_path: Path,
) -> None:
    mebibyte = 1024 * 1024
    estimates = {
        "image-0.jpg": 6 * mebibyte,
        "image-1.jpg": 6 * mebibyte,
        "image-2.jpg": 4 * mebibyte,
    }
    active_bytes = 0
    active_jobs = 0
    maximum_active_bytes = 0
    maximum_active_jobs = 0
    parallel_observed = False
    condition = Condition()
    events: list[tuple[str, Path]] = []

    class Scanner:
        def __init__(self, paths: list[Path]) -> None:
            self._paths = paths

        def scan(self, directory: Path) -> list[Path]:
            return self._paths

    class BudgetTrackingCodec(WebPCodec[tuple[Path, int]]):
        def inspect(self, path: Path) -> ImageInspection:
            events.append(("inspect", path))
            return ImageInspection((1, 1), False, False, estimates[path.name])

        def decode(self, path: Path) -> DecodedImage[tuple[Path, int]]:
            nonlocal active_bytes, active_jobs
            nonlocal maximum_active_bytes, maximum_active_jobs, parallel_observed
            required = estimates[path.name]
            with condition:
                events.append(("decode", path))
                active_bytes += required
                active_jobs += 1
                maximum_active_bytes = max(maximum_active_bytes, active_bytes)
                maximum_active_jobs = max(maximum_active_jobs, active_jobs)
                if active_jobs > 1:
                    parallel_observed = True
                    condition.notify_all()
            return DecodedImage((path, required), (1, 1), False, False)

        def encode(
            self,
            frame: tuple[Path, int],
            destination: Path,
            quality: int,
        ) -> None:
            with condition:
                condition.wait_for(lambda: parallel_observed, timeout=1)
            destination.write_bytes(b"x")

        def dimensions(self, path: Path) -> tuple[int, int]:
            return (1, 1)

        def release(self, frame: tuple[Path, int]) -> None:
            nonlocal active_bytes, active_jobs
            with condition:
                active_bytes -= frame[1]
                active_jobs -= 1

    sources = [tmp_path / f"image-{index}.jpg" for index in range(3)]
    for source in sources:
        source.write_bytes(b"x" * 100)

    result = WebPDirectoryConverter(
        BudgetTrackingCodec(),
        scanner=Scanner(sources),
    ).convert(
        tmp_path,
        WebPOptions(replace=True, max_workers=3, memory_limit_mb=10),
    )

    assert [conversion.source for conversion in result.conversions] == sources
    assert result.skips == ()
    assert maximum_active_bytes <= 10 * mebibyte
    assert maximum_active_jobs == 2
    assert events[:3] == [("inspect", source) for source in sources]
    assert active_bytes == 0
    assert active_jobs == 0


def test_converts_files_in_parallel_and_preserves_scan_order(tmp_path: Path) -> None:
    active = 0
    maximum_active = 0
    lock = Lock()

    class Scanner:
        def __init__(self, paths: list[Path]) -> None:
            self._paths = paths

        def scan(self, directory: Path) -> list[Path]:
            return self._paths

    class TrackingCodec(WebPCodec[Path]):
        def decode(self, path: Path) -> DecodedImage[Path]:
            return DecodedImage(path, (1, 1), False, False)

        def inspect(self, path: Path) -> ImageInspection:
            return ImageInspection((1, 1), False, False, 1)

        def encode(self, frame: Path, destination: Path, quality: int) -> None:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            sleep(0.03)
            destination.write_bytes(b"x")
            with lock:
                active -= 1

        def dimensions(self, path: Path) -> tuple[int, int]:
            return (1, 1)

    sources = []
    for index in range(4):
        source = tmp_path / f"image-{index}.jpg"
        source.write_bytes(b"x" * 100)
        sources.append(source)

    converter = WebPDirectoryConverter(TrackingCodec(), scanner=Scanner(sources))

    result = converter.convert(
        tmp_path,
        WebPOptions(replace=True, max_workers=4),
    )
    conversions = result.conversions
    skips = result.skips

    assert [conversion.source for conversion in conversions] == sources
    assert skips == ()
    assert maximum_active > 1
