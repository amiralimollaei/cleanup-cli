"""Persistent cache for image signatures."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TypeGuard

from ..abstractions import FileIdentity, IndexedFile, file_identity
from .signatures import PHASH_BITS, ImageSignature


IMAGE_SIGNATURE_CACHE_VERSION = "phash-256-v1"
IMAGE_SIGNATURE_CACHE_DIRECTORY = "image-signatures"


class ImageSignatureCache:
    """Best-effort persistent cache for directory image signatures."""

    def __init__(self, cache_directory: Path | None = None) -> None:
        self._cache_directory = cache_directory

    def load(
        self, directory: Path, paths: Sequence[Path]
    ) -> dict[Path, IndexedFile[ImageSignature]]:
        try:
            root = directory.resolve()
            payload = json.loads(self._cache_path(root).read_text(encoding="utf-8"))
        except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError):
            return {}

        if not self._is_current_payload(payload, root):
            return {}

        current_paths = self._relative_paths(directory, paths)
        loaded: dict[Path, IndexedFile[ImageSignature]] = {}
        for raw_entry in payload["entries"]:
            entry = self._decode_entry(raw_entry)
            if entry is None:
                continue
            relative_path, identity, signature = entry
            path = current_paths.get(relative_path)
            if path is None or not self._has_identity(path, identity):
                continue
            loaded[path] = IndexedFile(path, signature, identity)
        return loaded

    def save(
        self,
        directory: Path,
        indexed: Sequence[IndexedFile[ImageSignature]],
    ) -> None:
        temporary_path: Path | None = None
        try:
            root = directory.resolve()
            cache_path = self._cache_path(root)
            cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            entries = [
                self._encode_entry(
                    item.path.relative_to(directory).as_posix(),
                    item.identity,
                    item.value,
                )
                for item in indexed
                if item.identity is not None
            ]
            payload = {
                "version": IMAGE_SIGNATURE_CACHE_VERSION,
                "directory": str(root),
                "entries": entries,
            }
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=cache_path.parent,
                prefix=f".{cache_path.name}.",
                delete=False,
            ) as cache_file:
                temporary_path = Path(cache_file.name)
                json.dump(payload, cache_file, separators=(",", ":"), sort_keys=True)
            os.replace(temporary_path, cache_path)
            temporary_path = None
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _is_current_payload(payload: object, root: Path) -> bool:
        return (
            isinstance(payload, dict)
            and payload.get("version") == IMAGE_SIGNATURE_CACHE_VERSION
            and payload.get("directory") == str(root)
            and isinstance(payload.get("entries"), list)
        )

    @staticmethod
    def _relative_paths(
        directory: Path, paths: Sequence[Path]
    ) -> dict[str, Path]:
        relative_paths: dict[str, Path] = {}
        for path in paths:
            try:
                relative_paths[path.relative_to(directory).as_posix()] = path
            except ValueError:
                continue
        return relative_paths

    @staticmethod
    def _has_identity(path: Path, expected: FileIdentity) -> bool:
        try:
            return file_identity(path) == expected
        except OSError:
            return False

    def _cache_path(self, root: Path) -> Path:
        cache_directory = self._cache_directory
        if cache_directory is None:
            configured = os.environ.get("XDG_CACHE_HOME")
            cache_root = Path(configured) if configured else Path.home() / ".cache"
            cache_directory = cache_root / "cleanup-cli" / IMAGE_SIGNATURE_CACHE_DIRECTORY
        key = hashlib.sha256(os.fsencode(root)).hexdigest()
        return cache_directory / f"{key}.json"

    @staticmethod
    def _encode_entry(
        relative_path: str,
        identity: FileIdentity,
        signature: ImageSignature,
    ) -> dict[str, object]:
        return {
            "path": relative_path,
            "identity": [
                identity.device,
                identity.inode,
                identity.size,
                identity.modified_ns,
            ],
            "phash": f"{signature.phash:064x}",
            "average_rgb": list(signature.average_rgb),
            "resolution": list(signature.resolution),
        }

    @staticmethod
    def _decode_entry(
        raw_entry: object,
    ) -> tuple[str, FileIdentity, ImageSignature] | None:
        if not isinstance(raw_entry, dict):
            return None
        relative_path = raw_entry.get("path")
        raw_identity = raw_entry.get("identity")
        raw_phash = raw_entry.get("phash")
        raw_rgb = raw_entry.get("average_rgb")
        raw_resolution = raw_entry.get("resolution")
        if not (
            isinstance(relative_path, str)
            and _is_integer_list(raw_identity, length=4)
            and isinstance(raw_phash, str)
            and len(raw_phash) == PHASH_BITS // 4
            and _is_integer_list(raw_rgb, length=3, minimum=0, maximum=255)
            and _is_integer_list(raw_resolution, length=2, minimum=0)
        ):
            return None
        try:
            phash = int(raw_phash, 16)
        except ValueError:
            return None
        if not 0 <= phash < 1 << PHASH_BITS:
            return None
        return (
            relative_path,
            FileIdentity(*raw_identity),
            ImageSignature(
                phash,
                (raw_rgb[0], raw_rgb[1], raw_rgb[2]),
                (raw_resolution[0], raw_resolution[1]),
            ),
        )


def _is_integer_list(
    value: object,
    *,
    length: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> TypeGuard[list[int]]:
    if not isinstance(value, list) or len(value) != length:
        return False
    return all(
        type(item) is int
        and (minimum is None or item >= minimum)
        and (maximum is None or item <= maximum)
        for item in value
    )
