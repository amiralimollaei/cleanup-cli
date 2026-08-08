from pathlib import Path
from typing import cast

import av
import numpy as np
import pytest
from av.video.stream import VideoStream

from cleanup_cli import convert_directory_to_webp


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
    frame = av.VideoFrame.from_ndarray(pixels, format="rgba").reformat(format="bgra")
    with av.open(str(path), mode="w", format="webp") as container:
        stream = cast(VideoStream, container.add_stream("libwebp"))
        stream.width = width
        stream.height = height
        stream.pix_fmt = "bgra"
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def test_converts_recursively_in_place_without_changing_dimensions(tmp_path: Path) -> None:
    nested = tmp_path / "album"
    nested.mkdir()
    source = nested / "photo.ppm"
    _write_ppm(source)
    original_size = source.stat().st_size

    conversions, skips = convert_directory_to_webp(tmp_path)

    destination = nested / "photo.webp"
    assert skips == []
    assert len(conversions) == 1
    assert not source.exists()
    assert destination.stat().st_size < original_size
    with av.open(str(destination)) as container:
        frame = next(container.decode(video=0))
        assert container.streams.video[0].codec_context.name == "webp"
        assert (frame.width, frame.height) == (128, 96)


def test_skips_existing_webp_and_non_images(tmp_path: Path) -> None:
    webp = tmp_path / "existing.webp"
    _write_webp(webp)
    contents = webp.read_bytes()
    (tmp_path / "notes.txt").write_text("not an image")

    assert convert_directory_to_webp(tmp_path) == ([], [])
    assert webp.read_bytes() == contents


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


@pytest.mark.parametrize("quality", [-1, 101])
def test_rejects_invalid_quality(tmp_path: Path, quality: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        convert_directory_to_webp(tmp_path, quality=quality)