"""Filesystem identity and no-clobber mutation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Metadata identifying the directory entry that was analyzed."""

    device: int
    inode: int
    size: int
    modified_ns: int


def file_identity(path: Path) -> FileIdentity:
    """Return identity metadata used to reject stale destructive actions."""

    stat = path.stat()
    return FileIdentity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def hard_link_no_clobber(source: Path, destination: Path) -> None:
    """Make *destination* contain *source*'s data without clobbering it.

    Hard links give an atomic, same-filesystem, no-overwrite publish. When the
    platform or filesystem does not provide ``os.link``, fall back to a
    byte-for-byte copy that likewise refuses to overwrite an existing file.

    Raises ``FileExistsError`` if *destination* already exists.
    """

    try:
        os.link(source, destination, follow_symlinks=False)
    except (AttributeError, NotImplementedError):
        created = False
        try:
            with source.open("rb") as input_stream, destination.open(
                "xb"
            ) as output_stream:
                created = True
                while chunk := input_stream.read(65536):
                    output_stream.write(chunk)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise


def quarantine_if_unchanged(path: Path, expected: FileIdentity) -> Path:
    """Atomically move *path* aside and verify it is the analyzed file.

    Moving before checking closes the check/unlink race: even if a writer
    replaces the pathname at the worst moment, its data is retained either at
    the original name or at the returned quarantine path.
    """

    quarantine_directory = Path(
        tempfile.mkdtemp(dir=path.parent, prefix=f".{path.name}-quarantine-")
    )
    quarantine = quarantine_directory / path.name
    try:
        os.rename(path, quarantine)
    except BaseException:
        quarantine_directory.rmdir()
        raise
    if file_identity(quarantine) == expected:
        return quarantine

    try:
        hard_link_no_clobber(quarantine, path)
    except FileExistsError:
        raise OSError(
            f"file changed and was preserved at recovery path: {quarantine}"
        )
    quarantine.unlink()
    quarantine_directory.rmdir()
    raise OSError(f"file changed since it was analyzed: {path}")
