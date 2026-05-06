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
        self.assertIn("imsg_resolve_contacts", names)
        self.assertIn("imsg_sent_summary", names)

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

    def test_contact_phone_keys_include_us_variants(self):
        self.assertIn("18015551212", self.mcp.phone_lookup_keys("+1 (801) 555-1212"))
        self.assertIn("8015551212", self.mcp.phone_lookup_keys("+1 (801) 555-1212"))

    def test_addressbook_owner_join_uses_numbered_owner_columns(self):
        expr = self.mcp.owner_join_expr("p", {"ZOWNER", "Z21_OWNER", "ZFULLNUMBER"})
        self.assertEqual(expr, "COALESCE(p.ZOWNER, p.Z21_OWNER)")

    def test_enrich_chat_uses_contact_fallback(self):
        self.mcp._CONTACT_INDEX = {
            "phones": {"18015551212": "Alice"},
            "emails": {},
            "records": 1,
            "sources": [],
            "errors": [],
        }
        chat = self.mcp.enrich_chat({
            "id": 1,
            "identifier": "+18015551212",
            "name": "",
            "display_name": "",
            "contact_name": None,
            "participants": ["+18015551212"],
            "is_group": False,
        })
        self.assertEqual(chat["resolved_name"], "Alice")
        self.assertEqual(chat["display_name"], "Alice")

    def test_search_fields_include_resolved_contact_names(self):
        fields = self.mcp.text_fields_for_match({
            "text": "hello",
            "participant_names": {"+18015551212": "Alice"},
            "chat_display_name": "Alice",
        })
        self.assertIn("Alice", fields["participant_names"])
        self.assertEqual(fields["chat_display_name"], "Alice")

    def test_outgoing_messages_display_as_me(self):
        self.mcp._CONTACT_INDEX = {
            "phones": {"18015551212": "Alice"},
            "emails": {},
            "records": 1,
            "sources": [],
            "errors": [],
        }
        message = self.mcp.enrich_message({
            "sender": "+18015551212",
            "is_from_me": True,
            "participants": ["+18015551212"],
        })
        self.assertEqual(message["sender_display_name"], "Me")
        self.assertEqual(message["chat_display_name"], "Alice")

    def test_sent_summary_groups_bulk_report_rows(self):
        self.mcp._CONTACT_INDEX = {
            "phones": {"18015551212": "Alice"},
            "emails": {},
            "records": 1,
            "sources": [],
            "errors": [],
        }

        def fake_run_imsg(command, timeout=120):
            self.assertEqual(command[0], "report")
            self.assertIn("--direction", command)
            self.assertIn("sent", command)
            return "\n".join([
                '{"id":1,"chat_id":7,"chat_identifier":"+18015551212","sender":"+18015551212","is_from_me":true,"text":"hello","created_at":"2026-05-05T01:00:00.000Z","participants":["+18015551212"],"is_group":false}',
                '{"id":2,"chat_id":7,"chat_identifier":"+18015551212","sender":"+18015551212","is_from_me":true,"text":"later","created_at":"2026-05-05T02:00:00.000Z","participants":["+18015551212"],"is_group":false}',
            ])

        self.mcp.run_imsg = fake_run_imsg
        summary = self.mcp.sent_summary({
            "start": "2026-05-05T00:00:00Z",
            "end": "2026-05-06T00:00:00Z",
            "output_mode": "full",
        })
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["conversation_count"], 1)
        conversation = summary["conversations"][0]
        self.assertEqual(conversation["chat_display_name"], "Alice")
        self.assertEqual(conversation["message_count"], 2)
        self.assertEqual(conversation["messages"][0]["sender_display_name"], "Me")

    def test_sent_summary_defaults_to_brief_budgeted_output(self):
        self.mcp._CONTACT_INDEX = {
            "phones": {"18015551212": "Alice"},
            "emails": {},
            "records": 1,
            "sources": [],
            "errors": [],
        }

        def fake_run_imsg(command, timeout=120):
            self.assertNotIn("--no-text", command)
            return "\n".join([
                '{"id":1,"chat_id":7,"chat_identifier":"+18015551212","sender":"+18015551212","is_from_me":true,"text":"hello there","created_at":"2026-05-05T01:00:00.000Z","participants":["+18015551212"],"is_group":false}',
                '{"id":2,"chat_id":7,"chat_identifier":"+18015551212","sender":"+18015551212","is_from_me":true,"text":"this is a longer message","created_at":"2026-05-05T02:00:00.000Z","participants":["+18015551212"],"is_group":false}',
            ])

        self.mcp.run_imsg = fake_run_imsg
        summary = self.mcp.sent_summary({
            "day": "2026-05-05",
            "timezone": "America/Denver",
            "max_text_chars": 10,
            "max_total_text_chars": 15,
            "max_messages_per_conversation": 1,
        })
        self.assertEqual(summary["output_mode"], "brief")
        conversation = summary["conversations"][0]
        self.assertEqual(conversation["name"], "Alice")
        self.assertIn("lines", conversation)
        self.assertNotIn("messages", conversation)
        self.assertEqual(len(conversation["lines"]), 1)
        self.assertEqual(conversation["omitted_messages"], 1)
        self.assertLessEqual(summary["text_chars_returned"], 15)
        self.assertGreater(conversation["truncated_chars"], 0)

    def test_sent_summary_counts_mode_uses_no_text_report(self):
        commands = []

        def fake_run_imsg(command, timeout=120):
            commands.append(command)
            return "\n".join([
                '{"id":1,"chat_id":7,"chat_identifier":"+18015551212","sender":"+18015551212","is_from_me":true,"created_at":"2026-05-05T01:00:00.000Z","participants":["+18015551212"],"is_group":false}'
            ])

        self.mcp.run_imsg = fake_run_imsg
        summary = self.mcp.sent_summary({
            "start": "2026-05-05T00:00:00Z",
            "end": "2026-05-06T00:00:00Z",
            "output_mode": "counts",
        })
        self.assertIn("--no-text", commands[0])
        self.assertFalse(summary["include_text"])
        self.assertNotIn("lines", summary["conversations"][0])
        self.assertNotIn("text_chars", summary["conversations"][0])

    def test_direct_conversation_prefers_resolved_name_over_phone_chat_name(self):
        participant_names = {"+18015551212": "Alice"}
        name = self.mcp.display_name_for_conversation(
            {
                "chat_name": "+18015551212",
                "chat_identifier": "+18015551212",
                "chat_guid": "iMessage;-;+18015551212",
                "participants": ["+18015551212"],
                "is_group": False,
            },
            participant_names,
        )
        self.assertEqual(name, "Alice")

    def tearDown(self):
        self.mcp._CONTACT_INDEX = None


if __name__ == "__main__":
    unittest.main()
