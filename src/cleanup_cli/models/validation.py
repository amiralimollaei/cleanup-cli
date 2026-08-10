"""Small validation helpers shared by model option objects."""

from __future__ import annotations


def validate_inclusive_range(
    name: str,
    value: int,
    *,
    minimum: int,
    maximum: int,
) -> None:
    """Require *value* to be within the inclusive integer range."""

    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def validate_optional_positive(name: str, value: int | None) -> None:
    """Require an optional integer to be positive when provided."""

    if value is not None and value < 1:
        raise ValueError(f"{name} must be greater than 0")