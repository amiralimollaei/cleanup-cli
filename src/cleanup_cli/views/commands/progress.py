"""Progress rendering helpers for CLI subcommands."""

from __future__ import annotations

import sys
from typing import Protocol, TextIO

import tqdm

from ...models.abstractions import TaskProgress


class _ProgressBar(Protocol):
    """The tqdm instance operations needed by the CLI renderer."""

    def update(self, n: float | None = 1) -> bool | None:
        """Advance the bar by *n* items."""

    def write(
        self,
        s: str,
        file: TextIO | None = None,
        end: str = "\n",
        nolock: bool = False,
    ) -> None:
        """Write a message without corrupting the bar."""

    def close(self) -> None:
        """Close the progress bar."""


class CliProgress:
    """Adapt model progress events to tqdm and provide its safe writer."""

    def __init__(self, *, output: TextIO | None = None) -> None:
        self._output = output
        self._pbar: _ProgressBar | None = None
        self._activity: str | None = None
        self._total = 0
        self._completed = 0

    def __call__(self, progress: TaskProgress) -> None:
        """Create or advance the bar for a model progress event."""

        if (
            self._pbar is None
            or self._activity != progress.activity
            or self._total != progress.total
            or progress.completed < self._completed
        ):
            self.close()
            self._pbar = tqdm.tqdm(
                total=progress.total,
                desc=progress.activity,
                unit="file",
                file=self._output,
            )
            self._activity = progress.activity
            self._total = progress.total
            self._completed = 0

        pbar = self._pbar
        assert pbar is not None
        pbar.update(progress.completed - self._completed)
        self._completed = progress.completed

    def write(self, message: str) -> None:
        """Write with the active pbar when one is present."""

        if self._pbar is None:
            print(message, file=self._output, flush=True)
            return

        self._pbar.write(message, file=self._output)
        stream = self._output if self._output is not None else sys.stdout
        stream.flush()

    def close(self) -> None:
        """Close the active bar, if any."""

        if self._pbar is not None:
            self._pbar.close()
            self._pbar = None
