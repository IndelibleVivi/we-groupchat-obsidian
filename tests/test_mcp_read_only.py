import ast
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from core.project_identity import MCP_SERVER_ID

# The MCP SDK is an optional dependency (requirements-mcp.txt). When it is not
# installed, skip these tests instead of reporting a collection error; CI runs
# the base-only bootstrap check first, then installs the SDK and exercises the
# full contract in the same regression job.
try:
    import mcp.server.fastmcp  # noqa: F401 - optional SDK availability probe
    _MCP_SDK_AVAILABLE = True
except ModuleNotFoundError as exc:
    # Only a genuinely absent top-level SDK is optional. A broken or
    # incompatible installed SDK must surface as a real import failure instead
    # of being hidden behind the optional-dependency skip.
    if exc.name != "mcp":
        raise
    _MCP_SDK_AVAILABLE = False

if _MCP_SDK_AVAILABLE:
    import mcp_server


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _FakeSummaryDB:
    def __init__(self):
        self._contacts = {"room@chatroom": "Example Group"}

    def _load_contacts(self):
        return None

    def get_messages(self, username, since_ts=0, limit=500):
        return [
            {
                "timestamp": 123,
                "time_str": "2026-08-30 10:00",
                "sender": "Example Member",
                "text": "fixture",
            }
        ]

    def format_messages_for_ai(self, messages, show_group_nickname=True):
        return "fixture messages"


class _FakeAI:
    def build_prompt(self, **_kwargs):
        return "fixture prompt"

    def build_batch_prompt(self, _group_name, _groups_data):
        return "fixture batch prompt"

    def summarize(self, _prompt):
        return "fixture summary"


@unittest.skipUnless(
    _MCP_SDK_AVAILABLE,
    "optional MCP SDK is not installed; run "
    "`./.venv/bin/python -m pip install -r requirements-mcp.txt`",
)
class McpReadOnlyTests(unittest.TestCase):
    def test_fastmcp_registers_the_canonical_server_id(self):
        self.assertEqual(mcp_server.mcp.name, MCP_SERVER_ID)

    def test_read_only_mcp_tool_imports_and_operates(self):
        db = type(
            "DiscoveryDB",
            (),
            {
                "_contacts_full": [
                    {
                        "username": "room@chatroom",
                        "remark": "Example Group",
                        "nick_name": "",
                    }
                ],
                "_load_contacts": lambda self: None,
            },
        )()

        with patch("mcp_server._get_db", return_value=db):
            result = mcp_server.list_chats("group")

        self.assertIn("Example Group", result)

    def test_summary_tools_never_advance_bookmarks(self):
        db = _FakeSummaryDB()
        ai = _FakeAI()
        with patch("mcp_server._resolve", return_value=("room@chatroom", "Example Group")), patch(
            "mcp_server._get_db", return_value=db
        ), patch("mcp_server._get_ai", return_value=(ai, {})), patch(
            "core.bookmark.get_bookmark", return_value=0
        ), patch("core.bookmark.set_bookmark") as set_bookmark:
            result = mcp_server.summarize_chat(
                "Example Group",
                update_bookmark=True,
            )

        self.assertIn("fixture summary", result)
        set_bookmark.assert_not_called()

        with patch("core.chat_groups.get_group_chats", return_value=["room@chatroom"]), patch(
            "mcp_server._get_db", return_value=db
        ), patch("mcp_server._get_ai", return_value=(ai, {})), patch(
            "core.config.load_config", return_value={}
        ), patch("core.bookmark.get_bookmark", return_value=0), patch(
            "core.bookmark.set_bookmark"
        ) as set_bookmark:
            result = mcp_server.summarize_group_batch("Example Collection")

        self.assertIn("fixture summary", result)
        set_bookmark.assert_not_called()

    def test_group_mutations_are_inert_and_content_free(self):
        expected = {
            "code": "mcp_mutation_retired",
            "message": "MCP is read-only; manage groups in the menu-bar app",
        }
        for action in ("create", "delete", "add", "remove", "unknown"):
            with self.subTest(action=action), patch(
                "core.chat_groups.save_groups",
                side_effect=AssertionError("MCP must not write group state"),
            ) as save_groups, patch(
                "mcp_server._resolve",
                side_effect=AssertionError("retired group mutations must not resolve a target"),
            ):
                result = mcp_server.manage_chat_groups(
                    action,
                    group_name="private group",
                    chat_name="private chat",
                )
            self.assertEqual(json.loads(result), expected)
            self.assertNotIn("private group", result)
            self.assertNotIn("private chat", result)
            save_groups.assert_not_called()

    def test_retired_send_tools_ignore_all_arguments_and_legacy_config(self):
        expected = {
            "code": "mcp_send_retired",
            "message": "MCP message sending is no longer supported",
        }
        legacy_configs = (
            {"mcp_enable_send_message": True},
            {"mcp_send_mode": "enabled"},
            {
                "mcp_send_mode": "allowlist",
                "mcp_send_allowlist": ["room@chatroom"],
            },
        )
        for legacy_config in legacy_configs:
            with self.subTest(config=legacy_config), patch(
                "mcp_server.load_config",
                return_value=legacy_config,
            ) as load_config:
                responses = (
                    mcp_server.prepare_send_message("private text", "private chat"),
                    mcp_server.confirm_send_message(
                        "private nonce",
                        "private text",
                        "private chat",
                    ),
                    mcp_server.send_message("private text", "private chat"),
                )
            load_config.assert_not_called()
            for response in responses:
                self.assertEqual(json.loads(response), expected)
                self.assertNotIn("private", response)

    def test_mcp_runtime_has_no_sender_import_or_sender_module(self):
        source_path = REPOSITORY_ROOT / "mcp_server.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        self.assertNotIn("core.sender", imported_modules)
        self.assertNotIn("core.mcp_send_policy", imported_modules)
        self.assertNotIn("core.mcp_send_confirmation", imported_modules)
        self.assertFalse((REPOSITORY_ROOT / "core" / "sender.py").exists())
        self.assertFalse((REPOSITORY_ROOT / "core" / "mcp_send_policy.py").exists())
        self.assertFalse((REPOSITORY_ROOT / "core" / "mcp_send_confirmation.py").exists())


if __name__ == "__main__":
    unittest.main()
