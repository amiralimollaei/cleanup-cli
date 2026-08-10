"""Production dependency composition shared by CLI and GTK frontends."""

from __future__ import annotations

from dataclasses import dataclass

from .controllers import DeduplicationController, WebPConversionController
from .models.image_duplicates import ImageSignature, create_image_deduplicator
from .models.image_webp import PillowWebPCodec, WebPDirectoryConverter


@dataclass(frozen=True, slots=True)
class CleanupControllers:
    """Controllers required by the application's production views."""

    deduplication: DeduplicationController[ImageSignature]
    webp: WebPConversionController


def create_cleanup_controllers() -> CleanupControllers:
    """Build the concrete model and controller graph for the application."""

    return CleanupControllers(
        deduplication=DeduplicationController(create_image_deduplicator()),
        webp=WebPConversionController(WebPDirectoryConverter(PillowWebPCodec())),
    )