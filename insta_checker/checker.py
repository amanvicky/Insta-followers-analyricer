"""Core logic for the Instagram Profile Checker.

Wraps instagrapi (https://github.com/subzeroid/instagrapi) behind a small,
UI-agnostic API: sign in (with session caching), fetch one profile, and
convert results to CSV. No tkinter imports here so the logic stays testable.
"""

from __future__ import annotations

import csv
import io
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from instagrapi import Client
from instagrapi.exceptions import (
    BadCredentials,
    ChallengeError,
    ClientConnectionError,
    ClientError,
    ClientNotFoundError,
    ClientThrottledError,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    TwoFactorRequired,
    UserNotFound,
)

from public_lookup import PublicLookup, PublicLookupError, probe_public_access

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Substrings that appear in Instagram's generic "no photo" avatar URLs.
# If one of these is present, the account has NOT uploaded its own picture.
DEFAULT_AVATAR_MARKERS = (
    "44884218_345707102882519_2446069589734326528_n",  # classic generic avatar
    "150s_2f2c9648",   # small generic avatar
    "1080_2f2c9648",   # large generic avatar
    "anon_profile",    # anonymous placeholder
)


# Socket read ceiling per HTTP request (instagrapi's default is 25s).
READ_TIMEOUT = 10

# Public-mode retries are deliberately shallow: sleeps block run completion.
PUBLIC_RETRIES = 1

# instagrapi sleeps before EVERY request on its own: delay_range (random) plus
# request_timeout (a sleep, NOT a socket timeout — default 1s). That is 2–4s of
# invisible waiting per account. We pace requests ourselves via SPEED_PRESETS,
# so both are zeroed on every client we create (_tune_client), and re-zeroed
# after load_settings() because session files restore the library defaults.
CLIENT_DELAY_RANGE = [0.0, 0.0]

# One source of truth: the app needs this path to place its single-instance
# lock beside the checkpoint, and the checker owns the checkpoint.
DEFAULT_SESSION_DIR = Path.home() / ".insta_checker"

class CheckerError(Exception):
    """A user-facing error with a machine-readable kind."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.message = message
        # kind: "auth" | "challenge" | "network" | "rate_limit" | "invalid" | "error"
        self.kind = kind


@dataclass
class ProfileResult:
    """Outcome of checking a single username."""

    username: str
    status: str = "pending"  # ok | not_found | invalid | error
    dp_present: Optional[bool] = None
    posts: Optional[int] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    is_private: bool = False
    is_verified: bool = False
    full_name: str = ""
    error: str = ""
    profile_pic_url: str = ""
    checked_at: str = ""
    alive: Optional[bool] = None  # True = accessible, False = dead, None = unknown
    last_post_at: str = ""

    def as_row(self) -> dict:
        row = asdict(self)
        row["dp_present"] = "" if self.dp_present is None else ("yes" if self.dp_present else "no")
        row["alive"] = "" if self.alive is None else ("yes" if self.alive else "no")
        verdict, reason = verdict_for(self)
        row["verdict"] = verdict
        row["verdict_reason"] = reason
        return row


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_username(raw: str) -> str:
    name = (raw or "").strip().lstrip("@").strip().lower()
    if not name:
        raise CheckerError("Empty username.", "invalid")
    if not USERNAME_RE.match(name):
        raise CheckerError(f"'{name}' is not a valid Instagram username.", "invalid")
    return name


def parse_username_list(text: str) -> List[str]:
    """Parse free-form text (newlines, commas, semicolons) into a clean username list.

    Separators are line breaks, commas, semicolons and tabs — deliberately NOT
    bare spaces. Splitting on spaces is expensive here: a typo like "bad name"
    became two real lookups on two wrong accounts, and those rows looked
    perfectly normal in the table. One invalid row showing the user's own text
    is cheaper and honest. Instagram usernames never contain spaces, so no
    legitimate name is rejected by this.
    """
    seen: set = set()
    out: List[str] = []
    for chunk in re.split(r"[,;\t\r\n]+", text or ""):
        # U+FEFF is the byte-order mark that Notepad and Excel put on top of
        # "UTF-8" files. It is invisible, str.strip() does not remove it, and
        # it used to ride along inside the first username, turning that row
        # into "not a valid Instagram username" — the first line of the file,
        # silently lost. Stripped per chunk so a BOM is harmless wherever it
        # turns up: a loaded file, pasted text, two lists concatenated.
        raw = chunk.strip().lstrip("\ufeff").strip()
        if not raw:
            continue
        name = raw.lstrip("@").lower()
        # "@@" / "@" must survive as an invalid row instead of vanishing with
        # no feedback; fall back to the raw text when stripping '@' is empty.
        key = name or raw.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def dp_present_from_urls(*urls: Optional[str]) -> bool:
    """Decide whether an account has its own profile picture (DP).

    Instagram serves a generic grey avatar when no photo was uploaded; its URL
    contains one of the known marker substrings. No URL at all also means no DP.
    """
    joined = " ".join(str(u or "").lower() for u in urls)
    if not joined.strip():
        return False
    return not any(marker in joined for marker in DEFAULT_AVATAR_MARKERS)


CSV_COLUMNS = [
    "username", "alive", "verdict", "verdict_reason", "dp_present", "last_post_at", "posts",
    "followers", "following",
    "is_private", "is_verified", "full_name", "status", "error",
    "profile_pic_url", "checked_at",
]

# An account whose newest post is older than this counts as inactive.
INACTIVE_DAYS = 90


def verdict_for(result: ProfileResult, now: Optional[datetime] = None) -> Tuple[str, str]:
    """Transparent verdict label + reason, from data already fetched.

    Labels: likely-fake / suspicious / inactive / fine / dead.
    Failed checks get no verdict at all — guessing would erode trust.
    """
    if result.status == "not_found":
        return "dead", "No Instagram account with this username."
    if result.status != "ok":
        return "", ""  # invalid input or failed check: nothing to judge

    posts = result.posts or 0
    followers = result.followers or 0
    following = result.following or 0

    if result.dp_present is False and posts == 0:
        return "likely-fake", "no DP · 0 posts"
    if following >= 100 and followers * 20 <= following:
        return "suspicious", f"following {following:,} vs {followers:,} followers"
    if result.dp_present is False and posts <= 3:
        return "suspicious", f"no DP · {posts} posts"
    if posts == 0:
        return "inactive", "0 posts"
    if result.last_post_at:
        try:
            last = datetime.strptime(result.last_post_at, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now is None:
                now = datetime.now(timezone.utc)
            if (now - last).days > INACTIVE_DAYS:
                return "inactive", f"last post {result.last_post_at}"
        except ValueError:
            pass
    return "fine", ""

# Speed presets: (worker threads, per-task jitter seconds).
# All workers share the same Instagram session, so higher parallelism = higher
# throttle/ban risk. "lightning" pushes the private API hard — use it when you
# accept temporary action-blocking risk in exchange for speed.
SPEED_PRESETS: dict[str, Tuple[int, Tuple[float, float]]] = {
    "safe": (2, (0.8, 1.5)),
    "fast": (4, (0.25, 0.6)),
    "turbo": (6, (0.15, 0.35)),
    "lightning": (10, (0.05, 0.15)),
}

# Big-list batching: runs larger than this are split into chunks with quiet
# cool-downs between them, so a 3,000-name audit doesn't look like a bot burst.
BATCH_SIZE = 300
BATCH_PAUSE_RANGE: Tuple[float, float] = (20.0, 40.0)

# Circuit breaker: this many rate-limit hits with no successful lookup in
# between means Instagram is refusing the session right now. Retrying through
# that (20s, then 40s, then 60s, per account, for thousands of accounts) is the
# exact traffic pattern that escalates to an action block or a ban — so the
# whole run halts instead. The saved checkpoint makes resuming cheap later.
RATE_LIMIT_STREAK_LIMIT = 4
PUBLIC_THROTTLE_HALT_MESSAGE = (
    "Instagram is refusing the sign-in-free (public) lookups, so the run was halted "
    "rather than retried through it.\n\n"
    "This is an IP-level cool-down on logged-out lookups — your Instagram account "
    "is not involved and nothing about it was spent.\n\n"
    "What to do:\n"
    "1. Wait 15-60 minutes before trying public mode again.\n"
    "2. Sign in and use Session mode if you need the full data set (Last Post "
    "date, private profiles) — it uses the account's budget instead of the IP's."
)
PUBLIC_UNAVAILABLE_MESSAGE = (
    "Public mode cannot work right now: Instagram is refusing every signed-out "
    "profile request from this computer.\n\n"
    "That was checked before your list was touched: the JSON endpoint that serves "
    "signed-out lookups refuses even a username that cannot exist (so it rejects "
    "before resolving anything), and the public profile page comes back with no "
    "profile data for an account that certainly does exist. No list of yours was "
    "sent, nothing was checked, and no Instagram account was involved.\n\n"
    "What to do:\n"
    "1. Use Session mode. Sign in with a dedicated throwaway Instagram account "
    "for checking — not the one you use daily. That is the only reliable way to "
    "get full data now.\n"
    "2. Retry Public mode later: this is Instagram's policy for logged-out "
    "access, and it can change.\n\n"
    "Note: Public mode never costs account budget, so trying it is safe — it "
    "just cannot return data at the moment."
)
THROTTLE_HALT_MESSAGE = (
    "Instagram is refusing lookups (rate limit), so the run was halted rather "
    "than retried through it.\n\n"
    "Consecutive retries in this state are what get accounts action-blocked or "
    "banned, so the app stops instead of grinding on.\n\n"
    "What to do:\n"
    "1. Wait at least 15-60 minutes — longer if it keeps happening. Do not "
    "restart immediately; that extends the block.\n"
    "2. Press Start again with 'Skip rows already checked OK' ticked (the app "
    "ticks it for you after a halt). Finished accounts are not re-checked; the "
    "run continues from the checkpoint.\n"
    "3. For big lists, prefer safe speed, and consider Public mode — it never "
    "spends the account's rate budget."
)


def results_to_csv(results: Iterable[ProfileResult]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for result in results:
        writer.writerow(result.as_row())
    return buf.getvalue()


def write_csv_file(path: str, results: Iterable[ProfileResult]) -> None:
    """Save results as an Excel-friendly CSV.

    newline="" is required on Windows: text-mode translation would turn csv's
    \r\n into \r\r\n, producing a blank row after every account in Excel.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\ufeff" + results_to_csv(results))


class InstagramChecker:
    """Signs in once, then answers profile lookups one username at a time."""

    def __init__(
        self,
        session_dir: Optional[Path] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.session_dir = Path(session_dir) if session_dir else DEFAULT_SESSION_DIR
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.on_log = on_log or (lambda _msg: None)
        self.client: Optional[Client] = None
        self.username: str = ""
        # Set when the rate-limit circuit breaker halts a run; read (and
        # cleared) by the app so the halt can be explained to the user.
        self.halt_reason: str = ""

    # ------------------------------------------------------------------ auth

    @property
    def signed_in(self) -> bool:
        return self.client is not None

    @staticmethod
    def _tune_client(client: Client) -> None:
        """Strip instagrapi's hidden per-request sleeps; pacing stays ours."""
        client.delay_range = CLIENT_DELAY_RANGE
        client.request_timeout = 0  # a sleep before every request, not a timeout
        client.read_timeout = READ_TIMEOUT

    def _new_client(self) -> Client:
        client = Client()
        self._tune_client(client)
        return client

    def try_restore_session(self) -> Optional[str]:
        """Silently restore the most recent cached session, if still valid.

        Returns the username on success, None otherwise. Never raises:
        restore failure just means the user signs in normally.
        """
        candidates = sorted(
            self.session_dir.glob("session-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        client = self._new_client()
        try:
            client.load_settings(candidates[0])
        except Exception as exc:
            self._log(f"Saved session unreadable ({exc}).")
            return None
        self._tune_client(client)  # load_settings restores library sleep defaults
        try:
            me = client.account_info()
        except Exception as exc:
            self._log(f"Saved session expired or invalid — please sign in. ({type(exc).__name__})")
            return None
        self.client = client
        self.username = str(getattr(me, "username", "") or "unknown")
        self._log(f"Restored saved session for @{self.username}.")
        return self.username

    def _session_file(self, username: str) -> Path:
        safe = re.sub(r"[^a-z0-9._-]", "_", username.lower())
        return self.session_dir / f"session-{safe}.json"

    def login(self, username: str, password: str, verification_code: str = "") -> None:
        if not (username or "").strip():
            raise CheckerError("Enter your Instagram username.", "auth")
        username = normalize_username(username)
        if not password:
            raise CheckerError("Please enter your password.", "auth")

        client = self._new_client()
        session_file = self._session_file(username)
        if session_file.exists():
            try:
                client.load_settings(session_file)
                self._tune_client(client)
                self._log("Loaded saved session for this account.")
            except Exception as exc:  # corrupted file etc. -> fresh login
                self._log(f"Saved session unreadable ({exc}); signing in fresh.")

        self._log(f"Signing in as @{username}…")
        try:
            client.login(username, password, verification_code=verification_code or "")
        except BadCredentials as exc:
            raise CheckerError("Incorrect username or password.", "auth") from exc
        except TwoFactorRequired as exc:
            raise CheckerError(
                "Instagram needs a 2FA / verification code. "
                "Enter it in the code field and click Sign In again.",
                "challenge",
            ) from exc
        except ChallengeError as exc:
            raise CheckerError(
                "Instagram flagged this sign-in with a security challenge. "
                "Approve the login in your Instagram app or on instagram.com, then try again.",
                "challenge",
            ) from exc
        except ClientConnectionError as exc:
            raise CheckerError(f"Network problem while contacting Instagram: {exc}", "network") from exc
        except ClientError as exc:
            text = str(exc)
            if "out of date" in text.lower():
                raise CheckerError(
                    "Instagram rejected the app sign-in ('your version of Instagram is out of date'). "
                    "This happens with some accounts. Reliable fix: use the Session ID method — "
                    "copy your sessionid cookie from a browser where you are logged into instagram.com "
                    "(click How? in the app for steps), paste it into the Session ID field, and sign in.",
                    "auth",
                ) from exc
            raise CheckerError(f"Sign-in failed: {exc}", "auth") from exc

        self.client = client
        self.username = username
        try:
            client.dump_settings(session_file)
        except Exception:
            pass
        self._log(f"Signed in as @{username}. Session saved for next time.")

    def login_with_sessionid(self, sessionid: str) -> None:
        """Sign in using a sessionid cookie copied from a logged-in browser.

        Bypasses the password login flow entirely, so it also works when
        Instagram rejects emulated app logins with its 'app out of date' error.
        """
        sessionid = (sessionid or "").strip()
        if not sessionid:
            raise CheckerError(
                "Paste a sessionid value into the Session ID field first (click How? for steps).",
                "auth",
            )
        client = self._new_client()
        self._log("Signing in with browser Session ID…")
        try:
            client.login_by_sessionid(sessionid)
        except (ClientError, ValueError, AssertionError) as exc:
            text = str(exc)
            if "user_has_logged_out" in text:
                raise CheckerError(
                    "This Session ID has ended — that session was logged out (in the "
                    "browser you copied it from, in the Instagram app, or by Instagram).\n\n"
                    "Fix: log in to instagram.com in your browser again, copy the fresh "
                    "sessionid cookie (click How? for steps), and paste the new value.",
                    "auth",
                ) from exc
            raise CheckerError(
                f"Session sign-in failed (expired or invalid Session ID?): {text}",
                "auth",
            ) from exc

        # login_by_sessionid can report success even for a dead sessionid: its
        # internal fallbacks include endpoints that don't strictly validate the
        # session. Verify with a strictly-authenticated call before claiming
        # success, otherwise every later check fails with 'session expired'.
        try:
            me = client.account_info()
        except ClientConnectionError as exc:
            raise CheckerError(
                f"Network problem while verifying the session: {exc}", "network"
            ) from exc
        except ClientError as exc:
            raise CheckerError(
                "Instagram rejected this Session ID — the session has ended or was "
                "logged out.\n\n"
                "Fix: log in to instagram.com in your browser again, copy the fresh "
                "sessionid cookie (click How? for steps), and paste the new value.",
                "auth",
            ) from exc

        self.client = client
        self.username = str(getattr(me, "username", "") or client.username or "unknown")
        try:
            client.dump_settings(self._session_file(self.username))
        except Exception:
            pass
        self._log(f"Signed in as @{self.username} via Session ID. Session saved for next time.")

    def logout(self) -> None:
        """Sign out on Instagram's side and drop the local session cache."""
        if self.client is None:
            raise CheckerError("Not signed in.", "auth")
        username = self.username
        session_file = self._session_file(username)
        try:
            self.client.logout()  # server-side: invalidates this device's session
            self._log(f"Logged out of @{username} on Instagram.")
        except Exception as exc:
            # Server call failed; still drop local state so the app is usable.
            self._log(f"Server logout failed ({type(exc).__name__}); clearing local session anyway.")
        self.client = None
        self.username = ""
        try:
            session_file.unlink(missing_ok=True)
        except OSError as exc:
            self._log(f"Could not delete cached session file: {exc}")
        self._log("Local session cleared. Sign in to check with another account.")

    # ---------------------------------------------------------------- checks

    # ------------------------------------------------------------ public mode

    def public_check_many(
        self,
        usernames: List[str],
        workers: int = 4,
        delay_range: Tuple[float, float] = (0.3, 0.8),
        on_result: Optional[Callable[[ProfileResult], None]] = None,
        stop_event: Optional[threading.Event] = None,
        batch_size: Optional[int] = None,
        pause_range: Optional[Tuple[float, float]] = None,
        on_batch: Optional[Callable[[int, int], None]] = None,
        on_cooldown: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Check usernames with NO Instagram sign-in at all.

        Uses the logged-out web-profile endpoint (public_lookup.py) instead of
        instagrapi's GraphQL lookup, which answers 429 for signed-out clients —
        that was why Public mode produced nothing but rate-limit error rows.

        Nothing about the signed-in account is touched here: no cookies, no
        session, no request budget. Only this machine's IP can face a temporary
        cool-down, which is why the same circuit breaker as session mode
        applies: RATE_LIMIT_STREAK_LIMIT refused lookups in a row halts the run
        instead of grinding through thousands of names. The same chunked
        pacing applies too, for the same reason: this IP is what gets
        refused, and a burst is what earns the refusal.

        Limits, reported honestly rather than guessed: no Last Post date, and
        DP stays "unknown" for private profiles that withhold their avatar.
        Scraping a Following/Followers list still requires a sign-in.

        Two signed-out transports, chosen once by the preflight: Instagram's
        JSON endpoint when it answers, otherwise the logged-out profile page
        (same numbers; ~125 KB per account instead of ~2 KB). See
        public_lookup's module docstring.
        """
        on_result = on_result or (lambda _r: None)
        stop_event = stop_event or threading.Event()
        workers = max(1, min(workers, len(usernames) or 1))
        self.halt_reason = ""  # a fresh run clears any previous halt
        lookup = PublicLookup()

        # Preflight: one probe for a username that cannot exist. Without this,
        # a refused endpoint turns a 3,000-name list into 3,000 identical error
        # rows before the user learns anything. Fail fast, with the real reason.
        available, detail = probe_public_access(lookup)
        if not available:
            # The detail string carries the generic rate-limit wording ("wait a
            # few minutes"). Repeated in the log it reads as advice, and when
            # the refusal is this total — identical for a real account and for
            # one that cannot exist — waiting is exactly what will not help.
            # State the fact here; the dialog that follows gives the real plan.
            self._log(
                f"Public lookup preflight failed — Instagram is refusing signed-out "
                f"requests from this machine ({detail}). See the dialog for what to do."
            )
            raise CheckerError(PUBLIC_UNAVAILABLE_MESSAGE, "public_unavailable")
        if getattr(lookup, "transport", "api") == "html":
            self._log(
                "Public lookup preflight passed — Instagram's JSON endpoint is refusing "
                "signed-out requests from this machine, so this run reads the public "
                "profile page instead (~125 KB per account, no account budget spent)."
            )
        else:
            self._log("Public lookup preflight passed — signed-out lookups are available.")
        throttle_lock = threading.Lock()
        throttled = [0]

        def on_throttle() -> None:
            with throttle_lock:
                throttled[0] += 1
                count = throttled[0]
            if count >= RATE_LIMIT_STREAK_LIMIT and not self.halt_reason:
                self.halt_reason = PUBLIC_THROTTLE_HALT_MESSAGE
                self._log(
                    f"Instagram refused {count} public lookups in a row — halting the "
                    "run instead of retrying through the throttling."
                )
                stop_event.set()

        def task(name: str) -> Optional[ProfileResult]:
            if stop_event.is_set():
                return None
            time.sleep(random.uniform(*delay_range))  # stagger requests
            try:
                username = normalize_username(name)
            except CheckerError as exc:
                shown = (name or "").strip()  # keep a visible label for "@@" style junk
                return ProfileResult(
                    username=shown.lstrip("@").lower() or shown,
                    status="invalid", error=exc.message, checked_at=_now(),
                )
            attempt = 0
            while True:
                attempt += 1
                try:
                    profile = lookup.fetch(username)
                    # A withheld avatar means "unknown", not "no DP" — private
                    # profiles commonly answer without one.
                    return ProfileResult(
                        username=username,
                        status="ok",
                        alive=True,
                        dp_present=None if profile.avatar_withheld
                        else dp_present_from_urls(profile.profile_pic_url),
                        posts=profile.posts,
                        followers=profile.followers,
                        following=profile.following,
                        is_private=profile.is_private,
                        is_verified=profile.is_verified,
                        full_name=profile.full_name,
                        profile_pic_url=profile.profile_pic_url,
                        checked_at=_now(),
                    )
                except PublicLookupError as exc:
                    if exc.kind == "not_found":
                        return ProfileResult(
                            username=username, status="not_found", alive=False,
                            error=exc.message, checked_at=_now(),
                        )
                    if exc.kind == "rate_limited":
                        on_throttle()
                        if attempt > PUBLIC_RETRIES or stop_event.is_set():
                            return ProfileResult(
                                username=username, status="error", alive=None,
                                error=exc.message, checked_at=_now(),
                            )
                        # One visible, interruptible wait — then the streak
                        # counter decides whether the run keeps going.
                        self._interruptible_sleep(5 * attempt, stop_event)
                        continue
                    # 401/403 means the endpoint wants a sign-in now; retrying
                    # cannot fix that and is what earns an IP-level block.
                    if attempt > PUBLIC_RETRIES or stop_event.is_set():
                        return ProfileResult(
                            username=username, status="error", alive=None,
                            error=exc.message, checked_at=_now(),
                        )
                    self._interruptible_sleep(2 * attempt, stop_event)
                except Exception as exc:  # never lose a row to an unexpected error
                    return ProfileResult(
                        username=username, status="error", alive=None,
                        error=f"Public request failed: {exc}", checked_at=_now(),
                    )

        def run_batch(batch: List[str]) -> None:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(task, name) for name in batch]
                for future in as_completed(futures):
                    result = future.result()
                    if result is not None:
                        on_result(result)
                        if result.status != "error":
                            with throttle_lock:
                                throttled[0] = 0  # a clean lookup proves it is answering

        self._run_in_batches(
            run_batch, usernames, stop_event, on_batch=on_batch,
            on_cooldown=on_cooldown, batch_size=batch_size, pause_range=pause_range,
            subject="names",
        )

    def clone_client(self) -> Optional[Client]:
        """An independent Client sharing this session, safe for one worker thread.

        instagrapi's Client is not thread-safe (shared cookie/connection state),
        so parallel checking clones the saved session per worker. Returns None
        when no saved session exists — callers then fall back to the primary
        client and single-worker mode.
        """
        if self.client is None:
            raise CheckerError("Not signed in.", "auth")
        session_file = self._session_file(self.username)
        if not session_file.exists():
            return None
        clone = self._new_client()
        try:
            clone.load_settings(session_file)
        except Exception as exc:
            self._log(f"Could not clone session for a parallel worker ({exc}).")
            return None
        self._tune_client(clone)  # load_settings restores library sleep defaults
        return clone

    @staticmethod
    def _interruptible_sleep(seconds: float, stop_event: Optional[threading.Event]) -> None:
        """Sleep that ends the moment stop_event is set (0.25s granularity)."""
        if stop_event is None:
            time.sleep(seconds)
            return
        end = time.time() + seconds
        while not stop_event.is_set() and time.time() < end:
            time.sleep(min(0.25, max(0.0, end - time.time())))

    def fetch_profile(
        self,
        raw_username: str,
        retries: int = 2,
        client: Optional[Client] = None,
        with_activity: bool = False,
        stop_event: Optional[threading.Event] = None,
        on_throttle: Optional[Callable[[], None]] = None,
    ) -> ProfileResult:
        """Look up one account. Never raises for a bad username — returns a result.

        on_throttle fires on every rate-limit hit (before the retry sleep) so
        callers can run a circuit breaker. A security challenge raises
        CheckerError(kind="challenge") instead of returning a row: Instagram is
        challenging the session, so continuing to hammer it is what gets the
        account blocked.
        """
        try:
            username = normalize_username(raw_username)
        except CheckerError as exc:
            # Show the text the user actually typed when it normalizes to
            # nothing ("@@", "..."): a blank username cell in an invalid row
            # tells them nothing about which entry to fix.
            shown = (raw_username or "").strip()
            return ProfileResult(
                username=shown.lstrip("@").lower() or shown,
                status="invalid",
                error=exc.message,
                checked_at=_now(),
            )
        if not self.signed_in or self.client is None:
            raise CheckerError("Not signed in.", "auth")
        cl = client or self.client

        attempt = 0
        while True:
            attempt += 1
            if stop_event is not None and stop_event.is_set():
                raise CheckerError("Stopped by user.", "stopped")
            try:
                info = cl.user_info_by_username(username, use_cache=False)
                result = self._to_result(info, username)
                if with_activity:
                    self._enrich_with_activity(cl, result, pk=str(getattr(info, "pk", "") or ""))
                return result
            except (UserNotFound, ClientNotFoundError) as exc:
                return ProfileResult(
                    username=username,
                    status="not_found",
                    alive=False,  # Instagram reports no such account: deleted, deactivated, banned, or never existed
                    error="No Instagram account with this username.",
                    checked_at=_now(),
                )
            except (RateLimitError, PleaseWaitFewMinutes, ClientThrottledError) as exc:
                if on_throttle is not None:
                    on_throttle()  # every hit counts toward the circuit breaker
                if attempt > retries:
                    return ProfileResult(
                        username=username,
                        status="error",
                        error="Instagram rate limit reached — wait a few minutes, then retry.",
                        checked_at=_now(),
                    )
                wait = 20 * attempt
                self._log(f"@{username}: rate limited, waiting {wait}s…")
                self._interruptible_sleep(wait, stop_event)
            except LoginRequired as exc:
                raise CheckerError("Your session expired. Please sign in again.", "auth") from exc
            except ChallengeError as exc:
                raise CheckerError(
                    "Instagram is asking for a security challenge — check the Instagram app, "
                    "clear the prompt, sign in again here, then retry.",
                    "challenge",
                ) from exc
            except ClientConnectionError as exc:
                if attempt > retries:
                    return ProfileResult(
                        username=username,
                        status="error",
                        error=f"Network error: {exc}",
                        checked_at=_now(),
                    )
                self._log(f"@{username}: network hiccup, retrying…")
                self._interruptible_sleep(3 * attempt, stop_event)
            except ClientError as exc:
                if attempt > retries:
                    return ProfileResult(
                        username=username,
                        status="error",
                        error=str(exc) or "Instagram request failed.",
                        checked_at=_now(),
                    )
                self._interruptible_sleep(2 * attempt, stop_event)

    def check_many(
        self,
        usernames: List[str],
        workers: int = 3,
        delay_range: Tuple[float, float] = (0.4, 0.9),
        on_result: Optional[Callable[[ProfileResult], None]] = None,
        stop_event: Optional[threading.Event] = None,
        with_activity: bool = False,
    ) -> None:
        """Check many usernames in parallel, calling on_result as each finishes.

        Results arrive out of order. Raises the first auth-level CheckerError
        (session expired, challenge) after stopping the remaining work.
        """
        on_result = on_result or (lambda _r: None)
        stop_event = stop_event or threading.Event()
        workers = max(1, min(workers, len(usernames) or 1))

        if workers > 1 and self.clone_client() is None:
            workers = 1  # no saved session to clone: stay single-threaded (Client is not thread-safe)

        local = threading.local()
        fatal: list[CheckerError] = []
        self.halt_reason = ""  # a fresh run clears any previous halt
        throttle_lock = threading.Lock()
        throttled = [0]  # rate-limit hits since the last successful lookup

        def on_throttle() -> None:
            """Circuit breaker: too many throttled lookups in a row, no success
            in between, means Instagram is refusing this session. Halt the run.
            """
            with throttle_lock:
                throttled[0] += 1
                count = throttled[0]
            if count >= RATE_LIMIT_STREAK_LIMIT and not self.halt_reason:
                self.halt_reason = THROTTLE_HALT_MESSAGE
                self._log(
                    f"Instagram refused {count} lookups in a row — halting the run "
                    "instead of retrying through the throttling."
                )
                stop_event.set()

        def task(name: str) -> Optional[ProfileResult]:
            if stop_event.is_set():
                return None
            cl = getattr(local, "client", None)
            if cl is None:
                cl = self.clone_client() if workers > 1 else self.client
                if cl is None:  # no saved session: degrade to primary client
                    cl = self.client
                local.client = cl
            if stop_event.is_set():
                return None
            time.sleep(random.uniform(*delay_range))  # stagger requests
            try:
                result = self.fetch_profile(name, client=cl, with_activity=with_activity,
                                            stop_event=stop_event, on_throttle=on_throttle)
            except CheckerError as exc:
                if exc.kind == "stopped":
                    return None
                if exc.kind in ("auth", "challenge", "network"):
                    fatal.append(exc)
                    stop_event.set()
                    return None
                return ProfileResult(username=name, status="error", error=exc.message, checked_at=_now())
            if result is not None and result.status != "error":
                # A clean lookup proves Instagram is answering: clear the streak.
                with throttle_lock:
                    throttled[0] = 0
            return result

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(task, name) for name in usernames]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    on_result(result)

        if fatal:
            raise fatal[0]

    def check_many_batched(
        self,
        usernames: List[str],
        workers: int = 4,
        delay_range: Tuple[float, float] = (0.25, 0.6),
        on_result: Optional[Callable[[ProfileResult], None]] = None,
        stop_event: Optional[threading.Event] = None,
        with_activity: bool = False,
        batch_size: Optional[int] = None,
        pause_range: Optional[Tuple[float, float]] = None,
        on_batch: Optional[Callable[[int, int], None]] = None,
        on_cooldown: Optional[Callable[[float], None]] = None,
    ) -> None:
        """check_many in chunks with quiet cool-downs — the big-list mode.

        Thousands of back-to-back profile lookups are exactly the traffic
        pattern Instagram's limiter flags. Splitting the run into batches of
        ~300 with a 20–40s pause between them keeps the same total request
        count while looking far less like a burst. Results stream through
        on_result exactly as in check_many; on_batch(index, total) fires as
        each batch starts; on_cooldown(seconds_left) fires each second during
        the between-batch pause; Stop works everywhere (it cuts the cool-down
        immediately and ends after the in-flight batch).
        """
        stop_event = stop_event or threading.Event()

        def step(batch: List[str]) -> None:
            self.check_many(
                batch,
                workers=workers,
                delay_range=delay_range,
                on_result=on_result,
                stop_event=stop_event,
                with_activity=with_activity,
            )

        self._run_in_batches(
            step, usernames, stop_event, on_batch=on_batch,
            on_cooldown=on_cooldown, batch_size=batch_size, pause_range=pause_range,
        )

    def _run_in_batches(
        self,
        step: Callable[[List[str]], None],
        usernames: List[str],
        stop_event: threading.Event,
        on_batch: Optional[Callable[[int, int], None]] = None,
        on_cooldown: Optional[Callable[[float], None]] = None,
        batch_size: Optional[int] = None,
        pause_range: Optional[Tuple[float, float]] = None,
        subject: str = "accounts",
    ) -> None:
        """Run `step` over `usernames` in chunks with quiet cool-downs between.

        Shared by session and public mode on purpose. They are two different
        lookup engines, but to Instagram they are the same traffic: one IP
        asking about thousands of usernames back to back. Measured on the
        public engine, "lightning" with no ceiling fired 320 requests in 3.6s
        (88 req/s) — a 3,000-name list would finish in well under a minute of
        unbroken requests, which is exactly the shape that earns an IP-level
        refusal. Pacing that only one mode has is a trap for the user who
        picked the "fast" mode for a big list.
        """
        batch_size = batch_size or BATCH_SIZE
        pause_range = pause_range or BATCH_PAUSE_RANGE
        batches = [usernames[i:i + batch_size] for i in range(0, len(usernames), batch_size)]
        for index, batch in enumerate(batches, 1):
            if stop_event.is_set():
                return
            self._log(f"Batch {index}/{len(batches)} — checking {len(batch)} {subject}…")
            if on_batch:
                try:
                    on_batch(index, len(batches))
                except Exception:
                    pass
            step(batch)
            if index < len(batches) and not stop_event.is_set():
                pause = random.uniform(*pause_range)
                self._log(
                    f"Batch {index}/{len(batches)} done — cooling down {pause:.0f}s "
                    "to stay under the radar (Stop skips the wait)."
                )
                end = time.time() + pause
                while not stop_event.is_set() and time.time() < end:
                    if on_cooldown:
                        try:
                            on_cooldown(max(0.0, end - time.time()))
                        except Exception:
                            pass
                    self._interruptible_sleep(1.0, stop_event)
                if on_cooldown:
                    try:
                        on_cooldown(0.0)
                    except Exception:
                        pass

    def _enrich_with_activity(self, cl: Client, result: ProfileResult, pk: Optional[str] = None) -> None:
        """Fill in last_post_at from the account's most recent post (activity context).

        Best-effort: any failure here leaves the field blank instead of failing
        the whole check. Private accounts we don't follow yield an empty media
        list, so the date stays blank rather than guessed.
        """
        try:
            if result.posts is not None and result.posts <= 0:
                return  # never posted; skip the extra request
            user_id = pk or cl.user_id_from_username(result.username)
            medias = cl.user_medias(user_id, amount=1, sleep=0)
            latest = medias[0].taken_at if medias else None
            result.last_post_at = latest.strftime("%Y-%m-%d") if latest else ""
        except (RateLimitError, PleaseWaitFewMinutes, ClientThrottledError):
            self._log(f"@{result.username}: skipped activity check (rate limited).")
        except Exception as exc:
            self._log(f"@{result.username}: activity check unavailable ({exc}).")

    @staticmethod
    def _to_result(info, username: str) -> ProfileResult:
        pic = str(getattr(info, "profile_pic_url", "") or "")
        pic_hd = str(getattr(info, "profile_pic_url_hd", "") or "")
        is_private = bool(getattr(info, "is_private", False))
        # A private profile commonly withholds its avatar from an account that
        # does not follow it, answering with an empty URL instead of the grey
        # placeholder. No URL here means "we were not shown", not "no photo" —
        # and dp_present=False is not a neutral value: verdict_for turns it
        # into "suspicious"/"likely-fake", so guessing here brands real private
        # accounts as fake on the strength of data Instagram refused to send.
        # Public mode already works this way; this is session mode, the default.
        dp_present: Optional[bool] = None
        if pic or pic_hd or not is_private:
            dp_present = dp_present_from_urls(pic, pic_hd)
        return ProfileResult(
            username=username,
            status="ok",
            alive=True,  # the profile resolved, so the account is accessible
            dp_present=dp_present,
            posts=int(getattr(info, "media_count", 0) or 0),
            followers=int(getattr(info, "follower_count", 0) or 0),
            following=int(getattr(info, "following_count", 0) or 0),
            is_private=is_private,
            is_verified=bool(getattr(info, "is_verified", False)),
            full_name=str(getattr(info, "full_name", "") or ""),
            profile_pic_url=pic,
            checked_at=_now(),
        )

    def _log(self, message: str) -> None:
        """Logging must never break a run.

        on_log callbacks often touch Tk from worker threads (which Tkinter
        forbids), so any callback failure is swallowed — losing a log line is
        always better than losing a check.
        """
        try:
            self.on_log(message)
        except Exception:
            pass
