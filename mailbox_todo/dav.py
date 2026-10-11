"""Narrow CalDAV client: discovery plus access to two explicit VTODO lists."""

from __future__ import annotations

import base64
import html
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

from . import ical


DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
MAX_XML = 512 * 1024
MAX_ICAL = 2 * 1024 * 1024


def _normalize_etag(value: str) -> str:
    """Mailbox.org omits quotes in DAV:getetag but includes them on GET.

    Preserve weak/strong identity; only normalize the representation of the
    same opaque tag. Conditional HTTP writes always use a quoted entity tag.
    """
    value = value.strip()
    if not value:
        return ""
    weak = value.startswith("W/")
    tag = value[2:] if weak else value
    if tag.startswith('"') and tag.endswith('"'):
        tag = tag[1:-1]
    if not tag or len(tag) > 1024 or any(ord(c) <= 32 or ord(c) == 127 or c == '"' for c in tag):
        raise DavError("DAV server returned an invalid ETag")
    return ('W/' if weak else '') + '"' + tag + '"'


class DavError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.operation: str | None = None


@dataclass(frozen=True)
class CollectionInfo:
    display_name: str
    url: str
    supports_vtodo: bool
    is_calendar: bool


@dataclass(frozen=True)
class Resource:
    href: str
    etag: str


@dataclass(frozen=True)
class StoredCollection:
    name: str
    url: str


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CalDAV:
    """Only allow the server origin and the concrete collection URLs in config."""

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        collections: dict[str, str] | None = None,
        opener=None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 10.0,
    ):
        parsed = urllib.parse.urlsplit(server_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("DAV server must be an https URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("DAV server URL must not include a query or fragment")
        self.server_url = server_url.rstrip("/") + "/"
        self.username = username
        self._password = password
        self._origin = (parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or 443)
        self.collections = {k: self._canonical_collection(v) for k, v in (collections or {}).items()}
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            NoRedirect(),
        )
        self._sleep = sleep
        self._timeout = timeout

    def _same_origin(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
        ) == self._origin

    def _canonical_collection(self, url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        if not self._same_origin(url) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("collection URL is outside the configured DAV server")
        if not parsed.path.endswith("/"):
            parsed = parsed._replace(path=parsed.path + "/")
        return urllib.parse.urlunsplit(parsed)

    def _is_allowed_url(self, url: str, onboarding: bool = False) -> bool:
        if not self._same_origin(url):
            return False
        parsed = urllib.parse.urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if onboarding:
            server_path = urllib.parse.urlsplit(self.server_url).path
            return parsed.path.startswith(server_path)
        for collection in self.collections.values():
            base = urllib.parse.urlsplit(collection)
            if parsed.path == base.path:
                return True
            if parsed.path.startswith(base.path):
                tail = parsed.path[len(base.path) :]
                # A DAV object is a direct child of the authorized collection.
                decoded = urllib.parse.unquote(tail)
                if tail and "/" not in decoded and decoded not in {".", ".."}:
                    return True
        return False

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        *,
        onboarding: bool = False,
        retry: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        if not self._is_allowed_url(url, onboarding=onboarding):
            raise DavError("DAV request outside the approved URL scope")
        auth = base64.b64encode(f"{self.username}:{self._password}".encode("utf-8")).decode("ascii")
        request_headers = {
            "Authorization": "Basic " + auth,
            "User-Agent": "Hermes-Mailbox-ToDo-Pilot/1.0",
            "Accept": "application/xml, text/calendar, */*",
            "Connection": "close",
        }
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            try:
                with self._opener.open(req, timeout=self._timeout) as response:
                    status = response.status
                    response_headers = {key.lower(): value for key, value in response.headers.items()}
                    limit = MAX_XML if "xml" in response_headers.get("content-type", "").lower() else MAX_ICAL
                    data = response.read(limit + 1)
                    if len(data) > limit:
                        raise DavError("DAV response exceeded the size limit")
                    return status, response_headers, data
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                # Close without inspecting the server's error body. This also
                # prevents cleanup warnings from exposing response details.
                exc.close()
                # Redirects stay disabled so credentials cannot leave the DAV host.
                retryable = status in {408, 425, 429, 500, 502, 503, 504}
                if retryable and attempt + 1 < attempts:
                    self._sleep(0.25 * (2**attempt))
                    continue
                raise DavError(f"DAV request failed with HTTP {status}", status, retryable) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < attempts:
                    self._sleep(0.25 * (2**attempt))
                    continue
                # The exception detail can contain a URL or credentials from a
                # transport implementation; expose only the exception class.
                raise DavError(f"DAV connection failed ({type(exc).__name__})", retryable=True) from None
        raise DavError("DAV request failed", retryable=True)

    @staticmethod
    def _xml(body: str) -> bytes:
        return body.encode("utf-8")

    @staticmethod
    def _parse_multistatus(data: bytes) -> list[ET.Element]:
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise DavError("DAV XML declarations are not accepted")
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise DavError("DAV server returned invalid XML") from None
        if root.tag != DAV + "multistatus":
            raise DavError("DAV server returned an unexpected XML response")
        return root.findall(DAV + "response")

    @staticmethod
    def _href(parent: ET.Element | None) -> str | None:
        if parent is None:
            return None
        node = parent.find(DAV + "href")
        return node.text.strip() if node is not None and node.text else None

    def _resolve_href(self, base: str, href: str, onboarding: bool) -> str:
        resolved = urllib.parse.urljoin(base, html.unescape(href))
        if not self._is_allowed_url(resolved, onboarding=onboarding):
            raise DavError("DAV server returned a URL outside the allowed scope")
        return resolved

    def _propfind(self, url: str, properties: str, depth: str, onboarding: bool = False, operation: str | None = None) -> list[ET.Element]:
        body = self._xml(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"<d:prop>{properties}</d:prop></d:propfind>"
        )
        try:
            status, _, data = self._request(
                "PROPFIND",
                url,
                body,
                {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                onboarding=onboarding,
            )
        except DavError as exc:
            exc.operation = operation
            raise
        if status not in {200, 207}:
            raise DavError(f"DAV PROPFIND returned HTTP {status}", status)
        return self._parse_multistatus(data)

    def discover(self) -> tuple[str, list[CollectionInfo]]:
        """Discover only collection metadata; never fetch any VTODO contents."""
        root_responses = self._propfind(
            self.server_url,
            "<d:current-user-principal/>",
            "0",
            onboarding=True,
            operation="principal_discovery",
        )
        principal_href = None
        for response in root_responses:
            principal_href = self._href(response.find(DAV + "propstat/" + DAV + "prop/" + DAV + "current-user-principal"))
            if principal_href:
                break
        if not principal_href:
            raise DavError("DAV server did not expose current-user-principal")
        principal_url = self._resolve_href(self.server_url, principal_href, onboarding=True)
        principal_responses = self._propfind(
            principal_url,
            "<c:calendar-home-set/>",
            "0",
            onboarding=True,
            operation="home_discovery",
        )
        home_href = None
        for response in principal_responses:
            prop = response.find(DAV + "propstat/" + DAV + "prop/" + CAL + "calendar-home-set")
            home_href = self._href(prop)
            if home_href:
                break
        if not home_href:
            raise DavError("DAV server did not expose calendar-home-set")
        home_url = self._resolve_href(principal_url, home_href, onboarding=True)
        if not home_url.endswith("/"):
            home_url += "/"

        responses = self._propfind(
            home_url,
            "<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>",
            "1",
            onboarding=True,
            operation="collection_discovery",
        )
        collections: list[CollectionInfo] = []
        for response in responses:
            href = self._href(response)
            if not href:
                continue
            url = self._resolve_href(home_url, href, onboarding=True)
            props = response.find(DAV + "propstat/" + DAV + "prop")
            if props is None:
                continue
            resource_types = props.find(DAV + "resourcetype")
            is_calendar = resource_types is not None and resource_types.find(CAL + "calendar") is not None
            if not is_calendar:
                continue
            display = props.find(DAV + "displayname")
            display_name = (display.text or "").strip() if display is not None else ""
            component_set = props.find(CAL + "supported-calendar-component-set")
            supports = False
            if component_set is not None:
                supports = any((comp.attrib.get("name") or "").upper() == "VTODO" for comp in component_set.findall(CAL + "comp"))
            collections.append(CollectionInfo(display_name, self._canonical_collection(url), supports, True))
        return home_url, collections

    def create_vtodo_collection(self, home_url: str, name: str) -> str:
        """Create a new VTODO-only collection; never changes an existing collection."""
        if name not in {"Inbox", "Hermes"}:
            raise ValueError("only the approved pilot collection names may be created")
        target = urllib.parse.urljoin(home_url, urllib.parse.quote(name) + "/")
        if not self._is_allowed_url(target, onboarding=True):
            raise DavError("new collection URL is outside the DAV home")
        body = self._xml(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:set><d:prop>"
            f"<d:displayname>{html.escape(name)}</d:displayname>"
            "<c:supported-calendar-component-set><c:comp name=\"VTODO\"/></c:supported-calendar-component-set>"
            "</d:prop></d:set></c:mkcalendar>"
        )
        status, _, _ = self._request(
            "MKCALENDAR",
            target,
            body,
            {"Content-Type": "application/xml; charset=utf-8"},
            onboarding=True,
            retry=False,
        )
        if status not in {201, 200}:
            raise DavError(f"MKCALENDAR returned HTTP {status}", status)
        return self._canonical_collection(target)

    def set_allowlist(self, collections: dict[str, str]) -> None:
        if set(collections) != {"Inbox", "Hermes"}:
            raise ValueError("exactly Inbox and Hermes collection URLs are required")
        normalized = {name: self._canonical_collection(url) for name, url in collections.items()}
        if len(set(normalized.values())) != 2:
            raise ValueError("Inbox and Hermes must be distinct collections")
        self.collections = normalized

    def list_resources(self, collection_name: str) -> list[Resource]:
        collection = self._collection(collection_name)
        responses = self._propfind(
            collection,
            "<d:resourcetype/><d:getetag/>",
            "1",
        )
        resources: list[Resource] = []
        collection_path = urllib.parse.urlsplit(collection).path
        for response in responses:
            href = self._href(response)
            if not href:
                continue
            url = self._resolve_href(collection, href, onboarding=False)
            path = urllib.parse.urlsplit(url).path
            if path == collection_path:
                continue
            if not self._is_allowed_url(url):
                raise DavError("resource escaped the approved collection")
            props = response.find(DAV + "propstat/" + DAV + "prop")
            if props is None:
                continue
            resource_type = props.find(DAV + "resourcetype")
            if resource_type is not None and resource_type.find(DAV + "collection") is not None:
                continue
            etag_node = props.find(DAV + "getetag")
            etag = _normalize_etag(etag_node.text or "") if etag_node is not None else ""
            if not etag:
                raise DavError("a task resource did not provide an ETag")
            resources.append(Resource(url, etag))
        return resources

    def get_vtodo(self, collection_name: str, href: str, *, retry: bool = True) -> tuple[str, str]:
        collection = self._collection(collection_name)
        if not self._is_allowed_url(href):
            raise DavError("GET URL is outside the approved collections")
        if not href.startswith(collection):
            raise DavError("GET URL is not in the selected collection")
        status, headers, body = self._request("GET", href, retry=retry)
        if status != 200:
            raise DavError(f"GET returned HTTP {status}", status)
        etag = _normalize_etag(headers.get("etag", ""))
        if not etag:
            raise DavError("GET did not return an ETag")
        try:
            payload = body.decode("utf-8")
            uid = ical.task_identity(payload)
        except (UnicodeDecodeError, ical.ICalendarError):
            raise DavError("DAV resource is not a valid VTODO") from None
        return payload, etag

    def create_task(self, collection_name: str, payload: str) -> tuple[str, str]:
        collection = self._collection(collection_name)
        uid = ical.task_identity(payload)
        href = urllib.parse.urljoin(collection, urllib.parse.quote(uid, safe="") + ".ics")
        if not self._is_allowed_url(href) or not href.startswith(collection):
            raise DavError("new VTODO URL is outside the approved collection")
        status, headers, _ = self._request(
            "PUT",
            href,
            payload.encode("utf-8"),
            {
                "Content-Type": "text/calendar; charset=utf-8",
                "If-None-Match": "*",
            },
            retry=False,
        )
        if status not in {201, 204}:
            raise DavError(f"VTODO creation returned HTTP {status}", status)
        return href, _normalize_etag(headers.get("etag", ""))

    def put_vtodo(self, collection_name: str, href: str, payload: str, etag: str) -> str:
        collection = self._collection(collection_name)
        if not self._is_allowed_url(href) or not href.startswith(collection):
            raise DavError("PUT URL is outside the approved collection")
        status, headers, _ = self._request(
            "PUT",
            href,
            payload.encode("utf-8"),
            {
                "Content-Type": "text/calendar; charset=utf-8",
                "If-Match": etag,
            },
            retry=False,
        )
        if status not in {200, 201, 204}:
            raise DavError(f"VTODO update returned HTTP {status}", status)
        return _normalize_etag(headers.get("etag", ""))

    def _collection(self, name: str) -> str:
        if name not in {"Inbox", "Hermes"} or name not in self.collections:
            raise DavError("collection is not in the explicit pilot allowlist")
        return self.collections[name]
