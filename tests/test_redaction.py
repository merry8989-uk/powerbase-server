"""Redaction rules, including the precedence cases that are easy to get wrong.

These overlap by nature: a card number contains a 12-digit run that also looks
like an Aadhaar, and a `+91` mobile number contains one too. If the Aadhaar rule
ran first it would swallow both, so the ordering in `redaction.py` is a
correctness property, not a style choice — these tests pin it down.
"""

from powerbase.redaction import redact_text, redact_value


def test_email():
    assert redact_text("mail amit@example.com here") == "mail [REDACTED_EMAIL] here"


def test_indian_mobile():
    assert redact_text("call 9812345678 now") == "call [REDACTED_PHONE] now"
    assert redact_text("call +919812345678 now") == "call [REDACTED_PHONE] now"


def test_aadhaar_spaced():
    assert "[REDACTED_AADHAAR]" in redact_text("id 1234 5678 9012 ok")


def test_pan():
    assert "[REDACTED_PAN]" in redact_text("pan ABCDE1234F ok")


def test_card_beats_aadhaar():
    out = redact_text("card 4111 1111 1111 1111 ok")
    assert "[REDACTED_CARD]" in out
    assert "AADHAAR" not in out


def test_phone_beats_aadhaar():
    out = redact_text("call +919812345678 now")
    assert "[REDACTED_PHONE]" in out
    assert "AADHAAR" not in out


def test_bearer_header():
    out = redact_text("Authorization: Bearer abc123def456")
    assert "[REDACTED_BEARER]" in out
    assert "abc123def456" not in out


def test_jwt():
    out = redact_text("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij")
    assert "[REDACTED_JWT]" in out


def test_secret_key_value_pair():
    assert "[REDACTED_SECRET]" in redact_text("token=abcdef123")


def test_nested_values():
    out = redact_value({"user": {"email": "a@b.com"}, "list": ["call 9812345678"], "n": 7})
    assert out["user"]["email"] == "[REDACTED_EMAIL]"
    assert out["list"] == ["call [REDACTED_PHONE]"]
    assert out["n"] == 7


def test_plain_text_untouched():
    assert redact_text("nothing sensitive here") == "nothing sensitive here"
