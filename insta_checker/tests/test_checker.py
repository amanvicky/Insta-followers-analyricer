"""Unit tests for parsing, DP detection, verdict logic, and CSV export."""

import csv
import io
import threading
import unittest
import types

from checker import (
    SPEED_PRESETS,
    CheckerError,
    ProfileResult,
    dp_present_from_urls,
    normalize_username,
    parse_username_list,
    results_to_csv,
    verdict_for,
    write_csv_file,
)

GENERIC_AVATAR = "https://example.cdninstagram.com/44884218_345707102882519_2446069589734326528_n.jpg"
REAL_AVATAR = "https://example.cdninstagram.com/v/t51.2885-19/392817_987654_n.jpg"


class ParseUsernameListTests(unittest.TestCase):
    def test_newlines_commas_and_mixed_formats(self):
        text = "user_one\n@UserTwo, user_three; user_one"
        self.assertEqual(
            parse_username_list(text),
            ["user_one", "usertwo", "user_three"],
        )

    def test_empty_text_gives_empty_list(self):
        self.assertEqual(parse_username_list("   \n \n"), [])

    def test_spaces_inside_a_name_are_not_a_separator(self):
        """Regression (playtest): "bad name" used to split into two real
        lookups on two wrong accounts, and both looked fine in the table.
        One visible invalid row is honest; two silent checks cost requests."""
        self.assertEqual(parse_username_list("bad name"), ["bad name"])
        self.assertEqual(parse_username_list("okname\nbad name"), ["okname", "bad name"])

    def test_junk_entry_survives_instead_of_vanishing(self):
        """An '@@' entry must produce a row the user can see and fix, not be
        dropped silently with no feedback at all."""
        self.assertEqual(parse_username_list("@@"), ["@@"])

    def test_tabs_and_windows_line_endings_still_split(self):
        self.assertEqual(parse_username_list("a\tb\r\nc\r"), ["a", "b", "c"])

    def test_byte_order_mark_does_not_eat_the_first_username(self):
        """Regression (playtest): Notepad/Excel write "UTF-8" with a BOM.

        It is invisible, str.strip() does not remove it, and it used to travel
        inside the first name — so the first line of the file became
        "not a valid Instagram username" and was silently lost.
        """
        self.assertEqual(
            parse_username_list("\ufefffirst_user\nsecond_user"),
            ["first_user", "second_user"],
        )

    def test_byte_order_mark_is_stripped_from_any_entry_not_only_the_first(self):
        self.assertEqual(
            parse_username_list("a\n\ufeffb, \ufeffc"),
            ["a", "b", "c"],
        )

    def test_bom_only_text_is_empty_not_a_username(self):
        self.assertEqual(parse_username_list("\ufeff\n\ufeff  \n"), [])

    def test_invalid_username_raises(self):
        with self.assertRaises(CheckerError) as ctx:
            normalize_username("bad name!")
        self.assertEqual(ctx.exception.kind, "invalid")

    def test_normalize_strips_at_and_case(self):
        self.assertEqual(normalize_username("@SomeUser.X"), "someuser.x")


class DpDetectionTests(unittest.TestCase):
    def test_real_avatar_detected(self):
        self.assertTrue(dp_present_from_urls(REAL_AVATAR))

    def test_generic_avatar_means_no_dp(self):
        self.assertFalse(dp_present_from_urls(GENERIC_AVATAR))
        self.assertFalse(dp_present_from_urls(REAL_AVATAR, GENERIC_AVATAR))

    def test_missing_url_means_no_dp(self):
        self.assertFalse(dp_present_from_urls(""))
        self.assertFalse(dp_present_from_urls(None, None))


class AliveTests(unittest.TestCase):
    """Alive = the account resolves (accessible); dead = not_found/deleted/banned."""

    def test_resolved_profile_is_alive(self):
        result = ProfileResult(username="a", status="ok", alive=True)
        self.assertEqual(result.as_row()["alive"], "yes")

    def test_not_found_is_dead(self):
        result = ProfileResult(username="b", status="not_found", alive=False)
        self.assertEqual(result.as_row()["alive"], "no")

    def test_unknown_alive_renders_blank(self):
        result = ProfileResult(username="c", status="error", alive=None)
        self.assertEqual(result.as_row()["alive"], "")


class VerdictTests(unittest.TestCase):
    def test_no_dp_no_posts_is_likely_fake(self):
        r = ProfileResult(username="a", status="ok", alive=True, dp_present=False, posts=0)
        self.assertEqual(verdict_for(r), ("likely-fake", "no DP · 0 posts"))

    def test_following_heavily_exceeds_followers_is_suspicious(self):
        r = ProfileResult(username="b", status="ok", alive=True, dp_present=True,
                          posts=10, followers=5, following=3000)
        self.assertEqual(verdict_for(r)[0], "suspicious")

    def test_no_dp_few_posts_is_suspicious(self):
        r = ProfileResult(username="c", status="ok", alive=True, dp_present=False, posts=2)
        self.assertEqual(verdict_for(r)[0], "suspicious")

    def test_zero_posts_with_dp_is_inactive(self):
        r = ProfileResult(username="d", status="ok", alive=True, dp_present=True, posts=0)
        self.assertEqual(verdict_for(r), ("inactive", "0 posts"))

    def test_stale_last_post_is_inactive(self):
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        r = ProfileResult(username="e", status="ok", alive=True, dp_present=True,
                          posts=10, last_post_at=stale)
        verdict, reason = verdict_for(r)
        self.assertEqual(verdict, "inactive")
        self.assertIn(stale, reason)

    def test_healthy_profile_is_fine(self):
        r = ProfileResult(username="f", status="ok", alive=True, dp_present=True,
                          posts=50, followers=500, following=300)
        self.assertEqual(verdict_for(r), ("fine", ""))

    def test_not_found_is_dead(self):
        r = ProfileResult(username="g", status="not_found", alive=False)
        self.assertEqual(verdict_for(r), ("dead", "No Instagram account with this username."))

    def test_failed_check_gets_no_verdict(self):
        r = ProfileResult(username="h", status="error", error="boom")
        self.assertEqual(verdict_for(r), ("", ""))

    def test_unknown_dp_never_becomes_a_fake_verdict(self):
        """Regression (playtest): dp_present=False is not a neutral value.

        verdict_for turns it straight into "suspicious" / "likely-fake", so
        reporting *unknown* as *no* brands real accounts on the strength of
        data Instagram simply did not send.
        """
        unknown = ProfileResult(username="p", status="ok", alive=True,
                                dp_present=None, posts=1, is_private=True)
        self.assertEqual(verdict_for(unknown), ("fine", ""))  # judged on posts only
        zero_posts = ProfileResult(username="p", status="ok", alive=True,
                                   dp_present=None, posts=0, is_private=True)
        self.assertEqual(verdict_for(zero_posts)[0], "inactive")
        # And the contrast: when the data really does say "no photo", the
        # verdict must still fire.
        really_no_photo = ProfileResult(username="p", status="ok", alive=True,
                                        dp_present=False, posts=0)
        self.assertEqual(verdict_for(really_no_photo)[0], "likely-fake")


class WithheldAvatarTests(unittest.TestCase):
    """Session mode must not read a withheld avatar as a missing photo.

    Instagram answers with an empty profile_pic_url for a private profile the
    signed-in account does not follow. Public mode already treats that as
    "unknown"; session mode — the default, and the only mode that currently
    works from this machine — used to treat it as "no photo", which then
    flowed straight into the verdict column.
    """

    @staticmethod
    def _info(**kw):
        base = dict(pk="pk", username="u", media_count=2, follower_count=10,
                    following_count=5, is_private=True, is_verified=False,
                    full_name="u", profile_pic_url="", profile_pic_url_hd="")
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_private_profile_with_no_avatar_url_is_unknown_not_no(self):
        from checker import InstagramChecker

        result = InstagramChecker._to_result(self._info(), "u")
        self.assertIsNone(result.dp_present)
        self.assertNotIn("no DP", verdict_for(result)[1])

    def test_public_profile_with_no_avatar_url_really_has_no_photo(self):
        from checker import InstagramChecker

        result = InstagramChecker._to_result(self._info(is_private=False), "u")
        self.assertFalse(result.dp_present)
        self.assertIn("no DP", verdict_for(result)[1])

    def test_private_profile_with_the_grey_placeholder_is_still_no_photo(self):
        from checker import InstagramChecker

        result = InstagramChecker._to_result(self._info(profile_pic_url=GENERIC_AVATAR), "u")
        self.assertFalse(result.dp_present)


class SpeedPresetTests(unittest.TestCase):
    def test_presets_have_workers_and_delays(self):
        for name, (workers, (low, high)) in SPEED_PRESETS.items():
            self.assertGreaterEqual(workers, 1, name)
            self.assertLessEqual(low, high, name)
            self.assertGreaterEqual(low, 0.0, name)

    def test_turbo_is_fastest(self):
        self.assertGreater(SPEED_PRESETS["turbo"][0], SPEED_PRESETS["fast"][0])
        self.assertGreater(SPEED_PRESETS["fast"][0], SPEED_PRESETS["safe"][0])


class CsvExportTests(unittest.TestCase):
    def test_rows_and_headers(self):
        results = [
            ProfileResult(
                username="user_one",
                status="ok",
                alive=True,
                dp_present=True,
                posts=3,
                followers=100,
                following=50,
            ),
            ProfileResult(username="ghost", status="not_found", alive=False, error="missing"),
        ]
        raw = results_to_csv(results)
        rows = list(csv.DictReader(io.StringIO(raw)))
        self.assertEqual(
            list(rows[0].keys())[:8],
            ["username", "alive", "verdict", "verdict_reason", "dp_present", "last_post_at", "posts", "followers"],
        )
        self.assertEqual(rows[0]["verdict"], "fine")
        self.assertEqual(rows[0]["dp_present"], "yes")
        self.assertEqual(rows[0]["alive"], "yes")
        self.assertEqual(rows[0]["followers"], "100")
        self.assertEqual(rows[1]["dp_present"], "")
        self.assertEqual(rows[1]["alive"], "no")
        self.assertEqual(rows[1]["status"], "not_found")

    def test_write_csv_file_has_no_blank_rows(self):
        """Regression: Windows text-mode translation must not double the \r\n."""
        import os
        import tempfile

        results = [
            ProfileResult(username=f"user_{i}", status="ok", alive=True, dp_present=True,
                          posts=1, followers=2, following=3)
            for i in range(3)
        ]
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            write_csv_file(path, results)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 4)  # header + 3 data rows, no blanks
        finally:
            os.remove(path)


class SessionIdSigninTests(unittest.TestCase):
    """Regression: a dead sessionid must fail sign-in, not report success.

    Found live: instagrapi's login_by_sessionid can 'succeed' via fallback
    endpoints that don't validate the session, so checker.py verifies with a
    strictly-authenticated account_info() call before declaring success.
    """

    def _checker_with_client(self):
        import types
        from checker import InstagramChecker

        ck = InstagramChecker(session_dir=__import__("pathlib").Path(
            __import__("tempfile").mkdtemp(prefix="ig_test_sid_")))
        return ck

    def test_dead_sessionid_rejected_when_account_info_fails(self):
        from instagrapi.exceptions import ClientError

        class DeadClient:
            delay_range = [0, 0]
            read_timeout = 10

            def login_by_sessionid(self, sid):
                self.username = "alxdcoawe"  # instagrapi reports success anyway
                return True

            def account_info(self):
                raise ClientError("login_required")

            def dump_settings(self, path):
                raise OSError("must not be reached")

        ck = self._checker_with_client()
        import checker as checker_mod

        real_client = checker_mod.Client
        checker_mod.Client = DeadClient
        try:
            with self.assertRaises(CheckerError) as ctx:
                ck.login_with_sessionid("24349645916%3Adeadbeefdeadbeefdeadbeef")
        finally:
            checker_mod.Client = real_client
        self.assertIn("has ended", ctx.exception.message)
        self.assertIsNone(ck.client)  # no phantom session retained

    def test_valid_sessionid_signs_in_and_saves(self):
        import types
        import checker as checker_mod

        class GoodClient:
            delay_range = [0, 0]
            read_timeout = 10
            username = "tester"

            def login_by_sessionid(self, sid):
                return True

            def account_info(self):
                return types.SimpleNamespace(username="tester")

            def dump_settings(self, path):
                types.SimpleNamespace(path=path)  # pretend to save
                open(path, "w").write("{}")

        ck = self._checker_with_client()
        real_client = checker_mod.Client
        checker_mod.Client = GoodClient
        try:
            ck.login_with_sessionid("24349645916%3Agoodbeefgoodbeefgoodbeefgood")
        finally:
            checker_mod.Client = real_client
        self.assertIsNotNone(ck.client)
        self.assertEqual(ck.username, "tester")


class FakeScrapeClient:
    """Minimal client for scrape_user_list: totals from the lookup, N pages.

    Pages are addressed by cursor (max_id), like Instagram's real pagination,
    so resume-from-cursor can be tested: the cursor printed by one page is
    passed back in and lands on the next one.
    """

    def __init__(self, pages, fail_on_page=None, raise_mode="feedback"):
        # pages: list of (usernames, next_max_id); fail_on_page: 0-based
        # *request* index (retries included) that raises. raise_mode:
        #   "feedback"      — the dedicated FeedbackRequired class
        #   "client_feedback" — plain ClientError carrying feedback_required text
        #   "ratelimit"     — RateLimitError (429)
        #   "other"          — plain ClientError with unrelated text
        from instagrapi.exceptions import FeedbackRequired
        self._pages = pages
        self._fail_on_page = fail_on_page
        self._FeedbackRequired = FeedbackRequired
        self._raise_mode = raise_mode
        self._page_calls = 0
        self._cursor_index = {"": 0}
        for index, (_users, next_max_id) in enumerate(pages):
            if next_max_id:
                self._cursor_index[next_max_id] = index + 1

    @property
    def page_calls(self) -> int:
        """Requests actually made (a raising request counts once per attempt)."""
        return self._page_calls

    def _raise(self, message="We limit how often you can do certain things on Instagram"):
        from instagrapi.exceptions import ClientError, RateLimitError
        if self._raise_mode == "feedback":
            raise self._FeedbackRequired(message)
        if self._raise_mode == "ratelimit":
            raise RateLimitError("429 Too Many Requests")
        if self._raise_mode == "other":
            raise ClientError(message)
        raise ClientError(f"feedback_required: {message}")

    def user_info_by_username(self, name, use_cache=False):
        if self._fail_on_page == "lookup":
            self._raise()
        total = sum(len(users) for users, _ in self._pages)
        return types.SimpleNamespace(pk="12345", username=name,
                                     follower_count=total, following_count=1)

    def user_followers_v1_chunk(self, user_id, max_amount=200, max_id=""):
        if self._fail_on_page is not None and self._page_calls == self._fail_on_page:
            self._raise()  # _page_calls deliberately not advanced: a retry hits it again
        self._page_calls += 1
        index = self._cursor_index.get(max_id, len(self._pages))
        if index >= len(self._pages):
            return ([], "")  # cursor past the scripted pages: end of list
        users, next_max_id = self._pages[index]
        return ([types.SimpleNamespace(username=u) for u in users], next_max_id)

    # Both list types paginate identically from the scraper's point of view.
    user_following_v1_chunk = user_followers_v1_chunk


class ScrapeFeedbackTests(unittest.TestCase):
    """Regression: feedback_required must keep partial names, not raw-error.

    Found live: Instagram action-blocked a followers scrape mid-way; the app
    showed the raw 'feedback_required: ...' text and offered no way to keep
    the ~390 names already scraped.
    """

    def test_block_on_first_page_returns_friendly_error(self):
        from followers import scrape_user_list

        cl = FakeScrapeClient([(["a", "b"], "")], fail_on_page=0)
        with self.assertRaises(CheckerError) as ctx:
            scrape_user_list(cl, "target", "followers")
        self.assertIn("feedback_required", ctx.exception.message)
        self.assertIn("Wait a few hours", ctx.exception.message)

    def test_block_mid_scrape_keeps_partial_names(self):
        from followers import scrape_user_list

        cl = FakeScrapeClient([(["a", "b"], "more"), (["c", "d"], "")],
                              fail_on_page=1)
        outcome = scrape_user_list(cl, "target", "followers")
        self.assertEqual(outcome.names, ["a", "b"])
        self.assertTrue(outcome.stopped)
        self.assertIn("Instagram paused this account's scraping", outcome.note)
        self.assertIn("kept", outcome.note)
        self.assertTrue(outcome.blocked)  # a real block: app records the 6h window
        self.assertEqual(outcome.cursor, "more")  # and can resume from page 2

    def test_clean_scrape_has_no_note(self):
        from followers import scrape_user_list

        cl = FakeScrapeClient([(["a", "b"], "")])
        outcome = scrape_user_list(cl, "target", "followers")
        self.assertEqual(outcome.names, ["a", "b"])
        self.assertFalse(outcome.stopped)
        self.assertEqual(outcome.note, "")

    def test_page_pause_can_be_disabled_for_lightning_scrapes(self):
        import followers as followers_mod

        pauses = []
        original_wait = followers_mod._stop_wait
        followers_mod._stop_wait = lambda seconds, stop_event: (
            pauses.append(seconds) or stop_event.is_set()
        )
        self.addCleanup(setattr, followers_mod, "_stop_wait", original_wait)

        outcome = followers_mod.scrape_user_list(
            FakeScrapeClient([(["a"], "more"), (["b"], "")]),
            "target",
            "followers",
            page_pause_seconds=0,
        )

        self.assertEqual(outcome.names, ["a", "b"])
        self.assertEqual(pauses, [0])

    def test_plain_clienterror_feedback_sniffed_in_page_loop(self):
        """Regression (screenshot round 5): the block can surface as a plain
        ClientError whose text starts 'feedback_required:' — it must route to
        the friendly keep-partial-names path, not the raw-error dialog."""
        from followers import scrape_user_list

        cl = FakeScrapeClient([(["a", "b"], "more"), (["c", "d"], "")],
                              fail_on_page=1, raise_mode="client_feedback")
        outcome = scrape_user_list(cl, "target", "followers")
        self.assertEqual(outcome.names, ["a", "b"])
        self.assertIn("Instagram paused this account's scraping", outcome.note)

    def test_plain_clienterror_feedback_sniffed_at_lookup(self):
        from followers import scrape_user_list

        cl = FakeScrapeClient([(["a", "b"], "")],
                              fail_on_page="lookup", raise_mode="client_feedback")
        with self.assertRaises(CheckerError) as ctx:
            scrape_user_list(cl, "target", "followers")
        self.assertEqual(ctx.exception.kind, "feedback")
        self.assertIn("Wait a few hours", ctx.exception.message)  # friendly wording

    def test_unrelated_clienterror_keeps_raw_error(self):
        """Only feedback_required text is sniffed; other failures stay raw."""
        from followers import scrape_user_list

        cl = FakeScrapeClient([(["a", "b"], "more"), (["c", "d"], "")],
                              fail_on_page=1, raise_mode="other")
        with self.assertRaises(CheckerError) as ctx:
            scrape_user_list(cl, "target", "followers")
        self.assertEqual(ctx.exception.kind, "error")
        self.assertIn("scrape failed", ctx.exception.message)


class ScrapeResumeTests(unittest.TestCase):
    """Big lists must survive an interruption: keep the names, remember the page.

    Found live: a target with 2,000+ following lost a whole partial scrape to a
    single rate limit, so the next attempt re-fetched every page — doubling the
    request volume that got the account blocked in the first place.
    """

    PAGES = [(["a", "b"], "more"), (["c", "d"], "more2"), (["e"], "")]

    def _no_wait(self):
        """Zero out the rate-limit backoff so tests never really sleep."""
        import followers as followers_mod
        original = followers_mod.RATE_LIMIT_WAIT_SECONDS
        followers_mod.RATE_LIMIT_WAIT_SECONDS = 0
        self.addCleanup(setattr, followers_mod, "RATE_LIMIT_WAIT_SECONDS", original)

    def test_resume_skips_pages_already_fetched(self):
        from followers import ScrapeResume, scrape_user_list

        cl = FakeScrapeClient(list(self.PAGES))
        outcome = scrape_user_list(cl, "target", "followers",
                                   resume=ScrapeResume(max_id="more2", names=["a", "b", "c", "d"]))
        self.assertEqual(outcome.names, ["a", "b", "c", "d", "e"])
        self.assertEqual(cl.page_calls, 1)  # only the last page was requested
        self.assertFalse(outcome.stopped)
        self.assertEqual(outcome.cursor, "")  # complete: a later scrape starts fresh

    def test_resume_dedupes_names_already_held(self):
        """Followers churn between attempts: overlap must not duplicate rows."""
        from followers import ScrapeResume, scrape_user_list

        # The page after the cursor repeats an already-held name ("b").
        cl = FakeScrapeClient([(["x", "y"], "more"), (["b", "c"], "")])
        outcome = scrape_user_list(cl, "target", "followers",
                                   resume=ScrapeResume(max_id="more", names=["a", "b"]))
        self.assertEqual(outcome.names, ["a", "b", "c"])

    def test_rate_limit_mid_scrape_keeps_names_and_cursor(self):
        from followers import scrape_user_list

        self._no_wait()
        cl = FakeScrapeClient(list(self.PAGES), fail_on_page=1, raise_mode="ratelimit")
        outcome = scrape_user_list(cl, "target", "following")
        self.assertEqual(outcome.names, ["a", "b"])  # not thrown away
        self.assertTrue(outcome.stopped)
        self.assertEqual(outcome.cursor, "more")
        self.assertFalse(outcome.blocked)  # a rate limit is not a feedback block
        self.assertIn("rate-limited", outcome.note)
        self.assertIn("resume", outcome.note.lower())

        # The saved cursor really does finish the list on the next attempt.
        cl2 = FakeScrapeClient(list(self.PAGES))
        from followers import ScrapeResume
        resumed = scrape_user_list(cl2, "target", "following",
                                   resume=ScrapeResume(max_id=outcome.cursor, names=outcome.names))
        self.assertEqual(resumed.names, ["a", "b", "c", "d", "e"])

    def test_rate_limit_before_first_page_still_raises(self):
        """Nothing scraped yet: there is no progress to keep, so a clear error."""
        from followers import scrape_user_list

        self._no_wait()
        cl = FakeScrapeClient([(["a", "b"], "")], fail_on_page=0, raise_mode="ratelimit")
        with self.assertRaises(CheckerError) as ctx:
            scrape_user_list(cl, "target", "followers")
        self.assertEqual(ctx.exception.kind, "rate_limit")

    def test_user_stop_keeps_resume_cursor(self):
        from followers import scrape_user_list

        cl = FakeScrapeClient(list(self.PAGES))
        stop = threading.Event()
        outcome = scrape_user_list(
            cl, "target", "followers", stop_event=stop,
            on_progress=lambda p: stop.set() if p.scraped >= 2 else None,
        )
        self.assertTrue(outcome.stopped)
        self.assertEqual(outcome.names, ["a", "b"])
        self.assertEqual(outcome.cursor, "more")  # a later click can continue


class RecordingProfileClient:
    """Minimal session client for fetch_profile: canned profiles, scripted errors."""

    delay_range = [0, 0]
    read_timeout = 10

    def __init__(self, throttle=(), challenge=()):
        self._throttle = set(throttle)
        self._challenge = set(challenge)
        self.calls = []

    def user_info_by_username(self, username, use_cache=False):
        from instagrapi.exceptions import ChallengeError, RateLimitError
        self.calls.append(username)
        if username in self._challenge:
            raise ChallengeError("challenge_required")
        if username in self._throttle:
            raise RateLimitError("429 Too Many Requests")
        return types.SimpleNamespace(
            pk=f"pk_{username}", username=username, media_count=5,
            follower_count=10, following_count=10, is_private=False,
            is_verified=False, full_name="", profile_pic_url=REAL_AVATAR,
            profile_pic_url_hd="",
        )


class ThrottleCircuitBreakerTests(unittest.TestCase):
    """Sustained 429s must halt the run instead of retrying through them.

    Found live: the checker spent a 2,000-name audit logging "rate limited,
    waiting 20s…" row after row; that retry-through-throttle traffic is what
    escalated to the signed-in account being action-blocked and lost.
    """

    def _checker(self):
        import pathlib
        import tempfile
        from checker import InstagramChecker

        ck = InstagramChecker(session_dir=pathlib.Path(tempfile.mkdtemp(prefix="ig_test_halt_")))
        ck._interruptible_sleep = lambda *_: None  # no real backoff sleeps in tests
        return ck

    def _run(self, ck, cl, names):
        ck.client = cl
        ck.username = "me"
        stop = threading.Event()
        results = []
        ck.check_many(names, workers=1, delay_range=(0, 0),
                      on_result=results.append, stop_event=stop)
        return stop, results

    def test_sustained_rate_limits_halt_the_run(self):
        from checker import RATE_LIMIT_STREAK_LIMIT

        ck = self._checker()
        names = [f"user{i}" for i in range(50)]
        cl = RecordingProfileClient(throttle=names)
        stop, results = self._run(ck, cl, names)
        self.assertTrue(stop.is_set())  # the run was cut short, not finished
        self.assertIn("halted", ck.halt_reason)
        self.assertLessEqual(len(cl.calls), RATE_LIMIT_STREAK_LIMIT + 1)
        self.assertLess(len(results), len(names))

    def test_new_run_clears_the_previous_halt(self):
        ck = self._checker()
        names = [f"user{i}" for i in range(10)]
        self._run(ck, RecordingProfileClient(throttle=names), names)
        self.assertTrue(ck.halt_reason)
        # Instagram recovered: the next run must be clean and complete.
        cl = RecordingProfileClient()
        stop, results = self._run(ck, cl, names[:3])
        self.assertFalse(stop.is_set())
        self.assertEqual(ck.halt_reason, "")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "ok" for r in results))

    def test_isolated_throttles_do_not_halt(self):
        """Two throttled accounts separated by successes must not trip it:
        only a *streak* means Instagram is refusing everything."""
        ck = self._checker()
        names = ["bad1", "u1", "bad2", "u2", "u3"]
        cl = RecordingProfileClient(throttle={"bad1", "bad2"})
        stop, results = self._run(ck, cl, names)
        self.assertFalse(stop.is_set())
        self.assertEqual(ck.halt_reason, "")
        self.assertEqual(len(results), len(names))
        self.assertEqual(sum(1 for r in results if r.status == "error"), 2)

    def test_security_challenge_halts_the_run(self):
        """A challenge means Instagram is questioning the session: every further
        request makes it worse, so the run stops and the UI explains it."""
        ck = self._checker()
        names = ["bad"] + [f"user{i}" for i in range(20)]
        cl = RecordingProfileClient(challenge={"bad"})
        with self.assertRaises(CheckerError) as ctx:
            self._run(ck, cl, names)
        self.assertEqual(ctx.exception.kind, "challenge")
        self.assertIn("security challenge", ctx.exception.message)
        self.assertLess(len(cl.calls), len(names))  # stopped, not hammering on


class PublicBatchingTests(unittest.TestCase):
    """Public mode must pace big lists exactly like session mode does.

    Found by playtest: the two engines generate the same traffic to Instagram
    — one IP asking about thousands of usernames — but only session mode
    chunked. Measured, a 320-name public run at "lightning" finished in 3.6s
    (88 req/s) with no ceiling, and a 3,000-name list would have done the same
    in well under a minute of unbroken requests.
    """

    def _checker(self):
        import pathlib
        import tempfile

        from checker import InstagramChecker

        ck = InstagramChecker(session_dir=pathlib.Path(tempfile.mkdtemp(prefix="ig_test_pub_")))
        ck._interruptible_sleep = lambda *_: None
        return ck

    def _public_run(self, ck, names, **kwargs):
        """Run public_check_many with the transport and preflight stubbed out."""
        import checker as checker_mod

        class FakeProfile:
            avatar_withheld = False
            posts, followers, following = 1, 2, 3
            is_private = is_verified = False
            full_name = "n"
            profile_pic_url = REAL_AVATAR

        class FakeLookup:
            def fetch(self, username):
                return FakeProfile()

        orig_lookup, orig_probe = checker_mod.PublicLookup, checker_mod.probe_public_access
        checker_mod.PublicLookup = lambda *a, **k: FakeLookup()
        checker_mod.probe_public_access = lambda *a, **k: (True, "")
        try:
            results, batches, cools = [], [], []
            ck.public_check_many(
                names, workers=2, delay_range=(0, 0),
                on_result=results.append,
                on_batch=lambda i, n: batches.append((i, n)),
                on_cooldown=cools.append,
                **kwargs,
            )
        finally:
            checker_mod.PublicLookup, checker_mod.probe_public_access = orig_lookup, orig_probe
        return results, batches, cools

    def test_a_list_over_the_batch_size_is_chunked_and_paused(self):
        from checker import BATCH_SIZE

        ck = self._checker()
        names = [f"user{i}" for i in range(BATCH_SIZE + 5)]
        # A real but tiny pause: the point is that one happened, and the
        # production 20-40s range is not something a test should sit through.
        results, batches, cools = self._public_run(ck, names, pause_range=(0.01, 0.02))

        self.assertEqual(len(results), len(names))  # nothing lost to chunking
        self.assertEqual(batches, [(1, 2), (2, 2)])
        self.assertTrue(any(c > 0 for c in cools), "no between-batch cool-down was reported")

    def test_a_short_list_is_one_batch_with_no_pause(self):
        ck = self._checker()
        results, batches, cools = self._public_run(ck, [f"user{i}" for i in range(5)])
        self.assertEqual(len(results), 5)
        self.assertEqual(batches, [(1, 1)])
        self.assertFalse([c for c in cools if c > 0], "paused after a single batch")

    def test_stopping_mid_run_ends_the_remaining_batches(self):
        from checker import BATCH_SIZE

        ck = self._checker()
        stop = threading.Event()
        stop.set()  # as if the user hit Stop before the first batch
        names = [f"user{i}" for i in range(BATCH_SIZE + 5)]
        results, batches, cools = self._public_run(ck, names, stop_event=stop)
        self.assertEqual(results, [])
        self.assertEqual(batches, [])


if __name__ == "__main__":
    unittest.main()
