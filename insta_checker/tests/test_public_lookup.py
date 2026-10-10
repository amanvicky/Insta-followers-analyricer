"""Tests for the sign-in-free public lookup path.

Found live: Public mode returned nothing but "Public endpoint rate-limited" rows
(0 checked out of a full list) because instagrapi's GraphQL lookup answers 429
for logged-out clients. These tests pin the replacement — the logged-out
web-profile endpoint — to the behaviour the table depends on, and pin the
circuit breaker so a public run cannot grind through thousands of names.
"""

import pathlib
import tempfile
import unittest

import public_lookup
from checker import InstagramChecker
from public_lookup import PublicLookup

PROFILE_JSON = {
    "data": {
        "user": {
            "username": "gooduser",
            "full_name": "Good User",
            "is_private": False,
            "is_verified": True,
            "profile_pic_url": (
                "https://example.cdninstagram.com/v/t51.2885-19/392817_987654_n.jpg"
            ),
            "edge_owner_to_timeline_media": {"count": 42},
            "edge_followed_by": {"count": 1234},
            "edge_follow": {"count": 567},
        }
    }
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, body=None, url=""):
        self.status_code = status_code
        self._payload = payload
        self._body = body
        self.url = url

    @property
    def text(self):
        return self._body or ""

    def json(self):
        if self._body is not None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Stands in for requests.Session; answers from per-username scripts.

    `script` answers the JSON endpoint (username in the query string),
    `page_script` answers the logged-out profile page (username in the path).
    An unstubbed page returns the degraded shell Instagram serves when it will
    not answer a page request: HTTP 200 with no profile payload.
    """

    headers = {}
    script = {}  # username -> FakeResponse (or callable)
    page_script = {}  # username -> FakeResponse (or callable)
    calls = []

    def mount(self, *_args, **_kwargs):
        pass

    def get(self, url, params=None, timeout=None, **_kwargs):
        if params is None:  # profile-page transport: no query string
            username = url.rstrip("/").rsplit("/", 1)[-1]
            FakeSession.calls.append(username)
            answer = FakeSession.page_script.get(username)
            if callable(answer):
                answer = answer()
            if answer is None:
                return FakeResponse(body="<html><title>Instagram</title></html>")
            return answer
        username = (params or {}).get("username", "")
        FakeSession.calls.append(username)
        answer = FakeSession.script.get(username)
        if callable(answer):
            answer = answer()
        if answer is None:
            return FakeResponse(404)
        return answer


def _install_script(script, page_script=None):
    FakeSession.script = script
    FakeSession.page_script = page_script or {}
    FakeSession.calls = []
    real_session = public_lookup.requests.Session
    real_headers = public_lookup.requests.adapters.HTTPAdapter
    public_lookup.requests.Session = FakeSession
    public_lookup.requests.adapters.HTTPAdapter = lambda **kwargs: None
    return real_session, real_headers


def _restore(real_session, real_adapter):
    public_lookup.requests.Session = real_session
    public_lookup.requests.adapters.HTTPAdapter = real_adapter


class PublicLookupTests(unittest.TestCase):
    def setUp(self):
        self._patches = _install_script({})
        self.addCleanup(_restore, *self._patches)
        self.lookup = PublicLookup()

    def test_maps_the_endpoint_payload_onto_a_profile(self):
        FakeSession.script = {"gooduser": FakeResponse(payload=PROFILE_JSON)}
        profile = self.lookup.fetch("gooduser")
        self.assertEqual(profile.username, "gooduser")
        self.assertEqual(profile.posts, 42)
        self.assertEqual(profile.followers, 1234)
        self.assertEqual(profile.following, 567)
        self.assertFalse(profile.avatar_withheld)
        self.assertEqual(profile.full_name, "Good User")

    def test_flat_count_fields_are_also_accepted(self):
        """Instagram serves some responses with flat *_count fields."""
        FakeSession.script = {"flat": FakeResponse(payload={"data": {"user": {
            "username": "flat", "media_count": 7, "follower_count": 8, "following_count": 9,
            "profile_pic_url": "https://example.cdninstagram.com/v/t51.2885-19/392817_987654_n.jpg",
        }}})}
        profile = self.lookup.fetch("flat")
        self.assertEqual((profile.posts, profile.followers, profile.following), (7, 8, 9))

    def test_missing_account_is_reported_as_not_found(self):
        FakeSession.script = {"ghost": FakeResponse(status_code=404)}
        with self.assertRaises(public_lookup.PublicLookupError) as ctx:
            self.lookup.fetch("ghost")
        self.assertEqual(ctx.exception.kind, "not_found")

    def test_private_profile_keeps_the_avatar_withheld_flag(self):
        """No avatar from Instagram means 'unknown', never 'no DP'."""
        FakeSession.script = {"secret": FakeResponse(payload={"data": {"user": {
            "username": "secret", "is_private": True, "profile_pic_url": "",
        }}})}
        profile = self.lookup.fetch("secret")
        self.assertTrue(profile.avatar_withheld)
        self.assertTrue(profile.is_private)

    def test_forbidden_status_points_at_session_mode(self):
        FakeSession.script = {"locked": FakeResponse(status_code=403)}
        with self.assertRaises(public_lookup.PublicLookupError) as ctx:
            self.lookup.fetch("locked")
        self.assertEqual(ctx.exception.kind, "error")
        self.assertIn("sign-in", ctx.exception.message.lower())

    def test_rate_limit_status_is_its_own_kind(self):
        FakeSession.script = {"busy": FakeResponse(status_code=429)}
        with self.assertRaises(public_lookup.PublicLookupError) as ctx:
            self.lookup.fetch("busy")
        self.assertEqual(ctx.exception.kind, "rate_limited")

    def test_unexpected_body_is_reported_not_guessed(self):
        FakeSession.script = {"weird": FakeResponse(status_code=200, body="<html>")}
        with self.assertRaises(public_lookup.PublicLookupError) as ctx:
            self.lookup.fetch("weird")
        self.assertEqual(ctx.exception.kind, "error")


class PublicAccessProbeTests(unittest.TestCase):
    """The preflight that keeps a refused endpoint from producing N error rows.

    Verified live: a username that cannot exist returns the SAME 429 as @instagram,
    so Instagram refuses signed-out profile requests before resolving anything.
    """

    def setUp(self):
        self._patches = _install_script({})
        self.addCleanup(_restore, *self._patches)

    def test_probe_passes_when_the_endpoint_answers_not_found(self):
        FakeSession.script = {"ig_probe_no_such_account_7f3a91": FakeResponse(status_code=404)}
        available, detail = public_lookup.probe_public_access()
        self.assertTrue(available, detail)
        self.assertEqual(FakeSession.calls, ["ig_probe_no_such_account_7f3a91"])

    def test_probe_fails_when_everything_is_rate_limited(self):
        FakeSession.script = {"ig_probe_no_such_account_7f3a91": FakeResponse(status_code=429)}
        available, detail = public_lookup.probe_public_access()
        self.assertFalse(available)
        self.assertIn("rate-limited", detail.lower())

    def test_probe_rejects_a_200_for_a_username_that_cannot_exist(self):
        FakeSession.script = {"ig_probe_no_such_account_7f3a91": FakeResponse(
            payload={"data": {"user": {"username": "ig_probe_no_such_account_7f3a91"}}})}
        available, _detail = public_lookup.probe_public_access()
        self.assertFalse(available)


class PublicCheckManyTests(unittest.TestCase):
    """public_check_many drives real ProfileResult rows from that endpoint."""

    def setUp(self):
        self._patches = _install_script({})
        self.addCleanup(_restore, *self._patches)
        self.checker = InstagramChecker(
            session_dir=pathlib.Path(tempfile.mkdtemp(prefix="ig_test_public_"))
        )
        # No sleeping in tests; the retry waits are real seconds.
        self.checker._interruptible_sleep = lambda *_: None

    def _run(self, names):
        import threading
        stop = threading.Event()
        results = []
        self.checker.public_check_many(
            names, workers=1, delay_range=(0, 0),
            on_result=results.append, stop_event=stop,
        )
        return stop, results

    def _probe_ok(self, **script):
        """Every test's script implies the preflight passes (404 for the probe)."""
        script.setdefault(public_lookup.PROBE_USERNAME, FakeResponse(status_code=404))
        FakeSession.script = script

    def test_refused_endpoint_fails_before_producing_error_rows(self):
        """Regression: a 3,000-name list used to become 3,000 identical error
        rows. The preflight must stop the run with one clear reason instead."""
        FakeSession.script = {
            public_lookup.PROBE_USERNAME: FakeResponse(status_code=429),
            "u0": FakeResponse(status_code=200, payload=PROFILE_JSON),
        }
        import threading
        from checker import CheckerError
        with self.assertRaises(CheckerError) as ctx:
            self.checker.public_check_many(
                [f"u{i}" for i in range(50)], workers=1, delay_range=(0, 0),
                on_result=lambda _r: None, stop_event=threading.Event(),
            )
        self.assertEqual(ctx.exception.kind, "public_unavailable")
        self.assertIn("cannot work right now", ctx.exception.message)
        # Only the two preflight checks were sent — the impossible username, then
        # the page control (twice: a single bare shell must not close public mode
        # on its own) — and never a username from the user's list.
        self.assertEqual(set(FakeSession.calls),
                         {public_lookup.PROBE_USERNAME, public_lookup.CONTROL_USERNAME})
        self.assertNotIn("u0", FakeSession.calls)

    def test_public_run_needs_no_session_and_returns_full_rows(self):
        self._probe_ok(
            gooduser=FakeResponse(payload=PROFILE_JSON),
            ghost=FakeResponse(status_code=404),
        )
        stop, results = self._run(["gooduser", "ghost"])
        by_name = {r.username: r for r in results}
        self.assertFalse(stop.is_set())
        self.assertIsNone(self.checker.client)  # nothing signed in, nothing needed
        good = by_name["gooduser"]
        self.assertEqual(good.status, "ok")
        self.assertTrue(good.alive)
        self.assertTrue(good.dp_present)
        self.assertEqual((good.posts, good.followers, good.following), (42, 1234, 567))
        self.assertEqual(good.last_post_at, "")  # honestly blank, not guessed
        ghost = by_name["ghost"]
        self.assertEqual(ghost.status, "not_found")
        self.assertFalse(ghost.alive)

    def test_private_profile_reports_dp_unknown(self):
        self._probe_ok(secret=FakeResponse(payload={
            "data": {"user": {
                "username": "secret", "is_private": True, "profile_pic_url": "",
                "edge_followed_by": {"count": 10},
            }}
        }))
        _stop, results = self._run(["secret"])
        self.assertIsNone(results[0].dp_present)

    def test_sustained_429s_halt_the_run_instead_of_grinding(self):
        """The public mode must get the same circuit breaker as session mode."""
        self._probe_ok(**{f"u{i}": FakeResponse(status_code=429) for i in range(60)})
        stop, results = self._run([f"u{i}" for i in range(60)])
        self.assertTrue(stop.is_set())
        self.assertIn("public", self.checker.halt_reason.lower())
        self.assertLess(len(FakeSession.calls), 60)  # stopped early, not 60 requests

    def test_one_429_does_not_halt_a_healthy_run(self):
        script = {"gooduser": FakeResponse(payload=PROFILE_JSON)}
        script.update({f"u{i}": FakeResponse(payload=PROFILE_JSON) for i in range(6)})
        script["flaky"] = FakeResponse(status_code=429)
        self._probe_ok(**script)
        stop, results = self._run(["gooduser", "flaky", "u0", "u1", "u2", "u3", "u4", "u5"])
        self.assertFalse(stop.is_set())
        self.assertEqual(self.checker.halt_reason, "")
        self.assertEqual(len(results), 8)

    def test_invalid_username_never_hits_the_network(self):
        self._probe_ok()
        _stop, results = self._run(["@@"])
        self.assertEqual(FakeSession.calls, [public_lookup.PROBE_USERNAME])
        self.assertNotIn("@@", FakeSession.calls)  # junk never becomes a request
        self.assertEqual(results[0].status, "invalid")
        self.assertEqual(results[0].username, "@@")  # visible, so the user can fix it

    def test_a_refused_endpoint_still_produces_rows_via_the_profile_page(self):
        """The whole point of the fallback: the audit runs anyway.

        Measured live: while the JSON endpoint answered 429 to every signed-out
        request, the logged-out profile page still carried the same counters. A
        user who wants public mode most of the time must not lose the run.
        """
        FakeSession.script = {public_lookup.PROBE_USERNAME: FakeResponse(status_code=429)}
        FakeSession.page_script = {
            public_lookup.CONTROL_USERNAME: FakeResponse(
                body=profile_page_html(username=public_lookup.CONTROL_USERNAME)),
            "gooduser": FakeResponse(body=profile_page_html()),
            "ghost": FakeResponse(body="<html><title>Instagram</title></html>"),
        }
        stop, results = self._run(["gooduser", "ghost"])
        by_name = {r.username: r for r in results}
        self.assertFalse(stop.is_set())
        self.assertEqual(by_name["gooduser"].status, "ok")
        self.assertEqual(by_name["gooduser"].followers, 1234)
        self.assertEqual(by_name["gooduser"].following, 567)
        self.assertEqual(by_name["ghost"].status, "not_found")
        self.assertFalse(by_name["ghost"].alive)


def profile_page_html(username="gooduser", *, anchor_username=None, followers=1234,
                      following=567, posts="42", private=False, verified=True,
                      full_name="Good User", include_anchor=True, escaped=True,
                      post_line=True,
                      pic="https://example.cdninstagram.com/v/t51.2885-19/392817_987654_n.jpg"):
    """A miniature logged-out profile page, shaped like the real one.

    The payload is JSON nested inside a JSON string, so its quotes arrive as
    \\" and its slashes as \\/ — the reader must handle that. The counters live in
    the payload; the post count exists only in the page's og:description.
    """
    anchor_name = anchor_username or username
    payload = (
        '{"__bbox":{"result":{"data":{"xig_user_by_username":{'
        f'"pk":"1","username":"{anchor_name}","profile_pic_url":"{pic}",'
        f'"is_private":{"true" if private else "false"},'
        f'"is_verified":{"true" if verified else "false"},'
        f'"full_name":"{full_name}",'
        f'"follower_count":{followers},"following_count":{following}'
        '}}}}}'
    )
    if escaped:
        payload = payload.replace('"', '\\"').replace("/", "\\/")
    anchor = ""
    if include_anchor:
        anchor = ('<script type="application/json">{"require":[["ScheduledServerJS",'
                  f'null,[\'{payload}\']]]}}</script>')
    og = ""
    if post_line:
        description = (f"{followers:,} Followers, {following:,} Following, {posts} Posts - "
                       f"See Instagram photos and videos from {full_name} (&#064;{username})")
        og = f'<meta property="og:description" content="{description}" />'
    return f"<html><head><title>{username}</title>{og}</head><body>{anchor}</body></html>"


class ProfilePageParsingTests(unittest.TestCase):
    """The page reader: it reads what is there, and returns nothing otherwise."""

    def test_reads_a_profile_off_an_escaped_payload(self):
        profile = public_lookup.profile_from_page(profile_page_html(), "gooduser")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.username, "gooduser")
        self.assertEqual((profile.followers, profile.following), (1234, 567))
        self.assertEqual(profile.posts, 42)
        self.assertEqual(profile.full_name, "Good User")
        self.assertFalse(profile.is_private)
        self.assertTrue(profile.is_verified)
        self.assertFalse(profile.avatar_withheld)

    def test_reads_the_same_page_unescaped(self):
        profile = public_lookup.profile_from_page(
            profile_page_html(escaped=False), "gooduser")
        self.assertIsNotNone(profile)
        self.assertEqual((profile.followers, profile.following), (1234, 567))

    def test_a_payload_that_sits_far_into_the_page_still_reads(self):
        """Regression (found by a live run, not by these fixtures): normalising
        the page shortens it, so the anchor has to be located *after*
        normalising. Locating it in the raw text pointed the checks at the wrong
        window, so real profiles parsed as nothing — a false "not found" for
        accounts that exist."""
        filler = '\\"padding\\":1,' * 4000  # ~8,000 chars of drift once normalised
        html = profile_page_html().replace("<body>", f"<body>{filler}", 1)
        profile = public_lookup.profile_from_page(html, "gooduser")
        self.assertIsNotNone(profile)
        self.assertEqual((profile.followers, profile.following), (1234, 567))

    def test_a_page_without_the_profile_payload_reads_as_nothing(self):
        self.assertIsNone(public_lookup.profile_from_page(
            profile_page_html(include_anchor=False), "gooduser"))

    def test_a_payload_about_another_account_is_rejected(self):
        """Never attribute somebody else's numbers to this username."""
        self.assertIsNone(public_lookup.profile_from_page(
            profile_page_html(anchor_username="someoneelse"), "gooduser"))

    def test_an_abbreviated_post_count_stays_unknown(self):
        """"12.3K Posts" must not become 12300 in a column that feeds verdicts."""
        profile = public_lookup.profile_from_page(profile_page_html(posts="12.3K"), "gooduser")
        self.assertIsNone(profile.posts)
        self.assertEqual(profile.followers, 1234)  # the rest still reads

    def test_without_the_link_preview_the_post_count_is_unknown(self):
        profile = public_lookup.profile_from_page(
            profile_page_html(post_line=False), "gooduser")
        self.assertIsNone(profile.posts)
        self.assertEqual(profile.followers, 1234)

    def test_a_private_profile_with_a_withheld_avatar_stays_unknown(self):
        profile = public_lookup.profile_from_page(
            profile_page_html(username="secret", private=True, pic=""), "secret")
        self.assertTrue(profile.is_private)
        self.assertTrue(profile.avatar_withheld)

    def test_the_post_count_helper_ignores_other_counts(self):
        html = ('<meta property="og:description" content="1,800 Followers, 102 Following, '
                '0 Posts - See Instagram photos and videos from Lucid (&#064;lucid.sync)" />')
        self.assertEqual(public_lookup.post_count_from_page(html), 0)


class PageTransportTests(unittest.TestCase):
    """fetch() on the page transport: reads, retries once, never guesses."""

    def setUp(self):
        self._patches = _install_script({}, page_script={})
        self.addCleanup(_restore, *self._patches)
        self.lookup = PublicLookup()
        self.lookup.transport = "html"

    def test_reads_a_profile_off_the_page(self):
        FakeSession.page_script = {"gooduser": FakeResponse(body=profile_page_html())}
        profile = self.lookup.fetch("gooduser")
        self.assertEqual((profile.followers, profile.following, profile.posts), (1234, 567, 42))
        self.assertEqual(FakeSession.calls, ["gooduser"])

    def test_an_empty_page_is_retried_then_reported_missing(self):
        """One degraded page must not decide an account's fate."""
        attempts = {"n": 0}

        def shell():
            attempts["n"] += 1
            return FakeResponse(body="<html><title>Instagram</title></html>")

        FakeSession.page_script = {"ghost": shell}
        with self.assertRaises(public_lookup.PublicLookupError) as ctx:
            self.lookup.fetch("ghost")
        self.assertEqual(ctx.exception.kind, "not_found")
        self.assertEqual(attempts["n"], 2, "the empty page was not retried")

    def test_a_page_that_recovers_on_the_retry_is_a_normal_row(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                return FakeResponse(body="<html><title>Instagram</title></html>")
            return FakeResponse(body=profile_page_html())

        FakeSession.page_script = {"gooduser": flaky}
        profile = self.lookup.fetch("gooduser")
        self.assertEqual(profile.followers, 1234)
        self.assertEqual(attempts["n"], 2)

    def test_a_rate_limited_page_is_its_own_kind_and_is_not_retried(self):
        FakeSession.page_script = {"busy": FakeResponse(status_code=429)}
        with self.assertRaises(public_lookup.PublicLookupError) as ctx:
            self.lookup.fetch("busy")
        self.assertEqual(ctx.exception.kind, "rate_limited")
        self.assertEqual(FakeSession.calls, ["busy"])

    def test_a_login_redirect_is_reported_rather_than_read_as_missing(self):
        FakeSession.page_script = {"locked": FakeResponse(
            url="https://www.instagram.com/accounts/login/?next=/locked/",
            body=profile_page_html(username="locked"))}
        with self.assertRaises(public_lookup.PublicLookupError) as ctx:
            self.lookup.fetch("locked")
        self.assertEqual(ctx.exception.kind, "error")
        self.assertIn("login", ctx.exception.message.lower())

    def test_the_json_endpoint_remains_the_default_transport(self):
        self.assertEqual(PublicLookup().transport, "api")


class PreflightFallbackTests(unittest.TestCase):
    """Public mode must not be declared dead while the page still serves us."""

    def setUp(self):
        self._patches = _install_script({}, page_script={})
        self.addCleanup(_restore, *self._patches)

    def test_a_refused_endpoint_switches_the_run_to_the_page(self):
        FakeSession.script = {public_lookup.PROBE_USERNAME: FakeResponse(status_code=429)}
        FakeSession.page_script = {public_lookup.CONTROL_USERNAME: FakeResponse(
            body=profile_page_html(username=public_lookup.CONTROL_USERNAME))}
        lookup = PublicLookup()
        available, detail = public_lookup.probe_public_access(lookup)
        self.assertTrue(available, detail)
        self.assertEqual(lookup.transport, "html")
        self.assertEqual(FakeSession.calls,
                         [public_lookup.PROBE_USERNAME, public_lookup.CONTROL_USERNAME])

    def test_the_page_probe_asks_about_an_account_that_cannot_not_exist(self):
        FakeSession.script = {public_lookup.PROBE_USERNAME: FakeResponse(status_code=429)}
        FakeSession.page_script = {public_lookup.CONTROL_USERNAME: FakeResponse(
            body=profile_page_html(username=public_lookup.CONTROL_USERNAME))}
        public_lookup.probe_public_access(PublicLookup())
        self.assertEqual(FakeSession.calls[-1], public_lookup.CONTROL_USERNAME)
        self.assertNotEqual(public_lookup.CONTROL_USERNAME, public_lookup.PROBE_USERNAME)

    def test_a_working_endpoint_still_needs_no_page_request(self):
        FakeSession.script = {public_lookup.PROBE_USERNAME: FakeResponse(status_code=404)}
        lookup = PublicLookup()
        available, detail = public_lookup.probe_public_access(lookup)
        self.assertTrue(available, detail)
        self.assertEqual(lookup.transport, "api")
        self.assertEqual(FakeSession.calls, [public_lookup.PROBE_USERNAME])

    def test_both_paths_refused_reports_both(self):
        FakeSession.script = {public_lookup.PROBE_USERNAME: FakeResponse(status_code=429)}
        lookup = PublicLookup()  # page_script empty: the control gets a bare shell
        available, detail = public_lookup.probe_public_access(lookup)
        self.assertFalse(available)
        self.assertIn("rate-limited", detail.lower())
        self.assertIn("profile page", detail.lower())
        self.assertEqual(lookup.transport, "api", "a failed probe switched transport anyway")


if __name__ == "__main__":
    unittest.main()