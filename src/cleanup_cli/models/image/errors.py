"""Shared exceptions treated as unsupported image input."""

from PIL import Image, UnidentifiedImageError


IMAGE_INPUT_ERRORS: tuple[type[Exception], ...] = (
    UnidentifiedImageError,
    OSError,
    EOFError,
    StopIteration,
    ValueError,
    IndexError,
    Image.DecompressionBombWarning,
    Image.DecompressionBombError,
)