"""Executable entry point for the optional GTK application."""

from __future__ import annotations

from collections.abc import Sequence


def main(arguments: Sequence[str] | None = None) -> int:
    """Load and run the GTK view without affecting CLI-only imports."""

    try:
        from cleanup_cli.views.gui import create_gui_view
    except (ImportError, ValueError) as error:
        raise SystemExit(
            "cleanup-gui requires GTK 4 and PyGObject (the python3-gobject package)"
        ) from error
    return create_gui_view().run(arguments)


__all__ = ["main"]