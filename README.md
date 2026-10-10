# Instagram Profile Checker (Windows)

A small desktop app that signs in to **your own** Instagram account, takes a list of
usernames, and reports for each one:

- **Username** (as checked)
- **Alive?** — is the account accessible? `No` means Instagram reports no such account
  (deleted, deactivated, banned, or never existed). Every row that resolves a profile is alive.
- **Verdict** — automatic triage from the fetched signals, with the reason in the CSV:
  `likely-fake` (no DP and 0 posts) · `suspicious` (following heavily exceeds followers,
  or no DP with ~no posts) · `inactive` (0 posts, or last post older than 90 days) ·
  `dead` (username doesn't resolve) · `fine`. Rows are colored by verdict; failed checks
  get no verdict rather than a guess.
- **DP present?** — has the account uploaded its own profile picture? Blank
  means *unknown*, not "no": Instagram withholds the avatar of a private profile
  from an account that does not follow it, and reading that withheld data as
  "no photo" would brand real accounts as fake in the verdict column.
- **Last Post** — date of the most recent post, when visible (activity context for alive accounts;
  blank for private accounts you don't follow or accounts with zero posts)
- **Number of posts**
- **Followers**
- **Following**
- plus Private / Verified flags, status, and error details

Results can be exported to CSV (opens in Excel).

## Quick start

1. Install Python 3.11+ from [python.org](https://www.python.org/downloads/) —
   tick **"Add Python to PATH"** during setup.
2. Double-click **`run.bat`**.
   The first run creates a local `.venv`, installs the required libraries, and opens the app.
   (Double-clicking it again while the app is open is safe: the second copy says so
   and closes, rather than quietly overwriting your saved results.)
3. Sign in with your Instagram username and password, paste usernames (one per line or
   comma-separated), and click **Start Checking**.

   **If sign-in fails with *"Your version of Instagram is out of date"*** (Instagram
   rejects emulated app logins for some accounts — a known upstream issue), use the
   **Session ID** method instead: click **How?** in the app and follow the steps
   (copy the `sessionid` cookie from a browser where you're logged into instagram.com).
4. Click **Export results to CSV…** when finished.

### Logging out

The **Log out** button (next to the sign-in status) signs the account out on Instagram's
side **and** deletes the cached session file on this PC — so the next launch asks for
sign-in again. It's refused while a check or scrape is running, and asks for
confirmation first.

### Moving this folder to another PC

Copy the whole folder, but **do not copy the `.venv` folder** — a virtual environment
records the absolute path of the Python that created it, so one copied from another
computer points at an interpreter that is not there and you get *"Python venv
launcher is sorry to say… did not find executable at …"*.

You do not have to delete it yourself: **`run.bat` detects a venv that does not work
on this PC, throws it away and builds a fresh one** (it needs internet access and
Python 3.11+ installed). To force that by hand, delete the `.venv` folder and
double-click `run.bat`. `build_exe.bat` does the same check before packaging.

### Build a standalone .exe (optional)

Double-click **`build_exe.bat`**. It prepares the virtual environment and installs the
build tools automatically. The finished app lands at
`dist\InstagramProfileChecker.exe` — you can copy that single file to any Windows PC.

The exe is a frozen snapshot: it does **not** pick up later source changes, and it
carries no auto-update. Re-run `build_exe.bat` after editing anything in
`insta_checker\`, and check the exe's date in Explorer before shipping it to another
PC. A stale exe looks identical when it launches, so it fails later and somewhere else.

## Notes & limits

- **Credentials** never leave your PC; they are used only to talk to Instagram. A session
  file is cached in `%USERPROFILE%\.insta_checker` so you don't have to sign in every time.
- **2FA / verification codes:** if Instagram asks for a code, enter it in the
  "2FA code" field and click **Sign In** again.
- **Rate limits:** Instagram throttles rapid lookups. Isolated throttles are waited out,
  but when several lookups in a row are refused with nothing succeeding in between, the
  run **halts** instead of retrying through it — that sustained retry traffic is what
  escalates to an action block. Wait 15–60 minutes, then press Start again with
  "Skip rows already checked OK" ticked (the app ticks it for you after a halt) to
  continue from the checkpoint.

### Follower audit

Type a **target username**, pick **Following** (who the target follows) or **Followers**
(who follows the target), and click **Scrape followers →**. The app pulls that list into
the username box (200 per page, live progress, Stop works between pages). Because a
scrape *replaces* whatever is in that box — and overwrites the saved copy of it, with no
undo — the app asks first if you have a list in there, and cancelling costs no requests.
From there, either:

- **Extract usernames only** — tick the checkbox first and you get just the follower
  list in the box (copy it, save it, or run checks later); no analysis is performed, or
- **Full audit** (default) — every follower is checked automatically through the normal
  pipeline: alive/dead, DP, posts, followers, following, and the verdict column.

- **Pasting usernames:** separate them with line breaks, commas, semicolons or tabs.
  A space is *not* a separator — `bad name` stays one row marked invalid instead of
  silently becoming two real lookups on two wrong accounts.
- **Session mode required** — follower lists only come from the logged-in API; the
  button is disabled without sign-in and the mode is forced to Session for the audit.
- **Volume = risk.** Auditing a 5,000-follower account means 5,000 profile lookups on
  your account. Keep speed at `safe` or `fast`, and expect big scrapes to take a while.
- If Instagram rate-limits the scrape mid-way, the app keeps every name already
  retrieved and **remembers the page it stopped on**. Scrape again later and it offers to
  resume from there: the pages already fetched are never requested a second time, so
  finishing a big list costs a fraction of the requests (and far less block risk). The
  same applies after a Stop — a partial scrape is never thrown away.
- **"feedback_required" block:** if Instagram pauses the account's scraping activity
  mid-scrape, the app keeps every name already retrieved (into the username box) and
  explains what to do. The block is temporary — usually a few hours, up to ~48h.
  Waiting is the only fix; retrying sooner can extend it. Use the `safe` speed preset
  and avoid scraping the same target repeatedly to prevent it.
- The app remembers a recent block (even across restarts) and asks for confirmation
  before you scrape again too soon — confirming once silences the reminder.
- **Pacing:** scrape attempts are spaced automatically — 60s between clicks normally,
  10 minutes while a block is active. Rapid retries are what Instagram punishes, so
  the app refuses to hammer the button on your behalf.
- **Nothing is lost on close:** the username box and every checked row (including
  results from a run still in progress) are saved the moment you close the window —
  a restart puts both back.

### Speed & modes

**Mode** — two ways to check:

- **Public (safest & fast)** — no Instagram sign-in required. It reads the
  logged-out profile data Instagram's own web frontend serves, so **no account,
  session or cookie is involved**: your account cannot be throttled or banned by
  this mode, only your IP can face a temporary cool-down. There are two
  signed-out paths and the app picks one per run:
  1. the JSON endpoint behind Instagram's web app (`web_profile_info`) — small
     (~2 KB per account) and exact;
  2. the **logged-out profile page** (`instagram.com/<username>/`) — used when
     Instagram is refusing the JSON endpoint from your machine, which is the
     common case on home/campus IPs. Same numbers from the page's embedded
     profile payload.
  - **Honest limits:** no Last Post date (that needs a session). The page path
    reads ~750 KB per account (~125 KB over the wire), so a 1,000-name public
    run moves roughly 125 MB — public mode is safe for your account, not free on
    your connection. Post counts come from the page's link preview, which
    Instagram abbreviates above ~10,000 — those rows are left blank rather than
    showing a rounded number. Scraping a Following/Followers list still needs a
    sign-in: Instagram only serves list pagination to a signed-in session.
  - **Preflight:** a public run starts by probing the JSON endpoint with a
    username that cannot exist, and — if that is refused — by reading one known
    real account's page to prove the page path serves data here. Only if both
    fail does the run stop with an explanation, before anything from your list
    is sent. Nothing is checked, no account is involved, and trying is always
    safe.
  - **For big or repeated checks, use a dedicated throwaway Instagram account**
    dedicated to checking — never the account you use daily.
- **Session (safest data)** — signs in and gets full data for every account you can
  see, including private ones you follow, plus the optional Last Post date. This
  spends your account's rate budget.

**Speed presets** (session mode; public mode adds one more worker automatically):

| Preset | Parallel workers | Pause between requests | Risk to your account |
|--------|------------------|------------------------|----------------------|
| `safe` | 2 | 0.8–1.5 s | Lowest |
| `fast` (default) | 4 | 0.25–0.6 s | Moderate |
| `turbo` | 6 | 0.15–0.35 s | Highest |
| `lightning` | 10 | 0.05–0.15 s | Extreme — use at your own risk |

All session workers share your one Instagram session, so faster presets raise the
chance of temporary action-blocking on the signed-in account. The app also strips
the Instagram library's hidden per-request sleeps (2–4 s per account), so every
preset is now several times faster than before at the same request count. For most
personal lists, `fast` checks ~100 accounts in well under a minute.

**Big lists (1,000+ names):** runs larger than 300 accounts are automatically split
into batches with a 20–40 s quiet cool-down between them (Stop skips the wait), so a
3,000-follower audit doesn't look like a bot burst. This applies to **both** modes:
public mode is signed-out, but the thing Instagram rate-limits there is your IP, and
a burst is what earns the refusal. Every result is checkpointed to
disk as it lands: close the app or crash mid-run, restart, and nothing already
checked is requested again — tick "Skip rows already checked OK" and continue where
you left off. A sustained rate limit ends the run early with a dialog explaining why
(the circuit breaker) instead of grinding through thousands of rows, so the checkpoint
is the natural way to finish a huge list across several sessions. "Clear results"
wipes the table and the checkpoint when you want a fresh start. The usernames box is saved too (`pending_names.txt` in the session
folder): restart the app and the list is back — a crash or an Instagram action
block mid-scrape never costs you the extracted names.

The **Last Post** date costs one extra Instagram request per account (session mode
only) — untick "Include Last Post date" for the fastest session run (alive/DP/counts
are unaffected).
- **DP detection:** when an account has no profile picture, Instagram serves a generic
  avatar URL; the app recognizes those known placeholders and reports `No`.
- **Alive check:** the account's accessibility falls out of the profile lookup itself —
  no extra request needed. `Last Post` costs one additional request per account and is
  best-effort: when Instagram won't serve it, the date stays blank instead of failing the row.
- **Private / deleted accounts:** you still see follower counts for private accounts, but
  posts on private accounts are only visible to approved followers. Deleted/renamed
  usernames show as `not_found`.
- This tool is for checking accounts you are permitted to review. Use responsibly and
  within Instagram's terms — aggressive scraping can get the signing-in account
  temporarily blocked or flagged.
- The library underneath ([instagrapi](https://github.com/subzeroid/instagrapi)) is
  unofficial; when Instagram changes its internal API, update it with
  `pip install -U instagrapi`.

## Project layout

```
run.bat              # one-click launcher (creates .venv on first run)
build_exe.bat        # builds a standalone .exe with PyInstaller
requirements.txt     # runtime dependencies
requirements-dev.txt # PyInstaller for exe builds
insta_checker/
  app.py             # Tkinter desktop GUI (and all local persistence)
  checker.py         # sign-in, profile fetching, verdicts, CSV export
  followers.py       # follower-list scraping (pagination, resume, stoppable)
  public_lookup.py   # signed-out profile endpoint: transport and payload only
  tests/
    test_checker.py      # unit tests for parsing/DP/verdict/CSV/batching logic
    test_public_lookup.py # unit tests for the signed-out lookup payload mapping
    test_app.py          # headless Tk tests: file loading, summaries, scrape box
```
