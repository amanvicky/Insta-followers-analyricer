"""Headless tests for the Tk surface itself.

Every defect fixed in a playtest pass so far — button states, the resume
prompt, the run summary, BOM'd list files — lived in app.py, which had no
tests at all: each one was verified by a throwaway harness that got deleted,
so the next edit could quietly undo it. These are the cheap ones, driven
through the real widgets and the real run pipeline, with only the network
(fake Instagram client) and the file dialogs stubbed.

All persistence hangs off checker.session_dir, so every test points the
checker at its own temp directory: nothing here touches ~/.insta_checker, and
no test sees another test's checkpoint.
"""

import gc
import os
import tempfile
import time
import types
import unittest
from pathlib import Path

import app as app_mod
import checker as checker_mod
from instagrapi.exceptions import UserNotFound
from tkinter import ttk

class FakeClient:
    """Just enough instagrapi Client for a check run to complete offline."""

    delay_range = [0, 0]
    read_timeout = 10
    request_timeout = 0
    public_request_retries_count = 1
    session_retry_total = 0
    username = "tester"

    #: pages served so far, so a test can assert nothing was requested
    pages_served = 0
    #: how many times a login actually reached the (fake) Instagram
    logins_attempted = 0

    def login(self, username, password, verification_code=""):
        FakeClient.logins_attempted += 1
        self.username = username
        return True

    def account_info(self):
        return types.SimpleNamespace(username=self.username)

    def user_info_by_username(self, name, use_cache=False):
        if name == "ghost_user":
            raise UserNotFound("no such user")
        if name.startswith("withheld"):
            # Instagram answers a private profile with an empty avatar URL
            # rather than the grey placeholder.
            return types.SimpleNamespace(
                pk="pk", username=name, media_count=0, follower_count=1, following_count=1,
                is_private=True, is_verified=False, full_name=name,
                profile_pic_url="", profile_pic_url_hd="")
        return types.SimpleNamespace(
            pk="pk", username=name, media_count=3, follower_count=10, following_count=5,
            is_private=False, is_verified=False, full_name=name,
            profile_pic_url="https://example.cdninstagram.com/v/t51.2885-19/392817_987654_n.jpg",
            profile_pic_url_hd="",
        )

    user_info_by_username_gql = user_info_by_username

    def _chunk(self, user_id, max_id="", **kw):
        page = FakeClient.pages_served
        FakeClient.pages_served += 1
        if page >= 2:
            return [], ""
        return [types.SimpleNamespace(username=f"follower{page}_{i}") for i in range(3)], f"C{page + 1}"

    user_followers_v1_chunk = _chunk
    user_following_v1_chunk = _chunk


class FakeMessageBox:
    seen: list = []
    #: answers returned by askyesno, popped in order (last one repeats)
    answers: list = []

    @staticmethod
    def _record(kind, args):
        FakeMessageBox.seen.append((kind, args[1] if len(args) > 1 else ""))

    @staticmethod
    def showwarning(*args, **kw):
        FakeMessageBox._record("warning", args)

    @staticmethod
    def showerror(*args, **kw):
        FakeMessageBox._record("error", args)

    @staticmethod
    def showinfo(*args, **kw):
        FakeMessageBox._record("info", args)

    @staticmethod
    def askyesno(*args, **kw):
        FakeMessageBox._record("askyesno", args)
        if len(FakeMessageBox.answers) > 1:
            return FakeMessageBox.answers.pop(0)
        return FakeMessageBox.answers[0] if FakeMessageBox.answers else True


class FakeDialog:
    open_path = ""
    save_path = ""

    @staticmethod
    def askopenfilename(**kw):
        return FakeDialog.open_path

    @staticmethod
    def asksaveasfilename(**kw):
        return FakeDialog.save_path


class AppTestCase(unittest.TestCase):
    """Builds a real (withdrawn) App around a fake Instagram."""

    @classmethod
    def setUpClass(cls):
        cls._orig_client = checker_mod.Client
        cls._orig_mb, cls._orig_dialog = app_mod.messagebox, app_mod.filedialog
        cls._orig_init = checker_mod.InstagramChecker.__init__
        cls._session_dir = Path(tempfile.mkdtemp(prefix="ig_apptest_"))
        checker_mod.Client = FakeClient
        app_mod.messagebox = FakeMessageBox
        app_mod.filedialog = FakeDialog

        def temp_init(self, session_dir=None, on_log=None):
            # One directory per test: the App restores its checkpoint on
            # startup, so a shared directory would hand the next test the
            # previous test's rows.
            cls._orig_init(self, session_dir=cls._session_dir, on_log=on_log)
            self._interruptible_sleep = lambda *_: None  # no real back-off in tests

        checker_mod.InstagramChecker.__init__ = temp_init

    @classmethod
    def tearDownClass(cls):
        checker_mod.Client = cls._orig_client
        app_mod.messagebox, app_mod.filedialog = cls._orig_mb, cls._orig_dialog
        checker_mod.InstagramChecker.__init__ = cls._orig_init

    def setUp(self):
        FakeMessageBox.seen.clear()
        FakeMessageBox.answers.clear()
        FakeClient.pages_served = 0
        FakeClient.logins_attempted = 0
        # type(self), not the base class: temp_init closes over the concrete
        # class, so this must write the attribute temp_init actually reads.
        type(self)._session_dir = Path(tempfile.mkdtemp(prefix="ig_apptest_"))
        self.app = app_mod.App()
        self.app.withdraw()
        self._pump(0.2)
        self.app.checker.client = FakeClient()
        self.app.checker.username = "tester"
        self.app.var_mode.set("Session (safest data)")
        self.app.var_speed.set("safe")

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        # Finalise this app's Tk objects here, on the main thread, while no
        # worker is running. unittest frees each finished TestCase (and with
        # it the App), but Tk widgets form reference cycles, so their
        # finalisers can run much later — during a *worker thread's*
        # allocation. Tkinter refuses Tcl calls from a thread that is not the
        # main one ("main thread is not in main loop"), and one of those
        # finalisers was caught wedging a scrape worker mid-page for over 30s,
        # which made an unrelated test fail intermittently.
        self.app = None
        gc.collect()

    def _pump(self, seconds=5.0, until=None):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            if until is not None and until():
                return True
            time.sleep(0.01)
        return bool(until()) if until else True

    def _settle(self, quiet=0.3, timeout=8.0):
        """Wait until the UI has drained every queued worker event.

        The helpers used to end with a fixed sleep ("worker is dead, sleep
        0.3s, assert"). That is racy by construction: a worker only *queues*
        its last result and "done" event before it dies, and the UI drains
        them on its next 100ms pump, so the sleep can expire with events still
        in flight. Waiting for the queue to go quiet replaces the bet on a
        fixed delay with the condition the assertion actually needs.
        """
        end = time.time() + timeout
        empty_since = None
        while time.time() < end:
            self.app.update()
            if self.app.ui_queue.empty():
                empty_since = empty_since or time.time()
                if time.time() - empty_since >= quiet:
                    return
            else:
                empty_since = None
            time.sleep(0.01)

    def _run(self, text, timeout=30):
        self.app.txt_input.delete("1.0", "end")
        self.app.txt_input.insert("1.0", text)
        self.app.update()
        self.app.btn_start.invoke()
        self._pump(timeout, until=lambda: self.app.worker is not None
                   and not self.app.worker.is_alive())
        self._settle()
        return str(self.app.lbl_progress["text"])

    def _rows(self):
        return [self.app.tree.item(i, "values") for i in self.app.tree.get_children()]


class LoadFileTests(AppTestCase):
    def test_utf8_bom_file_keeps_its_first_username(self):
        """Regression (playtest): Notepad and Excel write UTF-8 *with* a BOM.

        The BOM is invisible and str.strip() does not remove it, so the first
        line of the file used to arrive as "not a valid Instagram username"
        and was silently lost — every other line looked perfect.
        """
        path = self.app.checker.session_dir / "bom_list.txt"
        path.write_bytes("\ufefffirst_user\nsecond_user\n".encode("utf-8"))
        FakeDialog.open_path = str(path)
        self.app.on_load_file()
        self._pump(0.2)

        box = self.app.txt_input.get("1.0", "end")
        self.assertNotIn("\ufeff", box, "the BOM reached the text box")
        self.assertIn("first_user", box)
        # And the loaded list must actually check, not just look right.
        summary = self._run(box)
        self.assertIn("2 checked OK", summary)
        self.assertEqual([r[0] for r in self._rows()], ["first_user", "second_user"])


class RunSummaryTests(AppTestCase):
    def test_summary_describes_this_run_not_the_whole_table(self):
        """Regression (playtest): the table keeps earlier runs on purpose.

        Counting from the table made the second run over 2 names report
        "7 checked OK" — 5 from the previous run — which reads as an audit
        result for the list that was just checked.
        """
        first = self._run("\n".join(f"first{i}" for i in range(5)))
        self.assertIn("5 checked OK", first)
        self.assertEqual(len(self._rows()), 5)

        second = self._run("second0\nsecond1")
        self.assertIn("2 checked OK", second)
        self.assertNotIn("5 checked OK", second)
        # The rows from run one are still there, untouched: only the tally is
        # scoped to the run.
        self.assertEqual(len(self._rows()), 7)

    def test_missing_and_error_rows_are_counted_for_the_run(self):
        summary = self._run("real_user\nghost_user")
        self.assertIn("1 checked OK", summary)
        self.assertIn("1 missing/invalid", summary)

    def test_a_withheld_avatar_is_never_shown_or_verdicted_as_no_photo(self):
        """Regression (playtest): Instagram withholds a private profile's
        avatar. dp_present=False is not neutral — the verdict column turns it
        into "likely-fake"/"suspicious" — so the table must show it as blank
        and judge the account on what was actually fetched."""
        self._run("withheld_user")
        row = next(r for r in self._rows() if r[0] == "withheld_user")
        self.assertEqual(row[3], "", "DP column claims a photo is absent")
        self.assertNotIn(row[2], ("likely-fake", "suspicious"),
                         f"accused a real account on withheld data: {row[2]} {row[10]}")


class BuildListControlsReachableTests(AppTestCase):
    """No control may be silently dropped below the fold.

    Regression (playtest): the build-list panel needs ~700px and the window
    offers less, and pack allocated the shortfall by dropping the overflow.
    Measured at 1120x720 — the app's own default — Start Checking, Stop,
    Export and Clear were not on screen at all, and only all appeared from
    900px up. Every earlier playtest had driven the app with
    ``btn.invoke()``, which works on a widget whether or not a human can see
    it, so this hid from all of them.
    """

    CONTROLS = ("btn_start", "btn_stop", "btn_export", "btn_clear", "btn_scrape", "btn_signin")

    def _all_usable(self):
        app = self.app
        app.left_canvas.yview_moveto(1.0)  # scroll to the bottom, as a user would
        self._pump(0.3)
        bad = []
        for name in self.CONTROLS:
            widget = getattr(app, name)
            if not widget.winfo_ismapped():
                bad.append(f"{name} is not on screen")
            elif widget.winfo_height() < 10:
                bad.append(f"{name} collapsed to {widget.winfo_height()}px")
        return bad

    def test_every_control_is_reachable_at_the_default_window_size(self):
        app = self.app
        app.deiconify()
        app.geometry("1120x720")
        self._pump(0.8)
        try:
            self.assertEqual(self._all_usable(), [])
        finally:
            app.withdraw()

    def test_every_control_is_reachable_at_the_minimum_window_size(self):
        app = self.app
        app.deiconify()
        app.geometry("960x600")
        self._pump(0.8)
        try:
            self.assertEqual(self._all_usable(), [])
        finally:
            app.withdraw()

    def test_the_build_list_panel_actually_scrolls(self):
        self.assertTrue(self.app.left_canvas.bind("<MouseWheel>"),
                        "no wheel binding: hidden controls cannot be reached")


class SingleInstanceTests(unittest.TestCase):
    """Only one copy of the app may run at a time.

    Regression (playtest): two copies share one checkpoint, one autosave CSV
    and one pending-names file, and each keeps the whole result set in memory
    and rewrites the whole file when it saves. Measured with two windows open,
    the second launch silently deleted the first's results and the first then
    deleted the second's — a finished audit destroyed, with no error shown to
    anyone. Double-clicking run.bat is exactly how this happens.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ig_lock_"))

    def test_the_second_copy_is_refused_and_the_lock_releases_on_exit(self):
        from app import acquire_single_instance

        first = acquire_single_instance(self.dir)
        self.assertIsNotNone(first, "the first copy could not take the lock")

        second = acquire_single_instance(self.dir)
        self.assertIsNone(second, "a second copy was allowed to start")

        # Releasing must not depend on a tidy shutdown: the OS frees the lock
        # when the handle closes, so a crash cannot lock the user out forever.
        first.close()
        third = acquire_single_instance(self.dir)
        self.assertIsNotNone(third, "the lock was not released; the user could never reopen the app")
        third.close()

    def test_the_pid_stays_readable_while_the_lock_is_held(self):
        """The second copy needs the pid precisely when it cannot take the lock.

        Regression: locking byte 0 made the very first read of the file fail,
        so the pid could never be obtained in the one case it is wanted.
        """
        from app import _LOCK_BYTE_OFFSET, acquire_single_instance

        lock = acquire_single_instance(self.dir)
        self.assertIsNotNone(lock)
        try:
            self.assertEqual((self.dir / "app.lock").read_text().strip(),
                             str(os.getpid()))
            self.assertGreater(_LOCK_BYTE_OFFSET, 8,
                               "the lock byte must sit past the pid text")
        finally:
            lock.close()

    def test_raising_a_missing_window_reports_failure_rather_than_lying(self):
        """If the existing window cannot be brought forward, the caller must
        fall back to telling the user — otherwise a double-click does nothing
        at all, which is worse than the dialog it replaced."""
        from app import _raise_existing_window

        (self.dir / "app.lock").write_text("999999")  # a pid owning no window
        self.assertFalse(_raise_existing_window(self.dir))
        (self.dir / "app.lock").write_text("not-a-pid")
        self.assertFalse(_raise_existing_window(self.dir))

    def test_different_session_dirs_do_not_block_each_other(self):
        """A test (or a deliberately isolated copy) must not be locked out by
        the real app's own lock."""
        from app import acquire_single_instance

        real = acquire_single_instance(Path(tempfile.mkdtemp(prefix="ig_lock_other_")))
        self.assertIsNotNone(real)
        real.close()

    def test_main_still_opens_the_app_when_the_lock_cannot_be_created(self):
        """A safety mechanism must never be the reason the app fails to open.

        Regression (playtest): measured two ways the lock could not be created
        — the lock path occupied by a directory, and the session path occupied
        by a file. Both raised straight out of main(), so the app would refuse
        to launch with no explanation. It now starts unlocked instead.
        """
        import app as app_module

        base = Path(tempfile.mkdtemp(prefix="ig_lock_broken_"))
        (base / "app.lock").mkdir()  # lock path occupied by a directory
        as_file = base / "dir_is_file"
        as_file.write_text("not a directory")

        built = []
        original_app = app_module.App

        class FakeApp:
            def __init__(self, *a, **k):
                built.append(1)

            def mainloop(self):
                pass

        app_module.App = FakeApp
        shown = []
        original_mb = app_module.messagebox
        app_module.messagebox = type(
            "MB", (), {"showwarning": staticmethod(lambda *a, **k: shown.append(a[1]))}
        )
        try:
            for label, bad_dir in (("lock path is a directory", base),
                                   ("session path is a file", as_file)):
                built.clear()
                shown.clear()
                original_lock = app_module.acquire_single_instance
                app_module.acquire_single_instance = (
                    lambda d, _bad=bad_dir: original_lock(_bad))
                try:
                    app_module.main()
                except Exception as exc:  # noqa: BLE001 - that is the point
                    self.fail(f"main() raised with {label}: {type(exc).__name__}: {exc}")
                finally:
                    app_module.acquire_single_instance = original_lock
                self.assertEqual(len(built), 1, f"app did not open with {label}")
                self.assertEqual(shown, [], f"user was wrongly told it was already open ({label})")
        finally:
            app_module.App = original_app
            app_module.messagebox = original_mb


class MidRunSignInTests(AppTestCase):
    """Signing in during a run must be refused, not silently honoured.

    Regression (playtest): Log out was already refused mid-run, but Sign In
    was not. The running workers hold the old session, so a mid-run sign-in
    left one run's requests split across two Instagram accounts' budgets —
    quietly breaking the one promise this app makes, with nothing on screen
    to say so.
    """

    def _run_and_interfere(self):
        app = self.app
        app.txt_input.insert("1.0", "\n".join(f"g{i}" for i in range(30)))
        app.var_user.set("someone_else")
        app.var_pass.set("pw")
        app.update()
        app.btn_start.invoke()
        self._pump(15, until=lambda: app.worker is not None and app.worker.is_alive())
        self.assertTrue(app.worker and app.worker.is_alive(),
                        "the run finished before we could interfere")

    def _drain(self):
        app = self.app
        app.btn_stop.invoke()
        self._pump(15, until=lambda: app.worker is not None and not app.worker.is_alive())
        self._settle()

    def test_signing_in_mid_run_is_refused_and_the_account_is_unchanged(self):
        app = self.app
        self._run_and_interfere()
        before = app.checker.username
        try:
            app.on_signin()
            self._pump(1.0)
            self.assertEqual(app.checker.username, before,
                             "the account was swapped while a run was in progress")
            self.assertEqual(FakeClient.logins_attempted, 0,
                             "a login reached Instagram during a run")
            self.assertTrue([m for k, m in FakeMessageBox.seen if k == "warning"],
                            "no explanation was shown")
        finally:
            self._drain()

    def test_signing_in_again_once_the_run_is_over_still_works(self):
        """The guard must not lock the user out of switching accounts."""
        app = self.app
        self._run_and_interfere()
        self._drain()
        FakeMessageBox.seen.clear()
        app.on_signin()
        landed = self._pump(8.0, until=lambda: FakeClient.logins_attempted >= 1)
        self.assertTrue(landed, "signing in between runs was blocked")
        self.assertEqual(FakeClient.logins_attempted, 1)
        # Let the sign-in thread finish and the queue drain before the App is
        # destroyed, or the Tk root gets collected off-thread and prints
        # "Tcl interpreter is leaked".
        self._pump(0.5)


class LogPanelVisibleTests(AppTestCase):
    """The Log panel must be on screen, not merely constructed.

    Regression (playtest): it was packed after an expand=True main area and
    was allocated no space at all — measured unmapped and 1px tall at every
    window height up to 900px, including the app's own default of 720px. Every
    background message (batch cool-downs, scrape progress, "Copied DP URL",
    rate-limit notes, drain-loop tracebacks) is written there and nowhere
    else, so on an ordinary laptop the app told the user nothing at all.
    """

    def test_log_is_shown_at_the_default_window_size(self):
        app = self.app
        # A withdrawn window cannot express layout, so map it for this check.
        app.deiconify()
        app.geometry("1100x720")  # the app's own default
        self._pump(0.5)
        try:
            self.assertTrue(app.txt_log.master.winfo_ismapped(),
                            "the Log panel is not mapped")
            self.assertGreater(app.txt_log.winfo_height(), 20,
                               f"Log collapsed to {app.txt_log.winfo_height()}px")
        finally:
            app.withdraw()

    def test_log_is_reserved_from_the_bottom(self):
        """Structural guard: the log is packed to the bottom *before* the
        expanding area, which is what keeps it from being squeezed out."""
        side = self.app.txt_log.master.pack_info().get("side")
        self.assertEqual(side, "bottom",
                         f"log packed with side={side!r}; it must be reserved first")


class TableReachableTests(AppTestCase):
    """Every column must be reachable, not just the ones that happen to fit.

    Regression (playtest): the eleven result columns need ~1155px. The window
    defaults to 1120px and leaves the table 728px, so Details — the column
    carrying every error message the app produces — was clipped with no scroll
    bar and no way to see it, on the app's own default layout and on any 1366px
    laptop.
    """

    def _scrollbars(self):
        found = []
        for widget in self.app.winfo_children():
            stack = [widget]
            while stack:
                w = stack.pop()
                stack.extend(w.winfo_children())
                if isinstance(w, ttk.Scrollbar):
                    found.append(str(w.cget("orient")))
        return found

    def test_the_table_can_be_scrolled_sideways(self):
        self.assertTrue(self.app.tree.cget("xscrollcommand"),
                        "the results table has no horizontal scroll command")
        self.assertIn("horizontal", self._scrollbars(),
                      "no horizontal scrollbar: overflow columns are unreachable")

    def test_scrolling_reaches_the_last_column(self):
        self.app.tree.insert("", "end", values=(
            "someuser", "Yes", "fine", "Yes", "", "3", "10", "5", "No", "ok",
            "a long error message"))
        self.app.tree.xview_moveto(1.0)
        _, right_edge = self.app.tree.xview()
        self.assertAlmostEqual(right_edge, 1.0, places=5,
                               msg="cannot scroll to the right-hand columns")
        self.app.tree.xview_moveto(0.0)
        left_edge, _ = self.app.tree.xview()
        self.assertAlmostEqual(left_edge, 0.0, places=5)


class ScrapeBoxTests(AppTestCase):
    """A scrape writes into the same box the user types into.

    Regression (playtest): a 12-name list the user had pasted in was silently
    replaced by the scraped names, and because that box is mirrored to
    pending_names.txt the old list was overwritten on disk too — no undo, and
    a restart would not bring it back. The user must be asked *before* any
    request goes out, when cancelling is still free.
    """

    MY_LIST = "\n".join(f"my_own_name{i}" for i in range(12))

    def _set_box(self, text):
        self.app.txt_input.delete("1.0", "end")
        self.app.txt_input.insert("1.0", text)
        self.app.update()

    def _box(self):
        return self.app.txt_input.get("1.0", "end").strip()

    def _scrape(self, target="somecelebrity", timeout=30):
        self.app.var_target.set(target)
        self.app.var_names_only.set(True)  # no auto-audit: isolates the box
        # Ignore the 60s anti-hammering window between two scrape attempts;
        # these tests are not about that, and without this the second attempt
        # is refused before it ever reaches the code under test.
        self.app._last_scrape_attempt = 0.0
        self.app.btn_scrape.invoke()
        self._pump(1.0, until=lambda: self.app.worker is not None)
        if self.app.worker is None:
            return self._box()  # cancelled: no worker was ever started
        finished = self._pump(timeout, until=lambda: self.app.worker is not None
                              and not self.app.worker.is_alive())
        # Fail loudly instead of asserting on whatever the box happens to hold:
        # a worker still alive here means the scrape never finished, and the
        # old code returned a half-scraped (usually empty) box as if it had.
        self.assertTrue(finished,
                        f"the scrape worker was still alive after {timeout}s "
                        f"(pages served: {FakeClient.pages_served})")
        self._settle()
        return self._box()

    def test_cancelling_a_scrape_keeps_the_users_list_and_sends_nothing(self):
        self._set_box(self.MY_LIST)
        FakeMessageBox.answers[:] = [False]
        self._scrape()
        self.assertEqual(self._box(), self.MY_LIST)
        self.assertEqual(FakeClient.pages_served, 0, "a cancelled scrape sent requests")
        self.assertEqual(str(self.app.btn_scrape["state"]), "normal")

    def test_a_cancelled_scrape_does_not_start_the_pacing_window(self):
        """Regression: arming the 60s window before the confirm meant that
        reading the dialog and choosing No still locked the user out of their
        next attempt — a punishment for a click that sent nothing."""
        self._set_box(self.MY_LIST)
        FakeMessageBox.answers[:] = [False]
        self.app.var_target.set("somecelebrity")
        self.app.btn_scrape.invoke()
        self._pump(0.5)
        self.assertEqual(self.app._last_scrape_attempt, 0.0,
                         "cancelling still armed the 60s scrape pacing window")

    def test_agreeing_replaces_the_list_with_the_scraped_names(self):
        self._set_box(self.MY_LIST)
        FakeMessageBox.answers[:] = [True]
        box = self._scrape()
        self.assertTrue(box.startswith("follower0_0"), box[:60])
        self.assertNotIn("my_own_name", box)
        # A real attempt must still arm the anti-hammering window; only the
        # cancelled path is exempt.
        self.assertGreater(self.app._last_scrape_attempt, 0.0,
                           "a real scrape did not start the pacing window")

    def test_lightning_speed_removes_interpage_pause(self):
        import followers as followers_mod

        pauses = []
        original_wait = followers_mod._stop_wait
        followers_mod._stop_wait = lambda seconds, stop_event: (
            pauses.append(seconds) or stop_event.is_set()
        )
        self.addCleanup(setattr, followers_mod, "_stop_wait", original_wait)
        self.app.var_speed.set("lightning")

        self._scrape()

        self.assertTrue(pauses, "the scrape did not paginate past its first page")
        self.assertEqual(pauses, [0.0, 0.0])

    def test_scrape_target_then_auto_check_every_follower(self):
        target = "random_public_target_8472"
        expected_followers = [f"follower{page}_{index}"
                              for page in range(2) for index in range(3)]
        self.app.var_target.set(target)
        self.app.var_names_only.set(False)
        self.app.var_speed.set("lightning")
        self._set_box("")
        self.app._last_scrape_attempt = 0.0

        self.app.btn_scrape.invoke()
        completed = self._pump(
            15,
            until=lambda: self.app.lbl_progress["text"].startswith("Done — 6 checked OK")
            and self.app.worker is not None
            and not self.app.worker.is_alive(),
        )
        self._settle()

        self.assertTrue(completed, f"workflow did not finish: {self.app.lbl_progress['text']}")
        self.assertEqual(self._box().splitlines(), expected_followers)
        self.assertEqual({result.username for result in self.app.results},
                         set(expected_followers))
        self.assertTrue(all(result.status == "ok" for result in self.app.results))
        self.assertIn("6 checked OK", self.app.lbl_progress["text"])

    def test_rescraping_the_same_list_does_not_ask_again(self):
        """The box then holds the last scrape's own output. Nagging there is
        how a protective dialog becomes something the user clicks past."""
        self._set_box(self.MY_LIST)
        FakeMessageBox.answers[:] = [True]
        self._scrape()
        first_pages = FakeClient.pages_served
        self.assertGreater(first_pages, 0, "the first scrape never ran")

        FakeMessageBox.seen.clear()
        FakeMessageBox.answers[:] = [True]  # if it did ask, this would replace
        box = self._scrape()
        # Prove the second scrape actually ran, or "did not ask" is vacuous.
        self.assertGreater(FakeClient.pages_served, first_pages,
                           "the second scrape never ran; the assertion below is vacuous")
        self.assertFalse([m for kind, m in FakeMessageBox.seen if kind == "askyesno"],
                         "re-scraping the same list asked about replacing it")
        self.assertTrue(box.startswith("follower0_0"), box[:60])

    def test_an_empty_box_is_never_asked_about(self):
        self._set_box("")
        FakeMessageBox.answers[:] = [False]  # would cancel if asked
        box = self._scrape()
        prompts = [m for k, m in FakeMessageBox.seen if k == "askyesno"]
        self.assertTrue(box.startswith("follower0_0"),
                        f"box={box[:40]!r} prompts={prompts} pages={FakeClient.pages_served}")

    def _saved_progress(self):
        """Save a partial scrape for the list type the app is actually set to."""
        list_type = self.app.var_list_type.get().lower()
        self.app._save_scrape_progress(
            "somecelebrity", list_type, "CURSOR2", ["n1", "n2", "n3"]
        )
        return self.app._scrape_progress_path("somecelebrity", list_type)

    def test_cancelling_at_the_replace_prompt_keeps_the_saved_resume_cursor(self):
        """Regression (playtest): the resume prompt's "No, start over" deleted
        the saved cursor on the spot — but the *next* prompt (replace the box?)
        can still be cancelled. A cancelled click sends no request, so it must
        not discard the record of the pages already paid for: the next attempt
        would re-fetch them all, which is exactly the extra block risk every
        warning in this app is about."""
        self._set_box(self.MY_LIST)
        path = self._saved_progress()
        self.assertTrue(path.exists())
        FakeMessageBox.answers[:] = [False, False]  # No to resume, No to replace

        box = self._scrape()

        # Both prompts must have been reached, or the file assertion below is
        # vacuously true (the save is keyed by list type; get it wrong and the
        # resume prompt never appears).
        prompts = [m for k, m in FakeMessageBox.seen if k == "askyesno"]
        self.assertEqual(len(prompts), 2, prompts)
        self.assertIn("Resume the interrupted scrape", prompts[0])
        self.assertEqual(box, self.MY_LIST)
        self.assertEqual(FakeClient.pages_served, 0, "a cancelled scrape sent requests")
        self.assertTrue(path.exists(),
                        "cancelling at the second prompt discarded the resume cursor")
        saved = self.app._load_scrape_progress(
            "somecelebrity", self.app.var_list_type.get().lower()
        )
        self.assertEqual(saved.get("names"), ["n1", "n2", "n3"])

    def test_starting_over_and_scraping_replaces_the_saved_cursor(self):
        """The cursor must still be gone once a start-over scrape really runs:
        keeping the protection is not the same as keeping stale state."""
        self._set_box(self.MY_LIST)
        path = self._saved_progress()
        FakeMessageBox.answers[:] = [False, True]  # No to resume, Yes to replace

        box = self._scrape()

        prompts = [m for k, m in FakeMessageBox.seen if k == "askyesno"]
        self.assertIn("Resume the interrupted scrape", prompts[0], prompts)
        self.assertTrue(box.startswith("follower0_0"), box[:60])
        self.assertNotIn("n1", box, "the discarded attempt's names came back")
        self.assertFalse(path.exists(), "the finished scrape left the old resume marker")


class LogoutExportTests(AppTestCase):
    """Logging out must not take the results table's export with it.

    Regression (playtest): Log out disabled Export even though the rows were
    still on screen and in memory. Clicking Export then did nothing at all —
    no dialog, no message — so the only ways out were signing in again (and
    running another check) or restarting the app.
    """

    def test_results_can_still_be_exported_after_logging_out(self):
        app = self.app
        self._run("alice\nbob")
        self.assertEqual(str(app.btn_export["state"]), "normal")
        app._after_signin(True, None)  # the state a real sign-in leaves behind

        FakeMessageBox.answers[:] = [True]  # confirm the log out
        app.btn_logout.invoke()
        self._pump(5, until=lambda: app.checker.client is None)
        self._settle()
        self.assertFalse(app.checker.signed_in)
        self.assertEqual(len(app.results), 2, "logging out dropped the results")
        self.assertEqual(str(app.btn_export["state"]), "normal",
                         "Export was greyed out while the audit was still on screen")

        # Not just enabled: it must actually write the file.
        target = self.app.checker.session_dir / "audit.csv"
        FakeDialog.save_path = str(target)
        FakeMessageBox.seen.clear()
        app.btn_export.invoke()  # synchronous: writes the file before returning
        self.assertTrue(target.exists(), "Export wrote nothing after a log out")
        body = target.read_text(encoding="utf-8-sig")
        self.assertIn("alice", body)
        self.assertIn("bob", body)


class PublicModeRefusalTests(AppTestCase):
    """The refusal dialog must offer the way out its own text describes.

    Regression (playtest): after "Public mode cannot work right now" the app
    stayed on "Public (safest & fast)" with an OK-only dialog, so the user's
    next click (Start) reproduced the same dialog — and the only way forward,
    switching to Session mode, had to be done by hand in a control the dialog
    never pointed at.
    """

    def _refuse(self):
        error = checker_mod.CheckerError(checker_mod.PUBLIC_UNAVAILABLE_MESSAGE,
                                         "public_unavailable")
        self.app.ui_queue.put(("run_failed", error))
        self._pump(2.0)
        return [m for k, m in FakeMessageBox.seen if k == "askyesno"]

    def test_the_refusal_offers_to_switch_to_session_mode(self):
        self.app.var_mode.set("Public (safest & fast)")
        FakeMessageBox.answers[:] = [True]
        asked = self._refuse()
        self.assertTrue(asked, "the refusal was still an OK-only notice")
        self.assertIn("Public mode cannot work right now", asked[0])
        self.assertEqual(self.app.var_mode.get(), "Session (safest data)",
                         "the dialog offered the switch but did not make it")

    def test_declining_the_switch_leaves_the_mode_alone(self):
        """The user must be able to say no and keep the explanation."""
        self.app.var_mode.set("Public (safest & fast)")
        FakeMessageBox.answers[:] = [False]
        asked = self._refuse()
        self.assertTrue(asked)
        self.assertEqual(self.app.var_mode.get(), "Public (safest & fast)")
        self.assertIn("Public mode can't run right now",
                      str(self.app.lbl_progress["text"]), "no result state was left behind")

    def test_private_profile_in_public_mode_offers_session_switch(self):
        """Only private accounts should trigger the sign-in prompt; public accounts stay in public mode."""
        self.app.var_mode.set("Public (safest & fast)")
        self.app.checker.client = None
        self.app._private_prompt_done = False
        FakeMessageBox.answers[:] = [True]

        self.app._maybe_offer_private_session_mode(
            checker_mod.ProfileResult(
                username="private_user",
                status="ok",
                alive=True,
                is_private=True,
                dp_present=None,
                posts=10,
                followers=5,
                following=20,
            )
        )

        self.assertEqual(self.app.var_mode.get(), "Session (safest data)")
        self.assertTrue(self.app._private_prompt_done)

    def test_declining_private_profile_prompt_keeps_public_mode(self):
        self.app.var_mode.set("Public (safest & fast)")
        self.app.checker.client = None
        self.app._private_prompt_done = False
        FakeMessageBox.answers[:] = [False]

        self.app._maybe_offer_private_session_mode(
            checker_mod.ProfileResult(
                username="private_user",
                status="ok",
                alive=True,
                is_private=True,
                dp_present=None,
                posts=10,
                followers=5,
                following=20,
            )
        )

        self.assertEqual(self.app.var_mode.get(), "Public (safest & fast)")
        self.assertTrue(self.app._private_prompt_done)
