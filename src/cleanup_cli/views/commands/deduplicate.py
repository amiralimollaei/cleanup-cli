"""CLI module for the image deduplication subcommand."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from cleanup_cli.controllers.core import (
    Controller,
    DeduplicationRequest,
    DeduplicationResult,
)
from cleanup_cli.models.deduplication import DeduplicationOptions, Duplicate
from cleanup_cli.views.commands.arguments import positive_int
from cleanup_cli.views.commands.progress import CliProgress


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
            help="maximum structural/color distance from 0 to 256 (default: 0)",
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

        reported: set[Path] = set()
        progress = CliProgress(output=self._output)
        try:
            def on_result(duplicate: Duplicate) -> None:
                reported.add(duplicate.removed)
                action = "deleted" if args.delete else "would delete"
                self._print_duplicate(action, duplicate, progress=progress)

            result = self._controller.execute(
                DeduplicationRequest(
                    args.directory,
                    DeduplicationOptions(
                        threshold=args.threshold,
                        delete=args.delete,
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

        action = "deleted" if result.deleted else "would delete"
        for duplicate in result.duplicates:
            if duplicate.removed not in reported:
                self._print_duplicate(action, duplicate)
        status = "deleted" if result.deleted else "found"
        self._print(f"{len(result.duplicates)} duplicate(s) {status}")
        savings = "saved" if result.deleted else "that would be saved"
        self._print(
            f"total space {savings}: {result.total_saved_bytes} bytes"
        )

    def _print_duplicate(
        self,
        action: str,
        duplicate: Duplicate,
        *,
        progress: CliProgress | None = None,
    ) -> None:
        savings = "saved" if action == "deleted" else "would save"
        self._print(
            f"{action}: {duplicate.removed} "
            f"(keeping {duplicate.kept}, distance {duplicate.distance}, "
            f"{savings} {duplicate.saved_bytes} bytes)",
            progress=progress,
        )

    def _print(self, message: str, *, progress: CliProgress | None = None) -> None:
        if progress is None:
            print(message, file=self._output, flush=True)
        else:
            progress.write(message)
