"""Reusable bounded parallel-execution primitives."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import os
from typing import TypeVar

from .validation import validate_optional_positive


InputT = TypeVar("InputT")
ResultT = TypeVar("ResultT")


def worker_count(configured: int | None) -> int:
    """Resolve an optional worker limit using ThreadPoolExecutor's policy."""

    validate_optional_positive("max_workers", configured)
    return configured or min(32, (os.cpu_count() or 1) + 4)


def ordered_parallel_map(
    function: Callable[[InputT], ResultT],
    values: Iterable[InputT],
    *,
    max_workers: int | None = None,
    pending_per_worker: int = 2,
) -> Iterator[ResultT]:
    """Map in input order while bounding work submitted to the executor."""

    if pending_per_worker < 1:
        raise ValueError("pending_per_worker must be greater than 0")

    workers = worker_count(max_workers)
    iterator = iter(values)
    pending: deque[Future[ResultT]] = deque()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for _ in range(workers * pending_per_worker):
            try:
                value = next(iterator)
            except StopIteration:
                break
            pending.append(executor.submit(function, value))

        while pending:
            result = pending.popleft().result()
            try:
                value = next(iterator)
            except StopIteration:
                pass
            else:
                pending.append(executor.submit(function, value))
            yield result


def weighted_parallel_map(
    function: Callable[[InputT], ResultT],
    values: Sequence[InputT],
    *,
    weight: Callable[[InputT], int],
    capacity: int,
    max_workers: int | None = None,
) -> Iterator[tuple[InputT, ResultT]]:
    """Map under a shared capacity limit and yield completed input/result pairs.

    Heavier work is admitted first to prevent small jobs from indefinitely
    delaying the inputs that are hardest to fit. Every input must have a
    positive weight no greater than the total capacity.
    """

    if capacity < 1:
        raise ValueError("capacity must be greater than 0")

    workers = worker_count(max_workers)
    for value in values:
        required = weight(value)
        if required < 1:
            raise ValueError("weights must be greater than 0")
        if required > capacity:
            raise ValueError("weights cannot exceed capacity")

    # sorted() is stable, so equally weighted inputs retain their input order.
    waiting = sorted(values, key=weight, reverse=True)
    pending: dict[Future[ResultT], InputT] = {}
    available = capacity

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while waiting or pending:
            while len(pending) < workers:
                waiting_position = next(
                    (
                        position
                        for position, value in enumerate(waiting)
                        if weight(value) <= available
                    ),
                    None,
                )
                if waiting_position is None:
                    break

                value = waiting.pop(waiting_position)
                available -= weight(value)
                pending[executor.submit(function, value)] = value

            if not pending:
                raise RuntimeError("weighted scheduler could not admit pending work")

            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                value = pending.pop(future)
                available += weight(value)
                yield value, future.result()