"""Attribute sanitisation: the rule that a field naming a credential is never stored."""

import pytest

from powerbase.attributes import (
    MAX_DEPTH,
    MAX_KEYS,
    is_sensitive_name,
    sanitize_attributes,
)


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "Password",
        "PASSWORD",
        "passwd",
        "pwd",
        "user_password",
        "user.password",
        "password_hash",
        "secret",
        "client_secret",
        "apiKey",
        "api_key",
        "api-key",
        "x-api-key",
        "API_KEY",
        "accessToken",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "Authorization",
        "cookie",
        "set-cookie",
        "private_key",
        "database_url",
        "connectionString",
        "refreshToken",
        "signed_url",
        "card_number",
        "cvv",
    ],
)
def test_credential_shaped_names_are_caught(name):
    assert is_sensitive_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "region",
        "retry",
        "user_id",
        "sort_key",
        "keyboard",
        "monkey",
        "duration_ms",
        "status_code",
        "route_template",
        "author",          # contains "auth" but is not a credential
        "session_count",   # contains "session" but is not a credential
        "traces",
    ],
)
def test_ordinary_names_are_not_caught(name):
    assert is_sensitive_name(name) is False


def test_sensitive_keys_are_dropped_and_counted():
    clean, dropped = sanitize_attributes(
        {
            "region": "ap-south-1",
            "password": "hunter2",
            "apiKey": "sk-live-abcdef",
            "nested": {"ok": 1, "access_token": "t"},
        }
    )
    assert clean == {"region": "ap-south-1", "nested": {"ok": 1}}
    # password, apiKey and the nested access_token.
    assert dropped == 3
    assert "hunter2" not in repr(clean)
    assert "sk-live-abcdef" not in repr(clean)


def test_deep_nesting_is_bounded():
    node = {"leaf": 1}
    for _ in range(MAX_DEPTH + 3):
        node = {"next": node}
    clean, dropped = sanitize_attributes(node)
    assert dropped >= 1
    # Whatever survives, it is shallow enough to serialise cheaply.
    depth, cursor = 0, clean
    while isinstance(cursor, dict) and "next" in cursor:
        cursor = cursor["next"]
        depth += 1
    assert depth <= MAX_DEPTH


def test_wide_objects_are_capped():
    clean, dropped = sanitize_attributes({f"k{i}": i for i in range(MAX_KEYS + 20)})
    assert len(clean) == MAX_KEYS
    assert dropped == 20


def test_long_values_are_truncated_not_dropped():
    clean, dropped = sanitize_attributes({"blob": "x" * 10_000})
    assert dropped == 0
    assert len(clean["blob"]) == 2048


def test_scalar_types_survive():
    clean, dropped = sanitize_attributes(
        {"s": "a", "i": 1, "f": 1.5, "b": True, "n": None, "list": [1, "two"]}
    )
    assert clean == {"s": "a", "i": 1, "f": 1.5, "b": True, "n": None, "list": [1, "two"]}
    assert dropped == 0


def test_a_none_value_is_not_confused_with_a_dropped_key():
    # A legitimate None must survive; the drop sentinel must not leak out.
    clean, _ = sanitize_attributes({"a": None, "password": "x"})
    assert clean == {"a": None}
    assert "password" not in clean


def test_non_dict_input_is_refused():
    clean, dropped = sanitize_attributes(["not", "a", "dict"])
    assert clean == {}
    assert dropped == 1
