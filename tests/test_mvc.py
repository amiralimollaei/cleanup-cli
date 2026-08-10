import argparse
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
from cleanup_cli.models.abstractions import FileIdentity
from cleanup_cli.views import ArgparseCliView
from cleanup_cli.views.commands import DeduplicateCommand, WebPCommand


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
        self,
        directory: Path,
        *,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
    ) -> list[IndexedFile[int]]:
        return [
            IndexedFile(directory / "1.jpg", 1),
            IndexedFile(directory / "2.jpg", 1),
        ]


class RecordingCommand:
    """Minimal command used to verify view-level command registration."""

    help = "record a value"

    def __init__(self, name: str) -> None:
        self.name = name
        self.values: list[str] = []

    def add_to(self, subparsers: argparse._SubParsersAction) -> None:
        parser = subparsers.add_parser(self.name, help=self.help)
        parser.add_argument("value")
        parser.set_defaults(command_handler=self, command_parser=parser)

    def execute(
        self, args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> None:
        self.values.append(args.value)


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
    duplicate = Duplicate(
        Path("1.jpg"),
        Path("2.jpg"),
        3,
        FileIdentity(1, 2, 75, 3),
    )
    duplicate_controller = RecordingController(
        DeduplicationResult((duplicate,), deleted=False)
    )
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    output = StringIO()
    view = ArgparseCliView(
        DeduplicateCommand(duplicate_controller, output=output),
        WebPCommand(webp_controller, output=output),
        output=output,
    )

    exit_code = view.run(
        [
            "deduplicate",
            "/photos",
            "--threshold",
            "4",
            "--max-workers",
            "3",
            "--memory-limit-mb",
            "256",
        ]
    )

    assert exit_code == 0
    assert duplicate_controller.requests == [
        DeduplicationRequest(
            Path("/photos"),
            DeduplicationOptions(
                threshold=4,
                delete=False,
                max_workers=3,
                memory_limit_mb=256,
            ),
        )
    ]
    assert output.getvalue().splitlines() == [
        "would delete: 1.jpg (keeping 2.jpg, distance 3, would save 75 bytes)",
        "1 duplicate(s) found",
        "total space that would be saved: 75 bytes",
    ]


def test_cli_view_builds_webp_request_and_renders_result() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    conversion = WebPConversion(
        Path("photo.png"), Path("photo.webp"), original_size=100, webp_size=40
    )
    skip = WebPSkip(Path("small.png"), "WebP would not be smaller")
    webp_controller = RecordingController(WebPDirectoryConversionResult((conversion,), (skip,)))
    output = StringIO()
    view = ArgparseCliView(
        DeduplicateCommand(duplicate_controller, output=output),
        WebPCommand(webp_controller, output=output),
        output=output,
    )

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
        "total space saved: 60 bytes",
    ]


def test_cli_renders_streamed_results_before_controller_returns() -> None:
    duplicate = Duplicate(
        Path("early.jpg"),
        Path("kept.jpg"),
        0,
        FileIdentity(1, 2, 32, 3),
    )
    output = StringIO()

    class StreamingController(
        Controller[DeduplicationRequest, DeduplicationResult]
    ):
        def execute(self, request: DeduplicationRequest) -> DeduplicationResult:
            assert request.on_result is not None
            request.on_result(duplicate)
            assert "would delete: early.jpg" in output.getvalue()
            assert "duplicate(s) found" not in output.getvalue()
            return DeduplicationResult((duplicate,), deleted=False)

    view = ArgparseCliView(
        DeduplicateCommand(StreamingController(), output=output),
        output=output,
    )

    assert view.run(["deduplicate", "/photos"]) == 0
    assert output.getvalue().count("would delete: early.jpg") == 1


def test_cli_renders_streamed_webp_result_before_controller_returns() -> None:
    conversion = WebPConversion(
        Path("early.png"),
        Path("early.webp"),
        original_size=100,
        webp_size=25,
    )
    output = StringIO()

    class StreamingController(
        Controller[WebPConversionRequest, WebPDirectoryConversionResult]
    ):
        def execute(
            self,
            request: WebPConversionRequest,
        ) -> WebPDirectoryConversionResult:
            assert request.on_result is not None
            request.on_result(conversion)
            assert "converted: early.png" in output.getvalue()
            assert "image(s) converted" not in output.getvalue()
            return WebPDirectoryConversionResult((conversion,), ())

    view = ArgparseCliView(
        WebPCommand(StreamingController(), output=output),
        output=output,
    )

    assert view.run(["webp", "/photos", "--replace"]) == 0
    assert output.getvalue().count("converted: early.png") == 1


def test_cli_view_rejects_non_positive_webp_worker_count() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    view = ArgparseCliView(
        DeduplicateCommand(duplicate_controller),
        WebPCommand(webp_controller),
        output=StringIO(),
    )

    with pytest.raises(SystemExit):
        view.run(["webp", "/photos", "--max-workers", "0"])


def test_cli_view_rejects_non_positive_webp_memory_limit() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    view = ArgparseCliView(
        DeduplicateCommand(duplicate_controller),
        WebPCommand(webp_controller),
        output=StringIO(),
    )

    with pytest.raises(SystemExit):
        view.run(["webp", "/photos", "--memory-limit-mb", "0"])


def test_cli_view_rejects_non_positive_deduplication_memory_limit() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    view = ArgparseCliView(
        DeduplicateCommand(duplicate_controller),
        WebPCommand(webp_controller),
        output=StringIO(),
    )

    with pytest.raises(SystemExit):
        view.run(["deduplicate", "/photos", "--memory-limit-mb", "0"])


def test_cli_view_rejects_commandless_directory_input() -> None:
    duplicate_controller = RecordingController(DeduplicationResult((), False))
    webp_controller = RecordingController(WebPDirectoryConversionResult((), ()))
    view = ArgparseCliView(
        DeduplicateCommand(duplicate_controller),
        WebPCommand(webp_controller),
        output=StringIO(),
    )

    with pytest.raises(SystemExit):
        view.run(["/photos", "--delete"])

    assert duplicate_controller.requests == []


def test_cli_view_registers_arbitrary_subcommands() -> None:
    commands = [RecordingCommand(f"record-{index}") for index in range(3)]
    view = ArgparseCliView(*commands, output=StringIO())

    for index, command in enumerate(commands):
        assert view.run([command.name, f"value-{index}"]) == 0

    assert [command.values for command in commands] == [
        ["value-0"],
        ["value-1"],
        ["value-2"],
    ]


def test_cli_view_supports_no_subcommands() -> None:
    output = StringIO()
    view = ArgparseCliView(output=output)

    assert view.run([]) == 0
    assert "Clean up and optimize image directories." in output.getvalue()
