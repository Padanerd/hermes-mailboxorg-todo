"""One-time secure setup and metadata-only CalDAV collection discovery."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .dav import CalDAV, CollectionInfo, DavError
from .queue import QueueStore
from .secrets import SecretUnavailable, clear_dav_password, get_dav_password, request_mailbox_credentials, request_mailbox_username, store_dav_password
from .settings import DEFAULT_CONFIG, DEFAULT_DB, DEFAULT_SERVER


class OnboardingBlocked(RuntimeError):
    pass


def _select_exact(name: str, collections: list[CollectionInfo]) -> str | None:
    matches = [c for c in collections if c.display_name.casefold() == name.casefold()]
    if len(matches) > 1:
        raise OnboardingBlocked(f"Mehrere Sammlungen heißen {name}; eindeutige Zuordnung nötig.")
    if matches and not matches[0].supports_vtodo:
        raise OnboardingBlocked(
            f"Die vorhandene Sammlung {name} unterstützt kein VTODO. Sie bleibt unverändert."
        )
    return matches[0].url if matches else None


def _write_config(username: str, collections: dict[str, str], config_path: Path, db_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_path.parent, 0o700)
    payload = {
        "server_url": DEFAULT_SERVER,
        "username": username,
        "collections": collections,
        "state_db": str(db_path),
    }
    fd, temporary = tempfile.mkstemp(prefix=".pilot-", dir=config_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
        os.chmod(config_path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def onboard(
    config_path: str | Path = DEFAULT_CONFIG,
    db_path: str | Path = DEFAULT_DB,
    *,
    reuse_keyring: bool = False,
    create_missing: bool = True,
    inbox_name: str = "Inbox",
) -> list[CollectionInfo]:
    config_path = Path(config_path).expanduser()
    db_path = Path(db_path).expanduser()
    if reuse_keyring:
        password = get_dav_password()
        username = request_mailbox_username()
    else:
        username, password = request_mailbox_credentials()
        store_dav_password(password)
    try:
        dav = CalDAV(DEFAULT_SERVER, username, password)
        home_url, listed = dav.discover()
        # Only collection display names and the VTODO support property are
        # reported. No task resource or VTODO body is requested here.
        for item in sorted(listed, key=lambda c: (c.display_name.casefold(), c.url)):
            label = item.display_name or "(ohne Namen)"
            print(f"Sammlung: {label} | VTODO: {'ja' if item.supports_vtodo else 'nein'}")

        display_names = {"Inbox": inbox_name, "Hermes": "Hermes"}
        selected = {role: _select_exact(name, listed) for role, name in display_names.items()}
        missing = [name for name, url in selected.items() if url is None]
        if missing:
            if not create_missing:
                raise OnboardingBlocked("Die privaten Aufgabenlisten Inbox und Hermes müssen zuerst in Mailbox Office angelegt werden.")
            # Do not create a duplicate display name or alter an existing
            # non-task calendar. Missing exact names are safe to create.
            for name in missing:
                try:
                    selected[name] = dav.create_vtodo_collection(home_url, name)
                except DavError as exc:
                    exc.operation = "create_collection"
                    raise

        dav.set_allowlist(selected)
        _, verified = dav.discover()
        by_url = {item.url: item for item in verified}
        for name, url in selected.items():
            item = by_url.get(url)
            if not item or not item.supports_vtodo or item.display_name.casefold() != display_names[name].casefold():
                raise OnboardingBlocked(f"Mailbox.org hat die Sammlung {name} nicht als VTODO bestätigt.")

        # This first snapshot records only DAV child URLs and ETags. Existing
        # tasks are not downloaded, queued, or executed.
        queue = QueueStore(db_path)
        queue.baseline(dav.list_resources("Hermes"), "Hermes")
        _write_config(username, selected, config_path, db_path)
        print("Mailbox.org ToDo-Pilot eingerichtet; initiale Hermes-Aufgaben nur als ETag-Bestand erfasst.")
        return verified
    except DavError as exc:
        if exc.status == 401:
            clear_dav_password()
            step = {
                "principal_discovery": "DAV-Principal ermitteln",
                "home_discovery": "CalDAV-Aufgabenablage ermitteln",
                "collection_discovery": "Sammlungsmetadaten auflisten",
                "create_collection": "neue Aufgabenliste anlegen",
            }.get(exc.operation, "CalDAV-Einrichtung")
            raise OnboardingBlocked(
                f"Mailbox.org hat den Schritt '{step}' mit HTTP 401 abgewiesen. "
                "Das beweist allein kein falsches Passwort. Der eigene Pilot-Keyring-Eintrag wurde entfernt."
            ) from None
        if exc.status in {403, 404, 405, 409}:
            step = "neue Aufgabenliste anlegen (MKCALENDAR)" if exc.operation == "create_collection" else "CalDAV-Einrichtung"
            raise OnboardingBlocked(
                f"Mailbox.org hat den Schritt '{step}' mit HTTP {exc.status} abgewiesen; es wurden keine vorhandenen Listen geändert."
            ) from None
        raise OnboardingBlocked("CalDAV-Einrichtung fehlgeschlagen; Antwortdetails wurden nicht protokolliert.") from None
    finally:
        # Python cannot guarantee memory erasure, but the credential is never
        # placed in a file, argument, environment variable, or log.
        password = ""


def main() -> int:
    try:
        onboard()
        return 0
    except SecretUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OnboardingBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
