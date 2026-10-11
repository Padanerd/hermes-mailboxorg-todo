"""Non-secret pilot configuration. The DAV password is never stored here."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SERVER = "https://dav.mailbox.org/"
DEFAULT_CONFIG = Path.home() / ".hermes" / "plugins" / "mailbox-todo" / "pilot.json"
DEFAULT_DB = Path.home() / ".hermes" / "data" / "mailbox-todo" / "queue.sqlite3"
DEFAULT_COMMANDS = {"Inbox": ("Remember", "Merke"), "Hermes": ("Delegate", "Delegiere")}


def parse_commands(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {"Inbox", "Hermes"}:
        raise SettingsError("commands must specify exactly Inbox and Hermes")
    result = {}
    seen = set()
    for role, aliases in value.items():
        if not isinstance(aliases, (list, tuple)) or not 1 <= len(aliases) <= 10:
            raise SettingsError("each command role needs one to ten aliases")
        normalized = []
        for alias in aliases:
            if not isinstance(alias, str) or not 1 <= len(alias) <= 40 or alias != alias.strip():
                raise SettingsError("command aliases must be short names without surrounding whitespace")
            if not alias[0].isalpha() or any(not (c.isalpha() or c in " -") for c in alias):
                raise SettingsError("command aliases may contain letters, spaces and hyphens only")
            folded = alias.casefold()
            if folded in seen or folded in {"freigabe", "approve"}:
                raise SettingsError("command aliases must be unique and cannot use approval keywords")
            seen.add(folded)
            normalized.append(alias)
        result[role] = tuple(normalized)
    return result


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    server_url: str
    username: str
    collections: dict[str, str]
    state_db: Path
    commands: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_COMMANDS))
    reply_language: str = "en"


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.environ.get("MAILBOX_TODO_CONFIG", DEFAULT_CONFIG)).expanduser()
    try:
        mode = config_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise SettingsError("pilot configuration permissions must be 0600 or stricter")
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError("Mailbox ToDo pilot is not configured") from None
    except (OSError, ValueError):
        raise SettingsError("Mailbox ToDo pilot configuration is unreadable") from None
    if not isinstance(data, dict):
        raise SettingsError("Mailbox ToDo pilot configuration is invalid")
    server_url = data.get("server_url")
    username = data.get("username")
    collections = data.get("collections")
    state_db = data.get("state_db", str(DEFAULT_DB))
    if not isinstance(server_url, str) or not server_url.startswith("https://"):
        raise SettingsError("Mailbox ToDo pilot server URL is invalid")
    if not isinstance(username, str) or not username.strip():
        raise SettingsError("Mailbox ToDo pilot account is missing")
    if not isinstance(collections, dict) or set(collections) != {"Inbox", "Hermes"}:
        raise SettingsError("Mailbox ToDo pilot must have exactly Inbox and Hermes collections")
    if any(not isinstance(url, str) or not url.startswith("https://dav.mailbox.org/") for url in collections.values()):
        raise SettingsError("Mailbox ToDo pilot collection URL is outside the approved Mailbox.org host")
    if collections["Inbox"] == collections["Hermes"]:
        raise SettingsError("Mailbox ToDo pilot collections must be distinct")
    commands = parse_commands(data.get("commands", DEFAULT_COMMANDS))
    reply_language = data.get("reply_language", "en")
    if not isinstance(reply_language, str) or reply_language not in {"en", "de"}:
        raise SettingsError("reply_language must be en or de")
    return Settings(server_url, username.strip(), dict(collections), Path(state_db).expanduser(), commands, reply_language)
