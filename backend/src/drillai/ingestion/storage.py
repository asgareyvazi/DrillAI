"""Blob storage abstraction.

Documents on a drilling project are the *evidence*: the platform must be able to keep the
original bytes unchanged and reproduce every extraction from them. This module therefore
defines a minimal contract (``put`` returns the content hash; bytes are never overwritten) and
a local filesystem implementation used in development, tests and single-node deployments.

Object storage (S3/MinIO/Azure) implements the same protocol; the ingestion pipeline only ever
sees :class:`BlobStore`, so switching is a configuration change. The local implementation writes
to a temporary file and renames it into place, so a crash cannot leave a half-written blob.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from drillai.core.errors import IngestionError

__all__ = ["BlobRef", "BlobStore", "LocalBlobStore", "MemoryBlobStore", "blob_store_from_settings", "compute_sha256"]


@dataclass(frozen=True)
class BlobRef:
    """Content-addressed handle to stored bytes."""

    key: str
    sha256: str
    byte_size: int
    content_type: str

    @property
    def sha256_prefix(self) -> str:
        return self.sha256[:2]


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class BlobStore(Protocol):
    def put(self, data: bytes, *, content_type: str, prefix: str = "raw") -> BlobRef:  # pragma: no cover - protocol
        ...

    def get(self, key: str) -> bytes:  # pragma: no cover - protocol
        ...

    def exists(self, key: str) -> bool:  # pragma: no cover - protocol
        ...

    def delete(self, key: str) -> None:  # pragma: no cover - protocol
        ...


class LocalBlobStore:
    """Filesystem blob store using a ``<prefix>/<sha[0:2]>/<sha[2:]>`` layout."""

    def __init__(self, root: str | Path, *, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if not str(candidate).startswith(str(root)):
            raise IngestionError("blob key escapes the storage root", details={"key": key})
        return candidate

    def put(self, data: bytes, *, content_type: str, prefix: str = "raw") -> BlobRef:
        if len(data) > self.max_bytes:
            raise IngestionError(
                f"blob exceeds the storage limit of {self.max_bytes} bytes", details={"size": len(data)}
            )
        digest = compute_sha256(data)
        key = f"{prefix}/{digest[:2]}/{digest[2:]}"
        target = self._path(key)
        if target.exists():
            return BlobRef(key=key, sha256=digest, byte_size=len(data), content_type=content_type)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception:
            with contextlib_suppress():
                os.unlink(temporary)
            raise
        return BlobRef(key=key, sha256=digest, byte_size=len(data), content_type=content_type)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise IngestionError("blob not found", details={"key": key})
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()


class MemoryBlobStore:
    """In-process blob store (tests, ephemeral preview environments).

    Same content-addressed keys as :class:`LocalBlobStore`, so a deployment can switch backends
    without changing document rows — only the bytes are elsewhere.
    """

    def __init__(self, *, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self._blobs: dict[str, tuple[bytes, str]] = {}

    def put(self, data: bytes, *, content_type: str, prefix: str = "raw") -> BlobRef:
        if len(data) > self.max_bytes:
            raise IngestionError(
                f"blob exceeds the storage limit of {self.max_bytes} bytes", details={"size": len(data)}
            )
        digest = compute_sha256(data)
        key = f"{prefix}/{digest[:2]}/{digest[2:]}"
        self._blobs.setdefault(key, (data, content_type))
        return BlobRef(key=key, sha256=digest, byte_size=len(data), content_type=content_type)

    def get(self, key: str) -> bytes:
        if key not in self._blobs:
            raise IngestionError("blob not found", details={"key": key})
        return self._blobs[key][0]

    def exists(self, key: str) -> bool:
        return key in self._blobs

    def delete(self, key: str) -> None:
        self._blobs.pop(key, None)


def blob_store_from_settings(settings: object) -> BlobStore:
    """Build the configured blob store (``DRILLAI_BLOB_BACKEND=filesystem|memory``)."""
    backend = getattr(settings, "blob_backend", "filesystem")
    if backend == "memory":
        return MemoryBlobStore(max_bytes=getattr(settings, "max_upload_bytes", 512 * 1024 * 1024))
    if backend == "filesystem":
        return LocalBlobStore(
            settings.blob_root, max_bytes=getattr(settings, "max_upload_bytes", 512 * 1024 * 1024)
        )
    raise IngestionError(f"unknown blob backend {backend!r}", details={"backend": backend})


class contextlib_suppress:  # pragma: no cover - tiny local helper
    """Local ``contextlib.suppress(Exception)`` equivalent to avoid a noisy import."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None
