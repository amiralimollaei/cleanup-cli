"""GTK view composition for the cleanup application."""

from __future__ import annotations

from ...controllers import DeduplicationController, WebPConversionController
from ...models.abstractions import ImageDirectoryScanner, RecursiveDirectoryIndexer
from ...models.image_duplicates import (
    DirectoryDeduplicator,
    ImageSignature,
    ImageSignatureDistance,
    PillowImageSignatureAnalyzer,
    QualityAwareDuplicateDetector,
    image_quality_key,
)
from ...models.image_webp import PillowWebPCodec, WebPDirectoryConverter
from .application import GtkGuiView, GtkTab
from .tabs import DeduplicationGtkTab, WebPConversionGtkTab


def create_gui_view() -> GtkGuiView:
    """Compose the production models, controllers, and GTK view."""

    analyzer = PillowImageSignatureAnalyzer()
    indexer = RecursiveDirectoryIndexer(
        analyzer,
        scanner=ImageDirectoryScanner(),
        memory_estimator=analyzer,
    )
    deduplicator = DirectoryDeduplicator(
        indexer,
        QualityAwareDuplicateDetector[ImageSignature](
            ImageSignatureDistance(), image_quality_key
        ),
    )
    deduplication_controller = DeduplicationController(deduplicator)
    webp_controller = WebPConversionController(WebPDirectoryConverter(PillowWebPCodec()))
    return GtkGuiView(
        DeduplicationGtkTab(deduplication_controller),
        WebPConversionGtkTab(webp_controller),
    )


__all__ = [
    "DeduplicationGtkTab",
    "GtkGuiView",
    "GtkTab",
    "WebPConversionGtkTab",
    "create_gui_view",
]