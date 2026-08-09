from pathlib import Path
from threading import Lock
from time import sleep

import numpy as np
import pytest
from PIL import Image

from cleanup_cli import convert_directory_to_webp
from cleanup_cli.models.image_webp import (
    DecodedImage,
    WebPCodec,
    WebPDirectoryConverter,
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

    conversions, skips = convert_directory_to_webp(tmp_path, replace=True)

    destination = nested / "photo.webp"
    assert skips == []
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

    assert convert_directory_to_webp(tmp_path) == ([], [])
    assert webp.read_bytes() == contents


def test_skips_webp_by_path_before_decoding(tmp_path: Path) -> None:
    class FailingCodec(WebPCodec[object]):
        def decode(self, path: Path) -> DecodedImage[object]:
            raise AssertionError("existing WebP should not be decoded")

        def encode(self, frame: object, destination: Path, quality: int) -> None:
            raise AssertionError("existing WebP should not be encoded")

        def dimensions(self, path: Path) -> tuple[int, int]:
            raise AssertionError("existing WebP should not be inspected")

    webp = tmp_path / "already-converted.WEBP"
    webp.write_bytes(b"not even a decodable WebP")

    converter = WebPDirectoryConverter(FailingCodec())

    assert converter.convert(tmp_path, replace=True) == ([], [])


def test_keeps_source_when_webp_is_not_smaller(tmp_path: Path) -> None:
    source = tmp_path / "tiny.ppm"
    source.write_bytes(b"P6\n1 1\n255\n\xff\x00\x00")

    conversions, skips = convert_directory_to_webp(tmp_path)

    assert conversions == []
    assert len(skips) == 1
    assert skips[0].reason == "WebP would not be smaller"
    assert source.exists()
    assert not source.with_suffix(".webp").exists()


def test_does_not_overwrite_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "photo.ppm"
    destination = tmp_path / "photo.webp"
    _write_ppm(source)
    destination.write_bytes(b"existing")

    conversions, skips = convert_directory_to_webp(tmp_path)

    assert conversions == []
    assert len(skips) == 1
    assert "destination exists" in skips[0].reason
    assert source.exists()
    assert destination.read_bytes() == b"existing"


def test_dry_run_never_replaces_source(tmp_path: Path) -> None:
    source = tmp_path / "photo.ppm"
    _write_ppm(source)
    original = source.read_bytes()

    conversions, skips = convert_directory_to_webp(tmp_path)

    assert conversions == []
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

    conversions, skips = convert_directory_to_webp(tmp_path, replace=True)

    assert conversions == []
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

    converter = WebPDirectoryConverter(
        TrackingCodec(), scanner=Scanner(sources), max_workers=4
    )

    conversions, skips = converter.convert(tmp_path, replace=True)

    assert [conversion.source for conversion in conversions] == sources
    assert skips == []
    assert maximum_active > 1
