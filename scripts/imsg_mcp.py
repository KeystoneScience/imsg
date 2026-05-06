#!/usr/bin/env python3
"""Stdio MCP server for the local imsg CLI.

This server intentionally wraps the existing Swift CLI instead of duplicating
Messages.app behavior. Read operations are local and request/response. Mutating
operations are disabled by default and require an inspected payload hash plus an
environment gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVER_NAME = "imsg"
SERVER_VERSION = "0.6.0"
ROOT = Path(__file__).resolve().parents[1]
MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"
ADDRESSBOOK_ROOT = Path.home() / "Library" / "Application Support" / "AddressBook"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Codex imsg"
OP_LOG = APP_SUPPORT / "operation-log.jsonl"
MAX_SEND_ATTACHMENT_BYTES = 25 * 1024 * 1024
DANGEROUS_ATTACHMENT_SUFFIXES = {
    ".app",
    ".command",
    ".dmg",
    ".pkg",
    ".scpt",
    ".sh",
    ".terminal",
    ".workflow",
}
REACTIONS = {"love", "like", "dislike", "laugh", "emphasis", "question"}
SENSITIVE_LOG_KEYS = {
    "approval_note",
    "file",
    "path",
    "query",
    "recipient",
    "text",
    "to",
}


class ToolError(Exception):
    pass


_CONTACT_INDEX: dict[str, Any] | None = None


def json_text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}


def error_text(message: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def stable_hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clamp_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def optional_int(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} must be an integer") from exc


def string_list(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def strip_service_prefix(handle: str) -> str:
    trimmed = handle.strip()
    for prefix in ("iMessage;-;", "iMessage;+;", "SMS;-;", "SMS;+;", "any;-;", "any;+;"):
        if trimmed.startswith(prefix):
            return trimmed[len(prefix) :]
    return trimmed


def phone_digits(value: str) -> str:
    return re.sub(r"\D+", "", strip_service_prefix(value))


def phone_lookup_keys(value: str) -> list[str]:
    digits = phone_digits(value)
    if not digits:
        return []
    keys = [digits]
    if len(digits) == 10:
        keys.append("1" + digits)
    if len(digits) == 11 and digits.startswith("1"):
        keys.append(digits[1:])
    if len(digits) > 10:
        keys.append(digits[-10:])
    return list(dict.fromkeys(keys))


def display_name_from_row(row: sqlite3.Row) -> str | None:
    parts = []
    for key in ("ZNICKNAME", "ZFIRSTNAME", "ZMIDDLENAME", "ZLASTNAME"):
        value = row[key] if key in row.keys() else None
        if value:
            parts.append(str(value).strip())
    if parts:
        if row["ZNICKNAME"]:
            return str(row["ZNICKNAME"]).strip()
        return " ".join(part for part in parts if part)
    for key in ("ZNAME", "ZORGANIZATION"):
        value = row[key] if key in row.keys() else None
        if value:
            return str(value).strip()
    return None


def addressbook_dbs() -> list[Path]:
    candidates = [ADDRESSBOOK_ROOT / "AddressBook-v22.abcddb"]
    candidates.extend(sorted((ADDRESSBOOK_ROOT / "Sources").glob("*/AddressBook-v22.abcddb")))
    return [path for path in candidates if path.exists()]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def select_column(alias: str, columns: set[str], column: str) -> str:
    if column in columns:
        return f"{alias}.{column} AS {column}"
    return f"NULL AS {column}"


def owner_join_expr(alias: str, columns: set[str]) -> str | None:
    candidates = []
    if "ZOWNER" in columns:
        candidates.append(f"{alias}.ZOWNER")
    candidates.extend(f"{alias}.{column}" for column in sorted(columns) if re.fullmatch(r"Z\d+_OWNER", column))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return f"COALESCE({', '.join(candidates)})"


def load_contacts_index() -> dict[str, Any]:
    global _CONTACT_INDEX
    if _CONTACT_INDEX is not None:
        return _CONTACT_INDEX

    phones: dict[str, str] = {}
    emails: dict[str, str] = {}
    records = 0
    sources: list[str] = []
    errors: list[str] = []

    for db_path in addressbook_dbs():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error as exc:
            errors.append(f"{db_path}: {exc}")
            continue
        try:
            sources.append(str(db_path))
            record_columns = table_columns(conn, "ZABCDRECORD")
            phone_columns = table_columns(conn, "ZABCDPHONENUMBER")
            email_columns = table_columns(conn, "ZABCDEMAILADDRESS")
            name_selects = [
                select_column("r", record_columns, column)
                for column in ("ZNICKNAME", "ZFIRSTNAME", "ZMIDDLENAME", "ZLASTNAME", "ZNAME", "ZORGANIZATION")
            ]
            phone_owner = owner_join_expr("p", phone_columns)
            email_owner = owner_join_expr("e", email_columns)

            if record_columns and phone_columns and phone_owner:
                phone_selects = [
                    select_column("p", phone_columns, column)
                    for column in ("ZFULLNUMBER", "ZLOCALNUMBER", "ZCOUNTRYCODE", "ZAREACODE")
                ]
                phone_rows = conn.execute(
                    f"""
                    SELECT {", ".join(phone_selects + name_selects)}
                    FROM ZABCDPHONENUMBER p
                    LEFT JOIN ZABCDRECORD r ON {phone_owner} = r.Z_PK
                    """
                ).fetchall()
            else:
                phone_rows = []
            for row in phone_rows:
                name = display_name_from_row(row)
                if not name:
                    continue
                values = [row["ZFULLNUMBER"], row["ZLOCALNUMBER"]]
                combined = "".join(str(row[key] or "") for key in ("ZCOUNTRYCODE", "ZAREACODE", "ZLOCALNUMBER"))
                values.append(combined)
                for value in values:
                    if not value:
                        continue
                    for key in phone_lookup_keys(str(value)):
                        phones.setdefault(key, name)
                records += 1

            if record_columns and email_columns and email_owner:
                email_selects = [
                    select_column("e", email_columns, column)
                    for column in ("ZADDRESS", "ZADDRESSNORMALIZED")
                ]
                email_rows = conn.execute(
                    f"""
                    SELECT {", ".join(email_selects + name_selects)}
                    FROM ZABCDEMAILADDRESS e
                    LEFT JOIN ZABCDRECORD r ON {email_owner} = r.Z_PK
                    """
                ).fetchall()
            else:
                email_rows = []
            for row in email_rows:
                name = display_name_from_row(row)
                if not name:
                    continue
                for value in (row["ZADDRESS"], row["ZADDRESSNORMALIZED"]):
                    if value:
                        emails.setdefault(str(value).strip().lower(), name)
                records += 1
        except sqlite3.Error as exc:
            errors.append(f"{db_path}: {exc}")
        finally:
            conn.close()

    _CONTACT_INDEX = {
        "phones": phones,
        "emails": emails,
        "records": records,
        "sources": sources,
        "errors": errors,
    }
    return _CONTACT_INDEX


def contact_name_for_handle(handle: Any) -> str | None:
    if handle in (None, ""):
        return None
    raw = strip_service_prefix(str(handle))
    index = load_contacts_index()
    if "@" in raw:
        return index["emails"].get(raw.lower())
    for key in phone_lookup_keys(raw):
        if key in index["phones"]:
            return index["phones"][key]
    return None


def resolve_handles(handles: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for handle in handles:
        name = contact_name_for_handle(handle)
        if name:
            resolved[handle] = name
    return resolved


def enrich_chat(chat: dict[str, Any]) -> dict[str, Any]:
    participants = [str(item) for item in chat.get("participants") or []]
    participant_names = resolve_handles(participants)
    chat["participant_names"] = participant_names
    chat["resolved_participants"] = [
        {"handle": handle, "name": participant_names.get(handle)} for handle in participants
    ]
    existing_name = str(chat.get("display_name") or chat.get("contact_name") or chat.get("name") or "").strip()
    if existing_name:
        chat["resolved_name"] = existing_name
        return chat
    direct_name = None
    if not chat.get("is_group") and participants:
        direct_name = participant_names.get(participants[0])
    if direct_name:
        chat["contact_name"] = chat.get("contact_name") or direct_name
        chat["display_name"] = chat.get("display_name") or direct_name
        chat["resolved_name"] = direct_name
        return chat
    if chat.get("is_group") and participant_names:
        names = [participant_names.get(handle) or handle for handle in participants]
        label = ", ".join(names[:4])
        if len(names) > 4:
            label += f" +{len(names) - 4}"
        chat["resolved_name"] = label
    else:
        chat["resolved_name"] = str(chat.get("identifier") or chat.get("guid") or "")
    return chat


def enrich_message(message: dict[str, Any]) -> dict[str, Any]:
    sender = str(message.get("sender") or "")
    sender_name = str(message.get("sender_name") or "").strip()
    if message.get("is_from_me"):
        message["sender_display_name"] = "Me"
    elif not sender_name and sender:
        sender_name = contact_name_for_handle(sender) or ""
        if sender_name:
            message["sender_name"] = sender_name
        message["sender_display_name"] = sender_name or sender
    else:
        message["sender_display_name"] = sender_name or sender
    participants = [str(item) for item in message.get("participants") or []]
    participant_names = resolve_handles(participants)
    message["participant_names"] = participant_names
    chat_name = str(message.get("chat_name") or "").strip()
    if not chat_name:
        if not message.get("is_group") and participants:
            chat_name = participant_names.get(participants[0]) or ""
        elif participant_names:
            names = [participant_names.get(handle) or handle for handle in participants]
            chat_name = ", ".join(names[:4])
    if chat_name:
        message["chat_display_name"] = chat_name
    return message


def contacts_state() -> dict[str, Any]:
    index = load_contacts_index()
    return {
        "available": bool(index["phones"] or index["emails"]),
        "phone_keys": len(index["phones"]),
        "email_keys": len(index["emails"]),
        "records_seen": index["records"],
        "sources_count": len(index["sources"]),
        "errors": index["errors"][:5],
    }


def redact_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    scrubbed: dict[str, Any] = {}
    for key, value in payload.items():
        if key in SENSITIVE_LOG_KEYS:
            if value in (None, "", [], {}):
                scrubbed[f"{key}_present"] = False
            elif isinstance(value, list):
                scrubbed[f"{key}_count"] = len(value)
                scrubbed[f"{key}_sha256"] = stable_hash(value)
            else:
                scrubbed[f"{key}_present"] = True
                scrubbed[f"{key}_sha256"] = stable_hash(str(value))
        else:
            scrubbed[key] = value
    return scrubbed


def log_operation(kind: str, payload: dict[str, Any]) -> None:
    try:
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(APP_SUPPORT, 0o700)
        except OSError:
            pass
        event = {"at": now_iso(), "kind": kind, **redact_for_log(payload)}
        with OP_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        try:
            os.chmod(OP_LOG, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def command_candidate() -> tuple[list[str] | None, str, bool]:
    env_bin = os.environ.get("IMSG_BIN")
    if env_bin:
        return [env_bin], f"IMSG_BIN={env_bin}", False
    local_bin = ROOT / "bin" / "imsg"
    if local_bin.exists() and os.access(local_bin, os.X_OK):
        return [str(local_bin)], str(local_bin), False
    for build_bin in (ROOT / ".build" / "release" / "imsg", ROOT / ".build" / "debug" / "imsg"):
        if build_bin.exists() and os.access(build_bin, os.X_OK):
            return [str(build_bin)], str(build_bin), False
    swift = shutil.which("swift")
    if swift:
        return [swift, "run", "--package-path", str(ROOT), "imsg"], f"{swift} run --package-path {ROOT} imsg", True
    path_bin = shutil.which("imsg")
    if path_bin:
        return [path_bin], path_bin, False
    return None, "not found", False


def run_imsg(args: list[str], *, timeout: int = 120) -> str:
    command, display, uses_swift = command_candidate()
    if not command:
        raise ToolError(
            "Could not find imsg. Build this repo with `make build`, install `imsg` on PATH, "
            "or set IMSG_BIN=/path/to/imsg."
        )
    effective_timeout = max(timeout, 180) if uses_swift else timeout
    try:
        result = subprocess.run(
            command + args,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=effective_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Could not execute imsg command {display}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"Timed out running imsg command after {effective_timeout}s: {display}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ToolError(f"imsg command failed: {' '.join(args)}\n{detail}")
    return result.stdout


def parse_ndjson(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Expected imsg JSON output but could not parse line: {line[:160]}") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def db_quick_check(path: Path = MESSAGES_DB) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        result["ok"] = False
        result["error"] = "Messages database does not exist. Open Messages.app and make sure it has synced."
        return result
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only=ON")
            quick = conn.execute("PRAGMA quick_check").fetchone()
            result["ok"] = bool(quick and quick[0] == "ok")
            result["quick_check"] = quick[0] if quick else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["ok"] = False
        result["error"] = (
            f"Could not open Messages chat.db read-only: {exc}. Grant Full Disk Access to Codex "
            "and to Terminal/iTerm if testing from a shell, then restart the app."
        )
    return result


def base_args(args: dict[str, Any]) -> list[str]:
    db = str(args.get("db") or "").strip()
    return ["--db", db] if db else []


def append_history_args(command: list[str], args: dict[str, Any]) -> list[str]:
    command += ["--limit", str(clamp_int(args.get("limit"), default=50, maximum=1000))]
    for key, flag in (("start", "--start"), ("end", "--end")):
        value = str(args.get(key) or "").strip()
        if value:
            command += [flag, value]
    participants = string_list(args.get("participants"))
    if participants:
        command += ["--participants", ",".join(participants)]
    if args.get("attachments"):
        command.append("--attachments")
    if args.get("convert_attachments"):
        command.append("--convert-attachments")
    command.append("--json")
    return command


def list_chats(args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_int(args.get("limit"), default=20, maximum=200)
    rows = parse_ndjson(run_imsg(["chats", *base_args(args), "--limit", str(limit), "--json"]))
    rows = [enrich_chat(row) for row in rows]
    return {"chats": rows, "count": len(rows)}


def get_chat(args: dict[str, Any]) -> dict[str, Any]:
    chat_id = optional_int(args.get("chat_id"), "chat_id")
    if chat_id is None:
        raise ToolError("chat_id is required")
    rows = parse_ndjson(run_imsg(["group", *base_args(args), "--chat-id", str(chat_id), "--json"]))
    return {"chat": enrich_chat(rows[0]) if rows else None}


def read_messages(args: dict[str, Any]) -> dict[str, Any]:
    chat_id = optional_int(args.get("chat_id"), "chat_id")
    if chat_id is None:
        raise ToolError("chat_id is required")
    command = ["history", *base_args(args), "--chat-id", str(chat_id)]
    rows = parse_ndjson(run_imsg(append_history_args(command, args)))
    rows = [enrich_message(row) for row in rows]
    return {"messages": rows, "count": len(rows), "chat_id": chat_id}


def text_fields_for_match(message: dict[str, Any]) -> dict[str, str]:
    fields = {}
    for key in (
        "text",
        "sender",
        "sender_name",
        "sender_display_name",
        "chat_name",
        "chat_display_name",
        "resolved_name",
        "chat_identifier",
        "chat_guid",
    ):
        value = message.get(key)
        if value not in (None, ""):
            fields[key] = str(value)
    participant_names = message.get("participant_names")
    if isinstance(participant_names, dict) and participant_names:
        fields["participant_names"] = "\n".join(str(value) for value in participant_names.values() if value)
    return fields


def search_messages(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    case_sensitive = bool(args.get("case_sensitive", False))
    needle = query if case_sensitive else query.lower()
    max_results = clamp_int(args.get("limit"), default=25, maximum=100)
    per_chat_limit = clamp_int(args.get("per_chat_limit"), default=100, maximum=1000)
    chat_id = optional_int(args.get("chat_id"), "chat_id")
    matches: list[dict[str, Any]] = []
    searched_chat_ids: list[int] = []

    def scan_chat(target_chat_id: int) -> None:
        nonlocal matches
        if len(matches) >= max_results:
            return
        local_args = {**args, "chat_id": target_chat_id, "limit": per_chat_limit}
        try:
            messages = read_messages(local_args)["messages"]
        except ToolError:
            return
        searched_chat_ids.append(target_chat_id)
        for message in messages:
            fields = text_fields_for_match(message)
            haystack = "\n".join(fields.values())
            haystack_cmp = haystack if case_sensitive else haystack.lower()
            if needle not in haystack_cmp:
                continue
            match = dict(message)
            match["_match_fields"] = [key for key, value in fields.items() if needle in (value if case_sensitive else value.lower())]
            matches.append(match)
            if len(matches) >= max_results:
                return

    if chat_id is not None:
        scan_chat(chat_id)
    else:
        max_chats = clamp_int(args.get("max_chats"), default=25, maximum=200)
        chats = list_chats({**args, "limit": max_chats})["chats"]
        for chat in chats:
            target_chat_id = optional_int(chat.get("id"), "chat.id")
            if target_chat_id is not None:
                scan_chat(target_chat_id)
            if len(matches) >= max_results:
                break

    return {
        "query": query,
        "matches": matches,
        "count": len(matches),
        "searched_chat_ids": searched_chat_ids,
        "scope_note": "Search scans recent history per chat; increase max_chats/per_chat_limit for a wider pass.",
    }


def require_iso_window(args: dict[str, Any]) -> tuple[str, str]:
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    if not start:
        raise ToolError("start is required")
    if not end:
        raise ToolError("end is required")
    return start, end


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars < 1 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def sent_summary(args: dict[str, Any]) -> dict[str, Any]:
    start, end = require_iso_window(args)
    limit = clamp_int(args.get("limit"), default=1000, maximum=5000)
    max_text_chars = clamp_int(args.get("max_text_chars"), default=2000, maximum=20000)
    include_text = args.get("include_text") is not False
    command = [
        "report",
        *base_args(args),
        "--direction",
        "sent",
        "--start",
        start,
        "--end",
        end,
        "--limit",
        str(limit),
        "--json",
    ]
    participants = string_list(args.get("participants"))
    if participants:
        command += ["--participants", ",".join(participants)]
    rows = [enrich_message(row) for row in parse_ndjson(run_imsg(command, timeout=180))]

    conversations: dict[str, dict[str, Any]] = {}
    for message in rows:
        chat_id = str(message.get("chat_id") or "")
        key = chat_id or str(message.get("chat_guid") or message.get("chat_identifier") or "unknown")
        text = str(message.get("text") or "")
        rendered_text, truncated = truncate_text(text, max_text_chars)
        convo = conversations.setdefault(
            key,
            {
                "chat_id": message.get("chat_id"),
                "chat_identifier": message.get("chat_identifier"),
                "chat_guid": message.get("chat_guid"),
                "chat_display_name": message.get("chat_display_name") or message.get("chat_name") or key,
                "is_group": bool(message.get("is_group")),
                "participants": message.get("participants") or [],
                "participant_names": message.get("participant_names") or {},
                "message_count": 0,
                "text_chars": 0,
                "first_at": message.get("created_at"),
                "last_at": message.get("created_at"),
                "messages": [],
            },
        )
        convo["message_count"] += 1
        convo["text_chars"] += len(text)
        convo["last_at"] = message.get("created_at") or convo["last_at"]
        if message.get("participant_names"):
            convo["participant_names"].update(message["participant_names"])
        item = {
            "id": message.get("id"),
            "guid": message.get("guid"),
            "created_at": message.get("created_at"),
            "sender_display_name": message.get("sender_display_name"),
            "text_chars": len(text),
            "text_truncated": truncated,
        }
        if include_text:
            item["text"] = rendered_text
        convo["messages"].append(item)

    grouped = sorted(
        conversations.values(),
        key=lambda item: (str(item.get("first_at") or ""), str(item.get("chat_display_name") or "")),
    )
    return {
        "start": start,
        "end": end,
        "direction": "sent",
        "count": len(rows),
        "conversation_count": len(grouped),
        "limit": limit,
        "truncated_by_limit": len(rows) >= limit,
        "conversations": grouped,
        "scope_note": "Bulk report uses one imsg process and decodes attributedBody before grouping across chats.",
    }


def target_args(args: dict[str, Any]) -> list[str]:
    values: list[tuple[str, str]] = []
    for key, flag in (
        ("to", "--to"),
        ("chat_id", "--chat-id"),
        ("chat_identifier", "--chat-identifier"),
        ("chat_guid", "--chat-guid"),
    ):
        value = args.get(key)
        if value not in (None, ""):
            values.append((flag, str(value)))
    if len(values) != 1:
        raise ToolError("Provide exactly one target: to, chat_id, chat_identifier, or chat_guid.")
    result: list[str] = []
    for flag, value in values:
        result += [flag, value]
    return result


def validate_send_payload(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text") or "")
    file_value = str(args.get("file") or "").strip()
    if not text and not file_value:
        raise ToolError("text or file is required")
    file_info = None
    if file_value:
        path = Path(file_value).expanduser().resolve()
        if not path.exists():
            raise ToolError(f"Attachment does not exist: {path}")
        if not path.is_file():
            raise ToolError(f"Attachment must be a regular file: {path}")
        if path.suffix.lower() in DANGEROUS_ATTACHMENT_SUFFIXES:
            raise ToolError(f"Refusing potentially dangerous attachment type: {path.suffix}")
        size = path.stat().st_size
        if size > MAX_SEND_ATTACHMENT_BYTES:
            raise ToolError(f"Attachment exceeds {MAX_SEND_ATTACHMENT_BYTES} bytes: {path}")
        file_info = {"path": str(path), "bytes": size, "suffix": path.suffix.lower()}
    service = str(args.get("service") or "auto").strip().lower()
    if service not in {"auto", "imessage", "sms"}:
        raise ToolError("service must be auto, imessage, or sms")
    payload = {
        "type": "send",
        "target": {
            "to": args.get("to"),
            "chat_id": args.get("chat_id"),
            "chat_identifier": args.get("chat_identifier"),
            "chat_guid": args.get("chat_guid"),
        },
        "text": text,
        "file": file_info,
        "service": service,
        "region": str(args.get("region") or "US"),
    }
    target_args(args)
    return payload


def prepare_send(args: dict[str, Any]) -> dict[str, Any]:
    payload = validate_send_payload(args)
    token = stable_hash(payload)
    log_operation("prepare_send", {"target": payload["target"], "text": payload["text"], "file": payload["file"]})
    return {
        "send_preview": payload,
        "send_sha256": token,
        "send_gate": "Set ALLOW_IMSG_SEND=1, then call imsg_send_message with confirm_send=true, approval_note, and this send_sha256.",
    }


def send_message(args: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("ALLOW_IMSG_SEND") != "1":
        raise ToolError("Sending is disabled. Set ALLOW_IMSG_SEND=1 only after explicit user approval.")
    if args.get("confirm_send") is not True:
        raise ToolError("confirm_send=true is required")
    approval_note = str(args.get("approval_note") or "").strip()
    if not approval_note:
        raise ToolError("approval_note is required")
    expected = str(args.get("send_sha256") or "").strip()
    payload = validate_send_payload(args)
    actual = stable_hash(payload)
    if not expected or expected != actual:
        raise ToolError("send_sha256 does not match the current send payload. Re-run imsg_prepare_send.")
    command = ["send", *base_args(args), *target_args(args)]
    if payload["text"]:
        command += ["--text", payload["text"]]
    if payload["file"]:
        command += ["--file", payload["file"]["path"]]
    command += ["--service", payload["service"], "--region", payload["region"], "--json"]
    output = parse_ndjson(run_imsg(command, timeout=90))
    log_operation("send", {"target": payload["target"], "text": payload["text"], "file": payload["file"], "approval_note": approval_note})
    return {"ok": True, "result": output, "sent_payload_sha256": actual}


def validate_reaction_payload(args: dict[str, Any]) -> dict[str, Any]:
    chat_id = optional_int(args.get("chat_id"), "chat_id")
    if chat_id is None:
        raise ToolError("chat_id is required")
    reaction = str(args.get("reaction") or "").strip().lower()
    if reaction not in REACTIONS:
        raise ToolError("reaction must be one of: love, like, dislike, laugh, emphasis, question")
    return {"type": "reaction", "chat_id": chat_id, "reaction": reaction}


def latest_incoming_for_reaction(args: dict[str, Any]) -> dict[str, Any]:
    chat_id = optional_int(args.get("chat_id"), "chat_id")
    if chat_id is None:
        raise ToolError("chat_id is required")
    messages = read_messages({**args, "chat_id": chat_id, "limit": 25})["messages"]
    for message in messages:
        if message.get("is_from_me") or message.get("is_reaction"):
            continue
        text = str(message.get("text") or "")
        return {
            "id": message.get("id"),
            "guid": message.get("guid"),
            "created_at": message.get("created_at"),
            "sender": message.get("sender"),
            "sender_name": message.get("sender_name"),
            "text_excerpt": text[:240],
        }
    raise ToolError("Could not find a recent incoming message to react to in this chat.")


def prepare_reaction(args: dict[str, Any]) -> dict[str, Any]:
    payload = validate_reaction_payload(args)
    payload["latest_incoming"] = latest_incoming_for_reaction(args)
    token = stable_hash(payload)
    log_operation("prepare_reaction", payload)
    return {
        "reaction_preview": payload,
        "reaction_sha256": token,
        "reaction_gate": "Set ALLOW_IMSG_REACT=1, then call imsg_send_reaction with confirm_react=true, approval_note, and this reaction_sha256.",
    }


def send_reaction(args: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("ALLOW_IMSG_REACT") != "1":
        raise ToolError("Reactions are disabled. Set ALLOW_IMSG_REACT=1 only after explicit user approval.")
    if args.get("confirm_react") is not True:
        raise ToolError("confirm_react=true is required")
    approval_note = str(args.get("approval_note") or "").strip()
    if not approval_note:
        raise ToolError("approval_note is required")
    expected = str(args.get("reaction_sha256") or "").strip()
    payload = validate_reaction_payload(args)
    payload["latest_incoming"] = latest_incoming_for_reaction(args)
    actual = stable_hash(payload)
    if not expected or expected != actual:
        raise ToolError("reaction_sha256 does not match the current reaction payload. Re-run imsg_prepare_reaction.")
    output = parse_ndjson(
        run_imsg(
            [
                "react",
                *base_args(args),
                "--chat-id",
                str(payload["chat_id"]),
                "--reaction",
                payload["reaction"],
                "--json",
            ],
            timeout=90,
        )
    )
    log_operation("react", {**payload, "approval_note": approval_note})
    return {"ok": True, "result": output, "reaction_payload_sha256": actual}


def permissions_check(args: dict[str, Any]) -> dict[str, Any]:
    open_full_disk = bool(args.get("open_full_disk_access"))
    open_automation = bool(args.get("open_automation"))
    open_accessibility = bool(args.get("open_accessibility"))
    opened: list[str] = []
    if platform.system() == "Darwin":
        panes = []
        if open_full_disk:
            panes.append("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")
        if open_automation:
            panes.append("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")
        if open_accessibility:
            panes.append("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
        for pane in panes:
            try:
                subprocess.Popen(["open", pane])
                opened.append(pane)
            except OSError:
                pass
    state = get_state({"include_cli_status": False})
    return {
        "state": state,
        "opened": opened,
        "required": [
            "Full Disk Access for Codex, or the app that launches this plugin, for read/search history.",
            "Automation permission for Messages.app when sending or reacting.",
            "Accessibility permission may be needed for tapback reactions because imsg uses System Events UI automation.",
            "Contacts permission is optional and only improves name resolution.",
        ],
    }


def get_state(args: dict[str, Any]) -> dict[str, Any]:
    command, display, uses_swift = command_candidate()
    state: dict[str, Any] = {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "repo_root": str(ROOT),
        "imsg_command": {"available": command is not None, "display": display, "uses_swift_run": uses_swift},
        "messages_db": db_quick_check(),
        "send_enabled": os.environ.get("ALLOW_IMSG_SEND") == "1",
        "react_enabled": os.environ.get("ALLOW_IMSG_REACT") == "1",
        "contacts": contacts_state(),
    }
    if args.get("include_cli_status") and command is not None:
        try:
            state["imsg_status"] = parse_ndjson(run_imsg(["status", "--json"], timeout=60))
        except ToolError as exc:
            state["imsg_status_error"] = str(exc)
    return state


TOOLS: dict[str, dict[str, Any]] = {
    "imsg_get_state": {
        "description": "Check plugin state, imsg binary discovery, Messages database read access, and send/react gates.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_cli_status": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
        "handler": get_state,
    },
    "imsg_permissions_check": {
        "description": "Explain required macOS permissions and optionally open the relevant System Settings panes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "open_full_disk_access": {"type": "boolean", "default": False},
                "open_automation": {"type": "boolean", "default": False},
                "open_accessibility": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": permissions_check,
    },
    "imsg_list_chats": {
        "description": "List recent local Messages chats with identifiers, participants, and routing hints.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "additionalProperties": False,
        },
        "handler": list_chats,
    },
    "imsg_get_chat": {
        "description": "Inspect one Messages chat by chat_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer"},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "required": ["chat_id"],
            "additionalProperties": False,
        },
        "handler": get_chat,
    },
    "imsg_read_messages": {
        "description": "Read recent local iMessage/SMS history for a chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
                "start": {"type": "string", "description": "Inclusive ISO8601 start timestamp."},
                "end": {"type": "string", "description": "Exclusive ISO8601 end timestamp."},
                "participants": {"type": "array", "items": {"type": "string"}},
                "attachments": {"type": "boolean", "default": False},
                "convert_attachments": {"type": "boolean", "default": False},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "required": ["chat_id"],
            "additionalProperties": False,
        },
        "handler": read_messages,
    },
    "imsg_search_messages": {
        "description": "Search recent local iMessage/SMS history across one chat or recent chats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "chat_id": {"type": "integer", "description": "Optional single chat scope."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
                "max_chats": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
                "per_chat_limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                "case_sensitive": {"type": "boolean", "default": False},
                "start": {"type": "string", "description": "Inclusive ISO8601 start timestamp."},
                "end": {"type": "string", "description": "Exclusive ISO8601 end timestamp."},
                "participants": {"type": "array", "items": {"type": "string"}},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": search_messages,
    },
    "imsg_resolve_contacts": {
        "description": "Resolve phone numbers, emails, or Messages handles to local Contacts names when available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["handles"],
            "additionalProperties": False,
        },
        "handler": lambda args: {
            "resolved": resolve_handles(string_list(args.get("handles"))),
            "contacts": contacts_state(),
        },
    },
    "imsg_sent_summary": {
        "description": "Bulk-read sent messages across all chats for a date window, grouped by conversation for fast daily summaries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Inclusive ISO8601 start timestamp."},
                "end": {"type": "string", "description": "Exclusive ISO8601 end timestamp."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 1000},
                "participants": {"type": "array", "items": {"type": "string"}},
                "include_text": {"type": "boolean", "default": True},
                "max_text_chars": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 2000},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        "handler": sent_summary,
    },
    "imsg_prepare_send": {
        "description": "Inspect a Messages send payload and return the send_sha256 required before sending. Does not send.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "chat_id": {"type": "integer"},
                "chat_identifier": {"type": "string"},
                "chat_guid": {"type": "string"},
                "text": {"type": "string"},
                "file": {"type": "string"},
                "service": {"type": "string", "default": "auto"},
                "region": {"type": "string", "default": "US"},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "additionalProperties": False,
        },
        "handler": prepare_send,
    },
    "imsg_send_message": {
        "description": "Send an inspected Messages payload. Disabled unless ALLOW_IMSG_SEND=1, confirm_send=true, approval_note, and matching send_sha256 are present.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "chat_id": {"type": "integer"},
                "chat_identifier": {"type": "string"},
                "chat_guid": {"type": "string"},
                "text": {"type": "string"},
                "file": {"type": "string"},
                "service": {"type": "string", "default": "auto"},
                "region": {"type": "string", "default": "US"},
                "confirm_send": {"type": "boolean"},
                "approval_note": {"type": "string"},
                "send_sha256": {"type": "string"},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "required": ["confirm_send", "approval_note", "send_sha256"],
            "additionalProperties": False,
        },
        "handler": send_message,
    },
    "imsg_prepare_reaction": {
        "description": "Inspect a tapback reaction payload and return the reaction_sha256 required before reacting. Does not mutate Messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer"},
                "reaction": {"type": "string", "description": "love, like, dislike, laugh, emphasis, or question"},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "required": ["chat_id", "reaction"],
            "additionalProperties": False,
        },
        "handler": prepare_reaction,
    },
    "imsg_send_reaction": {
        "description": "Send a standard Messages tapback reaction. Disabled unless ALLOW_IMSG_REACT=1, confirm_react=true, approval_note, and matching reaction_sha256 are present.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer"},
                "reaction": {"type": "string", "description": "love, like, dislike, laugh, emphasis, or question"},
                "confirm_react": {"type": "boolean"},
                "approval_note": {"type": "string"},
                "reaction_sha256": {"type": "string"},
                "db": {"type": "string", "description": "Optional alternate chat.db path."},
            },
            "required": ["chat_id", "reaction", "confirm_react", "approval_note", "reaction_sha256"],
            "additionalProperties": False,
        },
        "handler": send_reaction,
    },
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": entry["description"], "inputSchema": entry["inputSchema"]}
        for name, entry in TOOLS.items()
    ]


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            result = error_text(f"Unknown tool: {name}")
        else:
            try:
                result = json_text(TOOLS[name]["handler"](args))
            except ToolError as exc:
                result = error_text(str(exc))
            except Exception as exc:
                result = error_text(f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method and method.startswith("notifications/"):
        return None
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle_request(message)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse or server error: {exc}"},
            }
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
