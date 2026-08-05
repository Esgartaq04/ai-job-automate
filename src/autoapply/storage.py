"""Hash-addressed artifact storage. Local filesystem by default, GCS in prod."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Artifact


@dataclass(frozen=True)
class StoredArtifact:
    uri: str
    sha256: str
    bytes: int


class ArtifactStore:
    """Content-addressed: the same bytes always land at the same URI."""

    def __init__(self, base_uri: str | None = None):
        self.base_uri = (base_uri or get_settings().artifact_uri).rstrip("/")
        parsed = urlparse(self.base_uri)
        self.scheme = parsed.scheme or "local"
        if self.scheme not in ("local", "file", "gs"):
            raise ValueError(f"unsupported artifact scheme: {self.scheme}")

    def put(self, data: bytes, *, kind: str, suffix: str = "") -> StoredArtifact:
        digest = hashlib.sha256(data).hexdigest()
        key = f"{kind}/{digest[:2]}/{digest}{suffix}"
        if self.scheme in ("local", "file"):
            root = Path(urlparse(self.base_uri).path)
            path = root / key
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
            uri = f"local://{path}"
        else:
            uri = self._put_gcs(key, data)
        return StoredArtifact(uri=uri, sha256=digest, bytes=len(data))

    def get(self, uri: str) -> bytes:
        parsed = urlparse(uri)
        if parsed.scheme in ("local", "file"):
            return Path(parsed.path).read_bytes()
        return self._get_gcs(uri)

    # -- GCS. Imported lazily so local dev needs no google-cloud-storage. --

    def _bucket_and_prefix(self) -> tuple[str, str]:
        parsed = urlparse(self.base_uri)
        return parsed.netloc, parsed.path.strip("/")

    def _put_gcs(self, key: str, data: bytes) -> str:
        from google.cloud import storage  # type: ignore[import-untyped]

        bucket_name, prefix = self._bucket_and_prefix()
        blob_name = f"{prefix}/{key}" if prefix else key
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        if not blob.exists():
            blob.upload_from_string(data)
        return f"gs://{bucket_name}/{blob_name}"

    def _get_gcs(self, uri: str) -> bytes:
        from google.cloud import storage  # type: ignore[import-untyped]

        parsed = urlparse(uri)
        return storage.Client().bucket(parsed.netloc).blob(parsed.path.lstrip("/")).download_as_bytes()


def record_artifact(session: Session, store: ArtifactStore, data: bytes, *, kind: str, suffix: str = "") -> Artifact:
    """Store bytes and register them. Re-storing identical bytes reuses the row."""
    stored = store.put(data, kind=kind, suffix=suffix)
    existing = (
        session.query(Artifact).filter(Artifact.sha256 == stored.sha256, Artifact.kind == kind).first()
    )
    if existing:
        return existing
    artifact = Artifact(kind=kind, uri=stored.uri, sha256=stored.sha256, bytes=stored.bytes)
    session.add(artifact)
    session.flush()
    return artifact
