"""Object storage abstraction: local filesystem by default, any S3-compatible service optionally."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from .config import get_settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", Path(name).name).strip("._")
    return cleaned[:150] or "file"


def object_key(workspace_id: str, area: str, object_id: str, filename: str) -> str:
    """All keys are prefixed with the workspace so that one workspace can never address another's objects."""
    return f"ws/{workspace_id}/{area}/{object_id}/{safe_name(filename)}"


def assert_workspace_key(workspace_id: str, key: str) -> None:
    if not key.startswith(f"ws/{workspace_id}/") or ".." in key:
        raise PermissionError("object key is outside this workspace")


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class FilesystemStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise PermissionError("object key escapes the storage root")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3Storage:
    """S3-compatible backend (MinIO, Ceph, AWS S3). Requires the ``s3`` extra."""

    def __init__(self, *, endpoint_url: str | None, bucket: str, access_key: str | None,
                 secret_key: str | None, region: str):
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("S3 storage needs the optional dependency: pip install 'kaizeval[s3]'") from exc
        self.bucket = bucket
        self.client = boto3.client(
            "s3", endpoint_url=endpoint_url, aws_access_key_id=access_key,
            aws_secret_access_key=secret_key, region_name=region,
        )
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            self.client.create_bucket(Bucket=bucket)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_backend == "s3":
            _storage = S3Storage(endpoint_url=s.s3_endpoint_url, bucket=s.s3_bucket, access_key=s.s3_access_key,
                                 secret_key=s.s3_secret_key, region=s.s3_region)
        else:
            _storage = FilesystemStorage(s.storage_dir)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
