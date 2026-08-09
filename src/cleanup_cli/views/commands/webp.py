"""CLI module for the WebP conversion subcommand."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from ...controllers import Controller, WebPConversionRequest
from ...models.image_webp import WebPDirectoryConversionResult, WebPOptions
from .arguments import positive_int


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
        parser.set_defaults(command_handler=self, command_parser=parser)

    def execute(
        self, args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> None:
        """Build the request, invoke the controller, and render its result."""

        try:
            result = self._controller.execute(
                WebPConversionRequest(
                    args.directory,
                    WebPOptions(
                        quality=args.quality,
                        replace=args.replace,
                        max_workers=args.max_workers,
                    ),
                )
            )
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