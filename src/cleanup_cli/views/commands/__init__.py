"""Self-contained implementations of cleanup CLI subcommands."""

from .deduplicate import DeduplicateCommand
from .webp import WebPCommand

__all__ = ["DeduplicateCommand", "WebPCommand"]