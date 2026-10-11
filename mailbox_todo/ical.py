"""Small, loss-preserving helpers for the VTODO properties this pilot edits.

Unknown properties and components are retained verbatim. Only DESCRIPTION and
the explicitly requested completion properties are changed.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Iterable


class ICalendarError(ValueError):
    pass


def unfold(payload: str) -> list[str]:
    lines = payload.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def _property(line: str) -> tuple[str, str, str] | None:
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    name = left.split(";", 1)[0].upper()
    if not re.fullmatch(r"[A-Z0-9-]+", name):
        return None
    return name, left, value


def _vtodo_bounds(lines: list[str]) -> tuple[int, int]:
    starts = [i for i, line in enumerate(lines) if line.strip().upper() == "BEGIN:VTODO"]
    if len(starts) != 1:
        raise ICalendarError("expected exactly one VTODO")
    start = starts[0]
    for end in range(start + 1, len(lines)):
        if lines[end].strip().upper() == "END:VTODO":
            return start, end
    raise ICalendarError("VTODO has no END")


def _values(lines: list[str], name: str) -> list[tuple[int, str, str]]:
    start, end = _vtodo_bounds(lines)
    found: list[tuple[int, str, str]] = []
    depth = 1
    for i in range(start + 1, end):
        marker = lines[i].strip().upper()
        if marker.startswith("BEGIN:"):
            depth += 1
            continue
        if marker.startswith("END:"):
            depth = max(1, depth - 1)
            continue
        if depth != 1:
            continue
        prop = _property(lines[i])
        if prop and prop[0] == name.upper():
            found.append((i, prop[1], prop[2]))
    return found


def unescape_text(value: str) -> str:
    """Decode RFC 5545 TEXT escapes without treating HTML as markup."""
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            i += 1
            escaped = value[i]
            out.append("\n" if escaped in "nN" else escaped)
        else:
            out.append(value[i])
        i += 1
    return "".join(out)


def escape_text(value: str) -> str:
    value = value.replace("\\", "\\\\")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\n", "\\n")
    value = value.replace(";", "\\;").replace(",", "\\,")
    return value


def _fold_content_line(line: str) -> list[str]:
    """Fold a new RFC 5545 content line at 75 UTF-8 octets."""
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    limit = 75
    for char in line:
        size = len(char.encode("utf-8"))
        if current and used + size > limit:
            chunks.append("".join(current))
            current = []
            used = 0
            limit = 74  # continuation lines start with one SP octet
        current.append(char)
        used += size
    chunks.append("".join(current))
    return [chunks[0], *[" " + chunk for chunk in chunks[1:]]]


def get_text(payload: str, name: str) -> str:
    matches = _values(unfold(payload), name)
    return unescape_text(matches[-1][2]) if matches else ""


def get_property_value(payload: str, name: str) -> str | None:
    matches = _values(unfold(payload), name)
    return matches[-1][2] if matches else None


def task_identity(payload: str) -> str:
    uid = get_property_value(payload, "UID")
    if not uid:
        raise ICalendarError("VTODO has no UID")
    return unescape_text(uid)


def task_status(payload: str) -> str:
    return (get_property_value(payload, "STATUS") or "NEEDS-ACTION").upper()


def due_date(payload: str) -> str | None:
    matches = _values(unfold(payload), "DUE")
    if not matches:
        return None
    _, left, value = matches[-1]
    if "VALUE=DATE" in left.upper():
        if not re.fullmatch(r"\d{8}", value):
            return None
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    if re.fullmatch(r"\d{8}", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    if re.fullmatch(r"\d{8}T\d{6}Z", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}T{value[9:11]}:{value[11:13]}:{value[13:15]}Z"
    return value


def set_properties(payload: str, replacements: dict[str, str | None]) -> str:
    """Replace named properties in one VTODO, preserving all other raw lines."""
    lines = unfold(payload)
    start, end = _vtodo_bounds(lines)
    names = {name.upper() for name in replacements}
    kept: list[str] = []
    depth = 1
    for line in lines[start + 1 : end]:
        marker = line.strip().upper()
        if marker.startswith("BEGIN:"):
            depth += 1
            kept.append(line)
            continue
        if marker.startswith("END:"):
            depth = max(1, depth - 1)
            kept.append(line)
            continue
        prop = _property(line)
        if depth == 1 and prop and prop[0] in names:
            continue
        kept.append(line)
    inserted = [
        physical
        for name, value in replacements.items()
        if value is not None
        for physical in _fold_content_line(f"{name.upper()}:{value}")
    ]
    lines[start + 1 : end] = inserted + kept
    # Re-fold the complete component so pre-existing folded properties remain valid.
    folded = [physical for line in lines if line != "" for physical in _fold_content_line(line)]
    return "\r\n".join(folded)


def merge_managed_note(existing: str, lines: Iterable[str]) -> str:
    """Replace our own note block while preserving all user-authored text."""
    begin = "[Hermes ToDo Pilot]"
    end = "[/Hermes ToDo Pilot]"
    block = begin + "\n" + "\n".join(lines) + "\n" + end
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _: block, existing, count=1)
    if begin in existing or end in existing:
        # A partial marker may be user content or a damaged prior update. Keep
        # it unchanged and append our new block rather than deleting anything.
        return existing.rstrip() + ("\n\n" if existing.strip() else "") + block
    return existing.rstrip() + ("\n\n" if existing.strip() else "") + block


def set_managed_note(payload: str, managed_lines: Iterable[str]) -> str:
    existing = get_text(payload, "DESCRIPTION")
    merged = merge_managed_note(existing, managed_lines)
    return set_properties(payload, {"DESCRIPTION": escape_text(merged)})


def build_vtodo(
    uid: str,
    summary: str,
    description: str,
    due: date | None = None,
    now: datetime | None = None,
) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Hermes Agent//Mailbox.org ToDo Pilot//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VTODO",
        f"UID:{escape_text(uid)}",
        f"DTSTAMP:{stamp}",
        f"CREATED:{stamp}",
        f"LAST-MODIFIED:{stamp}",
        f"SUMMARY:{escape_text(summary)}",
        f"DESCRIPTION:{escape_text(description)}",
        "STATUS:NEEDS-ACTION",
        "CLASS:PRIVATE",
    ]
    if due is not None:
        lines.append(f"DUE;VALUE=DATE:{due.strftime('%Y%m%d')}")
    lines.extend(["END:VTODO", "END:VCALENDAR", ""])
    return "\r\n".join(physical for line in lines for physical in _fold_content_line(line))


def set_completion(payload: str, managed_lines: Iterable[str], now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    completed = now.strftime("%Y%m%dT%H%M%SZ")
    updated = set_managed_note(payload, managed_lines)
    return set_properties(
        updated,
        {
            "STATUS": "COMPLETED",
            "PERCENT-COMPLETE": "100",
            "COMPLETED": completed,
            "LAST-MODIFIED": completed,
        },
    )
