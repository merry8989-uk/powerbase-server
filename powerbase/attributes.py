"""Attribute sanitisation for log events.

Two separate jobs, and they are deliberately not the same mechanism:

1.  **Never store a value whose key names a secret.** Redaction scrubs values
    that *look* like credentials; this drops the whole key when its *name* says
    "credential". A field called ``password`` is a credential whatever it
    contains, and a field called ``password_hint`` is not worth the risk.

2.  **Bound what a client can make us store.** Key count, nesting depth and
    value length are all capped, so a single event cannot balloon a row.

The denylist matches on whole names and on the pieces a name is built from, so
``apiKey``, ``api_key`` and ``x-api-key`` are all caught while ``sort_key`` and
``keyboard`` are not.
"""

from __future__ import annotations

import re
from typing import Any

#: Names that mean "this is a credential". Matched case-insensitively.
SENSITIVE_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "clientsecret",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "bearertoken",
        "csrftoken",
        "authorization",
        "authheader",
        "cookie",
        "cookies",
        "setcookie",
        "sessionkey",
        "sessiontoken",
        "privatekey",
        "publickey",
        "apikey",
        "apisecret",
        "secretkey",
        "signingkey",
        "credential",
        "credentials",
        "databaseurl",
        "dburl",
        "connectionstring",
        "dsn",
        "signedurl",
        "presignedurl",
        "cardnumber",
        "creditcard",
        "cvv",
        "cvc",
        "ssn",
        "aadhaar",
        "otp",
    }
)

#: Longest value we keep from an attribute. Anything beyond is truncated rather
#: than dropped, so a long stack string is still useful.
MAX_VALUE_CHARS = 2048

#: How many keys one attribute object may hold, and how deep it may nest.
MAX_KEYS = 64
MAX_DEPTH = 4

_SPLIT = re.compile(r"[^a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: Sentinel meaning "drop this entry", so a legitimate ``None`` survives.
_DROP = object()


def is_sensitive_name(key: str) -> bool:
    """True when a key names a credential and must never be stored."""
    if not key:
        return False
    if key.strip().lower() in SENSITIVE_NAMES:
        return True
    # camelCase -> "api key"; separators -> "api key"; then test each piece and
    # the rejoined form, which is what catches `apiKey`, `api_key`, `api-key`.
    normalised = _CAMEL.sub(" ", key).lower()
    pieces = [p for p in _SPLIT.split(normalised) if p]
    if any(piece in SENSITIVE_NAMES for piece in pieces):
        return True
    joined = "".join(pieces)
    if joined in SENSITIVE_NAMES:
        return True
    # Prefixed compounds: `x-api-key` -> "xapikey", `myAccessToken` -> "myaccesstoken".
    # A suffix test is safe here because a bare "key" is deliberately not on the
    # list, so `sort_key` and `monkey` still pass.
    return any(joined.endswith(name) for name in SENSITIVE_NAMES)


def sanitize_attributes(
    raw: dict[str, Any],
    *,
    max_keys: int = MAX_KEYS,
    max_depth: int = MAX_DEPTH,
) -> tuple[dict[str, Any], int]:
    """Return ``(clean_attributes, dropped_count)``.

    ``dropped_count`` is reported back to the client, so an integrator can see
    that something they sent was refused rather than silently losing it.
    """
    dropped = 0

    def walk(value: Any, depth: int) -> Any:
        nonlocal dropped

        if isinstance(value, dict):
            if depth >= max_depth:
                dropped += 1
                return _DROP
            out: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or is_sensitive_name(key):
                    dropped += 1
                    continue
                if len(out) >= max_keys:
                    dropped += 1
                    continue
                cleaned = walk(item, depth + 1)
                if cleaned is not _DROP:
                    out[key] = cleaned
            return out

        if isinstance(value, (list, tuple)):
            items = []
            for index, item in enumerate(value):
                if index >= max_keys:
                    dropped += 1
                    break
                cleaned = walk(item, depth + 1)
                if cleaned is not _DROP:
                    items.append(cleaned)
            return items

        if isinstance(value, str):
            return value[:MAX_VALUE_CHARS]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value
        # Anything else (dates, custom objects) is coerced rather than dropped.
        return str(value)[:MAX_VALUE_CHARS]

    if not isinstance(raw, dict):
        return {}, 1

    result = walk(raw, 0)
    if result is _DROP or not isinstance(result, dict):
        return {}, dropped + 1
    return result, dropped
