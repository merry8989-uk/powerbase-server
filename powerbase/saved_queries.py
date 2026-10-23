"""Saved log-explorer queries.

A saved search stores a *validated* query definition, not SQL. Validation runs
on the way in, so a saved search can never hold something the live query path
would refuse to run — and it runs again on the way out, so a definition written
by an older version cannot become a way around the allowlist.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .db import Database
from .logquery import QueryError, parse_filters, parse_sort
from .migrations import utcnow_iso

MAX_NAME = 120


def normalise_definition(spec: Any) -> dict:
    """Validate and canonicalise a query definition.

    Raises :class:`QueryError` for anything the query layer would refuse, which
    is the whole point: the console cannot save a search that will not run.
    """
    if spec in (None, {}):
        return {"filters": [], "sort": {"field": "event_time", "direction": "desc"}}
    if not isinstance(spec, dict):
        raise QueryError("a query definition must be an object")

    unknown = set(spec) - {"filters", "sort", "q", "bucket"}
    if unknown:
        raise QueryError(f"unknown query definition keys: {sorted(unknown)}")

    filters = parse_filters(spec.get("filters"))
    sort_field, direction = parse_sort(spec.get("sort"))

    q = spec.get("q")
    if q is not None:
        if not isinstance(q, str):
            raise QueryError("'q' must be a string")
        q = q[:256]

    bucket = spec.get("bucket", "hour")
    if bucket not in {"hour", "day"}:
        raise QueryError("'bucket' must be 'hour' or 'day'")

    # Store the canonical form: the typed filter values, so a definition means
    # the same thing every time it is read back.
    return {
        "filters": [
            {"field": item.field, "operator": item.op, "value": item.value}
            for item in filters
        ],
        "sort": {"field": sort_field, "direction": direction},
        "q": q,
        "bucket": bucket,
    }


def load_definition(raw: str | None) -> dict:
    """Read a stored definition back, re-validating it."""
    if not raw:
        return normalise_definition(None)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return normalise_definition(None)
    try:
        return normalise_definition(parsed)
    except QueryError:
        # A definition that no longer validates is not allowed to break the page;
        # it degrades to "no filters" and the name is still listed.
        return normalise_definition(None)


def create_saved_query(
    db: Database,
    app_id: str,
    name: str,
    definition: Any,
    created_by: str | None = None,
) -> dict:
    name = (name or "").strip()
    if not 1 <= len(name) <= MAX_NAME:
        raise QueryError(f"a name must be 1 to {MAX_NAME} characters")
    if db.one("SELECT id FROM apps WHERE id = ?", (app_id,)) is None:
        raise QueryError(f"unknown app: {app_id}")

    canonical = normalise_definition(definition)
    now = utcnow_iso()
    query_id = uuid.uuid4().hex

    with db.write() as conn:
        existing = conn.execute(
            "SELECT id FROM saved_queries WHERE app_id = ? AND name = ?", (app_id, name)
        ).fetchone()
        if existing is not None:
            # Saving over an existing name replaces it, which is what a person
            # expects from "save".
            conn.execute(
                "UPDATE saved_queries SET query_definition = ?, updated_at = ? WHERE id = ?",
                (json.dumps(canonical), now, existing["id"]),
            )
            query_id = existing["id"]
        else:
            conn.execute(
                """
                INSERT INTO saved_queries
                    (id, app_id, name, query_definition, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_id,
                    app_id,
                    name,
                    json.dumps(canonical),
                    created_by,
                    now,
                    now,
                ),
            )

    return get_saved_query(db, query_id)  # type: ignore[return-value]


def list_saved_queries(db: Database, app_id: str) -> list[dict]:
    rows = db.query(
        """
        SELECT id, app_id, name, query_definition, created_by, created_at, updated_at
        FROM saved_queries WHERE app_id = ? ORDER BY name
        """,
        (app_id,),
    )
    return [_row(row) for row in rows]


def get_saved_query(db: Database, query_id: str) -> dict | None:
    row = db.one(
        """
        SELECT id, app_id, name, query_definition, created_by, created_at, updated_at
        FROM saved_queries WHERE id = ?
        """,
        (query_id,),
    )
    return _row(row) if row is not None else None


def delete_saved_query(db: Database, query_id: str) -> bool:
    with db.write() as conn:
        cur = conn.execute("DELETE FROM saved_queries WHERE id = ?", (query_id,))
        return cur.rowcount == 1


def _row(row) -> dict:
    out = dict(row)
    out["definition"] = load_definition(out.pop("query_definition", None))
    return out
