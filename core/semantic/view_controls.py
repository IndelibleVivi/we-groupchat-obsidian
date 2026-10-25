"""Registered reader intent for existing View definitions and memberships."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import json
from pathlib import PurePosixPath
import re

from core.url_safety import redact_urls_in_text
from .controls import ReaderControls, ControlReceipt
from .contracts import SemanticStoreError
from .projections import ReadingError, fingerprint, property_value, wikilink
from .reading_home import ReadingHome


class ViewControls(ReaderControls):
    MANAGEMENT_ID = "control:views"
    ACTION_KINDS = frozenset({"view_definition", "view_pin", "view_exclude"})
    DEFINITION_KEY = "主题说明"
    SELECTION_KEY = "目录"
    ITEM_SELECTION_KEY = "条目"

    def __init__(self, store, publisher):
        super().__init__(publisher.ledger, publisher, search=None)
        if store.generation_id != self.ledger.generation_id:
            raise ReadingError("reading_generation_mismatch")
        self.store = store

    @staticmethod
    def _action_label(action):
        return (f"{action['label']}（目录说明 v{action['content_revision']}，"
                f"成员设置 v{action['preference_revision']}）")

    def _reference(self, selection, *, view):
        match = re.fullmatch(r"\[\[([^\]#^|]+)(?:\|[^\]]*)?\]\]", selection.strip())
        if not match:
            raise ReadingError("view_selection_requires_link")
        relative = match[1].removeprefix("WGO/")
        if not relative.endswith(".md"):
            relative += ".md"
        raw = self.publisher.read(relative)
        if raw is None:
            raise ReadingError("view_selection_unavailable")
        identity = property_value(raw.decode(), "wgo_id")
        if (not isinstance(identity, str) or identity.startswith("view:") != view
                or property_value(raw.decode(), "wgo_generation") != self.store.generation_id
                or self.publisher.resolve(identity) != PurePosixPath(relative).as_posix()):
            raise ReadingError("view_selection_invalid")
        return identity[5:] if view else identity

    def _views(self, selection, limit):
        with closing(self.store.connect()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM views").fetchone()[0]
            query = ("SELECT view_id,definition_revision,override_revision,"
                     "CASE WHEN length(title)<=160 THEN title ELSE '标题较长的目录' END AS title,"
                     "CASE WHEN length(definition)<=2000 THEN definition END AS definition FROM views ")
            if selection:
                identity = self._reference(selection, view=True)
                rows = conn.execute(query + "WHERE view_id=?", (identity,)).fetchall()
            else:
                rows = conn.execute(query + "ORDER BY view_id LIMIT ?", (limit,)).fetchall()
            return total, [dict(row) for row in rows]

    def _current(self, action):
        value = json.loads(action["value_json"])
        with closing(self.store.connect()) as conn:
            view = conn.execute("SELECT definition_revision,override_revision FROM views WHERE view_id=?",
                                (value["view_id"],)).fetchone()
        path = self.publisher.resolve(action["item_id"])
        raw = self.publisher.read(path) if path else None
        if (view is None or raw is None or fingerprint(raw) != action["file_hash"]
                or view["definition_revision"] != action["content_revision"]
                or view["override_revision"] != action["preference_revision"]):
            raise ReadingError("stale_view_control_action")
        if value.get("item_id"):
            with closing(self.store.connect()) as conn:
                item = conn.execute("SELECT content_revision,lifecycle FROM items WHERE item_id=?",
                                    (value["item_id"],)).fetchone()
            if item is None or item[0] != value["item_revision"] or item[1] != "active":
                raise ReadingError("stale_view_control_action")
        return value

    def process_management(self, *, acted_at=None):
        _hash, checked = self._validated_management_checks()
        groups, receipts = defaultdict(list), []
        for identity in checked:
            action = self.ledger.one("SELECT * FROM reader_actions WHERE action_id=?", (identity,))
            if action["status"] != "registered":
                receipts.append(ControlReceipt(identity, "already_consumed"))
            else:
                groups[action["item_id"]].append(action)
        for actions in groups.values():
            try:
                with self.publisher.locked():
                    # Another consumer may have completed after our initial
                    # checkbox read but before this shared root lock.
                    fresh = []
                    for action in actions:
                        status = self.ledger.one("SELECT status FROM reader_actions WHERE action_id=?",
                                                 (action["action_id"],))[0]
                        if status == "registered":
                            fresh.append(action)
                        else:
                            receipts.append(ControlReceipt(action["action_id"], "already_consumed"))
                    actions = fresh
                    if not actions:
                        continue
                    values = [self._current(action) for action in actions]
                    definition, members, items = None, {}, {}
                    for action, value in zip(actions, values):
                        if action["kind"] == "view_definition":
                            definition = value["definition"]
                        else:
                            item_id = value["item_id"]
                            members.setdefault(item_id, {})["pinned" if action["kind"] == "view_pin" else "excluded"] = value["value"]
                            items[item_id] = value["item_revision"]
                    first = actions[0]
                    if any((a["content_revision"], a["preference_revision"]) !=
                           (first["content_revision"], first["preference_revision"]) for a in actions):
                        raise ReadingError("stale_view_control_action")
                    with self.ledger.transaction() as conn:
                        for item_id, changes in members.items():
                            old = conn.execute("SELECT pinned,excluded FROM view_memberships WHERE view_id=? AND item_id=?",
                                               (values[0]["view_id"], item_id)).fetchone()
                            if old is None:
                                raise ReadingError("stale_view_control_action")
                            members[item_id] = {"pinned": bool(old[0]), "excluded": bool(old[1]), **changes}
                        self.store._update_view_controls(conn, values[0]["view_id"],
                            first["content_revision"], first["preference_revision"], definition=definition,
                            memberships=members, expected_item_revisions=items, include_memberships=False)
                        conn.executemany("UPDATE reader_actions SET status='applied' WHERE action_id=?",
                                         [(a["action_id"],) for a in actions])
                    receipts.extend(ControlReceipt(a["action_id"], "applied") for a in actions)
            except (ReadingError, SemanticStoreError, ValueError, KeyError, TypeError) as exc:
                code = getattr(exc, "code", "invalid_view_control_action")
                with self.ledger.transaction() as conn:
                    conn.executemany("UPDATE reader_actions SET status='rejected' WHERE action_id=? AND status='registered'",
                                     [(a["action_id"],) for a in actions])
                receipts.extend(ControlReceipt(a["action_id"], "rejected", code=code) for a in actions)
            except (OSError, UnicodeError):
                receipts.extend(ControlReceipt(a["action_id"], "deferred", code="view_control_unavailable")
                                for a in actions)
        return receipts

    def refresh_management(self, *, limit=20, member_limit=20):
        if type(limit) is not int or type(member_limit) is not int or not 1 <= limit <= 50 or not 1 <= member_limit <= 50:
            raise ReadingError("invalid_view_control_limit")
        with self.publisher.locked():
            if self.publisher.record(self.MANAGEMENT_ID) is None:
                self.publisher.publish_document(object_id=self.MANAGEMENT_ID, path="主题管理.md", title="主题管理",
                    body="目录管理正在准备。", content_revision=0, region="actions")
            self.register_property(self.MANAGEMENT_ID, "query", self.SELECTION_KEY, initial="")
            self.register_property(self.MANAGEMENT_ID, "view_member", self.ITEM_SELECTION_KEY, initial="")
            management = self.publisher.resolve(self.MANAGEMENT_ID)
            selection = property_value(self.publisher.read(management).decode(), self.SELECTION_KEY)
            if not isinstance(selection, str):
                raise ReadingError("invalid_view_selection")
            item_selection = property_value(self.publisher.read(management).decode(), self.ITEM_SELECTION_KEY)
            if not isinstance(item_selection, str):
                raise ReadingError("invalid_view_selection")
            selected_item = self._reference(item_selection, view=False) if item_selection else None
            authorized_hash, checked_ids = self._validated_management_checks()
            checked = set(checked_ids)
            total, views = self._views(selection, limit)
            selected_actions, notices = set(), []
            for view in views:
                object_id = "view:" + view["view_id"]
                if not self.publisher.ready(object_id, view["definition_revision"] + view["override_revision"]):
                    notices.append("有目录阅读页尚未就绪；完成发布后可在这里管理。")
                    continue
                # This field is a human-readable projection, never a private URL ledger.
                # Comparing the same display form avoids treating our own redaction as intent.
                displayed_definition = (redact_urls_in_text(view["definition"])
                                        if view["definition"] is not None else None)
                try:
                    if displayed_definition is not None:
                        self.register_property(object_id, "view_definition", self.DEFINITION_KEY,
                                               initial=displayed_definition)
                except (ReadingError, OSError, UnicodeError):
                    notices.append("有目录的主题说明字段冲突或暂不可用；其它目录仍可管理，请先在该目录页解决个人字段冲突。")
                    continue
                raw = self.publisher.read(self.publisher.resolve(object_id))
                expected_hash = fingerprint(raw)
                definition = (property_value(raw.decode(), self.DEFINITION_KEY)
                              if displayed_definition is not None else None)
                def register(kind, label, value):
                    identity = self._register_record(kind, object_id, view["definition_revision"], view["override_revision"],
                        expected_hash, label, {"view_id": view["view_id"], **value})
                    selected_actions.add(identity)
                    return identity
                if displayed_definition is None:
                    notices.append("有目录的说明超过 2000 字符；这里仅管理成员，完整说明保持在 canonical store。")
                elif definition != displayed_definition:
                    if not isinstance(definition, str) or not definition.strip() or len(definition) > 2000:
                        notices.append("主题说明需为 1–2000 字符的文本；原目录说明保持不变。")
                    else:
                        register("view_definition", "采用你填写的主题说明", {"definition": definition})
                with closing(self.store.connect()) as conn:
                    count = conn.execute("SELECT COUNT(*) FROM view_memberships WHERE view_id=?", (view["view_id"],)).fetchone()[0]
                    members = conn.execute("SELECT m.*,CASE WHEN length(i.title)<=160 THEN i.title "
                        "ELSE '标题较长的条目' END AS title,i.content_revision FROM view_memberships m JOIN items i USING(item_id) "
                        "WHERE m.view_id=? AND i.lifecycle='active' AND (? IS NULL OR m.item_id=?) "
                        "ORDER BY i.title,i.item_id LIMIT ?", (view["view_id"], selected_item, selected_item, member_limit)).fetchall()
                for member in members:
                    label = ReadingHome._text(member["title"]) if len(member["title"]) <= 160 else "标题较长的条目"
                    value = {"item_id": member["item_id"], "item_revision": member["content_revision"]}
                    register("view_pin", ("取消固定：" if member["pinned"] else "固定：") + label,
                             {**value, "value": not bool(member["pinned"])})
                    register("view_exclude", ("恢复显示：" if member["excluded"] else "从目录排除：") + label,
                             {**value, "value": not bool(member["excluded"])})
                if selected_item and not members:
                    notices.append("指定条目不是当前目录的可管理成员；没有改变归类。")
                elif selected_item:
                    notices.append("当前按指定条目筛选；目录总成员数 " + str(count) + "。")
                elif count > len(members):
                    notices.append("当前目录显示前 " + str(member_limit) + " 个可管理成员；总成员数 " + str(count) + "。")
            kinds = sorted(self.ACTION_KINDS)
            actions = self.ledger.all("SELECT * FROM reader_actions WHERE generation_id=? AND status='registered' AND kind IN ("
                + ",".join("?" for _ in kinds) + ") ORDER BY rowid", (self.store.generation_id, *kinds))
            lines = ["在目录页编辑「主题说明」后，勾选这里登记的操作确认应用。固定与排除只影响目录，不改写条目内容。",
                     "在本页「目录」属性填写一个目录页的双链，可选择其它目录；留空显示前 " + str(limit) + " 个（共 " + str(total) + " 个）。",
                     "在「条目」属性填写成员笔记的双链，可管理列表之外的成员；留空按目录显示前 " + str(member_limit) + " 个。"]
            lines.extend(dict.fromkeys(notices))
            for action in actions:
                stale = False
                if action["action_id"] not in checked:
                    try:
                        self._current(action)
                        stale = action["action_id"] not in selected_actions
                    except (ReadingError, ValueError, KeyError, TypeError):
                        stale = True
                if stale:
                    with self.ledger.transaction() as conn:
                        conn.execute("UPDATE reader_actions SET status='superseded' WHERE action_id=?", (action["action_id"],))
                    continue
                path = self.publisher.resolve(action["item_id"])
                with closing(self.store.connect()) as conn:
                    view = conn.execute("SELECT CASE WHEN length(title)<=160 THEN title "
                        "ELSE '标题较长的目录' END AS title FROM views WHERE view_id=?",
                        (action["item_id"][5:],)).fetchone()
                if view is None:
                    continue
                label = ReadingHome._text(view["title"]) if len(view["title"]) <= 160 else "标题较长的目录"
                try:
                    if path and any(c in path for c in "#^"):
                        raise ReadingError("invalid_projection_link")
                    link = wikilink("WGO/" + path, label) if path else label
                except ReadingError:
                    link = label + "（文件名暂无法安全跳转，请从知识库目录打开）"
                mark = "x" if action["action_id"] in checked else " "
                lines.extend(["\n" + link, f"- [{mark}] {self._action_label(action)} <!-- wgo-action:{action['action_id']} -->"])
            recent = self.ledger.all("SELECT label,status FROM reader_actions WHERE generation_id=? AND kind IN ("
                + ",".join("?" for _ in kinds) + ") AND status IN ('applied','rejected') ORDER BY rowid DESC LIMIT 10",
                (self.store.generation_id, *kinds))
            if recent:
                lines.append("## 最近操作结果（最多 10 项）")
                for action in recent:
                    result = "已应用" if action["status"] == "applied" else "未应用，请检查最新目录后重新勾选"
                    lines.append("- " + result + "：" + ReadingHome._text(action["label"]))
            revision = self.ledger.one("SELECT COUNT(*) FROM reader_actions WHERE generation_id=? AND kind IN ("
                + ",".join("?" for _ in kinds) + ")", (self.store.generation_id, *kinds))[0]
            return self.publisher.publish_document(object_id=self.MANAGEMENT_ID, path="主题管理.md", title="主题管理",
                body="\n\n".join(lines), content_revision=revision, region="actions", authorized_hash=authorized_hash)
