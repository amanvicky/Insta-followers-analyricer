"""Instagram Profile Checker — desktop app (Windows).

Sign in to an Instagram account, paste a list of usernames, and check each one:
DP present, posts, followers, following — then export everything to CSV.
"""

from __future__ import annotations

import json
import msvcrt
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from checker import DEFAULT_SESSION_DIR


class DeterministicGate:
    """Gate that makes in-flight count deterministic for Stop's bookkeeping.

    check_many's workers check stop_event BEFORE each request, so at gate-open
    time each worker's next action is deterministic: it will issue exactly one
    request (no worker can observe the stop and skip instead). Each result
    releases its slot. A subsequent stop_event.set() cannot reduce in-flight —
    workers already checked the gate while it was open; results still close
    slots. Therefore waiting for in_flight==0 bounds Stop's latency to the
    requests launched before the stop, with no post-wait re-check that could
    raise 'Stopped by user' after waiting.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._in_flight = 0

    def __enter__(self) -> "DeterministicGate":
        with self._cond:
            self._in_flight += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with self._cond:
            self._in_flight -= 1
            if self._in_flight <= 0:
                self._cond.notify_all()

    def wait_all(self, timeout: float | None = 10.0) -> bool:
        """Wait for all gated requests to land. True when drained."""
        with self._cond:
            if self._in_flight <= 0:
                return True
            return self._cond.wait_for(lambda: self._in_flight <= 0, timeout)

    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

from checker import (
    BATCH_SIZE,
    CheckerError,
    SPEED_PRESETS,
    InstagramChecker,
    ProfileResult,
    normalize_username,
    parse_username_list,
    verdict_for,
    write_csv_file,
)
from followers import PAGE_PAUSE_SECONDS, SCRAPE_PAGE_PAUSES, ScrapeResume, scrape_user_list
from public_lookup import PublicLookup, PublicLookupError

APP_TITLE = "Instagram Profile Checker"

# Hard pacing between Scrape clicks: Instagram counts rapid successive
# attempts against the account. Normal quiet window 60s; after a
# feedback_required block, 10 minutes — hammering the button extends blocks.
SCRAPE_RETRY_PACING = 60.0
SCRAPE_RETRY_PACING_BLOCKED = 600.0
STATUS_COLORS = {"ok": "#1a7f37", "not_found": "#c62828", "invalid": "#b45309", "error": "#c62828", "pending": "#555555"}
VERDICT_COLORS = {
    "likely-fake": "#c62828",
    "suspicious": "#b45309",
    "inactive": "#8a6d3b",
    "dead": "#7b1fa2",
    "fine": "#1a7f37",
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1120x720")
        self.minsize(960, 600)
        try:
            self.call("tk", "scaling", 1.25)
        except tk.TclError:
            pass

        self.checker = InstagramChecker(on_log=self.log)
        self.results: list[ProfileResult] = []
        # username -> position in self.results. _update_result used to scan
        # the whole list per arriving row (and rebuild it on a hit), which is
        # O(n²) on the UI thread — ~12.5M comparisons across the 5,000-name
        # audits this app is built for.
        self._result_index: dict[str, int] = {}
        self.row_by_username: dict[str, str] = {}
        # Tally of the CURRENT run only. The table keeps every previous run's
        # rows on purpose, so a summary counted from self.results described the
        # whole session while reading as a report on the run that just ended.
        self._run_counts: dict[str, int] = {"ok": 0, "missing": 0, "error": 0}
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()
        self._checkpoint_dirty = False
        self._last_checkpoint_ts = 0.0
        self._autosave_announced = False
        self._private_prompt_done = False
        self._run_gate = DeterministicGate()
        self._run_started_at = 0.0
        # Restart-proof username box (pending_names.txt) — throttle state must
        # exist before _build_ui, which binds the <<Modified>> autosave hook.
        self._names_dirty = False
        self._names_saved_at = 0.0
        # feedback_required block seen on this account: warn before re-scraping.
        self._feedback_block_since = 0.0
        self._feedback_block_until, self._feedback_block_since = self._load_feedback_block()
        # Pacing: Instagram counts rapid successive scrape attempts, so enforce
        # a hard quiet window between Scrape clicks (blocked → longer wait).
        self._last_scrape_attempt = 0.0
        # Exactly what the last successful scrape put in the usernames box, so
        # a repeat scrape of the same list is not asked to overwrite its own
        # output.
        self._last_scraped_text = ""
        # True when the current run ended because the user pressed Stop — as
        # opposed to the rate-limit circuit breaker halting it.
        self._user_stopped = False
        # Set when a run halts on a rate limit; on_start asks before restarting
        # inside this window.
        self._start_paused_until = 0.0

        self._build_style()
        self._build_ui()
        self._load_checkpoint()
        self._pump_after: str | None = None
        self._restore_after: str | None = None
        self._schedule_pump()
        self._restore_after = self.after(200, self._restore_session)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _restore_session(self) -> None:
        """Try the cached session in the background; UI stays fully usable."""
        def work():
            username = self.checker.try_restore_session()
            if username:
                self.ui_queue.put(("signed_in", None))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ style

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=6)
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))

    # --------------------------------------------------------------- widgets

    def _build_ui(self) -> None:
        # --- Sign-in panel
        auth = ttk.LabelFrame(self, text="1. Sign in to Instagram", padding=10)
        auth.pack(fill="x", padx=12, pady=(12, 6))

        ttk.Label(auth, text="Username:").grid(row=0, column=0, sticky="w")
        self.var_user = tk.StringVar()
        entry_user = ttk.Entry(auth, textvariable=self.var_user, width=24)
        entry_user.grid(row=0, column=1, padx=(4, 14), sticky="w")
        self.entry_user = entry_user  # focus target after an automatic mode switch

        ttk.Label(auth, text="Password:").grid(row=0, column=2, sticky="w")
        self.var_pass = tk.StringVar()
        entry_pass = ttk.Entry(auth, textvariable=self.var_pass, width=24, show="•")
        entry_pass.grid(row=0, column=3, padx=(4, 14), sticky="w")

        ttk.Label(auth, text="2FA code (only if asked):").grid(row=0, column=4, sticky="w")
        self.var_code = tk.StringVar()
        entry_code = ttk.Entry(auth, textvariable=self.var_code, width=12)
        entry_code.grid(row=0, column=5, padx=(4, 14), sticky="w")
        self.entry_code = entry_code  # focus target when a challenge is required

        self.btn_signin = ttk.Button(auth, text="Sign In", command=self.on_signin)
        self.btn_signin.grid(row=0, column=6, sticky="w")
        self.btn_logout = ttk.Button(auth, text="Log out", command=self.on_logout, state="disabled")
        self.btn_logout.grid(row=0, column=7, padx=(12, 0))
        self.lbl_signed_in = ttk.Label(auth, text="Not signed in", foreground="#c62828")
        self.lbl_signed_in.grid(row=0, column=8, padx=(12, 0))

        ttk.Separator(auth, orient="horizontal").grid(row=1, column=0, columnspan=8, sticky="ew", pady=8)
        ttk.Label(auth, text="Alternative — Session ID:").grid(row=2, column=0, sticky="w")
        self.var_sessionid = tk.StringVar()
        entry_sid = ttk.Entry(auth, textvariable=self.var_sessionid, width=58, show="•")
        entry_sid.grid(row=2, column=1, columnspan=3, padx=(4, 14), sticky="w")
        self.btn_signin_sid = ttk.Button(auth, text="Sign In with Session ID", command=self.on_signin_sessionid)
        self.btn_signin_sid.grid(row=2, column=4, columnspan=2, sticky="w")
        ttk.Button(auth, text="How?", command=self.on_sessionid_help).grid(row=2, column=6, sticky="w")

        note = ttk.Label(
            auth,
            foreground="#666666",
            text="Your credentials stay on this PC (session cached locally for re-use). Instagram may email you a login alert — that is normal. "
                 "If password sign-in fails with 'version of Instagram is out of date', use the Session ID method below.",
            wraplength=1040,
        )
        note.grid(row=3, column=0, columnspan=8, sticky="w", pady=(8, 0))

        # --- Log area
        # Packed BEFORE the expanding main area, and to the bottom, so its
        # height is reserved rather than left over. Packed after an
        # expand=True widget it was never allocated space at all: measured,
        # the log was unmapped (1px, invisible) at every window height up to
        # 900px, including the app's own default. Every background message —
        # batch cool-downs, scrape progress, "Copied DP URL", rate-limit
        # notes, drain-loop tracebacks — goes here and nowhere else.
        logf = ttk.LabelFrame(self, text="Log", padding=6)
        logf.pack(side="bottom", fill="x", padx=12, pady=(6, 12))
        self.txt_log = tk.Text(logf, height=6, font=("Consolas", 9), state="disabled", background="#111111", foreground="#c9d1d9")
        self.txt_log.pack(fill="x")

        # --- Main area: left = input, right = results
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=12, pady=6)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # The build-list panel needs ~700px of height (a 12-row text box plus
        # ten controls). The window offers it far less, and pack allocated the
        # shortfall by silently dropping the overflow: measured, at 1120x720 —
        # the app's own default — Start Checking, Stop, Export and Clear were
        # not on screen at all, and all four only appeared from 900px up. The
        # primary action was unreachable on an ordinary laptop. A scroll
        # container makes the declared 960x600 minimum truthful instead of
        # quietly hiding controls below the fold.
        left_wrap = ttk.Frame(main)
        left_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left_wrap.rowconfigure(0, weight=1)
        left_wrap.columnconfigure(0, weight=1)
        panel_bg = (ttk.Style().lookup("TFrame", "background")
                    or ttk.Style().lookup(".", "background") or "white")
        self.left_canvas = tk.Canvas(left_wrap, width=330, highlightthickness=0,
                                       borderwidth=0, background=panel_bg)
        left_scroll = ttk.Scrollbar(left_wrap, orient="vertical", command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=left_scroll.set)
        self.left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scroll.grid(row=0, column=1, sticky="ns")

        left = ttk.LabelFrame(self.left_canvas, text="2. Build the list", padding=10)
        left_window = self.left_canvas.create_window((0, 0), window=left, anchor="nw")
        # Keep the panel as wide as the viewport, and re-derive the scroll
        # range whenever its contents change size.
        self.left_canvas.bind("<Configure>",
                              lambda e: self.left_canvas.itemconfigure(left_window, width=e.width))
        left.bind("<Configure>",
                  lambda _e: self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all")))

        def _scroll_build_list(event: tk.Event) -> None:
            """Wheel over the build-list panel only — the results table owns
            its own scrolling, and the log is not scrollable."""
            if self.left_canvas.bbox("all") and self.left_canvas.winfo_height() < left.winfo_reqheight():
                self.left_canvas.yview_scroll(-(event.delta // 120) or -1, "units")

        self.left_canvas.bind("<MouseWheel>", _scroll_build_list)

        # --- Extract from target (top of panel, workflow order: extract first)
        faf = ttk.LabelFrame(left, text="Extract from target", padding=6)
        faf.pack(fill="x", pady=(0, 8))
        rowf = ttk.Frame(faf)
        rowf.pack(fill="x")
        ttk.Label(rowf, text="Target:").pack(side="left")
        self.var_target = tk.StringVar()
        ttk.Entry(rowf, textvariable=self.var_target, width=14).pack(side="left", padx=(4, 8))
        ttk.Label(rowf, text="List:").pack(side="left")
        self.var_list_type = tk.StringVar(value="Following")
        ttk.Combobox(
            rowf, textvariable=self.var_list_type, state="readonly",
            values=["Following", "Followers"], width=10,
        ).pack(side="left", padx=(4, 0))
        self.btn_scrape = ttk.Button(faf, text="Scrape →", command=self.on_scrape)
        self.btn_scrape.pack(fill="x", pady=(4, 0))
        self.var_names_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            faf, variable=self.var_names_only,
            text="Extract usernames only (don't auto-check)",
        ).pack(anchor="w")

        ttk.Label(left, text="Usernames (one per line, or comma-separated):").pack(anchor="w")
        self.txt_input = tk.Text(left, width=34, height=12, font=("Consolas", 10), undo=True)
        self.txt_input.pack(fill="both", expand=True, pady=(4, 8))
        # Restart-proof the box: every modification (typed, pasted, programmatic)
        # marks it dirty; saves are throttled to ~2s and flushed on close.
        self.txt_input.bind("<<Modified>>", self._on_input_modified, add="+")
        self._load_pending_names()

        ttk.Button(left, text="Load list from .txt…", command=self.on_load_file).pack(fill="x")
        self.btn_start = ttk.Button(left, text="Start Checking", command=self.on_start)
        self.btn_start.pack(fill="x", pady=(8, 4))
        self.btn_stop = ttk.Button(left, text="Stop", command=self.on_stop, state="disabled")
        self.btn_stop.pack(fill="x")
        self.btn_export = ttk.Button(left, text="Export results to CSV…", command=self.on_export, state="disabled")
        self.btn_export.pack(fill="x", pady=(8, 0))
        self.btn_clear = ttk.Button(left, text="Clear results", command=self.on_clear_results)
        self.btn_clear.pack(fill="x", pady=(4, 0))

        modef = ttk.Frame(left)
        modef.pack(fill="x", pady=(10, 0))
        ttk.Label(modef, text="Mode:").pack(side="left")
        self.var_mode = tk.StringVar(value="Session (safest data)")
        mode_box = ttk.Combobox(
            modef, textvariable=self.var_mode, state="readonly", width=24,
            values=["Public (safest & fast)", "Session (safest data)"],
        )
        mode_box.pack(side="left", padx=(4, 0))

        speedf = ttk.Frame(left)
        speedf.pack(fill="x")
        ttk.Label(speedf, text="Speed:").pack(side="left")
        self.var_speed = tk.StringVar(value="fast")
        speed_box = ttk.Combobox(
            speedf, textvariable=self.var_speed, state="readonly",
            values=list(SPEED_PRESETS.keys()), width=11,
        )
        speed_box.pack(side="left", padx=(4, 0))
        ttk.Label(
            left, foreground="#666666", wraplength=300,
            text="Public mode needs no sign-in and can't get your account blocked; "
                 "private profiles show limited data. Session mode gives full data "
                 "but spends your account's rate budget — turbo spends it fastest.",
        ).pack(anchor="w", pady=(4, 0))

        self.var_activity = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            left, variable=self.var_activity,
            text="Include Last Post date (session mode only)",
        ).pack(anchor="w")
        self.var_skip_existing = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            left, variable=self.var_skip_existing,
            text="Skip rows already checked OK",
        ).pack(anchor="w")

        right = ttk.LabelFrame(main, text="3. Results", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        columns = ("username", "alive", "verdict", "dp", "last_post", "posts", "followers", "following", "private", "status", "error")
        self.tree = ttk.Treeview(right, columns=columns, show="headings", selectmode="browse")
        headings = {
            "username": ("Username", 150),
            "alive": ("Alive?", 60),
            "verdict": ("Verdict", 110),
            "dp": ("DP?", 55),
            "last_post": ("Last Post", 95),
            "posts": ("Posts", 75),
            "followers": ("Followers", 95),
            "following": ("Following", 95),
            "private": ("Private", 70),
            "status": ("Status", 90),
            "error": ("Details", 260),
        }
        for col, (text, width) in headings.items():
            anchor = "e" if col in ("posts", "followers", "following") else "w"
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "error"))
        ysb = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        # The eleven columns need ~1155px, which no ordinary laptop window
        # offers (measured: 728px of table at the app's own default size,
        # 974px at 1366x768). Without this the right-hand columns — including
        # Details, which carries every error message the app produces — were
        # clipped with no way to scroll to them, on the app's default layout.
        xsb = ttk.Scrollbar(right, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=xsb.set)
        xsb.grid(row=1, column=0, sticky="ew")
        for tag, color in STATUS_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        for tag, color in VERDICT_COLORS.items():
            self.tree.tag_configure(f"v:{tag}", foreground=color)
        self.tree.bind("<Double-1>", self.on_copy_dp_url)

        self.lbl_progress = ttk.Label(right, text="Idle.")
        # Progress bar sits under the results table; label under the bar.
        self.progress_bar = ttk.Progressbar(right, mode="determinate", maximum=100)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.progress_bar.grid_remove()  # shown during runs
        self.lbl_progress.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # ------------------------------------------------------------- handlers

    def log(self, message: str) -> None:
        """Queue log lines so worker threads never touch Tk widgets directly."""
        self.ui_queue.put(("log", message))

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{stamp}] {message}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def on_logout(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "A check or scrape is running — wait for it or press Stop first.")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"Log out of @{self.checker.username}?\n\n"
            "This signs out on Instagram's side and deletes the cached session on this PC.",
        ):
            return

        def work():
            try:
                self.checker.logout()
                self.ui_queue.put(("logged_out", None))
            except Exception as exc:
                self.ui_queue.put(("logout_failed", CheckerError(str(exc), "error")))

        self.btn_logout.configure(state="disabled")
        threading.Thread(target=work, daemon=True).start()

    def on_signin(self) -> None:
        # Refused for the same reason as Log out, and this one matters more:
        # the running workers already hold the old session, so signing in
        # again would leave one run's requests split across two Instagram
        # accounts' budgets — quietly breaking the one promise this app makes.
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning(
                APP_TITLE,
                "A check or scrape is already running.\n\n"
                "Signing in now would switch accounts partway through it: the "
                "workers in flight keep using the old session, so the run would "
                "spend the new account's budget without saying so.\n\n"
                "Wait for it to finish, or press Stop first.",
            )
            return
        username = self.var_user.get().strip()
        password = self.var_pass.get()
        code = self.var_code.get().strip()  # read on the UI thread; Tk vars aren't thread-safe

        def work():
            try:
                self.checker.login(username, password, code)
                self.ui_queue.put(("signed_in", None))
            except CheckerError as exc:
                self.ui_queue.put(("signin_failed", exc))
            except Exception as exc:  # unexpected library errors
                self.ui_queue.put(("signin_failed", CheckerError(f"Sign-in failed: {exc}", "auth")))

        self.btn_signin.configure(state="disabled", text="Signing in…")
        self.log("Attempting sign-in…")
        threading.Thread(target=work, daemon=True).start()

    def on_signin_sessionid(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning(
                APP_TITLE,
                "A check or scrape is already running — signing in with a "
                "different session now would split this run's requests across "
                "two accounts. Wait for it to finish, or press Stop first.",
            )
            return
        sessionid = self.var_sessionid.get().strip()

        def work():
            try:
                self.checker.login_with_sessionid(sessionid)
                self.ui_queue.put(("signed_in", None))
            except CheckerError as exc:
                self.ui_queue.put(("signin_failed", exc))
            except Exception as exc:
                self.ui_queue.put(("signin_failed", CheckerError(f"Sign-in failed: {exc}", "auth")))

        self.btn_signin_sid.configure(state="disabled", text="Signing in…")
        self.log("Attempting Session ID sign-in…")
        threading.Thread(target=work, daemon=True).start()

    def on_sessionid_help(self) -> None:
        messagebox.showinfo(
            "How to get your Session ID",
            "1. On your PC, log in to instagram.com in Chrome/Edge/Firefox.\n"
            "2. Press F12 to open Developer Tools and go to the Application tab\n"
            "   (Firefox: Storage). Under Cookies, pick https://www.instagram.com.\n"
            "3. Find the cookie named sessionid and copy its Value.\n"
            "4. Paste it into the Session ID field and click 'Sign In with Session ID'.\n\n"
            "If sign-in later says the session 'has ended', grab a fresh one the same "
            "way — logging out anywhere kills the copied sessionid.\n\n"
            "Never share your Session ID with anyone — it grants access to your account.",
        )

    def on_load_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a username list",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            # utf-8-sig, not utf-8: a leading BOM would otherwise be inserted
            # into the text box where the user cannot see it, and then written
            # back out into their own lists by later copies of this text.
            text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not read file:\n{exc}")
            return
        self.txt_input.delete("1.0", "end")
        self.txt_input.insert("1.0", text)
        self.log(f"Loaded list from {path} ({len(parse_username_list(text))} usernames).")

    def _target_requires_session(self, username: str) -> bool:
        """Private targets need a session; public targets stay in public mode."""
        if self.checker.signed_in:
            return False
        try:
            profile = PublicLookup().fetch(normalize_username(username))
            return bool(profile.is_private)
        except (PublicLookupError, CheckerError):
            return False

    def on_start(self) -> None:
        usernames = parse_username_list(self.txt_input.get("1.0", "end"))
        if not usernames:
            messagebox.showwarning(APP_TITLE, "Enter at least one username to check.")
            return

        public_mode = self.var_mode.get().startswith("Public")
        if not public_mode and not self.checker.signed_in:
            if any(self._target_requires_session(name) for name in usernames):
                messagebox.showwarning(APP_TITLE, "Private profile(s) detected — sign in first or switch to Session mode.")
                return
            self.var_mode.set("Public (safest & fast)")
            self.log("Public profiles detected — continuing in Public mode without sign-in.")
            public_mode = True

        slow = self.var_speed.get()
        workers, delay = SPEED_PRESETS.get(slow, SPEED_PRESETS["fast"])
        if public_mode:
            workers += 1  # public endpoints carry no account risk; a bit more parallelism is fine
        with_activity = self.var_activity.get() and not public_mode
        skip_existing = self.var_skip_existing.get()

        # Reuse existing OK rows when asked
        if skip_existing:
            todo = []
            for name in usernames:
                row = self.row_by_username.get(name)
                if row and "ok" in self.tree.item(row, "tags"):
                    continue
                todo.append(name)
        else:
            todo = usernames

        if not todo:
            messagebox.showinfo(APP_TITLE, "Nothing to check — all rows are already OK.")
            return
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "A check is already running — wait for it or press Stop.")
            return

        # A throttled run must not be restarted immediately: Instagram needs a
        # quiet period, and hammering Start is what turns a rate limit into an
        # action block. Overridable, but never silently.
        pause_left = self._start_paused_until - time.time()
        if pause_left > 0:
            if not messagebox.askyesno(
                APP_TITLE,
                "Instagram was refusing lookups on this session (rate limit) so the "
                "last run was halted to protect the account.\n\n"
                f"Recommended: wait another ~{pause_left / 60:.0f} min before restarting — "
                "hammering Start can turn a rate limit into an action block.\n\n"
                "Rows already checked OK are kept either way.\n\n"
                "Start the run anyway?",
            ):
                return

        self.stop_event.clear()
        self._user_stopped = False
        self._private_prompt_done = False
        self.checker.halt_reason = ""  # last run's circuit breaker, if any
        self._run_gate = DeterministicGate()  # fresh accounting for this run
        self._run_counts = {"ok": 0, "missing": 0, "error": 0}
        self._run_started_at = time.time()
        if not self._autosave_announced:
            self._autosave_announced = True
            self.log(f"Auto-save on: results are written to {self._autosave_path()} as the run progresses.")
        self.btn_start.configure(state="disabled")
        # Scrape is disabled for the whole run too: it is refused mid-run anyway,
        # and leaving it clickable means a careless click only earns a modal
        # warning. Stop is the only thing that should look actionable here.
        self.btn_scrape.configure(state="disabled")  # type: ignore[union-attr]
        self.btn_stop.configure(state="normal")
        self.worker = threading.Thread(
            target=self._check_all,
            args=(todo, workers, delay, with_activity, public_mode),
            daemon=True,
        )
        self.worker.start()

    def on_stop(self) -> None:
        self._user_stopped = True
        self.stop_event.set()
        self.btn_stop.configure(state="disabled", text="Stopping…")
        self.log("Stop requested — the scrape/check ends between requests (a few seconds)…")
        self._poll_stop_wait()

    def _poll_stop_wait(self) -> None:
        """Live feedback while Stop drains in-flight requests."""
        if not (self.worker and self.worker.is_alive()):
            return
        if self._run_gate.in_flight() > 0:
            self.lbl_progress.configure(text="Stopping… waiting for in-flight request(s) to land.")
            self.after(150, self._poll_stop_wait)

    def on_scrape(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "A check is already running — wait for it or press Stop.")
            return
        target = self.var_target.get().strip().lstrip("@")
        if not target:
            messagebox.showwarning(APP_TITLE, "Enter a target username to scrape.")
            return
        try:
            target = normalize_username(target)
        except CheckerError as exc:
            messagebox.showwarning(APP_TITLE, exc.message)
            return

        if not self.checker.signed_in:
            if self._target_requires_session(target):
                messagebox.showwarning(APP_TITLE, "This target is private — sign in first to scrape the follower list.")
                return
            self.var_mode.set("Public (safest & fast)")
            messagebox.showinfo(APP_TITLE, "This target is public, so the app is staying in Public mode. Use Start Checking for public lookups; follower-list scraping still needs a signed-in session for full private data.")
            return

        # Hard pacing: Instagram counts rapid successive attempts against the
        # account, so block hammering the Scrape button (longer after a block).
        elapsed = time.time() - self._last_scrape_attempt
        block_active = self._feedback_block_until > time.time()
        pacing = SCRAPE_RETRY_PACING_BLOCKED if block_active else SCRAPE_RETRY_PACING
        if elapsed < pacing:
            wait = int(pacing - elapsed)
            self.lbl_progress.configure(
                text=f"Pacing: wait {wait}s before scraping again — rapid retries "
                     "extend Instagram's blocks."
            )
            messagebox.showwarning(
                APP_TITLE,
                f"You clicked Scrape {elapsed:.0f}s after the last attempt.\n\n"
                f"Instagram counts rapid retries against the account and extends "
                f"blocks. Wait {wait}s and try again.",
            )
            return

        # Advisory: Instagram action-blocked this account recently. Make the
        # user override rather than silently extending the block.
        remaining = self._feedback_block_until - time.time()
        if remaining > 0:
            waited = f" (~{(time.time() - self._feedback_block_since) / 3600:.1f}h since the block)"
            if not messagebox.askyesno(
                APP_TITLE,
                f"Instagram action-blocked this account's scraping{waited}.\n\n"
                f"Blocks usually last a few hours (up to ~48h). Scraping now will "
                f"most likely fail again and can extend the block.\n\n"
                f"Try the scrape anyway?",
            ):
                return  # user heeded the warning
            self._record_feedback_block(0)  # retrying: stop nagging on further clicks

        list_type = self.var_list_type.get().lower()  # "following" | "followers"
        page_pause_seconds = SCRAPE_PAGE_PAUSES.get(
            self.var_speed.get(), PAGE_PAUSE_SECONDS
        )

        # A previous attempt on this exact list ended early (Stop, rate limit,
        # or an Instagram block). Re-fetching those pages is pure waste and
        # extra block risk, so offer to continue from the saved cursor instead.
        resume = None
        saved = self._load_scrape_progress(target, list_type)
        if saved and saved.get("max_id") and saved.get("names"):
            count = len(saved["names"])
            if messagebox.askyesno(
                APP_TITLE,
                f"Resume the interrupted scrape of @{target}'s {list_type}?\n\n"
                f"{count:,} usernames already collected are kept, and the scrape "
                f"continues from the page it stopped on — it does not re-fetch "
                f"those pages, so it sends far fewer requests (much lower block "
                f"risk).\n\n"
                f"Choose No to start over from page 1 (not recommended).",
            ):
                resume = ScrapeResume(max_id=saved["max_id"], names=saved["names"])
                self.log(f"Resuming @{target}'s {list_type} from {count:,} names already "
                         "collected — already-fetched pages are skipped.")
            else:
                # Deliberately NOT deleted here. This is not the last user-facing
                # exit: the replace-the-box prompt below can still be cancelled,
                # and a cancelled click sends nothing — yet deleting here made it
                # discard the record of pages already fetched, so the next scrape
                # re-fetched them all (more requests, higher block risk). The
                # scrape itself replaces this file when it ends, so a stale marker
                # only ever costs one extra prompt.
                self.log(f"Starting @{target}'s {list_type} from page 1 as chosen "
                         "(the saved marker is kept until this attempt replaces it).")

        # A scrape writes into the same usernames box the user types into, and
        # that box is mirrored to pending_names.txt — so replacing it destroys
        # a pasted list with no undo, and a restart will not bring it back.
        # Asked BEFORE any request is sent: cancelling here is free, whereas
        # cancelling after the scrape has already spent (and risked) pages.
        # A repeat scrape of the same list is exempt: the box then holds the
        # names the previous scrape put there, and replacing those is the point.
        current = self.txt_input.get("1.0", "end").strip()
        if current and current != self._last_scraped_text:
            names_here = len(parse_username_list(current))
            if not messagebox.askyesno(
                APP_TITLE,
                f"The box holds {names_here:,} username(s) right now.\n\n"
                f"A scrape replaces them with @{target}'s {list_type} list, and the "
                "copy saved on disk is overwritten too — there is no undo.\n\n"
                f"Choose No to cancel this scrape (no requests are sent). If you "
                "want to keep that list, copy it out of the box first and then "
                "choose Yes to carry on.",
            ):
                self.log("Scrape cancelled: the usernames box was left untouched.")
                return

        # Armed only now: every exit above this line is a decision the user
        # made, and none of them sent a request. Arming it earlier made a
        # cancelled scrape cost the next 60 seconds anyway, which is a
        # punishment for reading a dialog.
        self._last_scrape_attempt = time.time()
        self.stop_event.clear()
        self._user_stopped = False
        if page_pause_seconds == 0:
            self.log("Lightning scrape selected: no extra pause between pages; rate-limit/block risk is higher.")
        self.btn_scrape.configure(state="disabled", text="Scraping…")  # type: ignore[union-attr]
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.worker = threading.Thread(
            target=self._scrape_worker,
            args=(target, list_type, resume, page_pause_seconds),
            daemon=True,
        )
        self.worker.start()

    def _scrape_worker(
        self,
        target: str,
        list_type: str,
        resume: ScrapeResume | None = None,
        page_pause_seconds: float = PAGE_PAUSE_SECONDS,
    ) -> None:
        """Scrape the chosen list on a worker thread; results queue to the UI."""
        self.ui_queue.put(("phase", f"Scraping @{target}'s {list_type}…"))
        try:
            cl = self.checker.clone_client() or self.checker.client
            outcome = scrape_user_list(
                cl,
                target,
                list_type,
                on_progress=lambda p: self.ui_queue.put(("scrape_progress", p)),
                stop_event=self.stop_event,
                resume=resume,
                page_pause_seconds=page_pause_seconds,
            )
        except CheckerError as exc:
            self.ui_queue.put(("scrape_failed", exc))
            return
        except Exception as exc:
            self.ui_queue.put(("scrape_failed", CheckerError(f"Scrape failed: {exc}", "error")))
            return
        self.ui_queue.put(("scrape_done", {
            "target": target,
            "list_type": list_type,
            "names": outcome.names,
            "stopped": outcome.stopped,
            "note": outcome.note,
            "cursor": outcome.cursor,
            "blocked": outcome.blocked,
        }))

    # ------------------------------------------------- scrape progress storage

    def _scrape_progress_path(self, target: str, list_type: str) -> Path:
        return self.checker.session_dir / f"scrape_progress_{target}_{list_type}.json"

    def _load_scrape_progress(self, target: str, list_type: str) -> dict:
        """Saved cursor + names for this target's list, or {} if there is none."""
        path = self._scrape_progress_path(target, list_type)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.log(f"Could not read saved scrape progress ({exc}); starting fresh.")
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("names"), list):
            return {}
        return data

    def _save_scrape_progress(self, target: str, list_type: str, cursor: str,
                              names: list[str]) -> None:
        """Persist an unfinished scrape so the next attempt resumes, not restarts."""
        path = self._scrape_progress_path(target, list_type)
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": 1, "target": target, "list_type": list_type,
                            "max_id": cursor, "names": names}),
                encoding="utf-8",
            )
            tmp.replace(path)
            self.log(f"Saved scrape progress: {len(names):,} names, next attempt continues "
                     f"from the saved page (file: {path.name}).")
        except OSError as exc:
            self.log(f"Could not save scrape progress: {exc}")

    def _clear_scrape_progress(self, target: str, list_type: str) -> None:
        try:
            self._scrape_progress_path(target, list_type).unlink(missing_ok=True)
        except OSError:
            pass  # advisory only

    # ------------------------------------------------------------ checkpoint

    def _checkpoint_path(self) -> Path:
        return self.checker.session_dir / "checkpoint.json"

    def _load_checkpoint(self) -> None:
        """Restore a previous session's rows so a restart never re-checks."""
        path = self._checkpoint_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            results = [ProfileResult(**item) for item in payload.get("results", [])]
        except (OSError, ValueError, TypeError) as exc:
            self.log(f"Could not read checkpoint ({exc}); starting fresh.")
            return
        if not results:
            return
        for result in results:
            self._update_result(result)
        self.log(f"Restored {len(results):,} results from the previous session "
                 "— those accounts will be skipped via 'Skip rows already checked OK'.")
        self.btn_export.configure(state="normal")

    def _checkpoint_result(self, result: ProfileResult) -> None:
        """Mark a result as needing a checkpoint save (drained on the UI thread)."""
        self._checkpoint_dirty = True

    def _maybe_save_checkpoint(self) -> None:
        """Throttled save from the drain loop. The dirty flag stays set until a
        write actually happens, so no result is ever lost to the throttle."""
        if not self._checkpoint_dirty:
            return
        if time.time() - self._last_checkpoint_ts < 5:
            return
        self._flush_checkpoint()

    def _flush_checkpoint(self) -> None:
        """Save the checkpoint now (used by the drain loop and at run end)."""
        self._checkpoint_dirty = False
        self._last_checkpoint_ts = time.time()
        path = self._checkpoint_path()
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": 1, "results": [asdict(r) for r in self.results]}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            self.log(f"Could not save checkpoint: {exc}")
        self._write_autosave_csv()

    # ------------------------------------------------------------ autosave CSV

    def _autosave_path(self) -> Path:
        return self.checker.session_dir / "results_autosave.csv"

    def _write_autosave_csv(self) -> None:
        """Rolling CSV mirror of the results table, rewritten at every
        checkpoint flush (~every 5s during a run) and at run end.

        The checkpoint restores the table; this file is the human-readable
        safety net — after a crash you can open it in Excel immediately, and
        a crash can never lose more than a few seconds of rows.
        """
        if not self.results:
            return
        try:
            write_csv_file(str(self._autosave_path()), self.results)
        except OSError as exc:
            self.log(f"Could not write autosave CSV: {exc}")

    def on_clear_results(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            # Clearing mid-run empties the table while the run keeps writing to
            # it, and it deletes the checkpoint the resume story depends on —
            # so the next Start would re-check rows the user already paid for.
            # Every other destructive mid-run action (Log out, Start, Scrape)
            # is refused the same way.
            messagebox.showwarning(
                APP_TITLE, "A check or scrape is running — wait for it or press Stop first."
            )
            return
        if not self.results:
            messagebox.showinfo(APP_TITLE, "Nothing to clear.")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"Clear all {len(self.results):,} result rows?\n\n"
            "The checkpoint and the autosave CSV are deleted too, so a future "
            "run will re-check everyone.",
        ):
            return
        self.results.clear()
        self.row_by_username.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.btn_export.configure(state="disabled")
        try:
            self._checkpoint_path().unlink(missing_ok=True)
            self._autosave_path().unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"Could not delete result files: {exc}")
        # pending_names.txt deliberately survives: it mirrors the usernames box,
        # which Clear results does not touch — it clears results, not input.
        self.lbl_progress.configure(text="Results cleared.")
        self.log("Cleared all results and the checkpoint.")

    def on_export(self) -> None:
        if not self.results:
            messagebox.showinfo(APP_TITLE, "No results to export yet.")
            return
        default = f"instagram_results_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path = filedialog.asksaveasfilename(
            title="Save CSV",
            defaultextension=".csv",
            initialfile=default,
            filetypes=[("CSV files", "*.csv")],
        )
        if not path:
            return
        try:
            write_csv_file(path, self.results)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not save file:\n{exc}")
            return
        self.log(f"Exported {len(self.results)} rows to {path}")
        messagebox.showinfo(APP_TITLE, f"Exported {len(self.results)} rows to:\n{path}")

    def on_copy_dp_url(self, _event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        row = sel[0]
        values = self.tree.item(row, "values")
        result = next((r for r in self.results if r.username == values[0]), None)
        if result and result.profile_pic_url:
            self.clipboard_clear()
            self.clipboard_append(result.profile_pic_url)
            self._append_log(f"Copied DP URL for @{result.username}.")
        else:
            status = values[9] if len(values) > 9 else "unknown"
            self._append_log(f"No DP URL available for @{values[0]} ({status}).")

    # ------------------------------------------------- pending-names storage

    def _feedback_block_path(self) -> Path:
        return self.checker.session_dir / "feedback_block.json"

    def _load_feedback_block(self) -> tuple:
        """(until, since) epoch pair of the last feedback_required block (0,0 = none)."""
        try:
            data = json.loads(self._feedback_block_path().read_text(encoding="utf-8"))
            return float(data.get("until", 0) or 0), float(data.get("since", 0) or 0)
        except (OSError, ValueError):
            return 0.0, 0.0

    def _record_feedback_block(self, seconds: float = 21600.0) -> None:
        """Remember an Instagram action-block (default: 6h advisory) across restarts."""
        since = time.time()
        self._feedback_block_until = since + seconds
        self._feedback_block_since = since
        try:
            self._feedback_block_path().write_text(
                json.dumps({"until": self._feedback_block_until, "since": since}),
                encoding="utf-8",
            )
        except OSError:
            pass  # advisory only

    def _pending_names_path(self) -> Path:
        return self.checker.session_dir / "pending_names.txt"

    def _load_pending_names(self) -> None:
        """Put last session's usernames back in the box (restart-proofing)."""
        path = self._pending_names_path()
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip("\n")
        except OSError as exc:
            self.log(f"Could not read saved usernames ({exc}); box starts empty.")
            return
        if not text.strip():
            return
        self.txt_input.insert("1.0", text)
        count = len(parse_username_list(text))
        self.log(f"Restored {count:,} usernames from the previous session "
                 "— they were kept so a restart never loses the list.")

    def _on_input_modified(self, _event=None) -> None:
        """Fires on every Text change; re-arms itself and schedules a save."""
        self.txt_input.edit_modified(False)  # re-arm <<Modified>> for the next change
        self._names_dirty = True

    def _maybe_save_pending_names(self) -> None:
        """Throttled (2s) save from the drain loop; dirty until actually written."""
        if not self._names_dirty or time.time() - self._names_saved_at < 2:
            return
        self._flush_pending_names()

    def _flush_pending_names(self) -> None:
        """Write the username box to pending_names.txt (atomic best-effort)."""
        self._names_dirty = False
        self._names_saved_at = time.time()
        path = self._pending_names_path()
        text = self.txt_input.get("1.0", "end").strip("\n")
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            self.log(f"Could not save usernames for next time: {exc}")

    def _on_close(self) -> None:
        self.stop_event.set()
        self._flush_pending_names()  # never lose the username list to a restart
        self._flush_checkpoint()  # the 5s drain throttle can leave fresh rows unsaved
        self.destroy()

    def _schedule_pump(self, delay_ms: int = 100) -> None:
        """(Re)arm the queue pump, remembering its id so shutdown can cancel it."""
        self._pump_after = self.after(delay_ms, self._drain_queue)

    def destroy(self) -> None:
        """Cancel the timers before tearing the interpreter down.

        A pending after() callback that fires after destroy() finds a dead Tcl
        command and prints "invalid command name" noise on every close; the
        pending session restore would go on to poke destroyed widgets.
        """
        for attr in ("_pump_after", "_restore_after"):
            handle = getattr(self, attr, None)
            if handle is not None:
                try:
                    self.after_cancel(handle)
                except Exception:
                    pass
                setattr(self, attr, None)
        super().destroy()

    # ----------------------------------------------------------- background

    # ---------------------------------------------------------- progress UI

    def _on_progress_event(self, payload) -> None:
        """Progress bar, percent, ETA, batch number, and Stop-wait feedback."""
        if isinstance(payload, dict):
            self._update_run_progress(payload)
        else:  # legacy tuple from scrape paths: (done, total, name)
            index, total, name = payload
            self.lbl_progress.configure(text=f"Checked @{name}  ({index}/{total})…")

    def _update_run_progress(self, state: dict) -> None:
        done, total = state.get("done", 0), max(1, state.get("total", 1))
        pct = max(0.0, min(100.0, done * 100.0 / total))
        self.progress_bar.grid()  # visible for the whole run, incl. 0%
        self.progress_bar.configure(value=pct)

        batch = state.get("batch") or 0
        batches = state.get("batches") or 1
        prefix = f"Batch {batch}/{batches} · " if batches > 1 else ""

        if state.get("paused"):
            wait = state.get("pause_left") or 0
            text = f"{prefix}Cooling down {wait:.0f}s — Stop skips the wait."
        else:
            remaining = max(0, total - done)
            eta = self._eta_text(done, remaining, total)
            text = f"{prefix}Checked {done:,}/{total:,} — {pct:.0f}% — {eta}"
        self.lbl_progress.configure(text=text)

    def _eta_text(self, done: int, remaining: int, total: int) -> str:
        if done <= 0:
            return "estimating…"
        elapsed = time.time() - self._run_started_at
        per_account = elapsed / done
        eta = remaining * per_account
        if eta >= 90 * 60:
            return f"~{eta / 3600:.1f}h remaining"
        if eta >= 90:
            return f"~{eta / 60:.0f}m remaining"
        return f"~{eta:.0f}s remaining"

    def _check_all(self, usernames: list[str], workers: int, delay, with_activity: bool, public_mode: bool) -> None:
        total = len(usernames)
        done = [0]
        lock = threading.Lock()
        self._pacing = (workers, delay)
        run_state = {"batch": 0, "batches": 1, "paused": False, "pause_left": 0.0}
        run_start = time.time()
        gate = self._run_gate

        def fire_progress() -> None:
            with lock:
                d = done[0]
            self.ui_queue.put(("progress", {"done": d, "total": total, **run_state}))

        rate_limited_count = [0]  # counted on the worker's own result stream

        def on_result(result: ProfileResult) -> None:
            self.ui_queue.put(("result", result))
            self._checkpoint_result(result)
            if result.status == "error" and "rate-limited" in (result.error or ""):
                rate_limited_count[0] += 1
            with lock:
                done[0] += 1
            fire_progress()

        def on_batch(index: int, n_batches: int) -> None:
            run_state["batch"] = index
            run_state["batches"] = n_batches
            fire_progress()

        self.ui_queue.put(("progress", {"done": 0, "total": total, **run_state}))

        def cooldown_handler(seconds_left: float) -> None:
            # Called once per second during the pause (and with 0.0 at the
            # end); the label simply follows seconds_left.
            run_state["paused"] = seconds_left > 0
            run_state["pause_left"] = seconds_left
            fire_progress()

        try:
            if public_mode:
                self.checker.public_check_many(
                    usernames,
                    workers=workers,
                    delay_range=delay,
                    on_result=on_result,
                    stop_event=self.stop_event,
                    batch_size=BATCH_SIZE,  # same ceiling as session mode
                    on_batch=on_batch,
                    on_cooldown=cooldown_handler,
                )
                # Count from the worker's own result stream: reading the UI's
                # results list here races the drain loop and misses rows.
                # Skipped when the circuit breaker halted the run: a 1-2 name
                # list can be fully throttled AND trip the breaker, and both
                # events firing showed two contradictory dialogs back to back
                # (run_halted also arms the restart pause — it owns this case).
                if (not self.checker.halt_reason
                        and rate_limited_count[0] == len(usernames)):
                    self.ui_queue.put(("all_throttled", None))
            else:
                checker = self.checker
                if len(usernames) > BATCH_SIZE:
                    checker.check_many_batched(
                        usernames,
                        batch_size=BATCH_SIZE,  # one source of truth for chunking
                        workers=workers,
                        delay_range=delay,
                        on_result=on_result,
                        stop_event=self.stop_event,
                        with_activity=with_activity,
                        on_batch=on_batch,
                        on_cooldown=cooldown_handler,
                    )
                else:
                    with gate:
                        checker.check_many(
                            usernames,
                            workers=workers,
                            delay_range=delay,
                            on_result=on_result,
                            stop_event=self.stop_event,
                            with_activity=with_activity,
                        )
                if not self.stop_event.is_set():
                    gate.wait_all(timeout=30)
            if self.checker.halt_reason and not self._user_stopped:
                # Circuit breaker, not a user Stop: explain it instead of a
                # quiet "Stopped" summary, and hold off an instant restart.
                self.ui_queue.put(("run_halted", self.checker.halt_reason))
                return
            self.ui_queue.put(("done", self.stop_event.is_set()))
        except CheckerError as exc:
            self.ui_queue.put(("run_failed", exc))
        except Exception as exc:  # never leave Start/Stop stuck disabled
            self.ui_queue.put(("run_failed", CheckerError(f"Unexpected error: {exc}", "error")))

    def _drain_queue(self) -> None:
        """One UI-thread pump over queued worker events.

        The try/except wraps each *event*, not the pump: one bad handler must
        never stop the loop, or every later sign-in/progress/result event sits
        in the queue forever behind it.
        """
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "result":
                    self._update_result(payload)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self._on_progress_event(payload)
                elif kind == "signed_in":
                    self._after_signin(True, None)
                elif kind == "logged_out":
                    self._after_logout()
                elif kind == "logout_failed":
                    self.btn_logout.configure(state="normal")
                    messagebox.showerror(APP_TITLE, payload.message)
                elif kind == "signin_failed":
                    self._after_signin(False, payload)
                elif kind == "run_failed":
                    # Public mode refused before any lookup ran: say that, not
                    # "Stopped — 0 checked OK", which reads like an audit result.
                    if getattr(payload, "kind", "") == "public_unavailable":
                        self._after_done(
                            stopped=True,
                            summary="Public mode can't run right now — see the dialog.",
                        )
                        self._offer_session_mode(payload.message)
                    else:
                        self._after_done(stopped=True)
                        messagebox.showerror(APP_TITLE, payload.message)
                elif kind == "all_throttled":
                    self._after_done(stopped=False)
                    messagebox.showwarning(
                        APP_TITLE,
                        "Instagram refused every sign-in-free lookup from this machine, so "
                        "no accounts could be checked.\n\n"
                        "This is an IP-level cool-down on logged-out lookups — it costs "
                        "nothing from any Instagram account, and your account was not "
                        "involved.\n\n"
                        "What to do:\n"
                        "1. Wait 15-60 minutes and run Public mode again.\n"
                        "2. Or sign in and use Session mode — that spends the account's "
                        "budget instead of the IP's, and also returns Last Post dates.\n"
                        "3. For big repeated checks, use a dedicated throwaway Instagram "
                        "account, not the one you use daily.",
                    )
                elif kind == "phase":
                    self.lbl_progress.configure(text=payload)
                elif kind == "scrape_progress":
                    list_word = getattr(payload, "list_type", "followers")
                    total = f" / {payload.total}" if getattr(payload, "total", None) else ""
                    self.lbl_progress.configure(
                        text=f"Scraping @{payload.target}'s {list_word}… {payload.scraped:,}{total} names"
                    )
                elif kind == "scrape_done":
                    self._after_scrape(**payload)
                elif kind == "scrape_failed":
                    self._after_scrape_failed(payload)
                elif kind == "run_halted":
                    self._after_done(stopped=True)
                    self._start_paused_until = time.time() + 600
                    self.var_skip_existing.set(True)  # next Start resumes the leftover rows
                    self.log("'Skip rows already checked OK' was ticked so the next run "
                             "continues from the checkpoint instead of re-checking finished rows.")
                    messagebox.showwarning(APP_TITLE, payload)
                elif kind == "done":
                    self._after_done(payload)
                    self._show_verdict_summary(payload)
            except Exception:
                # Keep pumping: a broken handler (e.g. a focus call on a
                # non-widget) must not starve the rest of the queue.
                import traceback
                self._append_log(f"UI error: {traceback.format_exc(limit=1)}")
        self._maybe_save_checkpoint()
        self._maybe_save_pending_names()
        self._schedule_pump()

    # ------------------------------------------------------------- UI state

    @staticmethod
    def _tags(result: ProfileResult) -> tuple:
        """Verdict tag first so its color wins over the status color."""
        verdict, _ = verdict_for(result)
        if verdict:
            return (f"v:{verdict}", result.status)
        return (result.status,)

    def _update_result(self, result: ProfileResult) -> None:
        self._maybe_offer_private_session_mode(result)
        # Counted here, on the UI thread, so the tally can never race the
        # drain loop; a re-checked username counts once per run, which is the
        # number the user asked for ("how did this list do?").
        if result.status == "ok":
            self._run_counts["ok"] += 1
        elif result.status in ("not_found", "invalid"):
            self._run_counts["missing"] += 1
        else:
            self._run_counts["error"] += 1
        if not any(r.username == result.username for r in self.results):
            self.results.append(result)
        else:
            self.results = [r if r.username != result.username else result for r in self.results]
        iid = self.row_by_username.get(result.username)
        if iid:
            self.tree.item(iid, values=self._values(result), tags=self._tags(result))
        else:  # parallel mode: results arrive without a prior pending row
            new_iid = self.tree.insert("", "end", values=self._values(result), tags=self._tags(result))
            self.row_by_username[result.username] = new_iid

    def _maybe_offer_private_session_mode(self, result: ProfileResult) -> None:
        """Public mode should keep running for public accounts, but ask once when a private account needs a session."""
        if self._private_prompt_done:
            return
        if self.checker.signed_in:
            return
        if not self.var_mode.get().startswith("Public"):
            return
        if not result.is_private or result.status != "ok":
            return

        self._private_prompt_done = True
        if not messagebox.askyesno(
            APP_TITLE,
            f"@{result.username} is a private profile. Public mode can only show limited public details; "
            "sign in to Session mode for the full private profile data?",
        ):
            self.log(f"Private profile @{result.username} detected — continuing in Public mode with limited public data.")
            return

        self.var_mode.set("Session (safest data)")
        self.entry_user.focus_set()
        self.log(f"Switched to Session mode for @{result.username} — sign in to fetch the full private profile data.")

    @staticmethod
    def _values(result: ProfileResult) -> tuple:
        def num(v):
            return "" if v is None else f"{v:,}"

        row = result.as_row()
        return (
            result.username,
            row["alive"].capitalize() if row["alive"] else "",
            row["verdict"],
            row["dp_present"].capitalize() if row["dp_present"] else "",
            result.last_post_at,
            num(result.posts),
            num(result.followers),
            num(result.following),
            "" if result.status != "ok" else ("Yes" if result.is_private else "No"),
            result.status,
            result.error,
        )

    def _offer_session_mode(self, message: str) -> None:
        """Turn the public-mode refusal from a notice into a next step.

        The dialog used to be OK-only while the app stayed on "Public (safest
        & fast)", which it had just proved cannot run — so the user's next
        click (Start) reproduced the same dialog, and the only way forward was
        finding the mode box themselves. The message already tells them to
        switch; ask for it and do it in the same breath.
        """
        if not messagebox.askyesno(APP_TITLE, f"{message}\n\nSwitch to Session mode now?"):
            return
        self.var_mode.set("Session (safest data)")
        self.entry_user.focus_set()
        self.log("Switched to Session mode — sign in (or use your Session ID) to start checking.")

    def _after_logout(self) -> None:
        self.lbl_signed_in.configure(text="Not signed in", foreground="#c62828")
        self.btn_logout.configure(state="disabled")
        # Export is left alone on purpose: the results table is local and still
        # full, so logging out must not grey out the only way to get the audit
        # out (it did; the user then had to sign in again or restart to export
        # rows that were sitting on screen). Export follows the table: enabled
        # iff there are rows.
        self.log("Logged out. You can sign in with another account.")

    def _after_signin(self, ok: bool, error: CheckerError | None) -> None:
        self.btn_signin.configure(state="normal", text="Sign In")
        self.btn_signin_sid.configure(state="normal", text="Sign In with Session ID")
        if ok:
            self.btn_logout.configure(state="normal")
            self.lbl_signed_in.configure(text=f"Signed in as @{self.checker.username}", foreground="#1a7f37")
            self.var_pass.set("")
            self.var_code.set("")
            self.var_sessionid.set("")
            self.log("You can now paste usernames and click Start Checking.")
        else:
            self.lbl_signed_in.configure(text="Not signed in", foreground="#c62828")
            messagebox.showerror(APP_TITLE, error.message if error else "Sign-in failed.")
            if error and error.kind == "challenge":
                self.entry_code.focus_set()

    def _after_scrape(self, target: str, list_type: str, names: list[str], stopped: bool,
                      note: str = "", cursor: str = "", blocked: bool = False) -> None:
        """Scrape finished: load names into the input box.

        A user Stop is honored absolutely: partial names are kept, but the
        audit never auto-starts — the user said stop. Names-only mode also
        stops here. Otherwise the audit starts immediately. A non-empty `note`
        (a rate limit, or an Instagram feedback_required action block mid-
        scrape) also keeps the partial names and skips the auto-audit: pushing
        on while Instagram is refusing requests is what gets accounts blocked.
        A non-empty `cursor` means the list is unfinished — it is saved along
        with the names, so the next Scrape click resumes from that page instead
        of re-fetching the whole list (fewer requests, lower block risk).
        """
        # The scrape worker may still be winding down; on_start's re-entry
        # guard would otherwise refuse the auto-audit. Join it briefly.
        if self.worker is not None:
            self.worker.join(timeout=5)
        self.btn_scrape.configure(state="normal", text="Scrape →")
        self.btn_stop.configure(state="disabled", text="Stop")
        if names and cursor:
            self._save_scrape_progress(target, list_type, cursor, names)
        elif names and not stopped:
            # The list arrived whole: a later scrape starts fresh.
            self._clear_scrape_progress(target, list_type)
        if not names:
            self.btn_start.configure(state="normal")
            if blocked:
                self._record_feedback_block()
            if note:
                self.lbl_progress.configure(text="Stopped by Instagram — 0 names retrieved.")
                messagebox.showwarning(APP_TITLE, note)
                return
            messagebox.showinfo(APP_TITLE, f"@{target}'s {list_type} list is empty (or the scrape was stopped).")
            return
        scraped_text = "\n".join(names)
        self.txt_input.delete("1.0", "end")
        self.txt_input.insert("1.0", scraped_text)
        self._last_scraped_text = scraped_text.strip()
        if note:
            # Instagram refused mid-scrape: the names that landed are kept, but
            # the audit must not auto-start while requests are being rejected.
            if blocked:
                self._record_feedback_block()  # blocked again: 10-minute pacing window
            else:
                self._last_scrape_attempt = time.time()  # restart the quiet window
            self.btn_start.configure(state="normal")
            self.lbl_progress.configure(
                text=f"Stopped by Instagram — kept {len(names):,} names from @{target}'s {list_type}."
            )
            resume_hint = " Scrape again later to resume from the saved page." if cursor else ""
            self.log(f"Instagram stopped the scrape; {len(names):,} usernames kept, "
                     f"no audit started.{resume_hint}")
            messagebox.showwarning(APP_TITLE, note)
            return
        if stopped:
            self.btn_start.configure(state="normal")
            self.lbl_progress.configure(text=f"Stopped — kept {len(names):,} names from @{target}'s {list_type}.")
            resume_hint = " Scrape again to resume from the saved page." if cursor else ""
            self.log(f"Scrape stopped by user; {len(names):,} usernames kept, "
                     f"no audit started.{resume_hint}")
            return
        if self.var_names_only.get():
            self.btn_start.configure(state="normal")
            self.lbl_progress.configure(text=f"Extracted {len(names):,} usernames from @{target}'s {list_type}.")
            self.log(f"Extracted {len(names):,} users from @{target}'s {list_type} list (no checks run).")
            return
        self.log(f"Scraped {len(names):,} users from @{target}'s {list_type} — starting the audit.")
        # Run the analysis through the normal Start path (respects mode/speed).
        self.var_mode.set("Session (safest data)")
        self.on_start()

    def _after_scrape_failed(self, error: CheckerError) -> None:
        """Scrape ended in failure: restore the buttons and say what failed.

        This must not reuse _after_done — that prints a *check-run* summary
        ('Stopped — 0 checked OK, 0 missing/invalid…') which reads like an
        audit outcome for a scrape that never checked anything.
        """
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=5)
        self.btn_scrape.configure(state="normal", text="Scrape →")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled", text="Stop")
        self.progress_bar.grid_remove()
        self.progress_bar.configure(value=0)
        self.lbl_progress.configure(text="Scrape failed — see the dialog and the log for details.")
        self._append_log(f"Scrape failed: {error.message}")
        if getattr(error, "kind", "") == "feedback":
            self._record_feedback_block()
        elif getattr(error, "kind", "") == "rate_limit":
            self._last_scrape_attempt = time.time()  # pause even if the attempt was quick
        messagebox.showerror(APP_TITLE, error.message)

    VERDICT_ORDER = ("likely-fake", "suspicious", "inactive", "dead", "fine")

    def _show_verdict_summary(self, stopped: bool) -> None:
        """Popup with the verdict breakdown after a run (finished or stopped).

        Counts cover every row in the table (including restored checkpoint
        rows). Runs that end in an error dialog (run_failed / all_throttled /
        scrape_failed) never reach this; runs with nothing judgeable skip the
        popup — the progress label already carries their one-line summary.
        """
        counts = {v: 0 for v in self.VERDICT_ORDER}
        unchecked = 0
        for result in self.results:
            verdict, _ = verdict_for(result)
            if verdict:
                counts[verdict] += 1
            else:
                unchecked += 1
        if not any(counts.values()):
            return
        lines = [
            ("Stopped run — summary so far" if stopped else "Run finished")
            + f" — {len(self.results):,} rows in the table.\n"
        ]
        for verdict in self.VERDICT_ORDER:
            lines.append(f"{verdict:<13}{counts[verdict]:>6,}")
        if unchecked:
            lines.append(f"{'not checkable':<13}{unchecked:>6,}   (errors / invalid names)")
        lines.append("\nExport to CSV to keep every detail.")
        messagebox.showinfo(APP_TITLE, "\n".join(lines))

    def _after_done(self, stopped: bool, summary: str | None = None) -> None:
        self._flush_checkpoint()  # final results are on disk the moment a run ends
        # A stopped run's workers exit quickly now; never block the UI long.
        if self.worker is not None and self.worker.is_alive() and not stopped:
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                self._append_log("Background run still finishing; UI unlocked.")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled", text="Stop")
        self.btn_scrape.configure(state="normal")  # type: ignore[union-attr]
        self.progress_bar.grid_remove()
        self.progress_bar.configure(value=0)
        # This run's tally, not the table's: earlier runs and restored
        # checkpoint rows stay in the table but must not inflate the number
        # that describes the run that just finished.
        ok_rows = self._run_counts["ok"]
        missing = self._run_counts["missing"]
        errors = self._run_counts["error"]
        if summary is None:
            summary = (
                f"{'Stopped' if stopped else 'Done'} — {ok_rows} checked OK, "
                f"{missing} missing/invalid, {errors} errors. "
                f"Double-click a row to copy its DP URL; Export to CSV to save."
            )
        self.lbl_progress.configure(text=summary)
        self._append_log(summary)
        if self.results:
            self.btn_export.configure(state="normal")


_LOCK_BYTE_OFFSET = 64  # room for the pid text, which must stay readable


def acquire_single_instance(session_dir: Path):
    """Take an OS lock so only one copy of the app can run at a time.

    Two copies share one checkpoint, one autosave CSV and one pending-names
    file, and each keeps the whole result set in memory and rewrites the whole
    file when it saves. Measured with two windows open: the second launch
    silently deleted the first's results, and the first then deleted the
    second's on its next save — a finished audit destroyed with no error shown
    to anyone. Both also wrote through the same `checkpoint.tmp`, so their
    writes could interleave.

    msvcrt.locking is used rather than a PID file because the OS releases the
    lock when the process dies: a crash, a force-quit or a hard power-off
    cannot leave a stale lock that blocks the user forever.

    Returns the open lock file, which must be kept alive for the process
    lifetime, or None if another copy already holds it.
    """
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    handle = open(session_dir / "app.lock", "a+b")
    # Lock a byte *past* the PID, not byte 0. Locking byte 0 makes the whole
    # first read of the file fail: a reader needs the PID precisely when
    # another copy holds the lock, and that is the one time it cannot be had.
    handle.seek(_LOCK_BYTE_OFFSET)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        # Refused: another copy holds the lock. Touch nothing — its PID is
        # in the file, and _raise_existing_window needs exactly that PID to
        # put the real window in front of this double-click.
        handle.close()
        return None
    # Only the holder writes its PID, and only after the lock is taken. The
    # write used to happen first, so every refused launch overwrote the
    # holder's PID with its own — after one duplicate click the file named a
    # process with no window, and raising the existing window could never
    # work again.
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii"))
    handle.flush()
    return handle


def _raise_existing_window(session_dir: Path) -> bool:
    """Bring the already-running copy to the front instead of just refusing.

    Being told "Alt+Tab to the window that is already open" is a dead end for
    the user who double-clicked the shortcut again expecting their window.
    Windows knows how to do this, so do it; the caller falls back to the
    dialog if it cannot.
    """
    try:
        import ctypes
        from ctypes import wintypes

        raw = (session_dir / "app.lock").read_text(encoding="ascii", errors="ignore").strip()
        pid = int(raw or 0)
        if pid <= 0:
            return False
        user32 = ctypes.windll.user32
        windows: list = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                windows.append(hwnd)
                return False  # stop enumerating
            return True

        user32.EnumWindows(_collect, 0)
        if not windows:
            return False
        hwnd = windows[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: also un-minimises it
        if not user32.SetForegroundWindow(hwnd):
            # Windows refuses foreground changes from a process that does not
            # hold foreground rights (typically a background launcher). Report
            # the failure rather than claiming success: if we quietly returned
            # True here, a double-click would produce no window and no dialog,
            # which is a worse dead end than the message we were replacing.
            return False
        return True
    except Exception:
        return False  # never let this stop the app opening


def main() -> None:
    lock = None
    try:
        lock = acquire_single_instance(DEFAULT_SESSION_DIR)
        if lock is None:
            if _raise_existing_window(DEFAULT_SESSION_DIR):
                return  # the window the user wanted is now in front of them
            messagebox.showwarning(
                APP_TITLE,
                "This app is already open.\n\n"
                "Only one copy can run at a time — two copies share the same "
                "saved results file, and each would quietly overwrite the "
                "other's audit.\n\n"
                "Switch to the window that is already open (Alt+Tab), or close "
                "it first and start again.",
            )
            return
    except OSError:
        # The lock is a convenience, never a reason to refuse to open. If it
        # cannot be created at all — a locked-down home folder, something else
        # sitting on the path — start anyway. The worst case is the old
        # clobbering, which the user can still avoid by not launching two
        # copies; failing here would leave them with an app that will not open
        # and no explanation, which is worse.
        pass
    app = App()
    try:
        app.mainloop()
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
