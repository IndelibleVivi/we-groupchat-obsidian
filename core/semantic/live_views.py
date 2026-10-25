"""Bounded metadata and exact program projection for live navigation.

No source, provider, publication or canonical write authority lives here.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .contracts import SemanticStoreError, digest, dumps

CONTRACT = "semantic-live-views-v2"
MEMBERS = "semantic-live-view-members-v2"
MAX_BYTES = 60000
PAGE_SIZE = 16


def require(value, code="semantic_live_view_binding_invalid"):
    if not value:
        raise SemanticStoreError(code)


def bounded(value):
    require(len(dumps(value).encode()) <= MAX_BYTES, "semantic_view_context_too_large")
    return value


def item_scope(row):
    require(row["identity_scope_json"] is not None, "semantic_view_context_too_large")
    return {"item_id": row["item_id"], "title": row["title"], "kind": row["kind"],
            "identity_scope": json.loads(row["identity_scope_json"]), "lifecycle": row["lifecycle"]}


def item_rows(conn, *, active=False):
    # The size check is performed by SQLite before Python receives a field.
    return conn.execute("SELECT item_id,lifecycle,content_revision, "
        "CASE WHEN length(CAST(identity_scope_json AS BLOB))+length(CAST(title AS BLOB))+length(CAST(kind AS BLOB))<=? "
        "THEN identity_scope_json END AS identity_scope_json, "
        "CASE WHEN length(CAST(title AS BLOB))<=60000 THEN title END AS title, "
        "CASE WHEN length(CAST(kind AS BLOB))<=60000 THEN kind END AS kind FROM items " +
        ("WHERE lifecycle='active' " if active else "") + "ORDER BY item_id", (MAX_BYTES,))


def view_catalog(conn):
    views, size = {}, 0
    for row in conn.execute("SELECT view_id,definition_revision,override_revision,origin, "
            "CASE WHEN length(CAST(title AS BLOB))+length(CAST(definition AS BLOB))<=? THEN title END AS title, "
            "CASE WHEN length(CAST(title AS BLOB))+length(CAST(definition AS BLOB))<=? THEN definition END AS definition "
            "FROM views ORDER BY view_id", (MAX_BYTES, MAX_BYTES)):
        require(row["definition"] is not None, "semantic_view_context_too_large")
        value = dict(row)
        size += len(dumps(value).encode())
        require(size <= MAX_BYTES, "semantic_view_context_too_large")
        views[row["view_id"]] = value
    return views


def overrides(conn, item_ids=None):
    # The global signature streams rows; a prompt receives overrides only for its page.
    sql = "SELECT view_id,item_id,CASE WHEN length(CAST(placement AS BLOB))<=60000 THEN placement END AS placement,origin,pinned,excluded FROM view_memberships WHERE (origin='user' OR pinned=1 OR excluded=1)"
    args = ()
    if item_ids is not None:
        if not item_ids:
            return iter(())
        sql += " AND item_id IN (" + ",".join("?" for _ in item_ids) + ")"
        args = tuple(item_ids)
    return conn.execute(sql + " ORDER BY view_id,item_id", args)


def catalog(conn):
    identities, item_hash, override_hash = [], hashlib.sha256(), hashlib.sha256()
    for row in item_rows(conn, active=True):
        value = item_scope(row)
        identities.append(value["item_id"])
        item_hash.update(dumps(value).encode() + b"\n")
    for row in overrides(conn):
        require(row["placement"] is not None, "semantic_view_context_too_large")
        override_hash.update(dumps(dict(row)).encode() + b"\n")
    views = view_catalog(conn)
    result = {"item_ids": identities, "item_signature": item_hash.hexdigest(),
              "override_signature": override_hash.hexdigest(), "views": views}
    result["signature"] = digest(result)
    return result


def page_state(conn, item_ids):
    identities = list(item_ids)
    require(len(identities) <= PAGE_SIZE and len(set(identities)) == len(identities))
    items, retired = {}, {}
    for identity in identities:
        row = conn.execute("SELECT item_id,lifecycle,content_revision,CASE WHEN length(CAST(title AS BLOB))<=60000 THEN title END AS title,CASE WHEN length(CAST(kind AS BLOB))<=60000 THEN kind END AS kind, "
            "CASE WHEN length(CAST(identity_scope_json AS BLOB))+length(CAST(title AS BLOB))+length(CAST(kind AS BLOB))<=? "
            "THEN identity_scope_json END AS identity_scope_json FROM items WHERE item_id=?", (MAX_BYTES, identity)).fetchone()
        require(row is not None, "semantic_view_item_missing")
        value = item_scope(row)
        if row["lifecycle"] == "active":
            items[identity] = value
        else:
            retired[identity] = {**value, "retirement_revision": row["content_revision"]}
    state = {"target_item_ids": identities, "items": items, "retired_items": retired,
             "views": view_catalog(conn), "overrides": []}
    for row in overrides(conn, identities):
        require(row["placement"] is not None, "semantic_view_context_too_large")
        state["overrides"].append(dict(row))
        bounded(state)
    return bounded(state)


def validate_plan(value, state):
    require(isinstance(value, dict) and set(value) == {"groups", "unassigned"}, "semantic_invalid_view_plan")
    require(isinstance(value["groups"], list) and isinstance(value["unassigned"], list), "semantic_invalid_view_plan")
    covered = set()
    for group in value["groups"]:
        require(isinstance(group, dict) and set(group) == {"view_id", "title", "definition", "item_ids", "reason"}, "semantic_invalid_view_plan")
        require(all(isinstance(group[k], str) and group[k].strip() for k in ("title", "definition", "reason")), "semantic_invalid_view_plan")
        require(group["view_id"] is None or group["view_id"] in state["views"], "semantic_invalid_view_reference")
        members = group["item_ids"]
        require(isinstance(members, list) and members and all(isinstance(x, str) for x in members)
                and len(set(members)) == len(members) and set(members) <= set(state["items"]), "semantic_invalid_view_members")
        covered.update(members)
    unassigned = set()
    for entry in value["unassigned"]:
        require(isinstance(entry, dict) and set(entry) == {"item_id", "reason"}
                and isinstance(entry["reason"], str) and entry["reason"].strip(), "semantic_invalid_view_plan")
        require(entry["item_id"] in state["items"] and entry["item_id"] not in unassigned, "semantic_invalid_view_members")
        unassigned.add(entry["item_id"])
    require(not covered & unassigned and covered | unassigned == set(state["items"]), "semantic_view_coverage_missing")
    require(set(state["items"]) | set(state["retired_items"]) == set(state["target_item_ids"])
            and not set(state["items"]) & set(state["retired_items"]))
    return value


def projected_groups(generation, plan_id, plan):
    return [{**group, "view_id": group["view_id"] or str(uuid.uuid5(uuid.NAMESPACE_URL,
        dumps([generation, plan_id, index, "live-view"])))} for index, group in enumerate(plan["groups"])]


def projected_actions(plan_id, groups):
    return [{"action_key": plan_id + ":member:" + str(index) + ":" + identity,
             "type": "ADD_MEMBERSHIP", "item_id": identity, "view_id": group["view_id"],
             "placement": "primary", "origin": "model", "reason": group["reason"]}
            for index, group in enumerate(groups) for identity in group["item_ids"]]


def redact_state(value):
    """The canonical URL authority prepares provider input; private evidence stays exact."""
    from core.url_safety import redact_urls_in_text
    if isinstance(value, str):
        return redact_urls_in_text(value)
    if isinstance(value, list):
        return [redact_state(part) for part in value]
    if isinstance(value, dict):
        return {redact_urls_in_text(key): redact_state(part) for key, part in value.items()}
    return value
