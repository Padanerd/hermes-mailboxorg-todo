"""GNOME Secret Service access and a masked pinentry handoff."""

from __future__ import annotations

import subprocess
import urllib.parse

SERVICE = "hermes-mailbox-todo"
ACCOUNT = "default"


class SecretUnavailable(RuntimeError):
    pass


def get_dav_password(timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SecretUnavailable("DAV password is unavailable in the unlocked local keyring") from None
    if result.returncode != 0 or not result.stdout:
        raise SecretUnavailable("DAV password is unavailable in the unlocked local keyring")
    password = result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout
    if not password:
        raise SecretUnavailable("DAV password is unavailable in the unlocked local keyring")
    return password


def _assuan_command(proc: subprocess.Popen, command: str) -> list[str]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(command + "\n")
    proc.stdin.flush()
    response: list[str] = []
    while True:
        line = proc.stdout.readline()
        if line == "":
            raise SecretUnavailable("local masked input was closed")
        line = line.rstrip("\r\n")
        if line.startswith(("D ", "S ")):
            response.append(line)
        elif line.startswith("OK"):
            return response
        elif line.startswith("ERR"):
            raise SecretUnavailable("local masked input was cancelled or unavailable")


def _pinentry(prompt: str, description: str) -> str:
    try:
        proc = subprocess.Popen(
            ["/usr/bin/pinentry-gnome3"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        raise SecretUnavailable("GNOME pinentry is not available") from None
    try:
        greeting = proc.stdout.readline() if proc.stdout else ""
        if not greeting.startswith("OK"):
            raise SecretUnavailable("GNOME pinentry did not start")
        _assuan_command(proc, "SETTITLE Mailbox.org ToDo Pilot")
        _assuan_command(proc, "SETDESC " + urllib.parse.quote(description, safe=" -"))
        _assuan_command(proc, "SETPROMPT " + urllib.parse.quote(prompt, safe=" -"))
        reply = _assuan_command(proc, "GETPIN")
        data = next((line[2:] for line in reply if line.startswith("D ")), None)
        if data is None:
            raise SecretUnavailable("local masked input returned no value")
        # Assuan data escapes non-printable bytes and percent signs as %HH.
        value = urllib.parse.unquote_to_bytes(data).decode("utf-8")
        if not value:
            raise SecretUnavailable("local input was empty")
        return value
    finally:
        try:
            _assuan_command(proc, "BYE")
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
            proc.wait()


def request_mailbox_username() -> str:
    username = _pinentry(
        "CalDAV app username:",
        "Enter the username shown by Mailbox.org when creating the CalDAV/CardDAV app password. Use that generated username, not automatically your email address. The matching password is stored separately in GNOME Keyring.",
    ).strip()
    if not username or len(username) > 512 or ":" in username or any(ord(c) < 32 or ord(c) == 127 for c in username):
        raise SecretUnavailable("CalDAV app username is invalid")
    return username


def request_mailbox_credentials() -> tuple[str, str]:
    username = request_mailbox_username()
    password = _pinentry(
        "DAV app password:",
        "Enter the separate, revocable DAV app password. It will be stored only in GNOME Keyring.",
    )
    return username, password


def store_dav_password(password: str) -> None:
    if not password or "\n" in password or "\r" in password:
        raise SecretUnavailable("DAV password has an unsupported format")
    # Replacing this pilot's own dedicated item is intentional. No other
    # Secret Service items are searched, read, or removed.
    subprocess.run(
        ["secret-tool", "clear", "service", SERVICE, "account", ACCOUNT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    try:
        result = subprocess.run(
            [
                "secret-tool",
                "store",
                "--label=Mailbox.org Hermes ToDo pilot DAV app password",
                "service",
                SERVICE,
                "account",
                ACCOUNT,
            ],
            input=password + "\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SecretUnavailable("GNOME Keyring could not store the DAV password") from None
    if result.returncode != 0:
        raise SecretUnavailable("GNOME Keyring could not store the DAV password")
    stored = get_dav_password()
    if stored != password:
        raise SecretUnavailable("GNOME Keyring verification failed")


def clear_dav_password() -> None:
    subprocess.run(
        ["secret-tool", "clear", "service", SERVICE, "account", ACCOUNT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
