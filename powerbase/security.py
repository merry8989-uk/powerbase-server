"""API key generation and verification.

Keys are never stored in plaintext. A token has the shape::

    pb_<key_id>_<secret>

``key_id`` is a public, indexed lookup handle (so we can find the row without
scanning), and ``secret`` is the private half, stored only as an Argon2id hash.
A leaked database therefore yields no usable keys, and a leaked token yields
only the ability to *write* if its scope is ``ingest``.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

TOKEN_PREFIX = "pb"

_hasher = PasswordHasher()


@dataclass(frozen=True)
class TokenParts:
    key_id: str
    secret: str


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_secret(stored_hash: str, secret: str) -> bool:
    try:
        return _hasher.verify(stored_hash, secret)
    except Argon2Error:
        return False
    except Exception:  # malformed hash in the row
        return False


def new_key_id() -> str:
    return secrets.token_hex(8)


def new_secret() -> str:
    # Hex keeps the token free of '_' so parsing is unambiguous.
    return secrets.token_hex(32)


def format_token(key_id: str, secret: str) -> str:
    return f"{TOKEN_PREFIX}_{key_id}_{secret}"


class VerificationCache:
    """Remembers recent successful verifications.

    Argon2id is deliberately expensive — around 370 ms per verification on a
    small box. That is the right cost for a login, and completely wrong for an
    ingest endpoint that authenticates on *every* request: at four concurrent
    clients it turned a 14 ms batch write into a multi-second request, which is
    a denial of service you inflict on yourself.

    So the hash at rest stays Argon2id, and a successful verification is
    remembered for a short while. Two properties keep this safe:

    * only *successful* verifications are cached, keyed by the SHA-256 of the
      presented token — the plaintext secret is never stored here;
    * the key row is still read and its `revoked_at` and `scope` re-checked on
      every single request, so revoking a key takes effect immediately and is
      not delayed by the cache.
    """

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 4096):
        self.ttl = float(ttl_seconds)
        self.max_entries = max_entries
        self._entries: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    @staticmethod
    def _fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def get(self, key_id: str, token: str) -> bool:
        if not self.enabled:
            return False
        cache_key = (key_id, self._fingerprint(token))
        now = time.monotonic()
        with self._lock:
            expiry = self._entries.get(cache_key)
            if expiry is None:
                return False
            if expiry <= now:
                self._entries.pop(cache_key, None)
                return False
            return True

    def put(self, key_id: str, token: str) -> None:
        if not self.enabled:
            return
        cache_key = (key_id, self._fingerprint(token))
        with self._lock:
            # A crude bound is fine: correctness never depends on an entry being
            # present, only on a miss falling through to a real verification.
            if len(self._entries) >= self.max_entries:
                self._entries.clear()
            self._entries[cache_key] = time.monotonic() + self.ttl

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


def parse_token(token: str) -> TokenParts | None:
    if not token:
        return None
    parts = token.split("_")
    if len(parts) != 3:
        return None
    prefix, key_id, secret = parts
    if prefix != TOKEN_PREFIX or not key_id or not secret:
        return None
    return TokenParts(key_id=key_id, secret=secret)
