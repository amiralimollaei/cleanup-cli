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
from cleanup_cli.views.commands.arguments import add_resource_arguments
from cleanup_cli.views.commands.progress import CliProgress
from cleanup_cli.views.commands.reporting import ResultReporter


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
        add_resource_arguments(parser, activity="image hashing")
        parser.set_defaults(command_handler=self, command_parser=parser)

    def execute(
        self, args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> None:
        """Build the request, invoke the controller, and render its result."""

        progress = CliProgress(output=self._output)
        reporter = ResultReporter[Duplicate](
            key=lambda duplicate: duplicate.removed,
            render=lambda duplicate: progress.write(
                self._duplicate_message(duplicate, deleted=args.delete)
            ),
        )
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
                    on_result=reporter,
                    on_progress=progress,
                )
            )
        except (NotADirectoryError, ValueError) as error:
            parser.error(str(error))
        finally:
            progress.close()

        reporter.complete(
            result.duplicates,
            render=lambda duplicate: progress.write(
                self._duplicate_message(duplicate, deleted=result.deleted)
            ),
        )
        status = "deleted" if result.deleted else "found"
        progress.write(f"{len(result.duplicates)} duplicate(s) {status}")
        savings = "saved" if result.deleted else "that would be saved"
        progress.write(
            f"total space {savings}: {result.total_saved_bytes} bytes"
        )

    @staticmethod
    def _duplicate_message(
        duplicate: Duplicate,
        *,
        deleted: bool,
    ) -> str:
        action = "deleted" if deleted else "would delete"
        savings = "saved" if deleted else "would save"
        return (
            f"{action}: {duplicate.removed} "
            f"(keeping {duplicate.kept}, distance {duplicate.distance}, "
            f"{savings} {duplicate.saved_bytes} bytes)"
        )
