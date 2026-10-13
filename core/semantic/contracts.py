"""Validated vocabulary at the canonical transaction boundary."""
from __future__ import annotations

import hashlib
import json
import math

RELATIONS = frozenset({"replay_same_source", "same_material", "restates", "independent_support",
                      "refines_scope", "changes_over_time", "disputes", "corrects_or_retracts", "unresolved"})
CLAIM_RELATIONS = RELATIONS | {"corrects", "retracts", "contradicts", "related"}
MERGE_MODES = frozenset({"append_detail", "add_evidence", "correct", "refine_scope", "continue", "attach_resource"})
EFFECTS = frozenset({"none", "detail_added", "evidence_changed", "state_changed", "scope_changed", "continuation", "resource_arrived"})
DISPOSITIONS = frozenset({"observed", "context_only", "repetition_only", "noise", "unsupported_type"})
PROVENANCE = frozenset({"raw_exact", "excerpt_only", "derived_summary", "manual_annotation"})
ACTION_TYPES = frozenset({"CREATE_ITEM", "MERGE_INTO_ITEM", "RELATE_ITEMS", "ADD_MEMBERSHIP", "MERGE_ITEMS", "SPLIT_ITEM"})


class SemanticStoreError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise SemanticStoreError("semantic_invalid_json") from exc


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise SemanticStoreError("semantic_invalid_text")
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise SemanticStoreError("semantic_invalid_integer")
    return value


def timestamp(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise SemanticStoreError("semantic_invalid_timestamp")
    return value


def ids(value, *, empty=False):
    if not isinstance(value, (list, tuple)) or (not empty and not value):
        raise SemanticStoreError("semantic_invalid_ids")
    result = [text(x) for x in value]
    if len(set(result)) != len(result):
        raise SemanticStoreError("semantic_duplicate_id")
    return result


def validate_action(value):
    if not isinstance(value, dict) or value.get("type") not in ACTION_TYPES:
        raise SemanticStoreError("semantic_invalid_action")
    text(value.get("action_key"))
    if "claim_relations" in value:
        if not isinstance(value["claim_relations"], list):
            raise SemanticStoreError("semantic_invalid_claim_relation")
        for relation in value["claim_relations"]:
            validate_claim_relation(relation)
    kind = value["type"]
    if kind in {"CREATE_ITEM", "MERGE_INTO_ITEM"}:
        text(value.get("item_id"))
        ids(value.get("observation_ids"))
    if kind == "CREATE_ITEM":
        text(value.get("kind")); text(value.get("title"))
        if not isinstance(value.get("identity_scope"), dict) or not value["identity_scope"]:
            raise SemanticStoreError("semantic_identity_scope_required")
    if kind == "MERGE_INTO_ITEM":
        if not set(ids(value.get("modes"))) <= MERGE_MODES or value.get("semantic_effect") not in EFFECTS:
            raise SemanticStoreError("semantic_invalid_merge")
    if kind == "RELATE_ITEMS":
        text(value.get("from_item_id")); text(value.get("to_item_id")); text(value.get("reason"))
        if value.get("relation") not in RELATIONS or value.get("direction") not in {"directed", "symmetric"}:
            raise SemanticStoreError("semantic_invalid_relation")
        ids(value.get("evidence_ids"))
    if kind == "ADD_MEMBERSHIP":
        text(value.get("item_id")); text(value.get("view_id"))
        if value.get("origin") not in {"user", "model", "rule"}:
            raise SemanticStoreError("semantic_invalid_membership_origin")
        for field in ("pinned", "excluded"):
            if field in value and (type(value[field]) is not bool or value["origin"] != "user"):
                raise SemanticStoreError("semantic_user_override_required")
    if kind == "MERGE_ITEMS":
        members = ids(value.get("item_ids")); text(value.get("reason"))
        if len(members) < 2 or value.get("target_item_id") not in members:
            raise SemanticStoreError("semantic_invalid_identity_merge")
    if kind == "SPLIT_ITEM":
        text(value.get("item_id")); text(value.get("reason"))
        if not isinstance(value.get("outputs"), list) or len(value["outputs"]) < 2:
            raise SemanticStoreError("semantic_invalid_split")
        for index, output in enumerate(value["outputs"]):
            validate_action({**output, "type": "CREATE_ITEM", "action_key": f"split-{index}"})
    dumps(value)
    return value


def validate_claim_relation(value):
    if (not isinstance(value, dict) or
            set(value) - {"from_claim_id", "to_claim_id", "relation", "reason"} or
            value.get("relation") not in CLAIM_RELATIONS):
        raise SemanticStoreError("semantic_invalid_claim_relation")
    left, right = text(value.get("from_claim_id")), text(value.get("to_claim_id"))
    if left == right or not isinstance(value.get("reason", ""), str):
        raise SemanticStoreError("semantic_invalid_claim_relation")
    return value
