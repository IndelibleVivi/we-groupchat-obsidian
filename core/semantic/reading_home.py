"""Bounded, source-free navigation over existing canonical reading facts."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import html
import re
from zoneinfo import ZoneInfo

from core.url_safety import redact_urls_in_text
from .controls import ReaderControls
from .projections import ProjectionReceipt, ReadingError, wikilink


class ReadingHome:
    """Publish one reader starting point without new tables, jobs or providers.

    ``publish`` reads metadata from the canonical store and uses the supplied
    publisher's ordinary identity, conflict and CAS rules. It never changes
    preferences, open observations, known versions or other outboxes.
    """

    OBJECT_ID = "control:home"
    PATH = "首页.md"
    MAX_LIMIT = 50
    TEXT_LIMIT = 160
    _SECTIONS = (
        ("recent", "最近条目", "1"),
        ("new", "尚未打开或确认", "opened_revision=0 AND known_revision=0"),
        ("updated", "有更新", "content_revision>max(opened_revision,known_revision) "
         "AND max(opened_revision,known_revision)>0"),
        ("followed", "关注与等待", "follow=1 OR has_wait=1"),
    )

    def __init__(self, store, publisher, controls, *, config=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= self.MAX_LIMIT:
            raise ReadingError("invalid_reading_home_limit")
        if (store.generation_id != publisher.ledger.generation_id
                or controls.ledger.generation_id != store.generation_id
                or controls.publisher is not publisher):
            raise ReadingError("reading_home_generation_mismatch")
        self.store, self.publisher, self.controls = store, publisher, controls
        self.limit = limit
        self.zone = ZoneInfo((config or {}).get("daily_digest_timezone", "Asia/Shanghai"))

    @classmethod
    def for_runtime(cls, runtime, *, limit=20):
        return cls(runtime.store, runtime.publisher, runtime.controls,
                   config=runtime.config, limit=limit)

    @staticmethod
    def _text(value):
        # Titles and waiting text remain canonical elsewhere. Here they are
        # bounded labels, never executable Markdown or machine instructions.
        text = " ".join(redact_urls_in_text(value or "").split())
        text = html.escape(text, quote=False)
        text = text.replace("[", "（").replace("]", "）").replace("|", "／")
        return re.sub(r"([\\`*_])", r"\\\1", text)

    def _metadata(self, day):
        generation = self.store.generation_id
        # Admit complete short labels only. A prefix cut inside a URL may lose
        # its userinfo delimiter and cannot be safely redacted. Long labels
        # therefore get a whole-label placeholder, rather than an arbitrary
        # raw prefix. SQLite never passes their unbounded text into Python.
        # No source, Item snapshot, BodyPlan or accepted artifact is hydrated.
        catalog = """WITH catalog AS (
          SELECT i.item_id,CASE WHEN length(i.title)<=? THEN i.title ELSE '' END AS title,
            length(i.title)>? AS title_truncated,i.content_revision,
            i.semantic_updated_at,coalesce(p.follow,0) AS follow,
            coalesce(p.mute,0) AS mute,coalesce(p.revision,0) AS preference_revision,
            CASE WHEN length(trim(coalesce(p.wait_condition,'')))>0 THEN 1 ELSE 0 END AS has_wait,
            CASE WHEN length(coalesce(p.wait_condition,''))<=? THEN coalesce(p.wait_condition,'')
              ELSE '' END AS wait_condition,
            length(coalesce(p.wait_condition,''))>? AS wait_truncated,
            coalesce((SELECT max(o.content_revision) FROM reader_open_observations o
              WHERE o.generation_id=? AND o.item_id=i.item_id
              AND o.origin='obsidian_visible_open'
              AND o.content_revision<=i.content_revision),0) AS opened_revision,
            coalesce((SELECT max(k.content_revision) FROM reader_known k
              WHERE k.generation_id=? AND k.item_id=i.item_id
              AND k.content_revision<=i.content_revision),0) AS known_revision
          FROM items i LEFT JOIN reader_preferences p
            ON p.generation_id=? AND p.item_id=i.item_id
          WHERE i.lifecycle='active') """
        parameters = (self.TEXT_LIMIT, self.TEXT_LIMIT, self.TEXT_LIMIT, self.TEXT_LIMIT,
                      generation, generation, generation)
        with closing(self.store.connect()) as conn:
            conn.execute("BEGIN")
            sections = {}
            for key, _title, condition in self._SECTIONS:
                total = conn.execute(catalog + "SELECT count(*) FROM catalog WHERE " + condition,
                                     parameters).fetchone()[0]
                rows = conn.execute(catalog + "SELECT * FROM catalog WHERE " + condition
                    + " ORDER BY semantic_updated_at DESC,item_id LIMIT ?", (*parameters, self.limit)).fetchall()
                sections[key] = {"total": total, "rows": [dict(row) for row in rows]}
            views = [dict(row) for row in conn.execute(
                "SELECT view_id,CASE WHEN length(title)<=? THEN title ELSE '' END AS title,"
                "length(title)>? AS title_truncated,"
                "definition_revision+override_revision AS revision "
                "FROM views ORDER BY title,view_id LIMIT ?", (self.TEXT_LIMIT, self.TEXT_LIMIT, self.limit))]
            view_count = conn.execute("SELECT count(*) FROM views").fetchone()[0]
            brief = None
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reader_brief_editions'").fetchone():
                row = conn.execute("SELECT edition_id,content_revision,status FROM reader_brief_editions "
                    "WHERE generation_id=? AND timezone=? AND local_day=? "
                    "AND content_revision>0 AND length(successful_text)>0",
                    (generation, self.zone.key, day)).fetchone()
                brief = dict(row) if row else None
            return {"sections": sections, "views": views, "view_count": view_count, "brief": brief}

    def publish(self, *, now=None) -> ProjectionReceipt:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ReadingError("reading_home_requires_aware_time")
        day = now.astimezone(self.zone).date().isoformat()
        with self.publisher.locked():
            metadata = self._metadata(day)
            targets = {}

            def target(object_id, label, revision=None):
                key = (object_id, revision)
                if key not in targets:
                    path = None
                    try:
                        if self.publisher.ready(object_id, revision):
                            path = self.publisher.resolve(object_id)
                    except (ReadingError, OSError, UnicodeError):
                        pass
                    targets[key] = path
                path = targets[key]
                label = self._text(label)
                if path:
                    try:
                        # In wikilinks these delimiters select a heading/block,
                        # not a literal filename. Encoding alone is no proof
                        # that Obsidian accepts such renamed files natively.
                        if "#" in path or "^" in path:
                            raise ReadingError("reading_home_filename_not_link_safe")
                        return wikilink("WGO/" + path, label)
                    except ReadingError:
                        return label + "（文件名暂无法安全跳转，请从知识库目录打开）"
                return label + "（阅读页待就绪）"

            lines = ["从这里继续阅读，查找问题的依据，或跟进你关心的事情。", "## 阅读入口"]
            for object_id, label in ((ReaderControls.QUERY_ID, "检索"),
                                     (ReaderControls.MANAGEMENT_ID, "阅读管理"),
                                     ("control:reader-status", "阅读状态")):
                lines.append("- " + target(object_id, label))
            brief = metadata["brief"]
            lines.append("## 今天的简报 · " + day)
            if brief:
                label = "阅读今天的简报" if brief["status"] == "success" else "阅读今天最近成功的一版简报"
                lines.append(target(brief["edition_id"], label, brief["content_revision"]))
            else:
                lines.append("今天还没有已完成的简报，可以先从最近条目开始。")
            lines.extend(("## 阅读进度", "打开记录只说明曾实际显示过对应版本；「已知」来自你的明确确认。"
                          "没有可见打开记录时，仍可在阅读管理中确认已知，或在条目属性里关注、静音和填写等待条件。"
                          "进入条目可以继续核对原话与证据。"))
            for key, title, _condition in self._SECTIONS:
                section = metadata["sections"][key]
                lines.append("## " + title + " · " + str(section["total"]) + " 条")
                if not section["rows"]:
                    lines.append("目前没有这类条目。")
                for item in section["rows"]:
                    current, opened, known = (item[field] for field in
                        ("content_revision", "opened_revision", "known_revision"))
                    states = ["最新 v" + str(current), "已打开至 v" + str(opened) if opened else "暂无可见打开记录",
                              "明确已知至 v" + str(known) if known else "尚未确认已知"]
                    if current > max(opened, known) and max(opened, known):
                        states.append("有更新，尚无打开或已知记录")
                    if item["follow"]:
                        states.append("已关注")
                    if item["mute"]:
                        states.append("已静音")
                    title = "标题较长的条目（打开查看完整标题）" if item["title_truncated"] else item["title"]
                    line = "- " + target(item["item_id"], title, current) + " — " + "；".join(states)
                    if item["has_wait"]:
                        if item["wait_truncated"]:
                            line += "。等待条件较长，完整条件见条目属性。"
                        else:
                            line += "。等待：" + self._text(item["wait_condition"])
                    lines.append(line)
                remaining = section["total"] - len(section["rows"])
                if remaining:
                    lines.append("这里显示最近 " + str(self.limit) + " 条，另有 " + str(remaining)
                                 + " 条；可通过检索或主题视图继续查找。")
            lines.append("## 主题视图 · " + str(metadata["view_count"]) + " 个")
            for view in metadata["views"]:
                title = "标题较长的主题（打开查看完整标题）" if view["title_truncated"] else view["title"]
                lines.append("- " + target("view:" + view["view_id"], title, view["revision"]))
            if not metadata["views"]:
                lines.append("主题视图尚未形成，可以先读最近条目。")
            if metadata["view_count"] > len(metadata["views"]):
                lines.append("这里显示前 " + str(self.limit) + " 个视图，另有 "
                             + str(metadata["view_count"] - len(metadata["views"])) + " 个。")
            body = "\n\n".join(lines)
            previous = self.publisher.record(self.OBJECT_ID)
            revision = (previous["content_revision"] if previous else 0) + int(
                previous is None or previous["machine_body"] != "\n" + body + "\n")

            def current():
                if self._metadata(day) != metadata:
                    return False
                try:
                    return all(self.publisher.ready(object_id, revision)
                               and self.publisher.resolve(object_id) == path
                               for (object_id, revision), path in targets.items() if path is not None)
                except (ReadingError, OSError, UnicodeError):
                    return False

            return self.publisher.publish_document(object_id=self.OBJECT_ID, path=self.PATH,
                title="阅读首页", body=body, content_revision=revision,
                is_current=current)
