from io import StringIO
from pathlib import Path
from typing import Generic, TypeVar

import pytest

from cleanup_cli.controllers import (
    Controller,
    DeduplicationController,
    DeduplicationRequest,
    DeduplicationResult,
    WebPConversionRequest,
)
from cleanup_cli.models import (
    DeduplicationOptions,
    DirectoryDeduplicator,
    DirectoryIndexer,
    Duplicate,
    IndexedFile,
    QualityAwareDuplicateDetector,
    WebPConversion,
    WebPDirectoryConversionResult,
    WebPOptions,
    WebPSkip,
)
from cleanup_cli.views import ArgparseCliView


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


class RecordingController(Controller[RequestT, ResultT], Generic[RequestT, ResultT]):
    def __init__(self, result: ResultT) -> None:
        self.result = result
        self.requests: list[RequestT] = []

    def execute(self, request: RequestT) -> ResultT:
        self.requests.append(request)
        return self.result


class StaticIndexer(DirectoryIndexer[int]):
    def index(
        self, directory: Path, *, max_workers: int | None = None
    ) -> list[IndexedFile[int]]:
        return [
            IndexedFile(directory / "1.jpg", 1),
            IndexedFile(directory / "2.jpg", 1),
        ]


class IntegerDistance:
    def distance(self, left: int, right: int) -> int:
        return abs(left - right)


def test_deduplication_controller_returns_immutable_view_model(tmp_path: Path) -> None:
    model = DirectoryDeduplicator(
        StaticIndexer(),
        QualityAwareDuplicateDetector(IntegerDistance()),
    )
    controller = DeduplicationController(model)

    result = controller.execute(DeduplicationRequest(tmp_path))

    assert result == DeduplicationResult(
        (Duplicate(tmp_path / "1.jpg", tmp_path / "2.jpg", 0),),
        deleted=False,
    )


def test_cli_view_builds_deduplication_request_and_renders_result() -> None:
    duplicate = Duplicate(Path("1.jpg"), Path("2.jpg"), 3)
    duplicate_controller = RecordingController(
        DeduplicationResult((duplicate,), deleted=False)
    )
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    output = StringIO()
    view = ArgparseCliView(duplicate_controller, webp_controller, output=output)

    exit_code = view.run(
        ["deduplicate", "/photos", "--threshold", "4", "--max-workers", "3"]
    )

    assert exit_code == 0
    assert duplicate_controller.requests == [
        DeduplicationRequest(
            Path("/photos"),
            DeduplicationOptions(threshold=4, delete=False, max_workers=3),
        )
    ]
    assert output.getvalue().splitlines() == [
        "would delete: 1.jpg (keeping 2.jpg, distance 3)",
        "1 duplicate(s) found",
    ]


def test_cli_view_builds_webp_request_and_renders_result() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    conversion = WebPConversion(
        Path("photo.png"), Path("photo.webp"), original_size=100, webp_size=40
    )
    skip = WebPSkip(Path("small.png"), "WebP would not be smaller")
    webp_controller = RecordingController(WebPDirectoryConversionResult((conversion,), (skip,)))
    output = StringIO()
    view = ArgparseCliView(duplicate_controller, webp_controller, output=output)

    exit_code = view.run(
        [
            "webp",
            "/photos",
            "--quality",
            "90",
            "--replace",
            "--max-workers",
            "3",
            "--memory-limit-mb",
            "256",
        ]
    )

    assert exit_code == 0
    assert webp_controller.requests == [
        WebPConversionRequest(
            Path("/photos"),
            WebPOptions(
                quality=90,
                replace=True,
                max_workers=3,
                memory_limit_mb=256,
            ),
        )
    ]
    assert output.getvalue().splitlines() == [
        "converted: photo.png -> photo.webp (saved 60 bytes)",
        "skipped: small.png (WebP would not be smaller)",
        "1 image(s) converted, 1 image(s) skipped",
    ]


def test_cli_view_rejects_non_positive_webp_worker_count() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    view = ArgparseCliView(duplicate_controller, webp_controller, output=StringIO())

    with pytest.raises(SystemExit):
        view.run(["webp", "/photos", "--max-workers", "0"])


def test_cli_view_rejects_non_positive_webp_memory_limit() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    view = ArgparseCliView(duplicate_controller, webp_controller, output=StringIO())

    with pytest.raises(SystemExit):
        view.run(["webp", "/photos", "--memory-limit-mb", "0"])


def test_cli_view_preserves_legacy_directory_command() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    view = ArgparseCliView(duplicate_controller, webp_controller, output=StringIO())

    view.run(["/photos", "--delete"])

    assert duplicate_controller.requests[0].directory == Path("/photos")
    assert duplicate_controller.requests[0].options.delete is True
