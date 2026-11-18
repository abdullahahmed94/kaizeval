"""S3 storage backend, exercised against a local S3 API emulator (moto). No network, no real bucket."""

from __future__ import annotations

import pytest

from conftest import free_port, new_user

boto3 = pytest.importorskip("boto3")
moto_server = pytest.importorskip("moto.server")

from kaizeval import storage as storage_module  # noqa: E402
from kaizeval.config import reset_settings  # noqa: E402
from kaizeval.sample_agent import KB_DIR  # noqa: E402
from kaizeval.storage import S3Storage, object_key  # noqa: E402

BUCKET = "kaizeval-test"
KEYS = {"access_key": "test-access", "secret_key": "test-secret"}


@pytest.fixture(scope="module")
def s3_endpoint():
    port = free_port()
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


def _client(endpoint):
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=KEYS["access_key"],
                        aws_secret_access_key=KEYS["secret_key"], region_name="us-east-1")


def test_s3_backend_round_trip(s3_endpoint):
    store = S3Storage(endpoint_url=s3_endpoint, bucket=BUCKET, region="us-east-1", **KEYS)  # creates the bucket
    assert BUCKET in [b["Name"] for b in _client(s3_endpoint).list_buckets()["Buckets"]]
    key = object_key("ws1", "documents", "doc1", "../../etc/pass wd.pdf")
    assert key == "ws/ws1/documents/doc1/pass_wd.pdf"  # the file name cannot escape its prefix
    payload = bytes(range(256)) * 40
    assert store.exists(key) is False
    store.put(key, payload)
    assert store.exists(key) is True and store.get(key) == payload
    store.put(key, b"second version")
    assert store.get(key) == b"second version"
    store.delete(key)
    assert store.exists(key) is False
    store.delete(key)  # deleting what is not there is not an error
    again = S3Storage(endpoint_url=s3_endpoint, bucket=BUCKET, region="us-east-1", **KEYS)  # an existing bucket is reused
    again.put(key, b"x")
    assert store.get(key) == b"x"


def test_documents_are_stored_in_s3_when_configured(client, s3_endpoint, monkeypatch):
    """The whole upload path with the S3 backend selected by configuration, as an operator would do it."""
    for name, value in {"KAIZEVAL_STORAGE_BACKEND": "s3", "KAIZEVAL_S3_ENDPOINT_URL": s3_endpoint,
                        "KAIZEVAL_S3_BUCKET": "kaizeval-docs", "KAIZEVAL_S3_ACCESS_KEY": KEYS["access_key"],
                        "KAIZEVAL_S3_SECRET_KEY": KEYS["secret_key"]}.items():
        monkeypatch.setenv(name, value)
    reset_settings()
    storage_module.reset_storage()
    try:
        s = new_user(client)
        source = sorted(KB_DIR.glob("*.md"))[0]
        document = s.post("/ws/documents", files={"file": (source.name, source.read_bytes(), "text/markdown")}, data={"kind": "kb"})
        assert document["status"] == "parsed"
        listed = _client(s3_endpoint).list_objects_v2(Bucket="kaizeval-docs", Prefix=f"ws/{s.ws}/")["Contents"]
        assert len(listed) == 1 and listed[0]["Key"].endswith(source.name)
        stored = _client(s3_endpoint).get_object(Bucket="kaizeval-docs", Key=listed[0]["Key"])["Body"].read()
        assert stored == source.read_bytes()
        # another workspace's upload lands under its own prefix
        other = new_user(client)
        other.post("/ws/documents", files={"file": (source.name, source.read_bytes(), "text/markdown")}, data={"kind": "kb"})
        keys = [o["Key"] for o in _client(s3_endpoint).list_objects_v2(Bucket="kaizeval-docs")["Contents"]]
        assert len(keys) == 2 and sum(k.startswith(f"ws/{s.ws}/") for k in keys) == 1
    finally:
        monkeypatch.undo()
        reset_settings()
        storage_module.reset_storage()
