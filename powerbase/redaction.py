"""PII redaction at the edge.

Applied to every free-text field (log messages, stacktraces, event properties,
crash messages) *before* the row is written, so personal data never reaches
disk. Patterns are deliberately conservative — over-redacting a diagnostic
string is far cheaper than storing a phone number.

Indian identifiers (mobile numbers, Aadhaar, PAN) are covered explicitly
alongside the generic email / card / token patterns.
"""

from __future__ import annotations

import re
from typing import Any

# Order is load-bearing, not cosmetic.
#
# These patterns overlap. A 16-digit card contains a 12-digit run that looks
# exactly like an Aadhaar, and "+91" followed by ten digits does too. Whichever
# rule runs first wins, so the list is ordered most-specific-and-longest first:
# a card is claimed before its inner twelve digits can be mistaken for an
# Aadhaar, and a +91 number is claimed before the same twelve digits can be.
# The SDK mirrors this order exactly (core/.../Redaction.kt).
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("BEARER", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{4,}")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+91[\-\s]?)?[6-9]\d{9}(?!\d)")),
    ("AADHAAR", re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")),
    ("SECRET", re.compile(r"(?i)\b(?:token|api[_-]?key|password|passwd|secret)\b\s*[:=]\s*\S+")),
]

_REDACTED = "[REDACTED_{kind}]"


def redact_text(value: str) -> str:
    if not value:
        return value
    out = value
    for kind, pattern in PATTERNS:
        out = pattern.sub(_REDACTED.format(kind=kind), out)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside dicts / lists / scalars."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value
