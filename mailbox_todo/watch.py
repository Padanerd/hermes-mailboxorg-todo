"""Hermes Cron monitor source. It emits a stable marker and never task contents."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path


def _load_plugin():
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    plugin_dir = Path(
        os.environ.get("MAILBOX_TODO_PLUGIN_DIR", hermes_home / "plugins" / "mailbox-todo")
    ).expanduser()
    entrypoint = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "mailbox_todo",
        entrypoint,
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("pilot plugin could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mailbox_todo"] = module
    spec.loader.exec_module(module)
    return module


def _marker(queue, event: dict[str, object] | None) -> str:
    """Keep exact monitor output stable until a new UID, attempt, or health state."""
    name = event.get("event") if event else None
    if name == "task":
        uid = str(event.get("uid", ""))
        row = queue.task_record(uid, "Hermes")
        attempt = int(row["attempts"]) if row else 0
        marker = f"task uid={uid} attempt={attempt}"
    elif name in {"connection_lost", "connection_recovered"}:
        marker = f"{name} reason={event.get('error_category', 'dav')}"
    elif name:
        marker = f"event={name}"
    else:
        marker = queue.get_meta("monitor:last_marker", "idle") or "idle"
    if name:
        queue.set_meta("monitor:last_marker", marker)
    return marker


def marker_hash_seed() -> str:
    """Cron monitor's safe starting value; no DAV poll and no mailbox task read."""
    return "idle"


def main() -> int:
    try:
        plugin = _load_plugin()
        settings = plugin.load_settings()
        queue = plugin.QueueStore(settings.state_db)
        _, next_poll, _ = queue.health()
        if time.time() < next_poll:
            print(_marker(queue, None))
            return 0
        try:
            _, dav, _ = plugin._client()
        except plugin.SecretUnavailable:
            event = queue.record_failure("secret_unavailable")
            value = {"event": event, "error_category": "secret_unavailable"} if event else None
            print(_marker(queue, value))
            return 0
        event = plugin.PilotPoller(dav, queue).tick()
        # Cron hashes exact stdout. It injects this opaque marker only when it
        # changes and suppresses unchanged checks without an LLM call.
        print(_marker(queue, event))
        return 0
    except Exception as exc:
        # Never print exception text: DAV, keyring, and subprocess errors can
        # contain user-controlled data.
        print(f"health_error={type(exc).__name__}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
