"""Hermes plugin for two explicitly allowlisted Mailbox.org VTODO collections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import contextlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import ical
from .dav import CalDAV, DavError
from .queue import PilotPoller, QueueStore
from .secrets import SecretUnavailable, get_dav_password
from .settings import SettingsError, load_settings

_SKILL = Path(__file__).with_name("SKILL.md")
_DATE_CUE = re.compile(
    r"(?i)\b(?:fällig(?:\s+am)?|bis|deadline|due(?:\s+date)?)(?:\s*[:=]|\s+)?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|übermorgen|morgen|heute|"
    r"in\s+\d+\s+(?:tagen?|wochen?))\b"
)
_DATE_MENTION = re.compile(r"(?i)\b(?:fällig|bis|deadline|due)\b")
_APPROVAL = re.compile(r"(?i)^freigabe\s*:?\s+(FREI-[A-F0-9]{8})$")
_MAX_NOTE = 12000


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _safe_text(value: Any, limit: int = _MAX_NOTE) -> str:
    text = str(value or "")
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return text[:limit].strip()


def _client():
    settings = load_settings()
    password = get_dav_password()
    try:
        dav = CalDAV(settings.server_url, settings.username, password, settings.collections)
    finally:
        password = ""
    return settings, dav, QueueStore(settings.state_db)


def _due_from_explicit_text(text: str) -> tuple[date | None, str | None, bool]:
    """Accept only a deadline explicitly cued by a date or German relative date."""
    match = _DATE_CUE.search(text)
    if not match:
        return None, None, bool(_DATE_MENTION.search(text))
    raw = match.group(1)
    try:
        lowered = raw.casefold()
        if lowered in {"heute", "morgen", "übermorgen"}:
            due = date.today() + timedelta(days={"heute": 0, "morgen": 1, "übermorgen": 2}[lowered])
        elif lowered.startswith("in "):
            amount, unit = re.fullmatch(r"in\s+(\d+)\s+(tagen?|wochen?)", lowered).groups()
            days = int(amount) * (7 if unit.startswith("woche") else 1)
            if days > 3650:
                raise ValueError("relative due date is too far away")
            due = date.today() + timedelta(days=days)
        else:
            due = date.fromisoformat(raw) if "-" in raw else datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return None, None, True
    cleaned = (text[: match.start()] + text[match.end() :]).strip(" \t\n,;:-")
    return due, cleaned, False


def _stable_signal_uid(message_id: str, chat_id: str, user_id: str) -> str:
    source = "\n".join((message_id, chat_id, user_id)).encode("utf-8")
    return hashlib.sha256(source).hexdigest() + "@hermes-mailbox-todo.local"


def _create_capture(collection: str, text: str, uid: str):
    _, dav, _ = _client()
    due, summary, date_ambiguous = _due_from_explicit_text(text)
    summary = summary or text
    description = text
    payload = ical.build_vtodo(uid, summary, description, due)
    try:
        href, etag = dav.create_task(collection, payload)
        return href, etag, due, date_ambiguous
    except DavError as exc:
        if exc.status != 412:
            raise
        # The same admitted Signal message may be redelivered. Its deterministic
        # UID makes the retry idempotent; a resource is accepted only if its
        # identity and original captured text still match.
        href = dav.collections[collection] + quote(uid, safe="") + ".ics"
        existing, etag = dav.get_vtodo(collection, href, retry=False)
        if ical.task_identity(existing) != uid or ical.get_text(existing, "DESCRIPTION") != description:
            raise DavError("a conflicting VTODO already uses this capture identifier") from None
        return href, etag, due, date_ambiguous


def _row_for_uid(queue: QueueStore, uid: str):
    row = queue.task_record(uid, "Hermes")
    if not row:
        raise DavError("task is not in the delegated queue")
    if row["state"] in {"completed", "removed", "failed", "needs_review"}:
        raise DavError(f"task is terminal ({row['state']})")
    if row["state"] not in {"dispatching", "in_progress"}:
        raise DavError("task is not currently claimed for this run")
    return row


def _read_current(dav: CalDAV, row):
    payload, etag = dav.get_vtodo("Hermes", row["href"])
    if ical.task_identity(payload) != row["uid"]:
        raise DavError("task UID changed; execution stopped")
    status = ical.task_status(payload)
    if status == "COMPLETED":
        return payload, etag, "completed"
    if status == "CANCELLED":
        return payload, etag, "removed"
    return payload, etag, "open"


def _write_managed_update(
    dav: CalDAV,
    queue: QueueStore,
    uid: str,
    managed_lines: list[str],
    *,
    complete: bool = False,
) -> dict[str, Any]:
    for _ in range(3):
        row = _row_for_uid(queue, uid)
        payload, etag, status = _read_current(dav, row)
        if status == "completed":
            queue.set_state("Hermes", uid, "completed", lease_until=0)
            return {"already_completed": True, "etag": etag, "payload": payload}
        if status == "removed":
            queue.set_state("Hermes", uid, "removed", lease_until=0)
            return {"removed": True}
        if complete:
            updated = ical.set_completion(payload, managed_lines)
        else:
            updated = ical.set_managed_note(payload, managed_lines)
            updated = ical.set_properties(
                updated,
                {
                    "STATUS": "IN-PROCESS",
                    "LAST-MODIFIED": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                },
            )
        try:
            dav.put_vtodo("Hermes", row["href"], updated, etag)
        except DavError as exc:
            if exc.status == 412:
                # An iPhone write won the race. Re-fetch and merge only our
                # managed note block into the latest revision.
                continue
            raise
        verified, new_etag = dav.get_vtodo("Hermes", row["href"])
        if ical.task_identity(verified) != uid:
            raise DavError("VTODO UID changed during write verification")
        verified_note = ical.get_text(verified, "DESCRIPTION")
        if complete and ical.task_status(verified) == "COMPLETED" and (
            "[Hermes ToDo Pilot]" not in verified_note or "Status: Erledigt" not in verified_note
        ):
            # The owner may have completed the item on the iPhone in the
            # write/readback window. Keep that terminal state, but do not claim
            # that our result note was verified.
            queue.set_state("Hermes", uid, "completed", lease_until=0)
            return {
                "already_completed": True,
                "verified": False,
                "etag": new_etag,
                "payload": verified,
            }
        if "[Hermes ToDo Pilot]" not in verified_note:
            raise DavError("Mailbox.org did not retain the managed task note")
        if complete:
            if ical.task_status(verified) != "COMPLETED":
                raise DavError("completion was not confirmed by Mailbox.org")
            if "Status: Erledigt" not in verified_note:
                raise DavError("completion note did not survive readback")
            if not queue.finish(uid, new_etag, "Hermes"):
                raise DavError("completion could not be committed to the local queue")
        else:
            if ical.task_status(verified) == "COMPLETED":
                queue.set_state("Hermes", uid, "completed", lease_until=0)
                return {"already_completed": True, "etag": new_etag, "payload": verified}
            queue.update_etag("Hermes", uid, new_etag, "in_progress")
        return {"already_completed": False, "etag": new_etag, "payload": verified}
    queue.set_state("Hermes", uid, "needs_review", lease_until=0)
    raise DavError("repeated concurrent edits; task left for review")


def _tool_start(params: dict[str, Any], **kwargs) -> str:
    del kwargs
    uid = _safe_text(params.get("uid"), 200)
    if not uid:
        return _json({"success": False, "message": "Missing task UID."})
    try:
        _, dav, queue = _client()
        row = _row_for_uid(queue, uid)
        if row["state"] not in {"dispatching", "in_progress"}:
            return _json({"success": False, "message": "This task is not currently queued for work."})
        payload, etag, status = _read_current(dav, row)
        if status == "completed":
            queue.set_state("Hermes", uid, "completed", lease_until=0)
            return _json({"success": True, "already_completed": True, "message": "The iPhone marked this task complete; it was left unchanged."})
        if status == "removed":
            queue.set_state("Hermes", uid, "removed", lease_until=0)
            return _json({"success": False, "message": "The task was cancelled and will not be started."})
        start_time = datetime.now(timezone.utc).isoformat(timespec="minutes")
        written = _write_managed_update(
            dav,
            queue,
            uid,
            ["Status: In Bearbeitung", f"Beginn: {start_time}"],
        )
        if written.get("already_completed"):
            return _json({"success": True, "already_completed": True, "message": "The iPhone marked this task complete; it was left unchanged."})
        if written.get("removed"):
            return _json({"success": False, "message": "The task was cancelled and will not be started."})
        current = written["payload"]
        row = queue.task_record(uid, "Hermes")
        approved_scope = row["approval_scope"] if row and row["approval_scope"] else None
        return _json(
            {
                "success": True,
                "task": {
                    "uid": uid,
                    "summary": ical.get_text(current, "SUMMARY"),
                    "notes": ical.get_text(current, "DESCRIPTION"),
                    "due": ical.due_date(current),
                    "status": ical.task_status(current),
                    "separate_approval_recorded_for": approved_scope,
                },
                "message": "Only this Hermes-list task was read.",
            }
        )
    except (DavError, SecretUnavailable, SettingsError) as exc:
        return _json({"success": False, "message": str(exc)})


def _tool_progress(params: dict[str, Any], **kwargs) -> str:
    del kwargs
    uid = _safe_text(params.get("uid"), 200)
    message = _safe_text(params.get("message"), 4000)
    if not uid or not message:
        return _json({"success": False, "message": "A task UID and progress note are required."})
    try:
        _, dav, queue = _client()
        result = _write_managed_update(
            dav,
            queue,
            uid,
            ["Status: In Bearbeitung", "Fortschritt: " + message],
        )
        if result.get("already_completed"):
            return _json({"success": True, "already_completed": True, "message": "The iPhone marked this task complete; no task field was changed."})
        if result.get("removed"):
            return _json({"success": False, "message": "The task was cancelled; no further work is allowed."})
        return _json({"success": True, "message": "Progress note was written and read back; existing notes and other VTODO properties were preserved."})
    except (DavError, SecretUnavailable, SettingsError) as exc:
        return _json({"success": False, "message": str(exc)})


def _tool_approval(params: dict[str, Any], **kwargs) -> str:
    del kwargs
    uid = _safe_text(params.get("uid"), 200)
    scope = _safe_text(params.get("scope"), 1000)
    if not uid or not scope:
        return _json({"success": False, "message": "Give the exact action that needs approval."})
    approval_id = "FREI-" + secrets.token_hex(4).upper()
    try:
        _, dav, queue = _client()
        result = _write_managed_update(
            dav,
            queue,
            uid,
            [
                "Status: Freigabe erforderlich",
                "Aktion: " + scope,
                "Signal-Befehl: Freigabe " + approval_id,
                "Der Freigabe-Befehl ist 24 Stunden gültig und gilt nur für diese Aktion.",
            ],
        )
        if result.get("already_completed"):
            return _json({"success": False, "message": "The iPhone already completed this task; no approval was registered."})
        if result.get("removed"):
            return _json({"success": False, "message": "The task was cancelled; no approval was registered."})
        if not queue.wait_for_approval(uid, approval_id, scope, "Hermes"):
            return _json({"success": False, "message": "The task could not enter the approval-wait state."})
        return _json(
            {
                "success": True,
                "approval_id": approval_id,
                "message": "Approval request saved to the task note. No external action was taken.",
            }
        )
    except (DavError, SecretUnavailable, SettingsError) as exc:
        return _json({"success": False, "message": str(exc)})


def _tool_complete(params: dict[str, Any], **kwargs) -> str:
    del kwargs
    uid = _safe_text(params.get("uid"), 200)
    result_text = _safe_text(params.get("result"), 4000)
    verification = _safe_text(params.get("verification"), 2000)
    if not uid or not result_text or not verification:
        return _json({"success": False, "message": "A result and a concrete verification statement are required."})
    try:
        _, dav, queue = _client()
        written = _write_managed_update(
            dav,
            queue,
            uid,
            [
                "Status: Erledigt",
                "Ergebnis: " + result_text,
                "Geprüft: " + verification,
                "Abgeschlossen (UTC): " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            ],
            complete=True,
        )
        if written.get("already_completed"):
            if not written.get("verified", True):
                return _json({"success": False, "already_completed": True, "message": "The iPhone completed this task concurrently, but the result note could not be verified. The task was left complete and unchanged."})
            return _json({"success": True, "already_completed": True, "message": "The iPhone had already completed this task; it was not reopened."})
        if written.get("removed"):
            return _json({"success": False, "message": "The task was removed; completion was not written."})
        return _json({"success": True, "verified": True, "message": "Mailbox.org readback confirmed the completion note and COMPLETED status."})
    except (DavError, SecretUnavailable, SettingsError) as exc:
        return _json({"success": False, "message": str(exc)})


def _source_dict(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    if hasattr(source, "to_dict"):
        value = source.to_dict()
        return value if isinstance(value, dict) else {}
    return {}


def _trusted_signal_source(platform: str, source: Any) -> bool:
    if (platform or "").casefold() != "signal":
        return False
    details = _source_dict(source)
    home = os.environ.get("SIGNAL_HOME_CHANNEL", "").strip()
    allowed = {
        item.strip()
        for item in os.environ.get("SIGNAL_ALLOWED_USERS", "").split(",")
        if item.strip()
    }
    chat_id = str(details.get("chat_id") or "").strip()
    sender = str(details.get("user_id") or "").strip()
    return bool(home and allowed and chat_id == home and sender in allowed)


def _annotate_signal_identity(event: Any, **kwargs):
    """Expose the native incoming identity through supported SessionSource metadata.

    Signal intentionally has no event.message_id because it cannot edit outgoing
    messages. Keep that field untouched. Do no DAV work before admission.
    """
    del kwargs
    source = getattr(event, "source", None)
    details = _source_dict(source)
    platform = getattr(getattr(source, "platform", None), "value", "")
    if details.get("profile") not in {None, "", "default"}:
        return None
    if not _trusted_signal_source(platform, source):
        return None
    if not re.match(r"(?is)^(merke|delegiere)\s*:", _safe_text(getattr(event, "text", ""), 8000)):
        return None
    raw = getattr(event, "raw_message", None)
    if not isinstance(raw, dict) or str(raw.get("sender") or "") != str(details.get("user_id") or ""):
        return None
    timestamp = raw.get("timestamp_ms")
    if isinstance(timestamp, bool) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(timestamp or "")):
        return None
    if not getattr(source, "message_id", None):
        source.message_id = str(timestamp)
    return None


def _capture_message(session_key: str, platform: str, source: Any, message_id: str, text: str, **kwargs):
    del kwargs
    raw = _safe_text(text, 8000)
    if not raw or (platform or "").casefold() != "signal":
        return None
    trusted = _trusted_signal_source(platform, source)
    details = _source_dict(source)
    chat_id = str(details.get("chat_id") or "")
    sender = str(details.get("user_id") or "")
    message_id = message_id or details.get("message_id")
    approval = _APPROVAL.fullmatch(raw)
    command_match = re.match(r"(?is)^(merke|delegiere)\s*:\s*(.+)$", raw)
    if not trusted and (approval or command_match):
        return {
            "action": "handled",
            "reply": "Dieser Befehl wurde nicht verarbeitet. Er ist nur im konfigurierten Signal-Heimkanal zulässig.",
        }
    if not trusted:
        return None
    if approval:
        try:
            settings = load_settings()
            queue = QueueStore(settings.state_db)
            approval_id = approval.group(1).upper()
            row = queue.task_record(
                next((r["uid"] for r in _pending_approval_rows(queue) if r["approval_id"] == approval_id), ""),
                "Hermes",
            )
            if row and queue.approve(approval_id, "Hermes"):
                return {
                    "action": "handled",
                    "reply": "Freigabe für genau diese Aufgabenaktion erfasst. Sie wurde nicht an eine andere Aufgabe übertragen.",
                }
            return {"action": "handled", "reply": "Kein gültiger, offener Freigabeantrag gefunden."}
        except (DavError, SecretUnavailable, SettingsError):
            return {"action": "handled", "reply": "Freigabe konnte lokal nicht geprüft werden; es wurde nichts ausgeführt."}

    if not command_match:
        return None
    command = command_match.group(1).casefold()
    captured = command_match.group(2).strip()
    if not captured:
        return None
    if not message_id:
        return {"action": "handled", "reply": "Die Signal-Nachricht hatte keine stabile ID; sie wurde nicht gespeichert."}
    collection = "Inbox" if command == "merke" else "Hermes"
    uid = _stable_signal_uid(str(message_id), chat_id, sender)
    try:
        _, _, due, date_ambiguous = _create_capture(collection, captured, uid)
        if collection == "Inbox":
            reply = "In Eingang (Inbox) gespeichert; diese Liste wird nicht ausgeführt."
        else:
            reply = "In Hermes gespeichert und für den nächsten fünfminütigen Abgleich vorgemerkt."
        if due:
            reply += f" Fälligkeit: {due.isoformat()}."
        elif date_ambiguous:
            reply += " Kein eindeutiges Fälligkeitsdatum eingetragen; nutze für eine Frist z. B. „bis 2026-10-15“."
        return {"action": "handled", "reply": reply}
    except (DavError, SecretUnavailable, SettingsError):
        return {
            "action": "handled",
            "reply": "Die Aufgabe konnte nicht sicher gespeichert werden. Es wurde nichts an andere Sammlungen gesendet.",
        }


def _pending_approval_rows(queue: QueueStore):
    # The lookup is strictly limited to the explicit Hermes list, and only the
    # pending approval identifiers are read; task bodies are never queried.
    with contextlib.closing(queue._connect()) as conn:
        return conn.execute(
            """SELECT uid,approval_id FROM tasks
               WHERE collection='Hermes' AND state='waiting_approval'
               AND approval_id IS NOT NULL AND updated_at>?""",
            (datetime.now(timezone.utc).timestamp() - 24 * 60 * 60,),
        ).fetchall()


def register(ctx):
    if _SKILL.exists():
        ctx.register_skill("mailbox-hermes-todos", str(_SKILL))

    ctx.register_hook("pre_gateway_dispatch", _annotate_signal_identity)
    ctx.register_hook("post_gateway_admission", _capture_message)

    common_uid = {
        "type": "string",
        "description": "The single UID supplied in the current queued-task context.",
    }
    ctx.register_tool(
        name="mailbox_todo_start",
        toolset="mailbox_todo",
        schema={
            "name": "mailbox_todo_start",
            "description": "Fetch and start exactly one queued VTODO from the approved Hermes collection. Never lists or reads other tasks.",
            "parameters": {
                "type": "object",
                "properties": {"uid": common_uid},
                "required": ["uid"],
            },
        },
        handler=_tool_start,
    )
    ctx.register_tool(
        name="mailbox_todo_progress",
        toolset="mailbox_todo",
        schema={
            "name": "mailbox_todo_progress",
            "description": "Append a readable progress note to the current Hermes VTODO while preserving user notes and all other properties.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": common_uid,
                    "message": {"type": "string", "description": "Short status or question for the owner."},
                },
                "required": ["uid", "message"],
            },
        },
        handler=_tool_progress,
    )
    ctx.register_tool(
        name="mailbox_todo_request_approval",
        toolset="mailbox_todo",
        schema={
            "name": "mailbox_todo_request_approval",
            "description": "Record the exact purchase, booking, third-party message, destructive, or privileged action needing separate approval. This tool does not perform it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": common_uid,
                    "scope": {"type": "string", "description": "The exact action and its important limits."},
                },
                "required": ["uid", "scope"],
            },
        },
        handler=_tool_approval,
    )
    ctx.register_tool(
        name="mailbox_todo_complete",
        toolset="mailbox_todo",
        schema={
            "name": "mailbox_todo_complete",
            "description": "Mark one delegated task complete only after its work and verification are done; performs conditional ETag update and readback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": common_uid,
                    "result": {"type": "string", "description": "Concise result to preserve in the task note."},
                    "verification": {"type": "string", "description": "Concrete evidence that the requested work is complete."},
                },
                "required": ["uid", "result", "verification"],
            },
        },
        handler=_tool_complete,
    )
