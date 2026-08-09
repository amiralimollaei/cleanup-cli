"""Command-line view and default application composition."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
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
    DirectoryDeduplicator,
    ImageSignature,
    ImageSignatureDistance,
    PillowImageSignatureAnalyzer,
    ReverseDuplicateDetector,
)
from ..models.image_webp import PillowWebPCodec, WebPDirectoryConverter
from .commands import DeduplicateCommand, WebPCommand


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
        self._output = output
        self._deduplicate_command = DeduplicateCommand(deduplication, output=output)
        self._commands = (
            self._deduplicate_command,
            WebPCommand(webp, output=output),
        )

    def run(self, arguments: Sequence[str] | None = None) -> int:
        parser = self._build_parser()
        values = list(arguments) if arguments is not None else None

        if values and values[0] not in {"deduplicate", "webp", "-h", "--help"}:
            legacy_parser = argparse.ArgumentParser(
                description="Recursively remove perceptually duplicate images."
            )
            self._deduplicate_command.configure_parser(legacy_parser)
            args = legacy_parser.parse_args(values)
            self._deduplicate_command.execute(args, legacy_parser)
            return 0

        args = parser.parse_args(values)
        handler = getattr(args, "command_handler", None)
        if handler is None:
            parser.print_help(file=self._output)
            return 0
        handler.execute(args, args.command_parser)
        return 0

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Clean up and optimize image directories."
        )
        subparsers = parser.add_subparsers(dest="command")
        for command in self._commands:
            command.add_to(subparsers)
        return parser


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
