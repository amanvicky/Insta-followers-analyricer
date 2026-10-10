"""Follower-list scraping for the Instagram Profile Checker.

One job: given a target username, collect the usernames of that account's
followers or following. Profile *analysis* lives in checker.py — this module
only collects usernames.

Pagination is driven HERE, page by page, via instagrapi's *_v1_chunk calls.
instagrapi's ready-made iterators (iter_user_followers_v1 / iter_user_following_v1)
were deliberately dropped: they hide opaque retry loops (a blind 60-second sleep
on request timeout), an internal GraphQL fallback, and zero stop awareness —
the bug behind a Stop button that kept saying "Stopping…" for minutes. Fetching
chunks ourselves gives a stop check between every page, interruptible waits,
and one honest retry before a clear error.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from instagrapi.exceptions import (
    ClientError,
    ClientNotFoundError,
    ClientThrottledError,
    FeedbackRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    UserNotFound,
)

from checker import CheckerError, normalize_username

# Instagram's mobile API returns up to 200 users per page.
PAGE_SIZE = 200

# When Instagram rate-limits a page fetch: one interruptible backoff, then a
# single retry; a second throttling ends the scrape with a clear error.
RATE_LIMIT_WAIT_SECONDS = 20

# Politeness pause between pages — interruptible, so Stop stays instant.
PAGE_PAUSE_SECONDS = 0.2
SCRAPE_PAGE_PAUSES = {
    "safe": 0.5,
    "fast": PAGE_PAUSE_SECONDS,
    "turbo": 0.1,
    "lightning": 0.0,
}

# Shown when Instagram slaps a feedback_required action-block on the account
# mid-scrape. It is a temporary block on scraping activity (hours, sometimes
# up to ~48h) — not something the app can lift, so we explain and keep names.
FEEDBACK_REQUIRED_NOTE = (
    "Instagram paused this account's scraping (feedback_required action block).\n\n"
    "The {target}'s {list_type} list reached {count:,} names — those are kept in "
    "the username box, so you can still run checks on them.\n\n"
    "What to do:\n"
    "1. Wait it out — usually a few hours (up to ~48h). Do not retry sooner; "
    "repeated attempts extend the block.\n"
    "2. Next time, use the safe speed preset and avoid scraping the same target "
    "repeatedly.\n\n"
    "Note: if Instagram shows a \"Tell us\" / \"This was me\" challenge in the app "
    "or a login email, confirming it can shorten the block."
)

# Friendly errors for a feedback_required action-block (raised as CheckerError
# kind="feedback"). Instagram normally raises the dedicated FeedbackRequired
# class, but the block has also been observed arriving as a plain ClientError
# carrying the raw "feedback_required: ..." text — hence the message sniff in
# is_feedback_block().
FEEDBACK_LOOKUP_ERROR = (
    "Instagram blocked this account's activity (feedback_required) before the "
    "scrape could start. Wait a few hours, then retry — repeated attempts "
    "extend the block."
)
FEEDBACK_PAGE0_ERROR = (
    "Instagram blocked this account's scraping (feedback_required) before any "
    "accounts were scraped. Wait a few hours, then retry — repeated attempts "
    "extend the block."
)

# Shown when a rate-limited scrape gave up mid-way but names DID land. Unlike a
# feedback_required block this one is soft: wait a little and continue. The
# saved cursor is what makes the retry cheap — no page is ever fetched twice.
RATE_LIMIT_PARTIAL_NOTE = (
    "Instagram rate-limited the scrape of {target}'s {list_type} after {count:,} names.\n\n"
    "Those names are kept in the username box, and the position in the list was saved. "
    "Wait at least 10-15 minutes, then click Scrape again: the app offers to resume "
    "from where it stopped instead of re-fetching the whole list (far fewer requests, "
    "much lower block risk)."
)


@dataclass
class ScrapeProgress:
    """One pagination step of a follower scrape."""

    scraped: int
    total: Optional[int]  # Instagram often doesn't disclose this upfront
    target: str
    list_type: str = "followers"  # "followers" | "following" — shown in the UI
    done: bool = False  # True on the final page


@dataclass
class ScrapeResume:
    """Where an interrupted scrape left off (saved/restored by app.py)."""

    max_id: str = ""  # page cursor to continue from ("" = start at page 1)
    names: Optional[List[str]] = None  # usernames already collected


@dataclass
class ScrapeOutcome:
    """Result of a scrape: the names plus whether the user stopped it.

    `cursor` is the page cursor to resume from — non-empty means more pages
    remain (Stop, a rate limit, or a feedback_required block ended the run
    early). On a complete scrape it is "" and the list is whole.
    """

    names: List[str]
    stopped: bool = False
    note: str = ""  # user-facing explanation when names were kept early
    cursor: str = ""  # non-empty: resume from here, these pages are done
    blocked: bool = False  # True = feedback_required (harder block than rate limit)


def _stop_wait(seconds: float, stop_event: threading.Event) -> bool:
    """Sleep that ends the moment stop_event is set. Returns True if stopped."""
    end = time.time() + seconds
    while not stop_event.is_set() and time.time() < end:
        time.sleep(min(0.25, max(0.0, end - time.time())))
    return stop_event.is_set()


def is_feedback_block(exc: BaseException) -> bool:
    """True when `exc` represents Instagram's feedback_required action-block.

    In the wild the block usually raises the dedicated FeedbackRequired class,
    but it has also been observed arriving as a plain ClientError carrying the
    raw "feedback_required: ..." text, so detect by message as well.
    """
    return isinstance(exc, FeedbackRequired) or "feedback_required" in str(exc).lower()


def scrape_user_list(
    cl,
    target: str,
    list_type: str = "followers",
    on_progress: Optional[Callable[[ScrapeProgress], None]] = None,
    stop_event: Optional[threading.Event] = None,
    max_followers: int = 0,
    resume: Optional[ScrapeResume] = None,
    page_pause_seconds: float = PAGE_PAUSE_SECONDS,
) -> ScrapeOutcome:
    """Return the follower OR following usernames of `target` (session client).

    - list_type: "followers" (who follows the target) or "following" (who the
      target follows).
    - Returns a ScrapeOutcome: the names plus `stopped` (True when the scrape
      ended before the last page — user Stop, rate limit, or a block) and
      `cursor` (where the next attempt continues from).
    - Fetches page by page (200/page) so progress is live and Stop lands
      between pages; rate-limit waits are interruptible too.
        - page_pause_seconds controls the extra wait between pages; the Lightning
            preset uses zero, while rate-limit backoff remains in effect.
    - max_followers caps the scrape (0 = all).
    - resume: continue a previous attempt — `resume.names` are carried over
      (deduped) and pagination restarts at `resume.max_id`, so a long list can
      be collected across several attempts without re-fetching one page.
    Raises CheckerError for user-facing failures (target missing, rate-limited
    before any page landed, auth, feedback_required block before any page).
    """
    on_progress = on_progress or (lambda _p: None)
    stop_event = stop_event or threading.Event()
    resume = resume or ScrapeResume()
    list_type = (list_type or "followers").strip().lower()
    if list_type not in ("followers", "following"):
        raise CheckerError(f"Unknown list type '{list_type}'.", "invalid")
    try:
        target_clean = normalize_username(target)
    except CheckerError as exc:
        raise CheckerError(f"Target: {exc.message}", "invalid") from exc

    try:
        info = cl.user_info_by_username(target_clean)
        user_id = str(getattr(info, "pk", "") or "")
    except (UserNotFound, ClientNotFoundError):
        raise CheckerError(
            f"Target @{target_clean} does not exist (deleted, deactivated, banned, or typo).",
            "invalid",
        ) from None
    except (RateLimitError, PleaseWaitFewMinutes, ClientThrottledError) as exc:
        raise CheckerError(
            f"Instagram rate-limited the lookup of @{target_clean} — wait a few minutes and retry.",
            "rate_limit",
        ) from exc
    except FeedbackRequired as exc:
        raise CheckerError(FEEDBACK_LOOKUP_ERROR, "feedback") from exc
    except ClientError as exc:
        if is_feedback_block(exc):
            raise CheckerError(FEEDBACK_LOOKUP_ERROR, "feedback") from exc
        raise CheckerError(f"Could not resolve @{target_clean}: {exc}", "error") from exc
    if not user_id:
        raise CheckerError(f"Could not resolve @{target_clean}.", "error")

    # The resolution request already carries the totals — surface the relevant
    # one immediately, before the first page is even fetched (no extra request).
    total = int(getattr(info, "following_count" if list_type == "following" else "follower_count", 0) or 0)

    def notify(scraped: int, done: bool = False) -> None:
        on_progress(ScrapeProgress(scraped=scraped, total=total, target=target_clean,
                                   list_type=list_type, done=done))

    chunk_fetch = cl.user_following_v1_chunk if list_type == "following" else cl.user_followers_v1_chunk
    names: List[str] = []
    seen: set = set()
    for name in resume.names or []:  # carry over what a previous attempt landed
        clean = (name or "").strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            names.append(clean)
    next_max_id = resume.max_id or ""  # the whole point: skip the pages already done

    notify(len(names))  # resumed names show up before the first request

    def handle_block(exc: Exception) -> ScrapeOutcome:
        """Mid-scrape action block. With names already scraped, hand back
        what landed so far (plus the resume cursor) — `note` is consumed by
        app.py, which warns instead of auto-starting the audit. With zero
        names (blocked on page 0) there is nothing worth keeping: friendly
        error instead.
        """
        if not names:
            raise CheckerError(FEEDBACK_PAGE0_ERROR, "feedback") from exc
        return ScrapeOutcome(
            names=names, stopped=True,
            note=_feedback_note(target_clean, list_type, len(names)),
            cursor=next_max_id, blocked=True,
        )

    while True:
        if stop_event.is_set():
            return ScrapeOutcome(names=names, stopped=True, cursor=next_max_id)
        try:
            users, next_max_id = chunk_fetch(user_id, max_amount=PAGE_SIZE, max_id=next_max_id)
        except (RateLimitError, PleaseWaitFewMinutes, ClientThrottledError):
            # Instagram hard-enforces list pagination: back off once, interruptibly.
            notify(len(names))
            if _stop_wait(RATE_LIMIT_WAIT_SECONDS, stop_event):
                return ScrapeOutcome(names=names, stopped=True, cursor=next_max_id)
            try:
                users, next_max_id = chunk_fetch(user_id, max_amount=PAGE_SIZE, max_id=next_max_id)
            except (RateLimitError, PleaseWaitFewMinutes, ClientThrottledError) as exc:
                # Names already landed: losing them and re-fetching from page 1
                # is what turns one throttle into a block. Keep them, save the
                # cursor, let the user continue later.
                if not names:
                    raise CheckerError(
                        f"Instagram rate-limited the {list_type} scrape before any accounts "
                        f"were scraped. Wait a few minutes and retry. ({exc})",
                        "rate_limit",
                    ) from exc
                return ScrapeOutcome(
                    names=names, stopped=True,
                    note=RATE_LIMIT_PARTIAL_NOTE.format(
                        target=f"@{target_clean}", list_type=list_type, count=len(names)),
                    cursor=next_max_id,
                )
        except FeedbackRequired as exc:
            return handle_block(exc)
        except ClientError as exc:
            if is_feedback_block(exc):
                return handle_block(exc)
            raise CheckerError(f"{list_type.capitalize()} scrape failed: {exc}", "error") from exc

        for user in users:
            name = (getattr(user, "username", "") or "").strip().lower()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        notify(len(names))

        if max_followers and len(names) >= max_followers:
            names = names[:max_followers]
            break
        if not next_max_id or not users:
            next_max_id = ""  # Instagram sent its last page: nothing left to resume
            break
        if _stop_wait(max(0.0, page_pause_seconds), stop_event):
            return ScrapeOutcome(names=names, stopped=True, cursor=next_max_id)

    notify(len(names), done=True)
    return ScrapeOutcome(names=names, stopped=False)


def _feedback_note(target: str, list_type: str, count: int) -> str:
    return FEEDBACK_REQUIRED_NOTE.format(target=f"@{target}", list_type=list_type, count=count)
