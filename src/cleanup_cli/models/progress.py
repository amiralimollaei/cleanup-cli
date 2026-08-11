"""View-independent progress events and iterable tracking."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

import tqdm


ValueT = TypeVar("ValueT")


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Completed work for one phase of a directory task."""

    activity: str
    completed: int
    total: int

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("total must not be negative")
        if not 0 <= self.completed <= self.total:
            raise ValueError("completed must be between 0 and total")

    @property
    def fraction(self) -> float:
        """Return progress as a GTK-compatible value between zero and one."""

        return self.completed / self.total if self.total else 0.0


ProgressObserver = Callable[[TaskProgress], None]


def track_progress(
    values: Iterable[ValueT],
    *,
    total: int,
    description: str,
    unit: str,
    on_progress: ProgressObserver | None,
    activity: str | None = None,
) -> Iterator[ValueT]:
    """Track an iterable in the console or report it to an external view."""

    if on_progress is None:
        yield from tqdm.tqdm(values, total=total, desc=description, unit=unit)
        return

    label = activity or description
    on_progress(TaskProgress(label, 0, total))
    for completed, value in enumerate(values, start=1):
        yield value
        on_progress(TaskProgress(label, completed, total))
