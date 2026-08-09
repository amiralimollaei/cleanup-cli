"""Pillow limits used when processing high-resolution user images."""

from PIL import Image


# Pillow's default is about 89 MP.  The CLI is intended to handle ordinary
# high-resolution photographs, while retaining a useful guard against truly
# unreasonable decompression sizes.
MAX_IMAGE_PIXELS = 200_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS