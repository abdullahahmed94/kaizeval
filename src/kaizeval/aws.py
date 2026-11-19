"""Minimal AWS client pieces: credentials, Signature Version 4 and the event-stream framing.

Kaizeval calls AWS over plain HTTPS through its own guarded client, so the network policy of the
workspace applies to these calls exactly as it does to every other endpoint. No AWS SDK is needed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")


@dataclass(frozen=True)
class AwsCredentials:
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    bearer_token: str = ""  # an Amazon Bedrock API key, used instead of a signature

    @property
    def usable(self) -> bool:
        return bool(self.bearer_token or (self.access_key_id and self.secret_access_key))


def parse_credentials(secret: str | None, *, access_key_id: str = "", env: dict[str, str] | None = None) -> AwsCredentials:
    """Read AWS credentials from the stored secret, falling back to the standard environment variables.

    Accepted forms of the stored secret:

    * a JSON object with ``access_key_id``, ``secret_access_key`` and optionally ``session_token``
    * ``ACCESS_KEY_ID:SECRET_ACCESS_KEY`` or ``ACCESS_KEY_ID:SECRET_ACCESS_KEY:SESSION_TOKEN``
    * the secret access key alone, when the access key id is given in the configuration
    * anything else is treated as an Amazon Bedrock API key (bearer token)
    """
    env = dict(os.environ) if env is None else env
    value = (secret or "").strip()
    if value.startswith("{"):
        try:
            data = {str(k).lower().replace("_", ""): v for k, v in json.loads(value).items()}
        except (ValueError, AttributeError):
            data = {}
        return AwsCredentials(str(data.get("accesskeyid") or access_key_id), str(data.get("secretaccesskey") or ""),
                              str(data.get("sessiontoken") or ""))
    if value and access_key_id:
        return AwsCredentials(access_key_id, value)
    parts = value.split(":")
    if len(parts) in (2, 3) and re.fullmatch(r"[A-Z0-9]{16,128}", parts[0]):
        return AwsCredentials(parts[0], parts[1], parts[2] if len(parts) == 3 else "")
    if value:
        return AwsCredentials(bearer_token=value)
    if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
        return AwsCredentials(env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"], env.get("AWS_SESSION_TOKEN", ""))
    if env.get("AWS_BEARER_TOKEN_BEDROCK"):
        return AwsCredentials(bearer_token=env["AWS_BEARER_TOKEN_BEDROCK"])
    return AwsCredentials()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def canonical_path(path: str) -> str:
    """The path as it must be sent and signed: every segment percent-encoded exactly once."""
    return "/".join(quote(segment, safe="-_.~") for segment in path.split("/"))


def sign(method: str, url: str, body: bytes, credentials: AwsCredentials, *, region: str, service: str,
         headers: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, str]:
    """Return the headers that authenticate one request (Signature Version 4, or a bearer API key).

    ``url`` must already carry the encoded path (see :func:`canonical_path`). Header names in the
    result are lower-case and include the caller's ``headers``.
    """
    out = {k.lower(): v.strip() for k, v in (headers or {}).items()}
    if credentials.bearer_token:
        out["authorization"] = f"Bearer {credentials.bearer_token}"
        return out
    parts = urlsplit(url)
    now = now or datetime.now(timezone.utc)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    out["host"] = parts.netloc
    out["x-amz-date"] = amz_date
    if credentials.session_token:
        out["x-amz-security-token"] = credentials.session_token
    payload_hash = hashlib.sha256(body).hexdigest()
    # The path in the URL is already encoded once; services other than S3 sign it encoded again.
    signed_path = quote(parts.path or "/", safe="/-_.~")
    query = "&".join(sorted(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
                            for k, v in parse_qsl(parts.query, keep_blank_values=True)))
    names = sorted(out)
    canonical = "\n".join([method.upper(), signed_path, query, "".join(f"{n}:{out[n]}\n" for n in names),
                           ";".join(names), payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + credentials.secret_access_key).encode(), date), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["authorization"] = (f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
                            f"SignedHeaders={';'.join(names)}, Signature={signature}")
    del out["host"]  # the HTTP client sets it from the URL; it only had to be part of the signature
    return out


class EventStreamError(ValueError):
    pass


def _headers(block: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pos = 0
    fixed = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
    while pos < len(block):
        name_len = block[pos]
        name = block[pos + 1:pos + 1 + name_len].decode()
        pos += 1 + name_len
        kind = block[pos]
        pos += 1
        if kind in (6, 7):
            (size,) = struct.unpack(">H", block[pos:pos + 2])
            raw = block[pos + 2:pos + 2 + size]
            pos += 2 + size
            out[name] = raw.decode() if kind == 7 else raw
        elif kind in fixed:
            out[name] = True if kind == 0 else False if kind == 1 else block[pos:pos + fixed[kind]]
            pos += fixed[kind]
        else:
            raise EventStreamError(f"unknown header type {kind}")
    return out


def decode_event_stream(data: bytes) -> list[tuple[dict[str, Any], bytes]]:
    """Split an ``application/vnd.amazon.eventstream`` body into ``(headers, payload)`` messages."""
    messages = []
    pos = 0
    while pos < len(data):
        if len(data) - pos < 16:
            raise EventStreamError("truncated message prelude")
        total, header_len, prelude_crc = struct.unpack(">III", data[pos:pos + 12])
        if binascii.crc32(data[pos:pos + 8]) != prelude_crc:
            raise EventStreamError("prelude checksum mismatch")
        end = pos + total
        if total < 16 or end > len(data):
            raise EventStreamError("truncated message")
        (message_crc,) = struct.unpack(">I", data[end - 4:end])
        if binascii.crc32(data[pos:end - 4]) != message_crc:
            raise EventStreamError("message checksum mismatch")
        headers = _headers(data[pos + 12:pos + 12 + header_len])
        messages.append((headers, data[pos + 12 + header_len:end - 4]))
        pos = end
    return messages


def encode_event(headers: dict[str, str], payload: bytes) -> bytes:
    """Build one event-stream message with string headers (used by tests and local stand-ins)."""
    block = b"".join(bytes([len(k)]) + k.encode() + b"\x07" + struct.pack(">H", len(v.encode())) + v.encode()
                     for k, v in headers.items())
    total = 16 + len(block) + len(payload)
    prelude = struct.pack(">II", total, len(block))
    prelude += struct.pack(">I", binascii.crc32(prelude))
    body = prelude + block + payload
    return body + struct.pack(">I", binascii.crc32(body))


def b64(value: str) -> bytes:
    return base64.b64decode(value)
