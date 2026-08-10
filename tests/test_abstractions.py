from pathlib import Path
from threading import Lock
from time import sleep
from typing import Any, Callable

import pytest

from cleanup_cli import (
    DeduplicationOptions,
    DecodedImage,
    DirectoryDeduplicator,
    DirectoryIndexer,
    ImageDirectoryScanner,
    ImageInspection,
    IndexedFile,
    RecursiveDirectoryIndexer,
    QualityAwareDuplicateDetector,
    WebPCodec,
    WebPDirectoryConverter,
    WebPOptions,
)
from cleanup_cli.models.image_duplicates import Duplicate
from cleanup_cli.models.abstractions import file_identity
from cleanup_cli.models.image_duplicates import FileChangedError, LocalFileRemover
from cleanup_cli.models import abstractions


class TextLengthAnalyzer:
    def analyze(self, path: Path) -> int:
        if path.suffix != ".txt":
            raise ValueError("unsupported file")
        return len(path.read_text())


class AbsoluteDistance:
    def distance(self, left: int, right: int) -> int:
        return abs(left - right)


class StaticIndexer(DirectoryIndexer[int]):
    def __init__(self, files: list[IndexedFile[int]]) -> None:
        self.files = files

    def index(
        self, directory: Path, *, max_workers: int | None = None
    ) -> list[IndexedFile[int]]:
        return self.files


class RecordingRemover:
    def __init__(self) -> None:
        self.removed: list[Path] = []

    def remove(self, path: Path) -> None:
        self.removed.append(path)


class StaticScanner:
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths

    def scan(self, directory: Path) -> list[Path]:
        return self.paths


class FakeWebPCodec(WebPCodec[str]):
    def __init__(self, encoded_size: int = 4) -> None:
        self.encoded_size = encoded_size
        self.qualities: list[int] = []

    def inspect(self, path: Path) -> ImageInspection:
        return ImageInspection((10, 20), False, False, 1)

    def decode(self, path: Path) -> DecodedImage[str]:
        return DecodedImage(
            frame=path.read_text(),
            dimensions=(10, 20),
            is_webp=False,
            is_multi_frame=False,
        )

    def encode(self, frame: str, destination: Path, quality: int) -> None:
        self.qualities.append(quality)
        destination.write_bytes(b"w" * self.encoded_size)

    def dimensions(self, path: Path) -> tuple[int, int]:
        return (10, 20)


def test_recursive_indexer_accepts_analyzer_and_skips_configured_errors(
    tmp_path: Path,
) -> None:
    (tmp_path / "item-10.txt").write_text("long")
    (tmp_path / "item-2.txt").write_text("ok")
    (tmp_path / "image.bin").write_bytes(b"ignored")
    indexer = RecursiveDirectoryIndexer(
        TextLengthAnalyzer(),
        ignored_errors=(ValueError,),
    )

    assert indexer.index(tmp_path) == [
        IndexedFile(tmp_path / "item-2.txt", 2),
        IndexedFile(tmp_path / "item-10.txt", 4),
    ]


def test_recursive_indexer_rejects_non_directory(tmp_path: Path) -> None:
    indexer = RecursiveDirectoryIndexer(TextLengthAnalyzer(), ignored_errors=())

    with pytest.raises(NotADirectoryError):
        indexer.index(tmp_path / "missing")


def test_recursive_indexer_analyzes_files_in_parallel_and_preserves_order(
    tmp_path: Path,
) -> None:
    active = 0
    maximum_active = 0
    lock = Lock()

    class Scanner:
        def scan(self, directory: Path) -> list[Path]:
            return [tmp_path / f"{index}.bin" for index in range(4)]

    class Analyzer:
        def analyze(self, path: Path) -> int:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            sleep(0.03)
            with lock:
                active -= 1
            return int(path.stem)

    paths = [tmp_path / f"{index}.bin" for index in range(4)]
    for path in paths:
        path.write_bytes(b"image")
    indexer = RecursiveDirectoryIndexer(Analyzer(), scanner=Scanner())

    indexed = indexer.index(tmp_path, max_workers=4)

    assert maximum_active > 1
    assert [item.path for item in indexed] == paths


def test_recursive_indexer_bounds_submitted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / f"{index}.txt" for index in range(100)]
    for path in paths:
        path.write_text("value")

    class DeferredFuture:
        def __init__(self, function: Callable[[Path], Any], path: Path) -> None:
            self.function = function
            self.path = path

        def result(self) -> Any:
            executor.outstanding -= 1
            return self.function(self.path)

    class RecordingExecutor:
        def __init__(self) -> None:
            self.outstanding = 0
            self.maximum_outstanding = 0

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def submit(
            self, function: Callable[[Path], Any], path: Path
        ) -> DeferredFuture:
            self.outstanding += 1
            self.maximum_outstanding = max(
                self.maximum_outstanding, self.outstanding
            )
            return DeferredFuture(function, path)

    executor = RecordingExecutor()
    monkeypatch.setattr(
        abstractions, "ThreadPoolExecutor", lambda max_workers: executor
    )
    indexer = RecursiveDirectoryIndexer(
        TextLengthAnalyzer(), scanner=StaticScanner(paths), ignored_errors=()
    )

    indexed = indexer.index(tmp_path, max_workers=3)

    assert [item.path for item in indexed] == paths
    assert executor.maximum_outstanding == 6


def test_duplicate_detector_uses_injected_generic_distance_metric() -> None:
    images = [
        IndexedFile(Path("1.txt"), 10),
        IndexedFile(Path("2.txt"), 12),
    ]

    assert QualityAwareDuplicateDetector(AbsoluteDistance()).find(images, threshold=2) == [
        Duplicate(Path("1.txt"), Path("2.txt"), 2)
    ]


def test_deduplicator_coordinates_abstract_indexer_detector_and_remover() -> None:
    first = IndexedFile(Path("1.txt"), 10)
    second = IndexedFile(Path("2.txt"), 12)
    remover = RecordingRemover()
    service = DirectoryDeduplicator(
        StaticIndexer([first, second]),
        QualityAwareDuplicateDetector(AbsoluteDistance()),
        remover=remover,
    )

    duplicates = service.deduplicate(
        Path("unused"),
        DeduplicationOptions(threshold=2, delete=True),
    )

    assert duplicates == [Duplicate(first.path, second.path, 2)]
    assert remover.removed == [first.path]


def test_local_remover_refuses_to_delete_a_replaced_file(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.jpg"
    path.write_bytes(b"analyzed")
    expected = file_identity(path)
    path.unlink()
    path.write_bytes(b"new user data")

    with pytest.raises(FileChangedError, match="changed"):
        LocalFileRemover().remove(path, expected)

    assert path.read_bytes() == b"new user data"


@pytest.mark.parametrize("threshold", [-1, 65])
def test_deduplication_options_validate_threshold(threshold: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 64"):
        DeduplicationOptions(threshold=threshold)


def test_webp_converter_uses_injected_codec(tmp_path: Path) -> None:
    source = tmp_path / "photo.ppm"
    source.write_text("original image data")
    codec = FakeWebPCodec(encoded_size=4)

    result = WebPDirectoryConverter(codec).convert(
        tmp_path, quality=73, replace=True
    )
    conversions = result.conversions
    skips = result.skips

    destination = tmp_path / "photo.webp"
    assert skips == ()
    assert conversions[0].source == source
    assert conversions[0].destination == destination
    assert conversions[0].webp_size == 4
    assert codec.qualities == [73]
    assert not source.exists()
    assert destination.read_bytes() == b"wwww"


def test_webp_converter_uses_injected_scanner(tmp_path: Path) -> None:
    included = tmp_path / "included.ppm"
    excluded = tmp_path / "excluded.ppm"
    included.write_text("included image data")
    excluded.write_text("excluded image data")

    result = WebPDirectoryConverter(
        FakeWebPCodec(),
        scanner=StaticScanner([included]),
    ).convert(tmp_path, replace=True)

    conversions = result.conversions
    assert [conversion.source for conversion in conversions] == [included]
    assert excluded.exists()


@pytest.mark.parametrize("quality", [-1, 101])
def test_webp_options_validate_quality(quality: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        WebPOptions(quality)


def test_image_directory_scanner_only_yields_still_images(
    tmp_path: Path,
) -> None:
    image = tmp_path / "photo-1.jpg"
    image.write_bytes(b"image")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    notes = tmp_path / "notes.txt"
    notes.write_text("not an image")
    unsupported = tmp_path / "design.psd"
    unsupported.write_bytes(b"not decodable by ffmpeg")

    assert list(ImageDirectoryScanner().scan(tmp_path)) == [image]


def test_image_directory_scanner_rejects_non_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        list(ImageDirectoryScanner().scan(tmp_path / "missing"))
