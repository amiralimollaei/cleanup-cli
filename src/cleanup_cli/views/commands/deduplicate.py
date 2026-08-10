"""CLI module for the image deduplication subcommand."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from ...controllers import Controller, DeduplicationRequest, DeduplicationResult
from ...models.image_duplicates import DeduplicationOptions
from .arguments import positive_int


class DeduplicateCommand:
    """Define, execute, and render the ``deduplicate`` subcommand."""

    name = "deduplicate"
    help = "find or remove perceptually duplicate images"

    def __init__(
        self,
        controller: Controller[DeduplicationRequest, DeduplicationResult],
        *,
        output: TextIO | None = None,
    ) -> None:
        self._controller = controller
        self._output = output

    def add_to(self, subparsers: argparse._SubParsersAction) -> None:
        """Register this command and all of its arguments."""

        parser = subparsers.add_parser(self.name, help=self.help)
        parser.add_argument(
            "directory", type=Path, help="directory to scan recursively"
        )
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
        parser.add_argument(
            "--max-workers",
            type=positive_int,
            default=None,
            metavar="N",
            help=(
                "maximum number of worker threads for image hashing "
                "(default: executor default)"
            ),
        )
        parser.add_argument(
            "--memory-limit-mb",
            type=positive_int,
            default=None,
            metavar="MiB",
            help=(
                "maximum estimated memory for concurrent image hashing "
                "(default: auto)"
            ),
        )
        parser.set_defaults(command_handler=self, command_parser=parser)

    def execute(
        self, args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> None:
        """Build the request, invoke the controller, and render its result."""

        try:
            result = self._controller.execute(
                DeduplicationRequest(
                    args.directory,
                    DeduplicationOptions(
                        threshold=args.threshold,
                        delete=args.delete,
                        max_workers=args.max_workers,
                        memory_limit_mb=args.memory_limit_mb,
                    ),
                )
            )
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

    def _print(self, message: str) -> None:
        print(message, file=self._output)