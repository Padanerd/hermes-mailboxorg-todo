# mailboxorg

A privacy-conscious **Mailbox.org CalDAV VTODO connector for Hermes Agent**.
Mailbox.org remains the canonical task store, including tasks used with iPhone
Reminders. This is an independent community pilot, not an official Mailbox.org
or Hermes product.

## Workflow

- Send `Merke: Buy coffee` in your existing Signal home channel to capture a
  task in Inbox. Inbox is never executed.
- An explicit deadline, such as `Merke: Review draft bis 2026-12-15`, becomes
  the task's due date. No deadline or time is invented.
- Send `Delegiere: Compare three options` or create a new task in Hermes to
  delegate work. Existing tasks are baselined at setup and never started.
- A separate Cron monitor checks about every five minutes. Unchanged output
  suppresses the model run and Signal delivery.
- Progress, questions and verified results are merged into the task note.
  Owner notes and other properties are preserved. Completion is read back
  before the local queue records success.

## Privacy and execution boundary

Discovery reads collection metadata and VTODO support, not all task contents.
Runtime operations are restricted to exactly two configured collection URLs
and their direct task resources. Contacts and other calendars are excluded.

Only the current delegated task and evidence necessary to do its work are
sent to the existing cloud model. This is **not an entirely local AI system**:
that task can contain personal information. Cloud provider retention and
Hermes session/output retention follow the user's existing configuration.
The SQLite queue stores identifiers, ETags and execution metadata rather than
task bodies; these identifiers and collection URLs are still private data.

The dedicated revocable DAV app password is entered in a local masked GNOME
Pinentry dialog and stored in Secret Service, never in a `.env` file, command
argument or source file. The generated DAV username and collection URLs are
stored in a private local configuration. Python cannot guarantee memory erasure.

Research, comparisons and drafts may run autonomously. Purchases, bookings,
third-party messages, destructive actions and privileged changes require
separate approval. The worker exposes only the web and pilot toolsets, with
MCP excluded. Recording an approval does not add transaction tools. Embedded
instructions in task notes or linked pages are untrusted data.

## Requirements

- A source/PM-managed Hermes installation with user plugins,
  `pre_gateway_dispatch`, `post_gateway_admission` and Cron monitor support.
  The pilot currently supports the **default profile only**.
- An already working Signal gateway with `SIGNAL_HOME_CHANNEL` and an explicit
  `SIGNAL_ALLOWED_USERS` allowlist in its existing configuration.
- Linux desktop session with unlocked GNOME Secret Service, `secret-tool`,
  and `/usr/bin/pinentry-gnome3` available. Setup stops if masked input is absent.
- A Mailbox.org **CalDAV/CardDAV** app credential: use its generated username
  and matching app password, not a file-oriented WebDAV credential.
- Two explicitly assigned private VTODO lists, Inbox and Hermes. The capture
  list can instead be named Eingang if selected explicitly during onboarding.

Connector runtime code uses the Python standard library. It does not modify
Hermes core, existing automations, other profiles or Signal services.

## Install manually

Review the code first. From this checkout, copy only the connector and monitor
into the default profile's user directories. **Stop if a pilot already exists**;
do not overwrite an existing installation or its private configuration.

```bash
test ! -e "$HOME/.hermes/plugins/mailbox-todo" || exit 1
test ! -e "$HOME/.hermes/scripts/mailbox-todo-watch.py" || exit 1
mkdir -p "$HOME/.hermes/plugins" "$HOME/.hermes/scripts"
cp -R mailbox_todo "$HOME/.hermes/plugins/mailbox-todo"
chmod 700 "$HOME/.hermes/plugins/mailbox-todo"
find "$HOME/.hermes/plugins/mailbox-todo" -type f -exec chmod 600 {} +
cp mailbox_todo/watch.py "$HOME/.hermes/scripts/mailbox-todo-watch.py"
chmod 600 "$HOME/.hermes/scripts/mailbox-todo-watch.py"
```

Create the two task lists in Mailbox Office before setup. Then, in this checkout,
run onboarding in your graphical session. It displays metadata-only collection
discovery and requests credentials through Pinentry:

```bash
python3 -c 'from mailbox_todo.onboard import onboard; onboard(create_missing=False, inbox_name="Inbox")'
```

Replace `inbox_name="Inbox"` with `inbox_name="Eingang"` only if that is your
explicitly selected capture list. Ambiguous names or missing VTODO support stop
setup. The configuration lives at `~/.hermes/plugins/mailbox-todo/pilot.json`
with mode 0600. Existing Hermes resources are baselined from href/ETag metadata.

```bash
hermes plugins enable mailbox-todo
python3 manage_pilot.py activate
```

`manage_pilot.py` uses the installed Hermes PM dependency generation and the
Cron API, not source updates. Set `HERMES_SOURCE` if your checkout is elsewhere.
It creates only the named pilot job, seeds the idle monitor baseline, and checks
that existing job definitions remain unchanged. Note the printed pilot job ID.
Use the gateway's supported plugin reload/restart procedure for your Hermes
version, then verify the plugin is active before sending a capture command.

## Reliability and limits

- Deterministic Signal UIDs use native message timestamp, sender and channel.
  Redelivery is idempotent; resending the same text as a new message creates a
  new capture.
- SQLite persists claims, deduplication, leases and bounded retries. Work that
  started before a crash is held for review rather than automatically repeated.
- UID and conditional ETag writes detect concurrent edits. HTTP 412 triggers
  a re-fetch and merge of the managed note block. Completed, cancelled and
  removed tasks are not reopened or newly started.
- Failures use timeouts, bounded retries and backoff. Quiet polling does not
  send a daily digest. Health transitions may send a generic status message.
- This is a pilot, not a promise of exactly-once external side effects or
  broad server/client compatibility. See [TEST_REPORT.md](TEST_REPORT.md).

## Pause and remove

Find the pilot ID with `hermes cron list`:

```bash
hermes cron pause <pilot-job-id>
hermes cron resume <pilot-job-id>
hermes plugins disable mailbox-todo
hermes cron remove <pilot-job-id>
```

Pausing the job stops processing/polling but keeps Signal capture available.
Disabling the plugin stops capture too. Remove only this plugin directory,
its `mailbox-todo-watch.py` script and its private data/config when rolling back.
Clear only its Secret Service item (`service=hermes-mailbox-todo`,
`account=default`) and revoke its own app password in Mailbox.org.
Keep task lists, iPhone accounts and other automations intact.

## Development and contributions

To run the existing offline regression suite:

```bash
python3 -m unittest discover -s tests -q
```

Bug reports and ideas are welcome through **Issues**. Pull requests are disabled
for now. Forks and local modifications remain permitted under the MIT license.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
