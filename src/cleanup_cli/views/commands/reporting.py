"""Reconcile live CLI result events with a controller's complete result."""

from collections.abc import Callable, Hashable, Iterable
from typing import Generic, TypeVar


ResultT = TypeVar("ResultT")


class ResultReporter(Generic[ResultT]):
    """Render each item once, whether streamed or returned at completion."""

    def __init__(
        self,
        *,
        key: Callable[[ResultT], Hashable],
        render: Callable[[ResultT], None],
    ) -> None:
        self._key = key
        self._render = render
        self._reported: set[Hashable] = set()

    def __call__(self, item: ResultT) -> None:
        """Render a live result immediately."""

        self._report(item, self._render)

    def complete(
        self,
        items: Iterable[ResultT],
        *,
        render: Callable[[ResultT], None] | None = None,
    ) -> None:
        """Render any items the controller did not stream.

        A completion renderer can use information from the final result that
        was unavailable when live events arrived.
        """

        renderer = render if render is not None else self._render
        for item in items:
            self._report(item, renderer)

    def _report(self, item: ResultT, render: Callable[[ResultT], None]) -> None:
        key = self._key(item)
        if key not in self._reported:
            render(item)
            self._reported.add(key)
