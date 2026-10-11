from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.error import HTTPError
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailbox_todo import ical, _capture_message, _write_managed_update
from mailbox_todo.dav import CalDAV, CollectionInfo, DavError, Resource
from mailbox_todo.queue import PilotPoller, QueueStore
from mailbox_todo.watch import _marker
from mailbox_todo.secrets import request_mailbox_credentials


SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTODO
UID:test-uid@local
DTSTAMP:20261011T100000Z
SUMMARY:Test task
DESCRIPTION:Owner note
PRIORITY:3
DUE;VALUE=DATE:20261015
STATUS:NEEDS-ACTION
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
DESCRIPTION:Reminder
END:VALARM
END:VTODO
END:VCALENDAR
"""


class FakeDAV:
    def __init__(self):
        self.collections = {
            "Inbox": "https://dav.mailbox.org/caldav/user/Inbox/",
            "Hermes": "https://dav.mailbox.org/caldav/user/Hermes/",
        }
        self.items: dict[str, tuple[str, str]] = {}
        self.get_count = 0
        self.put_count = 0
        self.create_count = 0
        self.list_count = 0

    def _href(self, collection: str, uid: str) -> str:
        from urllib.parse import quote
        return self.collections[collection] + quote(uid, safe="") + ".ics"

    def list_resources(self, collection: str) -> list[Resource]:
        self.list_count += 1
        prefix = self.collections[collection]
        return [
            Resource(href, etag)
            for href, (_, etag) in self.items.items()
            if href.startswith(prefix)
        ]

    def get_vtodo(self, collection: str, href: str, *, retry: bool = True) -> tuple[str, str]:
        del retry
        self.get_count += 1
        if not href.startswith(self.collections[collection]) or href not in self.items:
            raise DavError("not found", 404)
        return self.items[href]

    def create_task(self, collection: str, payload: str) -> tuple[str, str]:
        self.create_count += 1
        uid = ical.task_identity(payload)
        href = self._href(collection, uid)
        if href in self.items:
            raise DavError("already exists", 412)
        payload = payload.replace("\r\n", "\n").replace("\n", "\r\n")
        self.items[href] = (payload, '"e1"')
        return href, '"e1"'

    def put_vtodo(self, collection: str, href: str, payload: str, etag: str) -> str:
        self.put_count += 1
        if href not in self.items:
            raise DavError("not found", 404)
        _, current = self.items[href]
        if etag != current:
            raise DavError("conflict", 412)
        new_etag = f'"e{self.put_count + 1}"'
        self.items[href] = (payload, new_etag)
        return new_etag


class RacingDAV(FakeDAV):
    def __init__(self, race: str = "edit"):
        super().__init__()
        self.race = race
        self.raced = False

    def put_vtodo(self, collection: str, href: str, payload: str, etag: str) -> str:
        if not self.raced:
            self.raced = True
            current, _ = self.items[href]
            if self.race == "edit":
                current = ical.set_properties(
                    current,
                    {
                        "SUMMARY": ical.escape_text("Changed on iPhone"),
                        "DESCRIPTION": ical.escape_text("Owner note\nEdited on iPhone"),
                        "PRIORITY": "1",
                    },
                )
            else:
                current = ical.set_properties(current, {"STATUS": "COMPLETED"})
            self.items[href] = (current, '"iphone-etag"')
            raise DavError("conflict", 412)
        return super().put_vtodo(collection, href, payload, etag)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = QueueStore(Path(self.tmp.name) / "state.sqlite3")
        self.dav = FakeDAV()

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_baseline_records_only_href_and_etag(self):
        self.dav.items[self.dav._href("Hermes", "existing")] = (
            SAMPLE.replace("test-uid@local", "existing"),
            '"old"',
        )
        before = self.dav.get_count
        result = self.queue.scan(self.dav)
        self.assertTrue(result["baselined"])
        self.assertEqual(self.dav.get_count, before)
        self.assertEqual(self.queue.claim_next(), None)

    def test_new_uid_is_queued_once_and_claim_is_single_flight(self):
        self.queue.baseline([], "Hermes")
        payload = SAMPLE.replace("test-uid@local", "new-task")
        self.dav.create_task("Hermes", payload)
        self.assertEqual(self.queue.scan(self.dav)["new"], 1)
        self.assertEqual(self.queue.scan(self.dav)["new"], 0)
        uid = self.queue.claim_next()
        self.assertEqual(uid, "new-task")
        self.assertIsNone(self.queue.claim_next())
        row = self.queue.task_record(uid)
        self.assertEqual(row["state"], "dispatching")

    def test_removed_task_is_never_claimed(self):
        self.queue.baseline([], "Hermes")
        self.dav.create_task("Hermes", SAMPLE.replace("test-uid@local", "gone"))
        self.queue.scan(self.dav)
        href = self.dav._href("Hermes", "gone")
        self.dav.items.pop(href)
        self.queue.scan(self.dav)
        self.assertEqual(self.queue.task_record("gone")["state"], "removed")
        self.assertIsNone(self.queue.claim_next())

    def test_completed_task_is_terminal_after_external_edit(self):
        self.queue.baseline([], "Hermes")
        self.dav.create_task("Hermes", SAMPLE.replace("test-uid@local", "done"))
        self.queue.scan(self.dav)
        href = self.dav._href("Hermes", "done")
        payload, _ = self.dav.items[href]
        self.dav.items[href] = (ical.set_properties(payload, {"STATUS": "COMPLETED"}), '"iphone-done"')
        self.queue.scan(self.dav)
        self.assertEqual(self.queue.task_record("done")["state"], "completed")
        self.assertIsNone(self.queue.claim_next())

    def test_expired_prestart_lease_retries_are_bounded(self):
        self.queue.baseline([], "Hermes")
        self.dav.create_task("Hermes", SAMPLE.replace("test-uid@local", "retry"))
        self.queue.scan(self.dav)
        uid = self.queue.claim_next()
        self.assertEqual(uid, "retry")
        with self.queue._transaction() as conn:
            conn.execute(
                "UPDATE tasks SET lease_until=1,attempts=3 WHERE collection='Hermes' AND uid=?",
                (uid,),
            )
        self.queue.scan(self.dav)
        self.assertEqual(self.queue.task_record(uid)["state"], "failed")
        self.assertIsNone(self.queue.claim_next())

    def test_changed_etag_keeps_active_lease_and_restart_holds_started_task(self):
        self.queue.baseline([], 'Hermes')
        self.dav.create_task('Hermes', SAMPLE)
        self.queue.scan(self.dav)
        uid = self.queue.claim_next()
        lease = self.queue.task_record(uid)['lease_until']
        href = self.dav._href('Hermes', uid)
        payload, _ = self.dav.items[href]
        started = ical.set_managed_note(payload, ['Status: In Bearbeitung'])
        self.dav.items[href] = (started, '"changed-during-run"')
        self.queue.scan(self.dav)
        self.assertEqual(self.queue.task_record(uid)['lease_until'], lease)
        self.assertIsNone(self.queue.claim_next())
        self.queue.set_state('Hermes', uid, 'in_progress', lease_until=1)
        restarted = QueueStore(self.queue.path)
        restarted.scan(self.dav)
        self.assertEqual(restarted.task_record(uid)['state'], 'needs_review')
        self.assertEqual(restarted.task_record(uid)['attempts'], 1)
        self.assertIsNone(restarted.claim_next())

    def test_poll_is_silent_without_changes_and_notifies_once_after_outage(self):
        self.queue.baseline([], "Hermes")
        poller = PilotPoller(self.dav, self.queue)
        self.assertIsNone(poller.tick()["event"])
        self.assertIsNone(poller.tick()["event"])
        self.assertEqual(self.dav.get_count, 0)

        class Offline:
            def list_resources(self, _collection):
                raise DavError("network unreachable", retryable=True)

        offline = PilotPoller(Offline(), self.queue)
        self.assertIsNone(offline.tick()["event"])
        failures, next_poll, state = self.queue.health()
        self.assertEqual((failures, state), (1, "degraded"))
        self.assertGreater(next_poll, 0)
        self.assertIsNone(offline.tick()["event"])
        with self.queue._transaction() as conn:
            conn.execute("UPDATE meta SET value='0' WHERE key='health:next_poll'")
        self.assertIsNone(offline.tick()["event"])
        with self.queue._transaction() as conn:
            conn.execute("UPDATE meta SET value='0' WHERE key='health:next_poll'")
        self.assertEqual(offline.tick()["event"], "connection_lost")
        self.assertIsNone(offline.tick()["event"])
        failures, next_poll, state = self.queue.health()
        self.assertEqual(failures, 3)
        self.assertEqual(state, "offline")
        self.assertGreater(next_poll, 0)


class ICalendarTests(unittest.TestCase):
    def test_managed_note_preserves_owner_notes_and_other_properties(self):
        updated = ical.set_managed_note(SAMPLE, ["Status: In Bearbeitung", "Frage: none"])
        self.assertIn("Owner note", ical.get_text(updated, "DESCRIPTION"))
        self.assertIn("Status: In Bearbeitung", ical.get_text(updated, "DESCRIPTION"))
        self.assertEqual(ical.get_text(updated, "SUMMARY"), "Test task")
        self.assertEqual(ical.get_property_value(updated, "PRIORITY"), "3")
        self.assertEqual(ical.due_date(updated), "2026-10-15")
        self.assertIn("BEGIN:VALARM", updated)
        self.assertIn("DESCRIPTION:Reminder", updated)
        twice = ical.set_managed_note(updated, ["Status: Neuer Stand"])
        note = ical.get_text(twice, "DESCRIPTION")
        self.assertIn("Owner note", note)
        self.assertEqual(note.count("[Hermes ToDo Pilot]"), 1)
        self.assertIn("Status: Neuer Stand", note)

    def test_explicit_date_only_is_represented_as_date(self):
        from datetime import date
        created = ical.build_vtodo("uid", "Task", "Task bis 2026-10-15", date(2026, 10, 15))
        self.assertIn("DUE;VALUE=DATE:20261015", created)
        self.assertNotIn("DTEND", created)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in created.split("\r\n")))

    def test_folding_is_unfolded_before_preserving_a_long_existing_property(self):
        long_text = "Ä" * 120
        payload = SAMPLE.replace("DESCRIPTION:Owner note", "DESCRIPTION:" + long_text[:25] + "\r\n " + long_text[25:])
        updated = ical.set_managed_note(payload, ["Status: In Bearbeitung"])
        note = ical.get_text(updated, "DESCRIPTION")
        self.assertIn(long_text, note)
        self.assertIn("Status: In Bearbeitung", note)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in updated.split("\r\n")))


class FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 207):
        self.status = status
        self.headers = {"Content-Type": "application/xml; charset=utf-8"}
        self._body = body

    def read(self, limit: int = -1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class DiscoveryOpener:
    def __init__(self):
        self.calls = []
        self.responses = [
            b"""<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/caldav/user/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>""",
            b"""<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/caldav/user/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/caldav/user/calendar/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>""",
            b"""<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/caldav/user/calendar/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response><d:response><d:href>/caldav/user/calendar/Hermes/</d:href><d:propstat><d:prop><d:displayname>Hermes</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response><d:response><d:href>/caldav/user/calendar/Events/</d:href><d:propstat><d:prop><d:displayname>Events</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response></d:multistatus>""",
        ]

    def open(self, request, timeout=None):
        self.calls.append(request)
        if request.get_method() != "PROPFIND":
            raise AssertionError("discovery must not GET task bodies")
        body = self.responses.pop(0)
        return FakeHTTPResponse(body)


class CalDAVTests(unittest.TestCase):
    def test_unquoted_listing_etag_and_quoted_get_allow_single_queue_admission(self):
        class MailboxStyleOpener:
            gets = 0
            def open(self, request, timeout=None):
                if request.get_method() == 'GET':
                    self.gets += 1
                    response = FakeHTTPResponse(SAMPLE.encode(), 200)
                    response.headers = {'Content-Type':'text/calendar', 'Etag':'"server-version"'}
                    return response
                body = b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/caldav/hermes/test.ics</d:href><d:propstat><d:prop><d:resourcetype/><d:getetag>server-version</d:getetag></d:prop></d:propstat></d:response></d:multistatus>'
                return FakeHTTPResponse(body)
        opener = MailboxStyleOpener()
        dav = CalDAV('https://dav.mailbox.org/', 'user', 'hidden',
                     {'Inbox':'https://dav.mailbox.org/caldav/inbox/', 'Hermes':'https://dav.mailbox.org/caldav/hermes/'}, opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            queue = QueueStore(Path(directory) / 'queue.db')
            queue.baseline([])
            self.assertEqual(queue.scan(dav)['new'], 1)
            self.assertEqual(queue.scan(dav)['new'], 0)
            self.assertEqual(opener.gets, 1)
            self.assertEqual(queue.claim_next(), 'test-uid@local')
            self.assertIsNone(queue.claim_next())

    def test_get_etag_header_is_case_insensitive(self):
        for header in ('ETag', 'Etag', 'etag', 'ETAG'):
            class CalendarOpener:
                def open(self, request, timeout=None):
                    response = FakeHTTPResponse(SAMPLE.encode(), 200)
                    response.headers = {'content-type': 'text/calendar', header: '"live-style-etag"'}
                    return response
            with self.subTest(header=header):
                dav = CalDAV('https://dav.mailbox.org/', 'user', 'hidden',
                             {'Inbox': 'https://dav.mailbox.org/caldav/inbox/', 'Hermes': 'https://dav.mailbox.org/caldav/hermes/'}, opener=CalendarOpener())
                payload, etag = dav.get_vtodo('Hermes', 'https://dav.mailbox.org/caldav/hermes/test.ics')
                self.assertEqual(etag, '"live-style-etag"')
                self.assertEqual(payload, SAMPLE)

    def test_auth_failure_identifies_metadata_step_without_response_details(self):
        for fail_at, operation in enumerate(("principal_discovery", "home_discovery", "collection_discovery")):
            class Denied(DiscoveryOpener):
                def open(self, request, timeout=None):
                    if len(self.calls) == fail_at:
                        raise HTTPError(request.full_url, 401, "secret response", {}, None)
                    return super().open(request, timeout)
            with self.subTest(operation=operation):
                dav = CalDAV("https://dav.mailbox.org/", "generated-user", "hidden", opener=Denied())
                with self.assertRaises(DavError) as caught:
                    dav.discover()
                self.assertEqual(caught.exception.status, 401)
                self.assertEqual(caught.exception.operation, operation)
                self.assertNotIn("secret response", str(caught.exception))
                self.assertNotIn("hidden", str(caught.exception))

    def test_masked_input_accepts_generated_dav_username(self):
        with patch("mailbox_todo.secrets._pinentry", side_effect=[" generated-user ", "test-only-password"]) as prompt:
            username, password = request_mailbox_credentials()
        self.assertEqual(username, "generated-user")
        self.assertEqual(password, "test-only-password")
        self.assertIn("shown by Mailbox.org", prompt.call_args_list[0].args[1])

    def test_discovery_lists_metadata_without_fetching_tasks(self):
        opener = DiscoveryOpener()
        dav = CalDAV("https://dav.mailbox.org/", "user@example.test", "hidden", opener=opener)
        home, collections = dav.discover()
        self.assertEqual(home, "https://dav.mailbox.org/caldav/user/calendar/")
        self.assertEqual([(c.display_name, c.supports_vtodo) for c in collections], [
            ("Hermes", True),
            ("Events", False),
        ])
        self.assertEqual(len(opener.calls), 3)
        self.assertTrue(all(call.get_method() == "PROPFIND" for call in opener.calls))
        self.assertFalse(any(b"calendar-data" in call.data for call in opener.calls))
        self.assertNotIn("hidden", opener.calls[0].full_url)

    def test_collection_allowlist_blocks_other_urls(self):
        opener = DiscoveryOpener()
        dav = CalDAV(
            "https://dav.mailbox.org/",
            "user",
            "hidden",
            collections={"Inbox": "https://dav.mailbox.org/caldav/user/calendar/Inbox/",
                         "Hermes": "https://dav.mailbox.org/caldav/user/calendar/Hermes/"},
            opener=opener,
        )
        with self.assertRaises(DavError):
            dav.get_vtodo("Hermes", "https://dav.mailbox.org/caldav/user/calendar/Other/task.ics")
        self.assertEqual(opener.calls, [])

    def test_network_retry_is_bounded_and_error_does_not_expose_secret(self):
        class Unavailable:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout=None):
                self.calls += 1
                raise URLError("hidden credential must not appear")

        opener = Unavailable()
        sleeps = []
        dav = CalDAV(
            "https://dav.mailbox.org/",
            "user",
            "hidden",
            collections={"Inbox": "https://dav.mailbox.org/caldav/u/Inbox/",
                         "Hermes": "https://dav.mailbox.org/caldav/u/Hermes/"},
            opener=opener,
            sleep=sleeps.append,
        )
        with self.assertRaises(DavError) as caught:
            dav.list_resources("Hermes")
        self.assertEqual(opener.calls, 3)
        self.assertEqual(len(sleeps), 2)
        self.assertNotIn("hidden", str(caught.exception))


class PluginTests(unittest.TestCase):
    def setUp(self):
        from mailbox_todo.settings import DEFAULT_COMMANDS
        options = patch("mailbox_todo._capture_options", return_value=(DEFAULT_COMMANDS, "en"))
        options.start()
        self.addCleanup(options.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = QueueStore(Path(self.tmp.name) / "state.sqlite3")
        self.dav = FakeDAV()

    def tearDown(self):
        self.tmp.cleanup()

    def test_signal_native_timestamp_bridges_idless_adapter_and_deduplicates(self):
        import mailbox_todo
        mailbox_todo._client = lambda: (None, self.dav, self.queue)
        os.environ['SIGNAL_HOME_CHANNEL'] = 'home'
        os.environ['SIGNAL_ALLOWED_USERS'] = 'alice'
        class Source:
            platform = SimpleNamespace(value='signal')
            message_id = None
            def to_dict(self):
                return {'chat_id':'home', 'user_id':'alice', 'message_id':self.message_id}
        for timestamp in (1791720000000, 1791720000000, 1791720000001):
            source = Source()
            event = SimpleNamespace(source=source, text='Merke: Adaptertest', message_id=None,
                                    raw_message={'sender':'alice','timestamp_ms':timestamp})
            mailbox_todo._annotate_signal_identity(event)
            self.assertIsNone(event.message_id)
            self.assertEqual(source.message_id, str(timestamp))
            reply = _capture_message('s','signal',source.to_dict(),None,event.text)
            self.assertIn('Saved', reply['reply'])
        self.assertEqual(len(self.dav.items), 2)

    def test_signal_native_identity_rejects_missing_or_mismatched_transport_data(self):
        import mailbox_todo
        os.environ['SIGNAL_HOME_CHANNEL'] = 'home'
        os.environ['SIGNAL_ALLOWED_USERS'] = 'alice'
        class Source:
            platform = SimpleNamespace(value='signal')
            message_id = None
            def to_dict(self):
                return {'chat_id':'home', 'user_id':'alice', 'message_id':self.message_id}
        for raw in ({'sender':'alice','timestamp_ms':0}, {'sender':'mallory','timestamp_ms':1791720000000}):
            source = Source()
            event = SimpleNamespace(source=source,text='Merke: test',raw_message=raw)
            mailbox_todo._annotate_signal_identity(event)
            self.assertIsNone(source.message_id)

    def test_signal_capture_is_scoped_idempotent_and_does_not_invent_due(self):
        import mailbox_todo

        mailbox_todo._client = lambda: (None, self.dav, self.queue)
        os.environ["SIGNAL_HOME_CHANNEL"] = "home"
        os.environ["SIGNAL_ALLOWED_USERS"] = "alice"
        source = {"chat_id": "home", "user_id": "alice"}
        reply1 = _capture_message("s", "signal", source, "msg-1", "Merke: Beleg ablegen bis 2026-10-15")
        reply2 = _capture_message("s", "signal", source, "msg-1", "Merke: Beleg ablegen bis 2026-10-15")
        self.assertEqual(reply1["action"], "handled")
        self.assertEqual(reply2["action"], "handled")
        self.assertEqual(len(self.dav.items), 1)
        payload = next(iter(self.dav.items.values()))[0]
        self.assertEqual(ical.due_date(payload), "2026-10-15")

        _capture_message("s", "signal", source, "msg-2", "Merke: Kein ausdrücklich genanntes Datum")
        self.assertEqual(len(self.dav.items), 2)
        payloads = [value[0] for value in self.dav.items.values()]
        no_due = next(item for item in payloads if "Kein ausdrücklich" in ical.get_text(item, "SUMMARY"))
        self.assertIsNone(ical.due_date(no_due))

        before = self.dav.create_count
        blocked = _capture_message(
            "s", "signal", {"chat_id": "elsewhere", "user_id": "alice"},
            "msg-3", "Delegiere: not from the home channel",
        )
        self.assertEqual(blocked["action"], "handled")
        self.assertEqual(self.dav.create_count, before)

    def test_etag_conflict_merges_latest_iphone_note_and_preserves_fields(self):
        self.queue.baseline([], "Hermes")
        self.dav.create_task("Hermes", SAMPLE)
        self.queue.scan(self.dav)
        uid = self.queue.claim_next("Hermes")
        row = self.queue.task_record(uid, "Hermes")
        racing = RacingDAV("edit")
        racing.items[row["href"]] = self.dav.items[row["href"]]
        result = _write_managed_update(
            racing, self.queue, uid, ["Status: In Bearbeitung", "Fortschritt: erster Schritt"]
        )
        current = result["payload"]
        note = ical.get_text(current, "DESCRIPTION")
        self.assertIn("Edited on iPhone", note)
        self.assertIn("Fortschritt: erster Schritt", note)
        self.assertEqual(ical.get_text(current, "SUMMARY"), "Changed on iPhone")
        self.assertEqual(ical.get_property_value(current, "PRIORITY"), "1")
        self.assertEqual(ical.due_date(current), "2026-10-15")

    def test_if_iphone_completes_during_conflict_task_is_not_reopened(self):
        self.queue.baseline([], "Hermes")
        self.dav.create_task("Hermes", SAMPLE)
        self.queue.scan(self.dav)
        uid = self.queue.claim_next("Hermes")
        row = self.queue.task_record(uid, "Hermes")
        racing = RacingDAV("complete")
        racing.items[row["href"]] = self.dav.items[row["href"]]
        result = _write_managed_update(
            racing, self.queue, uid, ["Status: In Bearbeitung"]
        )
        self.assertTrue(result["already_completed"])
        self.assertEqual(ical.task_status(result["payload"]), "COMPLETED")
        self.assertEqual(self.queue.task_record(uid)["state"], "completed")
        self.assertEqual(racing.put_count, 0)

    def test_completion_is_written_only_after_readback(self):
        self.queue.baseline([], "Hermes")
        self.dav.create_task("Hermes", SAMPLE)
        self.queue.scan(self.dav)
        uid = self.queue.claim_next("Hermes")
        result = _write_managed_update(
            self.dav,
            self.queue,
            uid,
            ["Status: Erledigt", "Ergebnis: Testresultat", "Geprüft: readback"],
            complete=True,
        )
        self.assertEqual(ical.task_status(result["payload"]), "COMPLETED")
        self.assertIn("Testresultat", ical.get_text(result["payload"], "DESCRIPTION"))
        self.assertEqual(self.queue.task_record(uid)["state"], "completed")
        self.assertEqual(ical.get_property_value(result["payload"], "PERCENT-COMPLETE"), "100")

    def test_watcher_emits_no_wake_for_unchanged_state(self):
        self.assertEqual(_marker(self.queue, None), "idle")
        self.assertEqual(_marker(self.queue, None), "idle")
        self.assertEqual(_marker(self.queue, {"event": "task", "uid": "one"}), "task uid=one attempt=0")
        self.assertEqual(_marker(self.queue, None), "task uid=one attempt=0")
        self.queue.set_meta("monitor:last_marker", "task uid=one attempt=2")
        self.assertEqual(_marker(self.queue, None), "task uid=one attempt=2")

    def test_explicit_relative_due_is_saved_without_a_time(self):
        from mailbox_todo import _due_from_explicit_text
        from datetime import date, timedelta
        due, cleaned, ambiguous = _due_from_explicit_text("Einkauf bis morgen")
        self.assertEqual(due, date.today() + timedelta(days=1))
        self.assertEqual(cleaned, "Einkauf")
        self.assertFalse(ambiguous)


if __name__ == "__main__":
    unittest.main()
