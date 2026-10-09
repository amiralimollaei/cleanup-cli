"""CLI module for the WebP conversion subcommand."""

from __future__ import annotations

import argparse
from itertools import chain
from pathlib import Path
from typing import TextIO

from cleanup_cli.controllers.core import Controller, WebPConversionRequest
from cleanup_cli.models.image.webp import (
    WebPConversion,
    WebPDirectoryConversionResult,
    WebPOptions,
    WebPResult,
)
from cleanup_cli.views.commands.arguments import add_resource_arguments
from cleanup_cli.views.commands.progress import CliProgress
from cleanup_cli.views.commands.reporting import ResultReporter


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
        add_resource_arguments(parser, activity="conversions")
        parser.set_defaults(command_handler=self, command_parser=parser)

    def execute(
        self, args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> None:
        """Build the request, invoke the controller, and render its result."""

        progress = CliProgress(output=self._output)
        reporter = ResultReporter[WebPResult](
            key=lambda item: (
                type(item),
                item.source if isinstance(item, WebPConversion) else item.path,
            ),
            render=lambda item: progress.write(self._result_message(item)),
        )
        try:
            result = self._controller.execute(
                WebPConversionRequest(
                    args.directory,
                    WebPOptions(
                        quality=args.quality,
                        replace=args.replace,
                        max_workers=args.max_workers,
                        memory_limit_mb=args.memory_limit_mb,
                    ),
                    on_result=reporter,
                    on_progress=progress,
                )
            )
        except (NotADirectoryError, ValueError) as error:
            parser.error(str(error))
        finally:
            progress.close()

        reporter.complete(chain(result.conversions, result.skips))
        progress.write(
            f"{len(result.conversions)} image(s) converted, "
            f"{len(result.skips)} image(s) skipped"
        )
        progress.write(f"total space saved: {result.total_saved_bytes} bytes")

    @staticmethod
    def _result_message(item: WebPResult) -> str:
        if isinstance(item, WebPConversion):
            return (
                f"converted: {item.source} -> {item.destination} "
                f"(saved {item.saved_bytes} bytes)"
            )
        return f"skipped: {item.path} ({item.reason})"
