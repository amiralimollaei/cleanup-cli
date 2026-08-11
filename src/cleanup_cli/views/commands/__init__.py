"""Self-contained implementations of cleanup CLI subcommands."""

from cleanup_cli.views.commands.deduplicate import DeduplicateCommand
from cleanup_cli.views.commands.webp import WebPCommand

__all__ = ["DeduplicateCommand", "WebPCommand"]
