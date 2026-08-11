"""CLI module for the WebP conversion subcommand."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from cleanup_cli.controllers.core import Controller, WebPConversionRequest
from cleanup_cli.models.image.webp import (
    WebPConversion,
    WebPDirectoryConversionResult,
    WebPOptions,
    WebPResult,
    WebPSkip,
)
from cleanup_cli.views.commands.arguments import positive_int
from cleanup_cli.views.commands.progress import CliProgress


class WebPCommand:
    """Define, execute, and render the ``webp`` subcommand."""

    name = "webp"
    help = "recursively replace images with smaller WebP files"

    def __init__(
        self,
        controller: Controller[WebPConversionRequest, WebPDirectoryConversionResult],
        *,
        output: TextIO | None = None,
    ) -> None:
        self._controller = controller
        self._output = output

    def add_to(self, subparsers: argparse._SubParsersAction) -> None:
        """Register this command and all of its arguments."""

        parser = subparsers.add_parser(self.name, help=self.help)
        parser.add_argument(
            "directory", type=Path, help="directory to convert recursively"
        )
        parser.add_argument(
            "--quality",
            type=int,
            default=80,
            metavar="0-100",
            help="WebP encoding quality (default: 80)",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="replace originals; without this flag, only validate a dry run",
        )
        parser.add_argument(
            "--max-workers",
            type=positive_int,
            default=None,
            metavar="N",
            help="maximum number of worker threads (default: executor default)",
        )
        parser.add_argument(
            "--memory-limit-mb",
            type=positive_int,
            default=None,
            metavar="MiB",
            help=(
                "maximum estimated memory for concurrent conversions "
                "(default: auto)"
            ),
        )
        parser.set_defaults(command_handler=self, command_parser=parser)

    def execute(
        self, args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> None:
        """Build the request, invoke the controller, and render its result."""

        reported: set[tuple[str, Path]] = set()
        progress = CliProgress(output=self._output)
        try:
            def on_result(item: WebPResult) -> None:
                if isinstance(item, WebPConversion):
                    reported.add(("conversion", item.source))
                    self._print_conversion(item, progress=progress)
                elif isinstance(item, WebPSkip):
                    reported.add(("skip", item.path))
                    self._print_skip(item, progress=progress)

            result = self._controller.execute(
                WebPConversionRequest(
                    args.directory,
                    WebPOptions(
                        quality=args.quality,
                        replace=args.replace,
                        max_workers=args.max_workers,
                        memory_limit_mb=args.memory_limit_mb,
                    ),
                    on_result=on_result,
                    on_progress=progress,
                )
            )
        except (NotADirectoryError, ValueError) as error:
            parser.error(str(error))
        finally:
            progress.close()

        for conversion in result.conversions:
            if ("conversion", conversion.source) not in reported:
                self._print_conversion(conversion)
        for skip in result.skips:
            if ("skip", skip.path) not in reported:
                self._print_skip(skip)
        self._print(
            f"{len(result.conversions)} image(s) converted, "
            f"{len(result.skips)} image(s) skipped"
        )
        self._print(f"total space saved: {result.total_saved_bytes} bytes")

    def _print_conversion(
        self,
        conversion: WebPConversion,
        *,
        progress: CliProgress | None = None,
    ) -> None:
        self._print(
            f"converted: {conversion.source} -> {conversion.destination} "
            f"(saved {conversion.saved_bytes} bytes)",
            progress=progress,
        )

    def _print_skip(
        self, skip: WebPSkip, *, progress: CliProgress | None = None
    ) -> None:
        self._print(f"skipped: {skip.path} ({skip.reason})", progress=progress)

    def _print(self, message: str, *, progress: CliProgress | None = None) -> None:
        if progress is None:
            print(message, file=self._output, flush=True)
        else:
            progress.write(message)
