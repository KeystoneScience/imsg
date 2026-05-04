# Groups

## What counts as a group
- `chat.chat_identifier` or `chat.guid` contains `;+;`, for example
  `iMessage;+;chat1234567890`.
- `SERVICE;-;TARGET` is a direct 1:1 chat, for example `iMessage;-;+15551234567`,
  and is deliberately not flagged as a group.
- Direct chats typically use a single handle (phone/email) with no `;+;`.

## Where the identifiers live
- `chat.ROWID` -> `chat_id` (stable within one DB).
- `chat.chat_identifier` -> group handle (used by Messages).
- `chat.guid` -> group GUID (often same chat handle semantics).
- `chat.display_name` -> group name (optional).
- Participants in `chat_handle_join` + `handle`.

## Sending to a group
- `imsg send --chat-id <rowid>` (preferred; DB local).
- `imsg send --chat-identifier <handle>` (portable).
- `imsg send --chat-guid <guid>` (portable).
- Uses AppleScript `chat id "<handle>"` for group sends (Jared pattern).
- Attachments supported same as direct sends.
- On macOS 26/Tahoe, Messages.app can report success while creating an empty
  unjoined SMS row instead of delivering to the group. `imsg` detects that ghost
  row and reports the send as failed.

## Inbound metadata (JSON)
The direct CLI (`imsg chats`, `imsg history`, `imsg watch`) and JSON-RPC surface include:
- `chat_id`
- `chat_identifier`
- `chat_guid`
- `chat_name`
- `participants` (array of handles)
- `is_group`

`chat_id` is preferred for routing within one machine/DB.

### Participants exclude the local user
`participants` is sourced from Messages.app's `chat_handle_join` table, which
stores external handles. The local user's handle is implicit and message-specific:
use `is_from_me` plus `destination_caller_id` on sent messages when that distinction
matters.

## Focused group lookup
- `imsg group --chat-id <rowid>` prints id, identifier, guid, name, service,
  `is_group`, and participants for one chat. It works for direct chats too and
  supports `--json`.

## Notes
- Group send uses chat handle, not `buddy`.
- Messages from self may have empty `sender`; prefer `SenderName` + chat metadata.
