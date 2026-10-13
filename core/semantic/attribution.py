"""Attribution from captured metadata, without contact lookup or name matching.

The opaque identity identifies the message speaker/forwarder. It does not
identify a quoted author or the subject of every assertion in that message.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
import re
import uuid

from core.url_safety import redact_urls_in_text


EXTRACTION_PROMPT_VERSION = "semantic-extraction-v2-attribution"


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _scoped(parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))


def source_attribution(source_namespace, chat_id, payload):
    """Return an opaque speaker ID and that source's observed display label.

    raw_sender_id is an account identifier within the captured namespace.
    sender_id alone is local: only a captured shard plus chat establishes a
    usable conservative scope. Names never create or join identities. Exact
    account identifiers remain in the private source payload.
    """
    payload = payload if isinstance(payload, Mapping) else {}
    raw = payload.get("raw_sender_id")
    local = payload.get("sender_id")
    envelope = payload.get("source_envelope")
    shard = envelope.get("db_shard_id") if isinstance(envelope, Mapping) else None
    speaker_id = None
    if _nonempty(source_namespace):
        if _nonempty(raw):
            speaker_id = _scoped(["speaker-account-v1", source_namespace, raw])
        elif (_nonempty(chat_id) and _nonempty(shard)
              and (_nonempty(local) or type(local) is int and local > 0)):
            speaker_id = _scoped(["speaker-local-v1", source_namespace, chat_id, shard, local])

    name = payload.get("sender")
    if not _nonempty(name):
        name = None
    elif (name.strip() in {str(value) for value in (raw, local, chat_id) if value is not None}
          or _nonempty(raw) and raw in name
          or re.search(r"wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+@chatroom", name, re.IGNORECASE)):
        # The reader can show an unknown speaker; never substitute an account
        # fallback as a human name or manufacture a historical display label.
        name = None
    else:
        name = redact_urls_in_text(name)
    return {"speaker_id": speaker_id, "speaker_name": name}


def captured_source_metadata(source_namespace, chat_id, payload, *, legacy=False):
    """Bind extraction metadata in both the producer and canonical verifier.

    legacy exists only to verify/replay already frozen v1 artifacts. New jobs
    must preserve the distinction between explicit subject and speaker.
    """
    payload = payload if isinstance(payload, Mapping) else {}

    def scoped(kind, anchor, chat=True):
        return (_scoped([kind, source_namespace, chat_id if chat else "", anchor])
                if isinstance(anchor, str) and anchor else None)

    subject = payload.get("subject_anchor")
    if legacy:
        subject = subject or payload.get("raw_sender_id") or payload.get("sender_id")
    result = {"reply_to": payload.get("reply_to"),
              "case_id": scoped("case", payload.get("case_anchor") or payload.get("case_id")),
              "material_id": scoped("material", payload.get("material_id"), False),
              "subject_id": scoped("subject", subject)}
    if not legacy:
        result.update(source_attribution(source_namespace, chat_id, payload))
    return result
