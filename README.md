# 💬 imsg — Send, read, stream iMessage & SMS

A macOS Messages.app CLI to send, read, and stream iMessage/SMS (with attachment metadata). Read-only for receives; send uses AppleScript (no private APIs).

## Features
- List chats, view history, or stream new messages (`watch`).
- Send text and attachments via iMessage or SMS (AppleScript, no private APIs).
- Send standard tapback reactions with `react`; stream reaction events with `watch --reactions`.
- Phone normalization to E.164 for reliable buddy lookup (`--region`, default US).
- Optional attachment metadata output (mime, name, path, missing flag).
- Filters: participants, start/end time, JSON output for tooling.
- Read-only DB access (`mode=ro`), no DB writes.
- Event-driven watch via filesystem events, with a fallback poll for missed file events.
- Optional Contacts integration resolves phone numbers and emails to names.
- Optional advanced IMCore features (`typing`, `launch`, `status`) behind explicit SIP-off setup.

## Requirements
- macOS 14+ with Messages.app signed in.
- Full Disk Access for your terminal to read `~/Library/Messages/chat.db`.
- Automation permission for your terminal to control Messages.app (for sending).
- Contacts permission is optional; without it, raw phone numbers/emails are shown.
- For SMS relay, enable “Text Message Forwarding” on your iPhone to this Mac.

## Install

### Homebrew

```bash
brew install steipete/tap/imsg
```

### Build from source

```bash
make build
# binary at ./bin/imsg
```

## Commands
- `imsg chats [--limit 20] [--json]` — list recent conversations.
- `imsg group --chat-id <id> [--json]` — show identity and participants for one chat.
- `imsg history --chat-id <id> [--limit 50] [--attachments] [--participants +15551234567,...] [--start 2025-01-01T00:00:00Z] [--end 2025-02-01T00:00:00Z] [--json]`
- `imsg watch [--chat-id <id>] [--since-rowid <n>] [--debounce 250ms] [--attachments] [--reactions] [--participants …] [--start …] [--end …] [--json]`
- `imsg send --to <handle-or-contact-name> [--text "hi"] [--file /path/file] [--service imessage|sms|auto] [--region US]`
- `imsg react --chat-id <id> --reaction love|like|dislike|laugh|emphasis|question`
- `imsg read --to <handle> [--chat-id <id> | --chat-identifier <id> | --chat-guid <guid>]`
- `imsg typing --to <handle> [--duration 5s] [--stop true] [--service imessage|sms|auto]`
- `imsg status [--json]` — advanced feature and SIP status
- `imsg launch [--dylib <path>] [--kill-only] [--json]`

### Quick samples
```
# list 5 chats
imsg chats --limit 5

# list chats as JSON
imsg chats --limit 5 --json

# show one chat's identity + participants
imsg group --chat-id 1 --json

# last 10 messages in chat 1 with attachments
imsg history --chat-id 1 --limit 10 --attachments

# filter by date and emit JSON
imsg history --chat-id 1 --start 2025-01-01T00:00:00Z --json

# live stream a chat
imsg watch --chat-id 1 --attachments --debounce 250ms

# stream tapback add/remove events too
imsg watch --chat-id 1 --reactions --json

# send a file or audio attachment
imsg send --to "+14155551212" --text "hi" --file ~/Desktop/voice.m4a --service imessage

# send a standard tapback to the most recent incoming message in a chat
imsg react --chat-id 1 --reaction like

# mark a chat as read
imsg read --to "+14155551212"

# advanced status check
imsg status

# launch Messages with injection (SIP must be disabled first)
imsg launch

# show typing indicator for 5s
imsg typing --to "+14155551212" --duration 5s
```

## Attachment notes
`--attachments` prints per-attachment lines with name, MIME, missing flag, and resolved path (tilde expanded). Only metadata is shown; files aren’t copied.

`imsg send --file` can send regular file attachments, including audio files such as
`.m4a`, through Messages.app AppleScript. Before handing the file to Messages,
`imsg` copies it under `~/Library/Messages/Attachments/imsg/` so Messages can
read it reliably. Sending still requires macOS Automation permission for the
calling terminal or parent app to control Messages.app.

## Watch notes
`imsg watch` starts at the newest message by default and streams messages written
after it starts. Use `--since-rowid <id>` to replay from a known cursor.

The watcher listens for filesystem events on `chat.db`, `chat.db-wal`, and
`chat.db-shm`, and also performs a lightweight fallback poll so missed macOS file
events do not leave the stream silent. Watching only needs Full Disk Access for
the calling terminal or parent app; Automation permission is only needed for
send/read/typing/reaction commands that control Messages.app.

## JSON output
`imsg chats --json` emits one JSON object per chat with fields: `id`, `name`, `identifier`, `service`, `last_message_at`, `guid`, `display_name`, `contact_name`, `is_group`, `participants`.
`imsg history --json` and `imsg watch --json` emit one JSON object per message with fields: `id`, `chat_id`, `chat_identifier`, `chat_guid`, `chat_name`, `participants`, `is_group`, `guid`, `reply_to_guid`, `destination_caller_id`, `sender`, `sender_name`, `is_from_me`, `text`, `created_at`, `attachments` (array of metadata with `filename`, `transfer_name`, `uti`, `mime_type`, `total_bytes`, `is_sticker`, `original_path`, `missing`), `reactions`.
When `watch --reactions --json` sees a tapback event, the message object also includes `is_reaction`, `reaction_type`, `reaction_emoji`, `is_reaction_add`, and `reacted_to_guid`.

Note: `reply_to_guid`, `destination_caller_id`, and `reactions` are read-only metadata.

## Permissions troubleshooting
If you see “unable to open database file” or empty output:
1) Grant Full Disk Access: System Settings → Privacy & Security → Full Disk Access → add your terminal.
2) If you launch `imsg` from an editor, Node process, gateway, or shell wrapper, grant Full Disk Access to that parent app too.
3) Also add the built-in Terminal.app (`/System/Applications/Utilities/Terminal.app`). macOS can require the default terminal even when you normally use iTerm, VS Code, or another launcher.
4) Toggle the Full Disk Access entry off and on after terminal, Homebrew, Node, or app updates; stale TCC grants can look enabled but still produce `authorization denied (code: 23)`.
5) Ensure Messages.app is signed in and `~/Library/Messages/chat.db` exists.
6) For send, allow the terminal under System Settings → Privacy & Security → Automation → Messages.

`imsg` opens `chat.db` read-only. It does not use SQLite `immutable=1` by default because immutable reads can miss new Messages rows and WAL-backed updates.

## Advanced Features (SIP-Off Only)
Advanced features (`typing`, `launch`, IMCore bridge) require injecting a helper dylib into `Messages.app`.

Important:
- This is opt-in only. Default send/history/watch flows do not need injection.
- `imsg launch` refuses to inject when SIP is enabled.
- `imsg status` is read-only and does not auto-launch or auto-inject.
- macOS 26 can also block Messages.app dylib injection with library validation.
  In that case `imsg status` reports advanced features unavailable even with SIP
  disabled; normal send/history/watch commands still work.

Setup:
1) Disable SIP from Recovery mode: `csrutil disable`
2) Grant Full Disk Access to your terminal
3) Build helper dylib: `make build-dylib`
4) Launch with injection: `imsg launch`
5) Verify: `imsg status`

To revert after testing, re-enable SIP in Recovery mode: `csrutil enable`.

## Testing
```bash
make test
```

Note: `make test` applies a small patch to SQLite.swift to silence a SwiftPM warning about `PrivacyInfo.xcprivacy`.

## Linting & formatting
```bash
make lint
make format
```

## Core library
The reusable Swift core lives in `Sources/IMsgCore` and is consumed by the CLI target. Apps can depend on the `IMsgCore` library target directly.
