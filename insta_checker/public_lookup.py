"""Sign-in-free profile lookup for the Instagram Profile Checker.

Why this module exists: the only public path instagrapi offers
(`user_info_by_username_gql`) is answered with a 429 for logged-out clients, so
"Public mode" produced nothing but "Public endpoint rate-limited" rows — 0
checked accounts out of a full list. This module talks to the two signed-out
paths Instagram's own web frontend uses:

1. The JSON endpoint (preferred: small, exact):

    GET https://www.instagram.com/api/v1/users/web_profile_info/?username=X
    X-IG-App-ID: 936619743392459

   It needs no account, no session cookie and no signature — just the public
   web app id, a constant baked into Instagram's own frontend. It returns the
   profile the table needs: full name, privacy flag, DP URL, post count,
   follower count, following count, verification.

2. The logged-out profile page (fallback: heavier, same numbers):

    GET https://www.instagram.com/<username>/

   Measured from a machine where (1) answers 429 for every signed-out client
   and for every header shape tried: the page itself is still served, and its
   embedded profile payload carries the same counters. A user who wants public
   mode most of the time should not lose it to one closed endpoint.

Which transport is used is decided once per run by the preflight below
(`lookup.transport`), so a run never pays for both.

What it cannot do, and says so instead of guessing:
  * Last Post date (needs a session or the private media endpoints) -> blank
  * full DP detection for some private profiles (avatar URL withheld)
  * the post count on the page transport comes from the page's og:description,
    which Instagram abbreviates above ~10,000 ("12.3K Posts") — those are left
    blank rather than rounded into a wrong number
  * the following/follower SCRAPE still needs a session (see followers.py)

Cost, stated plainly: the page transport is ~750 KB decoded (~125 KB over the
wire, measured) per account, against ~2 KB for the JSON endpoint — so a
1,000-name public run reads roughly 125 MB. Public mode still spends no
account budget, which is the whole reason it is worth having.

Design notes:
  * One requests.Session per worker thread and per transport (requests.Session
    is not thread-safe), with connection pooling kept small.
  * No retry/backoff sleeps inside here: the caller owns pacing (SPEED_PRESETS)
    and the circuit breaker, exactly like the session-mode paths.
  * Every failure mode maps to an explicit status so the table never shows a
    guessed result: ok / not_found / rate_limited / error.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Optional

import requests

# Public web client id — the same constant Instagram's own web frontend sends.
IG_APP_ID = "936619743392459"

PROFILE_INFO_URL = "https://www.instagram.com/api/v1/users/web_profile_info/"

# The logged-out profile page, used when the JSON endpoint above is refused.
PROFILE_PAGE_URL = "https://www.instagram.com/{username}/"
# The profile payload on that page is anchored by this query name. Measured: it
# appears once for an account that exists, and not at all for a username that
# cannot exist. It is also absent when Instagram serves a stripped shell, which
# is why the control probe below has to prove data flows before an absent
# payload is allowed to mean "no such account".
PROFILE_ANCHOR = "xig_user_by_username"
# How far after the anchor the counters may sit (highlights and bio links sit in
# between). Generous on purpose; the whole page is the fallback scope because
# each counter key occurs exactly once on a profile page.
PAGE_WINDOW = 100_000
# An account that cannot not-exist: the control that proves this machine's page
# requests really do come back with profile data before a run relies on them.
# Without that proof an empty page is ambiguous (missing account vs. withheld
# page), and this app must never call a real account missing on a maybe.
CONTROL_USERNAME = "instagram"
# One retry on an empty page: a single degraded response must never become a row.
PAGE_RETRIES = 1
# The page is a *browser* document, and Instagram decides what to serve from the
# request's shape. Measured with this set: full profile payload.
#   * Accept-Encoding is pinned to gzip/deflate: the default negotiation picks
#     zstd, which older urllib3 cannot decode (that would silently turn every
#     row into "missing"), and asking for brotli returns a stripped 113 KB
#     shell with no profile data at all.
#   * Sec-Fetch-* and Upgrade-Insecure-Requests matter: without them Instagram
#     serves that same stripped shell even for accounts that exist.
PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Instagram serves a generic grey avatar when a photo was never uploaded; these
# markers identify that placeholder. Kept in sync with checker.DEFAULT_AVATAR_MARKERS
# by importing the checker helper rather than duplicating the list.
REQUEST_TIMEOUT = 10

# Browsers get 429 far less often when they look like browsers. No cookies, no
# session — this is only about not being trivially distinguishable as a script.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": IG_APP_ID,
    "Referer": "https://www.instagram.com/",
}


@dataclass
class PublicProfile:
    """What the logged-out endpoint tells us about one account."""

    username: str
    full_name: str = ""
    is_private: bool = False
    is_verified: bool = False
    profile_pic_url: str = ""
    posts: Optional[int] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    # True when the endpoint answered but withheld the avatar (private profiles
    # often do). dp_present must stay None then — "unknown" is honest, "no" is a lie.
    avatar_withheld: bool = False


class PublicLookupError(Exception):
    """Lookup failed in a way the table should show as an error row."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.message = message
        self.kind = kind  # "rate_limited" | "network" | "error"


class PublicLookup:
    """Per-thread HTTP client for the logged-out profile endpoint."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT) -> None:
        self.timeout = timeout
        self._local = threading.local()
        self.calls = 0
        self._lock = threading.Lock()
        # "api" (JSON endpoint) or "html" (logged-out profile page). The
        # preflight decides; see probe_public_access and probe_page_transport.
        self.transport = "api"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(DEFAULT_HEADERS)
            # Small pool: this runs a handful of workers, not hundreds of hosts.
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def _page_session(self) -> requests.Session:
        session = getattr(self._local, "page_session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(PAGE_HEADERS)
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
            session.mount("https://", adapter)
            self._local.page_session = session
        return session

    def fetch(self, username: str) -> PublicProfile:
        """Return the profile, or raise PublicLookupError. Never sleeps."""
        if self.transport == "html":
            return self._fetch_via_page(username)
        return self._fetch_via_endpoint(username)

    def _fetch_via_endpoint(self, username: str) -> PublicProfile:
        """The JSON endpoint. Raises PublicLookupError."""
        with self._lock:
            self.calls += 1
        try:
            response = self._session().get(
                PROFILE_INFO_URL,
                params={"username": username},
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise PublicLookupError(f"Timeout after {self.timeout:.0f}s.", "network") from exc
        except requests.RequestException as exc:
            raise PublicLookupError(f"Network error: {exc}", "network") from exc

        status = response.status_code
        if status in (404, 400):
            raise PublicLookupError("No Instagram account with this username.", "not_found")
        if status in (429, 503):
            raise PublicLookupError(
                "Instagram rate-limited the public lookup — wait a few minutes.", "rate_limited"
            )
        if status in (401, 403):
            # Instagram now gates this endpoint; it must be surfaced plainly
            # rather than retried, because repeating it is what earns a block.
            raise PublicLookupError(
                "Instagram refused the logged-out lookup (it wants a sign-in for this "
                "endpoint now). Use Session mode for full data.",
                "error",
            )
        if status >= 500:
            raise PublicLookupError(f"Instagram server error (HTTP {status}).", "network")
        if status != 200:
            raise PublicLookupError(f"Unexpected HTTP {status} from Instagram.", "error")

        try:
            user = response.json()["data"]["user"]
        except (ValueError, KeyError, TypeError) as exc:
            # A 200 whose body is not a profile is Instagram changing the shape
            # (or a login wall). Say that plainly instead of raising garbage.
            raise PublicLookupError(
                "Instagram's logged-out endpoint returned an unexpected response.",
                "error",
            ) from exc
        if not isinstance(user, dict):
            raise PublicLookupError(
                "Instagram's logged-out endpoint returned an unexpected response.", "error"
            )
        return _profile_from_payload(user)

    # -------------------------------------------------- profile-page transport

    def probe_page_transport(self) -> None:
        """Use the profile page for this run — but only if it serves data here.

        The control is an account that cannot not-exist, not the impossible
        username the endpoint probe uses: the question is whether this machine's
        page requests come back with a profile payload at all. Once that is
        proven, an empty page for some other username is a real answer; before
        it is proven, an empty page means nothing at all.
        """
        last: Optional[PublicLookupError] = None
        for _attempt in range(PAGE_RETRIES + 1):
            try:
                self._fetch_from_page(CONTROL_USERNAME)
            except PublicLookupError as exc:
                last = exc
                if exc.kind != "no_data":
                    raise
            else:
                self.transport = "html"
                return
        raise PublicLookupError(
            f"Instagram served @{CONTROL_USERNAME}'s profile page without any profile "
            "data, so signed-out page reads are not working from this machine.",
            "error",
        ) from last

    def _fetch_via_page(self, username: str) -> PublicProfile:
        """Page transport with one retry, so a single bad page is not a verdict."""
        last: Optional[PublicLookupError] = None
        for _attempt in range(PAGE_RETRIES + 1):
            try:
                return self._fetch_from_page(username)
            except PublicLookupError as exc:
                last = exc
                if exc.kind != "no_data":
                    raise
        # Empty page twice, on a machine whose page requests are known to carry
        # profiles (the preflight proved it): Instagram resolved this username to
        # nothing. Anything less certain raises instead of guessing.
        raise PublicLookupError(
            "No Instagram account with this username.", "not_found"
        ) from last

    def _fetch_from_page(self, username: str) -> PublicProfile:
        """Read one profile off the logged-out profile page.

        Raises PublicLookupError. kind "no_data" means the page carried no
        profile payload — a stripped shell, or an account that is gone; the
        caller separates those (only the preflight may treat it as absence).
        """
        with self._lock:
            self.calls += 1
        try:
            response = self._page_session().get(
                PROFILE_PAGE_URL.format(username=username),
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.Timeout as exc:
            raise PublicLookupError(f"Timeout after {self.timeout:.0f}s.", "network") from exc
        except requests.RequestException as exc:
            raise PublicLookupError(f"Network error: {exc}", "network") from exc

        status = response.status_code
        if status in (404, 410):
            raise PublicLookupError("No Instagram account with this username.", "not_found")
        if status in (429, 503):
            raise PublicLookupError(
                "Instagram rate-limited the public lookup — wait a few minutes.", "rate_limited"
            )
        if status in (401, 403):
            raise PublicLookupError(
                "Instagram refused the logged-out profile page for this machine.", "error"
            )
        if status >= 500:
            raise PublicLookupError(f"Instagram server error (HTTP {status}).", "network")
        if status != 200:
            raise PublicLookupError(f"Unexpected HTTP {status} from Instagram.", "error")
        if "/accounts/login" in (getattr(response, "url", "") or ""):
            raise PublicLookupError(
                "Instagram sent the logged-out profile page to its login screen.", "error"
            )
        profile = profile_from_page(getattr(response, "text", "") or "", username)
        if profile is None:
            raise PublicLookupError(
                "Instagram served the profile page without any profile data.", "no_data"
            )
        return profile


def profile_from_page(html: str, username: str) -> Optional[PublicProfile]:
    """Pull one profile out of a logged-out profile page, or None if absent.

    None means "this page did not carry a profile payload", which is *not*
    the same as "this account does not exist" — see _fetch_via_page.
    """
    # Normalise one unescape level FIRST: the payload is JSON nested inside a
    # JSON string, so its quotes arrive as \" and its slashes as \/. Order
    # matters — the rewrite makes the page shorter, so an index taken from the
    # raw text points somewhere else entirely in the normalised text. Doing it
    # the other way round made real accounts parse as missing (found by a live
    # run, not by the unit tests: the fixtures were too small for the shift to
    # change which window the checks looked at).
    text = html.replace('\\"', '"').replace("\\/", "/")
    start = text.find(PROFILE_ANCHOR)
    if start < 0:
        return None
    # The anchor's own object names the account it belongs to; without that this
    # could be a payload about somebody else entirely.
    if f'"{username}"' not in text[start:start + 2000]:
        return None
    window = text[start:start + PAGE_WINDOW]
    scope = window if re.search(r'"follower_count":\d+', window) else text
    followers = _json_int(scope, "follower_count")
    following = _json_int(scope, "following_count")
    if followers is None and following is None:
        return None  # an anchor without counters is not the profile object
    pic = _json_string(scope, "profile_pic_url") or _json_string(scope, "profile_pic_url_hd")
    return PublicProfile(
        username=username,
        full_name=_json_string(scope, "full_name"),
        is_private=_json_bool(scope, "is_private"),
        is_verified=_json_bool(scope, "is_verified"),
        profile_pic_url=pic,
        # The payload has no post count; the page's og:description does.
        posts=post_count_from_page(html),
        followers=followers,
        following=following,
        avatar_withheld=not pic,
    )


def post_count_from_page(html: str) -> Optional[int]:
    """The post count from the page's link-preview description.

    "1,800 Followers, 102 Following, 0 Posts - See Instagram photos and videos
    from Lucid (@lucid.sync)". Instagram abbreviates above ~10,000 ("12.3K
    Posts"): that returns None — blank beats a rounded number in a table that
    feeds verdicts.
    """
    meta = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    if not meta:
        return None
    found = re.search(r"([\d,]+)\s+Posts\b", meta.group(1))
    if not found:
        return None
    digits = found.group(1).replace(",", "")
    return int(digits) if digits.isdigit() else None


def _json_bool(text: str, key: str) -> bool:
    found = re.search(rf'"{key}":(true|false)', text)
    return bool(found and found.group(1) == "true")


def _json_int(text: str, key: str) -> Optional[int]:
    found = re.search(rf'"{key}":(\d+)', text)
    return int(found.group(1)) if found else None


def _json_string(text: str, key: str) -> str:
    """A JSON string field, unescaped; "" when absent."""
    found = re.search(rf'"{key}":"((?:[^"\\]|\\.)*)"', text)
    if not found:
        return ""
    try:
        return json.loads(f'"{found.group(1)}"')
    except ValueError:
        return found.group(1)


def _profile_from_payload(user: dict) -> PublicProfile:
    """Map the endpoint's JSON onto PublicProfile, tolerating missing fields."""
    pic = str(user.get("profile_pic_url") or user.get("profile_pic_url_hd") or "")
    # Private profiles frequently answer with an empty/absent avatar. That is
    # "unknown", so the checker can leave DP blank instead of reporting "no DP".
    avatar_withheld = not pic
    return PublicProfile(
        username=str(user.get("username") or ""),
        full_name=str(user.get("full_name") or ""),
        is_private=bool(user.get("is_private", False)),
        is_verified=bool(user.get("is_verified", False)),
        profile_pic_url=pic,
        posts=_as_int(user.get("edge_owner_to_timeline_media", {}).get("count", user.get("media_count"))),
        followers=_as_int(user.get("edge_followed_by", {}).get("count", user.get("follower_count"))),
        following=_as_int(user.get("edge_follow", {}).get("count", user.get("following_count"))),
        avatar_withheld=avatar_withheld,
    )


def _as_int(value) -> Optional[int]:
    """Counts arrive nested (edge_*.count) or flat; never trust the type."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# A username that cannot exist. Used ONLY for the preflight probe below: if a
# name that does not exist comes back 200/404, the endpoint is serving us and
# the real lookups are worth running. If even this is refused, Instagram is
# turning us away before it resolves anything.
PROBE_USERNAME = "ig_probe_no_such_account_7f3a91"


def probe_public_access(lookup: Optional["PublicLookup"] = None,
                        timeout: float = REQUEST_TIMEOUT) -> tuple:
    """Can this machine read profiles without signing in — and on which path?

    Returns (available, detail). The JSON endpoint is tried first: it is exact,
    ~2 KB per account, and the only path that reports a post count directly.
    Probing a non-existent username is deliberate — it touches no real account,
    and a 404 there proves the endpoint is resolving usernames (a 429 proves it
    is not).

    If that endpoint is refused, the logged-out profile PAGE may still serve
    this machine, so it is tried before public mode is declared dead. That check
    is the opposite of the first one: it asks for an account that exists, because
    only a page for a real account can show that pages carry data here. When it
    passes, `lookup.transport` becomes "html" and every later fetch uses the
    page (never both, so a run pays once per account, not twice).
    """
    lookup = lookup or PublicLookup(timeout=timeout)
    endpoint_detail = ""
    try:
        lookup.fetch(PROBE_USERNAME)
    except PublicLookupError as exc:
        if exc.kind == "not_found":
            return True, ""  # the endpoint answered correctly: it is serving us
        endpoint_detail = exc.message
    except Exception as exc:  # network down, TLS failure, ...
        endpoint_detail = f"{type(exc).__name__}: {exc}"
    else:
        # 200 for a name that cannot exist means something odd; do not claim access.
        endpoint_detail = "Instagram returned a profile for a username that cannot exist."

    # Report both refusals: which one happened is exactly what the user (or a
    # later playtest) needs to tell a closed endpoint from a closed machine.
    try:
        lookup.probe_page_transport()
    except PublicLookupError as exc:
        return False, f"{endpoint_detail} (the profile page also failed: {exc.message})"
    except Exception as exc:  # network down, TLS failure, ...
        return False, f"{endpoint_detail} (the profile page also failed: {type(exc).__name__}: {exc})"
    return True, ""