# Test report and publication privacy review

## Pilot evidence

The original private pilot completed 25 offline regression tests and live
acceptance with marked, harmless test tasks. Its owner confirmed iPhone
visibility following the checks for capture and delegated-task results.

| Check | Observed result |
| --- | --- |
| Metadata-only discovery | Collection metadata and VTODO support discovered without reading unrelated task bodies |
| Initial task inventory | Existing tasks baselined from href/ETag; not executed |
| Signal capture | One marked task read back from the capture list with its explicitly specified due date; owner confirmed phone visibility |
| New delegated task | Real Hermes worker ran once, wrote a checked arithmetic result and COMPLETED |
| Note preservation | Original owner note retained on server readback |
| Repeat poll / process restart | Completed task not claimed again; attempt count unchanged |
| Unchanged scheduled polls | Cron reported model execution suppressed |
| Concurrent writes | Two live DAV clients: stale ETag rejected with HTTP 412; newer note and priority preserved |
| Access failure | Secret lookup deliberately denied in an isolated queue: bounded retries, backoff and one outage transition |
| Recovery | Restored real DAV access produced one recovery transition |

Mailbox.org's unquoted DAV listing ETags and quoted HTTP ETags, plus header
case differences, were found during live tests and covered by regression tests.
Signal's native transport timestamp bridges its intentionally empty message-ID
field through plugin hooks without changing Hermes core.

## Limits of the evidence

- The concurrency test used two real DAV clients, not a synchronized physical
  iPhone write race.
- The failure test denied local secret lookup; it did not revoke the production
  credential or simulate every HTTP/server outage.
- Restart evidence covers a fresh monitor/queue process, not a full machine reboot.
- The public installation helper and generic documentation were adapted from
  the private installation; a new installation on another machine has not been
  accepted. The live evidence above concerns the original pilot.
- No claim of a complete security audit, broad compatibility, or exactly-once
  external transactions is made.
- Configurable command aliases and English Signal replies were added after the
  original pilot acceptance. That acceptance does not verify these additions;
  the updated implementation has not undergone a new live Signal/iPhone test.

## Publication review

The publication was assembled from an explicit source-file allowlist in a new
directory and a fresh Git history. Live configuration, queues, logs, sessions,
credentials and machine-specific acceptance helpers were excluded.

Personal home paths, actual collection URLs, generated DAV usernames, Signal
identities, local Cron IDs and private test task identifiers are omitted from
this report. Fixtures in the offline suite are synthetic. The publisher's
GitHub identity remains public as the repository owner and commit author.

Before publication, the exact staged files were checked for credential patterns,
personal paths and installation identifiers, and compared locally against the
pilot credential, its generated username, configured collection URLs and Signal
identities without printing those values. Git commit metadata uses GitHub's
noreply address. This is a review of the published snapshot; it cannot guarantee
that future contributions or issue reports contain no sensitive information.
