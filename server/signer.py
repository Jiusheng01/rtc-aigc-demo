# SPDX-license-identifier: BSD-3-Clause
"""Minimal Volcengine OpenAPI signer compatible with @volcengine/openapi Signer."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

ALGORITHM = "HMAC-SHA256"
UNSIGNABLE_HEADERS = {
    "authorization",
    "content-type",
    "content-length",
    "user-agent",
    "presigned-expires",
    "expect",
}


def _hmac(key: bytes | str, value: str) -> bytes:
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def _sha256(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def _uri_escape(value: Any) -> str:
    return quote(str(value), safe="-_.~")


def query_params_to_string(params: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        escaped_key = _uri_escape(key)
        if isinstance(value, (list, tuple)):
            values = sorted(_uri_escape(item) for item in value)
            parts.extend(f"{escaped_key}={item}" for item in values)
        else:
            parts.append(f"{escaped_key}={_uri_escape(value)}")
    return "&".join(parts)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sign_headers(
    *,
    method: str,
    region: str,
    service: str,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    body: Any,
    access_key_id: str,
    secret_key: str,
    now: datetime | None = None,
) -> dict[str, str]:
    signed = dict(headers)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    datetime_text = now.strftime("%Y%m%dT%H%M%SZ")
    signed["X-Date"] = datetime_text

    body_text = body if isinstance(body, str) else compact_json(body)
    if body_text:
        signed["X-Content-Sha256"] = _sha256(body_text)

    canonical_header_items: list[tuple[str, str]] = []
    for key, value in signed.items():
        lowered = key.lower()
        if lowered in UNSIGNABLE_HEADERS:
            continue
        normalized = re.sub(r"\s+", " ", str(value)).strip()
        canonical_header_items.append((lowered, normalized))
    canonical_header_items.sort(key=lambda item: item[0])
    canonical_headers = "\n".join(f"{key}:{value}" for key, value in canonical_header_items)
    signed_header_names = ";".join(key for key, _ in canonical_header_items)

    sorted_params = dict(sorted((key, value) for key, value in params.items() if value is not None))
    canonical_query = query_params_to_string(sorted_params)
    body_hash = signed.get("X-Content-Sha256") or _sha256("")
    canonical_request = "\n".join(
        [
            method.upper(),
            "/",
            canonical_query,
            f"{canonical_headers}\n",
            signed_header_names,
            body_hash,
        ]
    )

    date = datetime_text[:8]
    scope = f"{date}/{region}/{service}/request"
    string_to_sign = "\n".join([ALGORITHM, datetime_text, scope, _sha256(canonical_request)])
    signing_key = _hmac(secret_key, date)
    signing_key = _hmac(signing_key, region)
    signing_key = _hmac(signing_key, service)
    signing_key = _hmac(signing_key, "request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    signed["Authorization"] = (
        f"{ALGORITHM} Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    return signed
