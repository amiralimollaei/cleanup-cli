"""GTK view composition for the cleanup application."""

from __future__ import annotations

from cleanup_cli.composition import create_cleanup_controllers
from cleanup_cli.views.gui.application import GtkGuiView, GtkTab
from cleanup_cli.views.gui.tabs import DeduplicationGtkTab, WebPConversionGtkTab


def create_gui_view() -> GtkGuiView:
    """Compose the production models, controllers, and GTK view."""

    controllers = create_cleanup_controllers()
    return GtkGuiView(
        DeduplicationGtkTab(controllers.deduplication),
        WebPConversionGtkTab(controllers.webp),
    )


__all__ = [
    "DeduplicationGtkTab",
    "GtkGuiView",
    "GtkTab",
    "WebPConversionGtkTab",
    "create_gui_view",
]