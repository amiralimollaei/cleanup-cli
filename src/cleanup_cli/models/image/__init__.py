"""Image analysis, deduplication, and conversion models."""

# Apply Pillow's process-wide safety limit before image codecs are imported.
from cleanup_cli.models.image import limits as _limits  # noqa: F401
