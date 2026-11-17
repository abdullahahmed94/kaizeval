"""Secrets, redaction and the outbound network policy (acceptance 11)."""

from __future__ import annotations

import socket

import pytest

from kaizeval import security
from kaizeval.netguard import EgressBlocked, EgressPolicy, check_url, guarded_client

OFFLINE = EgressPolicy(offline=True)
CONNECTED = EgressPolicy(offline=False, allow_public=False)
CONNECTED_PUBLIC = EgressPolicy(offline=False, allow_public=True)


def test_password_hash_roundtrip():
    stored = security.hash_password("a long passphrase")
    assert "a long passphrase" not in stored
    assert security.verify_password("a long passphrase", stored)
    assert not security.verify_password("wrong", stored)
    assert not security.verify_password("x", "garbage")


def test_secret_encryption_roundtrip():
    cipher = security.encrypt_secret("sk-super-secret-value")
    assert "sk-super-secret-value" not in cipher
    assert security.decrypt_secret(cipher) == "sk-super-secret-value"


def test_access_token_roundtrip():
    assert security.decode_access_token(security.create_access_token("user-1")) == "user-1"
    assert security.decode_access_token("not-a-token") is None


@pytest.mark.parametrize("text", [
    "Authorization: Bearer abcdef1234567890", "api_key=sk-abcdefgh12345678", 'password": "hunter2hunter2"',
    "token sk-proj-AAAAAAAAAAAA", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk",
])
def test_redaction_masks_credentials(text):
    redacted = security.redact(text)
    assert "[REDACTED]" in redacted
    for secret in ("abcdef1234567890", "sk-abcdefgh12345678", "hunter2hunter2", "sk-proj-AAAAAAAAAAAA"):
        assert secret not in redacted


def test_offline_blocks_public_names_without_dns(monkeypatch):
    def no_dns(*args, **kwargs):
        raise AssertionError("offline mode must not resolve external names")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    for url in ("https://api.openai.com/v1", "https://example.com/chat", "http://models.vendor.io:8080/v1"):
        with pytest.raises(EgressBlocked):
            check_url(url, OFFLINE)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/v1", "http://localhost:8000/chat", "http://10.1.2.3/v1", "http://192.168.1.20:8080",
    "http://ollama:11434/v1", "http://[::1]:9000/v1",
])
def test_offline_allows_local_destinations(url):
    assert check_url(url, OFFLINE)


def test_offline_blocks_public_ip_literal():
    with pytest.raises(EgressBlocked):
        check_url("http://8.8.8.8/v1", OFFLINE)


@pytest.mark.parametrize("policy", [OFFLINE, CONNECTED, CONNECTED_PUBLIC])
def test_metadata_and_link_local_always_blocked(policy):
    with pytest.raises(EgressBlocked):
        check_url("http://169.254.169.254/latest/meta-data", policy)


def test_connected_needs_explicit_opt_in_for_public():
    with pytest.raises(EgressBlocked):
        check_url("http://8.8.8.8/v1", CONNECTED)
    assert check_url("http://8.8.8.8/v1", CONNECTED_PUBLIC) == "public"


def test_allowlist_marks_dotted_host_as_internal():
    policy = EgressPolicy(offline=True, allowlist=frozenset({"llm.corp.internal"}))
    assert check_url("http://llm.corp.internal:8000/v1", policy) == "allowlisted"
    with pytest.raises(EgressBlocked):
        check_url("http://other.corp.internal/v1", policy)


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/x", "file:///etc/passwd", "http:///nohost"])
def test_bad_urls_rejected(url):
    with pytest.raises(EgressBlocked):
        check_url(url, OFFLINE)


def test_guarded_client_blocks_at_request_time():
    with guarded_client(OFFLINE) as client, pytest.raises(EgressBlocked):
        client.get("https://example.com/")
    assert guarded_client(OFFLINE).follow_redirects is False
