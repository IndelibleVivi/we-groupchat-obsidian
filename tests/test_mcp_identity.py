import unittest

from core.project_identity import MCP_SERVER_ID


class McpIdentityTests(unittest.TestCase):
    def test_mcp_server_id_uses_new_project_name(self):
        self.assertEqual(MCP_SERVER_ID, "we-groupchat-obsidian")


if __name__ == "__main__":
    unittest.main()
