---
name: imsg
description: Use for local iMessage/SMS archive reads, chat history, watch, and explicitly requested sends.
---

# imsg

Use this for Messages.app history, chat lookup, streaming, and sends. Reading is local DB access; sending uses Messages automation and must be explicitly requested. When the Codex plugin is installed, prefer the MCP tools first (`imsg_list_chats`, `imsg_read_messages`, `imsg_search_messages`, `imsg_prepare_send`, `imsg_send_message`, `imsg_prepare_reaction`, `imsg_send_reaction`) because they add bounded reads and write approval gates.

## Sources

- DB: `~/Library/Messages/chat.db`
- Repo: `~/Projects/imsg` or the installed plugin checkout
- CLI: `imsg`
- JSON output is NDJSON; pipe to `jq -s` for arrays.
- Codex plugin: `.codex-plugin/plugin.json` plus `.mcp.json` runs `scripts/run_mcp.sh`

## Read Workflow

Check DB access:

```bash
sqlite3 ~/Library/Messages/chat.db 'pragma quick_check;'
```

List chats:

```bash
imsg chats --json | jq -s
```

Read a chat:

```bash
imsg history --chat-id ID --json | jq -s
```

Use `--attachments` when attachment metadata matters. Use `--start`/`--end` with absolute timestamps for date-scoped questions.

## Sends

Only send, react, mark read, or show typing when the user explicitly asks. Prefer dry wording in the final confirmation: recipient, service, and what was sent. In the Codex plugin, use `imsg_prepare_send` first, then send only with `ALLOW_IMSG_SEND=1`, `confirm_send=true`, an approval note, and the matching `send_sha256`.

Common send command:

```bash
imsg send --to "+15551234567" --text "message" --service auto
```

For tapbacks, use `imsg_prepare_reaction` first, then react only with `ALLOW_IMSG_REACT=1`, `confirm_react=true`, an approval note, and the matching `reaction_sha256`.

## Verification

For repo edits:

```bash
make test
make build
```

For live read proof:

```bash
imsg chats --limit 3 --json | jq -s
```
