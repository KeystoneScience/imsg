import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_PATH = ROOT / "scripts" / "imsg_mcp.py"


def load_mcp():
    spec = importlib.util.spec_from_file_location("imsg_mcp", MCP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contains_schema_composition(value):
    if isinstance(value, dict):
        if any(key in value for key in ("anyOf", "oneOf", "allOf", "not")):
            return True
        return any(contains_schema_composition(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_schema_composition(item) for item in value)
    return False


class ImsgMcpTests(unittest.TestCase):
    def setUp(self):
        self.mcp = load_mcp()

    def test_tool_schemas_avoid_codex_rejected_composition_keywords(self):
        self.assertFalse(contains_schema_composition(self.mcp.tool_definitions()))

    def test_initialize_and_tools_list(self):
        init = self.mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "imsg")
        tools = self.mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("imsg_list_chats", names)
        self.assertIn("imsg_prepare_send", names)
        self.assertIn("imsg_send_reaction", names)

    def test_prepare_send_returns_stable_hash(self):
        args = {"to": "+15551234567", "text": "hello", "service": "imessage"}
        first = self.mcp.prepare_send(args)
        second = self.mcp.prepare_send(args)
        self.assertEqual(first["send_sha256"], second["send_sha256"])
        self.assertEqual(first["send_preview"]["service"], "imessage")

    def test_send_gate_blocks_without_env(self):
        os.environ.pop("ALLOW_IMSG_SEND", None)
        args = {
            "to": "+15551234567",
            "text": "hello",
            "service": "imessage",
            "confirm_send": True,
            "approval_note": "approved in test",
            "send_sha256": "not-used",
        }
        with self.assertRaises(self.mcp.ToolError) as ctx:
            self.mcp.send_message(args)
        self.assertIn("Sending is disabled", str(ctx.exception))

    def test_dangerous_attachment_is_blocked(self):
        with tempfile.NamedTemporaryFile(suffix=".sh") as handle:
            handle.write(b"echo nope\n")
            handle.flush()
            with self.assertRaises(self.mcp.ToolError) as ctx:
                self.mcp.prepare_send({"to": "+15551234567", "file": handle.name})
        self.assertIn("dangerous attachment", str(ctx.exception))

    def test_reaction_validation(self):
        payload = self.mcp.validate_reaction_payload({"chat_id": 42, "reaction": "like"})
        self.assertEqual(payload["reaction"], "like")
        with self.assertRaises(self.mcp.ToolError):
            self.mcp.validate_reaction_payload({"chat_id": 42, "reaction": "party"})


if __name__ == "__main__":
    unittest.main()
