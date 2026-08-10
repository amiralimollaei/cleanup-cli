"""Shared memory estimates and automatic budgets for image operations."""

from __future__ import annotations

import os
from pathlib import Path


MEBIBYTE = 1024 * 1024
# Pillow can retain the source raster while creating converted pixel buffers.
# This deliberately allows more than the common three or four bytes per pixel.
ESTIMATED_BYTES_PER_PIXEL = 32
ESTIMATED_FIXED_BYTES = 8 * MEBIBYTE
FALLBACK_MEMORY_LIMIT = 256 * MEBIBYTE
MAX_AUTOMATIC_MEMORY_LIMIT = 1024 * MEBIBYTE


def estimate_peak_bytes(dimensions: tuple[int, int]) -> int:
    """Conservatively estimate peak bytes needed to process one image."""

    width, height = dimensions
    if width < 1 or height < 1:
        raise ValueError("image dimensions must be greater than 0")
    return width * height * ESTIMATED_BYTES_PER_PIXEL + ESTIMATED_FIXED_BYTES


def format_mebibytes(byte_count: int) -> str:
    return f"{byte_count / MEBIBYTE:.1f} MiB"


def automatic_memory_limit() -> int:
    """Reserve a conservative fraction of memory currently available."""

    return memory_limit_for_available(available_memory_bytes())


def memory_limit_for_available(available: int | None) -> int:
    """Calculate the automatic budget from an available-memory reading."""

    if available is None:
        return FALLBACK_MEMORY_LIMIT
    return max(1, min(available // 4, MAX_AUTOMATIC_MEMORY_LIMIT))


def available_memory_bytes() -> int | None:
    """Best-effort host/container available-memory detection."""

    candidates: list[int] = []
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass

    try:
        cgroup_limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if cgroup_limit != "max":
            limit = int(cgroup_limit)
            usage = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
            candidates.append(max(0, limit - usage))
    except (OSError, ValueError):
        pass

    if candidates:
        return min(candidates)

    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return None