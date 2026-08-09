"""Command-line view and default application composition."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TextIO

from ..controllers import (
    Controller,
    DeduplicationController,
    DeduplicationRequest,
    DeduplicationResult,
    WebPConversionController,
    WebPConversionRequest,
    WebPConversionResult,
)
from ..models.abstractions import ImageDirectoryScanner, RecursiveDirectoryIndexer
from ..models.image_duplicates import (
    DeduplicationOptions,
    DirectoryDeduplicator,
    ImageSignature,
    ImageSignatureDistance,
    PillowImageSignatureAnalyzer,
    ReverseDuplicateDetector,
)
from ..models.image_webp import PillowWebPCodec, WebPDirectoryConverter, WebPOptions


class CliView(Protocol):
    """Contract for a command-line application view."""

    def run(self, arguments: Sequence[str] | None = None) -> int:
        """Parse arguments, invoke controllers, and render their results."""
        ...


class ArgparseCliView:
    """Argparse implementation of the cleanup command-line view."""

    def __init__(
        self,
        deduplication: Controller[DeduplicationRequest, DeduplicationResult],
        webp: Controller[WebPConversionRequest, WebPConversionResult],
        *,
        output: TextIO | None = None,
    ) -> None:
        self._deduplication = deduplication
        self._webp = webp
        self._output = output

    def run(self, arguments: Sequence[str] | None = None) -> int:
        parser, duplicate_parser, webp_parser = _build_parser()
        values = list(arguments) if arguments is not None else None

        if values and values[0] not in {"duplicates", "webp", "-h", "--help"}:
            legacy_parser = argparse.ArgumentParser(
                description="Recursively remove perceptually duplicate images."
            )
            _add_duplicate_arguments(legacy_parser)
            args = legacy_parser.parse_args(values)
            self._run_duplicates(args, legacy_parser)
            return 0

        args = parser.parse_args(values)
        if args.command == "duplicates":
            self._run_duplicates(args, duplicate_parser)
        elif args.command == "webp":
            self._run_webp(args, webp_parser)
        else:
            parser.print_help(file=self._output)
        return 0

    def _run_duplicates(
        self,
        args: argparse.Namespace,
        parser: argparse.ArgumentParser,
    ) -> None:
        try:
            request = DeduplicationRequest(
                args.directory,
                DeduplicationOptions(threshold=args.threshold, delete=args.delete),
            )
            result = self._deduplication.execute(request)
        except (NotADirectoryError, ValueError) as error:
            parser.error(str(error))

        action = "deleted" if result.deleted else "would delete"
        for duplicate in result.duplicates:
            self._print(
                f"{action}: {duplicate.removed} "
                f"(keeping {duplicate.kept}, distance {duplicate.distance})"
            )
        status = "deleted" if result.deleted else "found"
        self._print(f"{len(result.duplicates)} duplicate(s) {status}")

    def _run_webp(
        self,
        args: argparse.Namespace,
        parser: argparse.ArgumentParser,
    ) -> None:
        try:
            request = WebPConversionRequest(
                args.directory,
                WebPOptions(
                    quality=args.quality,
                    replace=args.replace,
                    max_workers=args.max_workers,
                ),
            )
            result = self._webp.execute(request)
        except (NotADirectoryError, ValueError) as error:
            parser.error(str(error))

        for conversion in result.conversions:
            saved = conversion.original_size - conversion.webp_size
            self._print(
                f"converted: {conversion.source} -> {conversion.destination} "
                f"(saved {saved} bytes)"
            )
        for skip in result.skips:
            self._print(f"skipped: {skip.path} ({skip.reason})")
        self._print(
            f"{len(result.conversions)} image(s) converted, "
            f"{len(result.skips)} image(s) skipped"
        )

    def _print(self, message: str) -> None:
        print(message, file=self._output)


def create_cli_view(*, output: TextIO | None = None) -> ArgparseCliView:
    """Compose the production models, controllers, and CLI view."""

    indexer = RecursiveDirectoryIndexer(
        PillowImageSignatureAnalyzer(),
        scanner=ImageDirectoryScanner(),
    )
    deduplicator = DirectoryDeduplicator(
        indexer,
        ReverseDuplicateDetector[ImageSignature](ImageSignatureDistance()),
    )
    deduplication_controller = DeduplicationController(deduplicator)
    webp_controller = WebPConversionController(WebPDirectoryConverter(PillowWebPCodec()))
    return ArgparseCliView(
        deduplication_controller,
        webp_controller,
        output=output,
    )


def _build_parser() -> tuple[
    argparse.ArgumentParser,
    argparse.ArgumentParser,
    argparse.ArgumentParser,
]:
    parser = argparse.ArgumentParser(
        description="Clean up and optimize image directories."
    )
    subparsers = parser.add_subparsers(dest="command")

    duplicate_parser = subparsers.add_parser(
        "duplicates", help="find or remove perceptually duplicate images"
    )
    _add_duplicate_arguments(duplicate_parser)

    webp_parser = subparsers.add_parser(
        "webp", help="recursively replace images with smaller WebP files"
    )
    webp_parser.add_argument(
        "directory", type=Path, help="directory to convert recursively"
    )
    webp_parser.add_argument(
        "--quality",
        type=int,
        default=80,
        metavar="0-100",
        help="WebP encoding quality (default: 80)",
    )
    webp_parser.add_argument(
        "--replace",
        action="store_true",
        help="replace originals; without this flag, only validate a dry run",
    )
    webp_parser.add_argument(
        "--max-workers",
        type=_positive_int,
        default=None,
        metavar="N",
        help="maximum number of worker threads (default: executor default)",
    )
    return parser, duplicate_parser, webp_parser


def _add_duplicate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("directory", type=Path, help="directory to scan recursively")
    parser.add_argument(
        "--threshold",
        type=int,
        default=0,
        metavar="BITS",
        help="maximum structural/color distance from 0 to 64 (default: 0)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete duplicates; without this flag, only show a dry run",
    )


def _positive_int(value: str) -> int:
    workers = int(value)
    if workers < 1:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return workers
