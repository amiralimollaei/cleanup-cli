"""Natural sorting helpers for numbered directory and file names."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from os import PathLike
from pathlib import Path
from typing import TypeVar


PathValue = TypeVar("PathValue", bound=str | PathLike[str])

# A decimal is kept as one component (``1.5``), while hyphen- or
# whitespace-separated numbers naturally become separate tuple components.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

_SEPARATED_DATE_RES = (
    # Year-first: 2026-08-08, 2026_08_08, or 2026.08.08.
    re.compile(
        r"(?<!\d)(?P<year>\d{4})[-_.](?P<month>\d{1,2})[-_.](?P<day>\d{1,2})"
        r"(?:[T _.-]+(?P<hour>\d{1,2})[:_.-](?P<minute>\d{1,2})"
        r"(?:[:_.-](?P<second>\d{1,2}))?)?(?!\d)",
        re.IGNORECASE,
    ),
    # Day-first: 08-08-2026, 08_08_2026, or 08.08.2026.
    re.compile(
        r"(?<!\d)(?P<day>\d{1,2})[-_.](?P<month>\d{1,2})[-_.](?P<year>\d{4})"
        r"(?:[T _.-]+(?P<hour>\d{1,2})[:_.-](?P<minute>\d{1,2})"
        r"(?:[:_.-](?P<second>\d{1,2}))?)?(?!\d)",
        re.IGNORECASE,
    ),
)

_COMPACT_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
    r"(?:[T _.-]?(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2}))?(?!\d)",
    re.IGNORECASE,
)


def _date_time_parts(name: str) -> tuple[Decimal, ...] | None:
    """Return a normalized valid date/time found in *name*, if any."""

    for pattern in (*_SEPARATED_DATE_RES, _COMPACT_DATE_RE):
        for match in pattern.finditer(name):
            values = match.groupdict(default="0")
            try:
                value = datetime(
                    int(values["year"]),
                    int(values["month"]),
                    int(values["day"]),
                    int(values["hour"]),
                    int(values["minute"]),
                    int(values["second"]),
                )
            except ValueError:
                # Keep searching; an invalid date may precede a valid one.
                continue

            return tuple(
                Decimal(part)
                for part in (
                    value.year,
                    value.month,
                    value.day,
                    value.hour,
                    value.minute,
                    value.second,
                )
            )
    return None


def _numeric_parts(name: str) -> tuple[Decimal, ...]:
    """Return numeric components found in one path component."""

    date_time = _date_time_parts(name)
    if date_time is not None:
        return date_time

    parts: list[Decimal] = []
    for match in _NUMBER_RE.finditer(name):
        try:
            parts.append(Decimal(match.group()))
        except InvalidOperation:  # Defensive: the regex only matches decimals.
            continue
    return tuple(parts)


def _component_key(name: str) -> tuple[bool, tuple[Decimal, ...], str, str]:
    numbers = _numeric_parts(name)
    return (not numbers, numbers, name.casefold(), name)


def path_number_key(
    path: str | PathLike[str],
) -> tuple[tuple[bool, tuple[Decimal, ...], str, str], ...]:
    """Build a natural-sort key for a path.

    Every path component is compared hierarchically from left to right.
    Valid common date/time components are normalized to chronological values.
    Otherwise, numbers are extracted left-to-right: ``chapter-2-part-10``
    gets ``(2, 10)`` and ``dir-1.5`` gets ``(1.5,)``. Numeric components sort
    before numberless components, and final fields make ties deterministic.

    The key is suitable for Python's :func:`sorted` and :meth:`list.sort`.
    """

    # Do not discard parent directories: ``1/dir-10`` must precede
    # ``100/dir-2``. Decimal provides exact comparisons for numeric labels.
    return tuple(_component_key(part) for part in Path(path).parts)


def sort_numbered_paths(paths: Iterable[PathValue]) -> list[PathValue]:
    """Return *paths* in natural numeric/alphabetical order.

    The original path values are returned unchanged, making this useful with
    both strings and :class:`~pathlib.Path` objects. Every directory and the
    final filename participates in the comparison.
    """

    return sorted(paths, key=path_number_key)
