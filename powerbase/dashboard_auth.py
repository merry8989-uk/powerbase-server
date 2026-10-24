"""Dashboard authentication.

One admin password, one HMAC-signed cookie. No user table, no session store,
no dependency — matching the Phase 0 decision that a single-admin dashboard is
the right default for a self-hosted tool.

The signing key is derived from the admin password, so rotating the password
invalidates every existing session, which is the behaviour you want.

Kept deliberately free of FastAPI so it can be unit-tested directly.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time

#: The break-glass cookie, signed with a key derived from the admin password.
COOKIE_NAME = "pb_session"

#: A user's session token, hashed in `user_sessions`. Separate from the cookie
#: above because the two carry different things: one proves "the person who
#: knows the admin password", the other identifies a user and carries their role.
USER_COOKIE = "pb_user"


def derive_secret(admin_password: str) -> str:
    """Session signing key, derived from the password (rotating it logs everyone out)."""
    return hashlib.sha256(f"powerbase-session:{admin_password}".encode("utf-8")).hexdigest()


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_cookie(secret: str, ttl_seconds: int, now: float | None = None) -> str:
    expiry = int((now if now is not None else time.time()) + ttl_seconds)
    payload = str(expiry)
    return f"{payload}.{_sign(secret, payload)}"


def verify_cookie(secret: str, cookie: str | None, now: float | None = None) -> bool:
    if not cookie or "." not in cookie:
        return False
    payload, _, signature = cookie.rpartition(".")
    if not payload or not signature:
        return False
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return False
    try:
        expiry = int(payload)
    except ValueError:
        return False
    return expiry > (now if now is not None else time.time())


def check_password(configured: str, supplied: str) -> bool:
    return hmac.compare_digest(configured, supplied)


class LoginThrottle:
    """In-memory brute-force guard: N failures per window, per client key.

    Single-process and best-effort by design — the same trade-off as the API
    rate limiter. It raises the cost of guessing without pretending to be a
    WAF.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        attempts = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = attempts
        return attempts

    def is_blocked(self, key: str, now: float | None = None) -> bool:
        moment = now if now is not None else time.time()
        with self._lock:
            return len(self._prune(key, moment)) >= self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        moment = now if now is not None else time.time()
        with self._lock:
            attempts = self._prune(key, moment)
            attempts.append(moment)
            self._failures[key] = attempts

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def retry_after(self, key: str, now: float | None = None) -> int:
        moment = now if now is not None else time.time()
        with self._lock:
            attempts = self._prune(key, moment)
            if not attempts:
                return 0
            return max(1, int(self.window_seconds - (moment - attempts[0])))
