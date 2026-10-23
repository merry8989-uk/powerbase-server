"""Powerbase is free forever. This test is what keeps that true.

Both planning documents make it a non-negotiable — "no billing, paid tiers,
usage paywalls, or subscription checkout" — and one of them lists it as the
first item in its definition of done. A promise in a README is worth what the
person who reads it believes; a promise with a test attached fails the build the
day somebody adds a paywall.

Two things are checked, and they are different in kind:

1. **Nothing monetisation-shaped exists.** A scan for the vocabulary of billing.
   Crude on purpose: it catches a dependency, a column, a route, a config key,
   or a stray import, without needing to understand any of them.
2. **Nothing is gated on a plan.** The permission table is the only way a route
   can be refused, so if no permission is plan-shaped, no feature is plan-gated.

The words are chosen narrowly. `quota`, `limit` and `tier` are *not* banned:
Powerbase has rate limits and retention limits, and those are abuse prevention,
not a paywall. Banning them would be banning the wrong thing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from powerbase import team

TREE = Path(__file__).resolve().parent.parent

#: Vocabulary that only appears if somebody is selling something. These are
#: flagged anywhere they appear.
BILLING_TERMS = (
    "stripe",
    "razorpay",
    "paypal",
    "braintree",
    "paddle",
    "billing",
    "paywall",
    "subscription_plan",
    "plan_tier",
    "pricing_page",
    "upgrade_plan",
    "upgrade_to_pro",
    "seat_count",
    "trial_expired",
    "credit_card",
    "payment_method",
)

#: Words that are *also* ordinary domain nouns, so they are only a problem
#: alongside a payment processor. `checkout` is a shopping flow — this project's
#: own sample data is an e-commerce app — and an `invoice` can be a domain
#: object. Flagging them on sight would make this test cry wolf, and a test that
#: cries wolf gets deleted, which is the one outcome that must not happen.
AMBIGUOUS_TERMS = ("checkout", "invoice", "subscription", "price", "plan")
#: Deliberately does NOT include the bare word "payment": a sample crash
#: trace from a shopping app says `PaymentViewModel`, and that is ordinary
#: domain vocabulary, not a billing integration.
PAYMENT_CONTEXT = ("stripe", "razorpay", "paypal", "braintree", "paddle", "billing")

SCANNED_SUFFIXES = (".py", ".sql", ".ts", ".js", ".mjs", ".json", ".toml", ".yml", ".yaml")

#: This file names the terms, so it cannot scan itself. The lockfile is excluded
#: because it lists every transitive dependency by name.
SKIP_FILES = {
    "tests/test_free_forever.py",
    "packages/sdk-ts/package-lock.json",
}
SKIP_DIRS = {"node_modules", "__pycache__", ".pytest_cache", ".git", "dist", "backups", "exports"}


def source_files() -> list[Path]:
    found = []
    for path in TREE.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        rel = path.relative_to(TREE)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if str(rel).replace("\\", "/") in SKIP_FILES:
            continue
        found.append(path)
    return found


def test_the_tree_is_actually_being_scanned():
    """A scan that silently finds nothing proves nothing."""
    files = source_files()
    assert len(files) > 100, f"only found {len(files)} files to scan"
    assert any(path.name == "api.py" for path in files)
    assert any(path.suffix == ".sql" for path in files)


def test_no_monetisation_vocabulary_exists():
    hits: list[str] = []
    for path in source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        rel = path.relative_to(TREE)
        for number, line in enumerate(text.splitlines(), 1):
            for term in BILLING_TERMS:
                if term in line:
                    hits.append(f"{rel}:{number}: {term}")
            # An ambiguous word only counts if a payment processor is on the
            # same line.
            if any(word in line for word in AMBIGUOUS_TERMS) and any(
                word in line for word in PAYMENT_CONTEXT
            ):
                hits.append(f"{rel}:{number}: {line.strip()[:90]}")

    assert hits == [], (
        "Powerbase is free forever, and this is the test that says so. "
        "Monetisation vocabulary found:\n  " + "\n  ".join(hits)
    )


def test_no_permission_is_plan_shaped():
    """The permission table is the only gate, so it is the place to check.

    If every permission is about access rather than entitlement, then no feature
    can be withheld from a self-hoster who has not paid — because there is
    nothing to pay.
    """
    forbidden = {"plan", "billing", "tier", "subscription", "paid", "premium", "pro"}
    # Whole words, not substrings: "pro" is inside "project:write", and a test
    # that flags `project:write` as a paid tier is a test nobody will keep.
    offenders = [
        permission
        for permission in team.PERMISSIONS
        if forbidden & set(re.split(r"[^a-z]+", permission.lower()))
    ]
    assert offenders == [], f"plan-shaped permissions exist: {offenders}"


def test_every_role_is_reachable_without_payment():
    """All four roles exist and none of them is an upsell."""
    assert set(team.ROLES) == {"owner", "admin", "developer", "readonly"}
    # The highest role is not a paid tier: it is the first account created, by
    # whoever runs the server.
    assert team.can("owner", "settings:write")
    assert team.can("readonly", "read")


def test_no_route_is_gated_on_a_plan():
    """Every route's permission must come from the table, and the table is clean."""
    import powerbase.api as api_module
    from powerbase import authz

    source = Path(api_module.__file__).read_text()
    # Routes name a permission; none of them names anything plan-shaped.
    named = set(re.findall(r'require\("([^"]+)"\)', source))
    named |= set(re.findall(r'require\("([^"]+)"\)', (TREE / "powerbase" / "team_api.py").read_text()))
    named |= set(
        re.findall(r'require\("([^"]+)"\)', (TREE / "powerbase" / "advanced_api.py").read_text())
    )
    assert named, "no permissions were found in the routes, which cannot be right"

    unknown = sorted(p for p in named if p not in team.PERMISSIONS)
    assert unknown == [], f"routes name permissions that are not in the table: {unknown}"


def test_the_licence_is_present_and_osi_approved():
    licence = TREE / "LICENSE"
    assert licence.exists(), "no LICENSE file"
    text = licence.read_text()
    assert "MIT License" in text or "Apache License" in text, "not a recognised OSI licence"


def test_nothing_sends_data_anywhere_by_default(env):
    """Self-hosters must be able to disable every outbound call, and by default
    the server makes none unless the operator configured one.

    The two outbound paths are alert webhooks and log drains. Both are empty
    until an operator adds them, which is what makes this true.
    """
    from powerbase import drains, notify

    assert notify.list_channels(env.db) == []
    assert drains.list_drains(env.db) == []
    assert env.settings.alert_webhook_url is None
    assert env.settings.smtp_host is None
