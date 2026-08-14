#!/usr/bin/env python3
"""
Watches public internship-listing feeds and emails you when new Summer 2027
SWE-type roles show up.

Sources (both are the well-known community-maintained GitHub repos):
  - SimplifyJobs/Summer2027-Internships   (large, updates several times/hour)
  - vanshb03/Summer2027-Internships       (smaller, secondary coverage)

State lives in state.json: a map of listing-key -> first-seen epoch. Anything
not in that map is "new" and gets emailed exactly once.

Usage:
  python3 bot.py                # normal run: fetch, diff, email, save state
  python3 bot.py --dry-run      # print what it *would* email; state untouched
  python3 bot.py --seed         # mark everything currently listed as seen
  python3 bot.py --test-email   # send a "hello, I'm alive" email and exit

Env vars (required for sending):
  SMTP_USER  gmail address the mail is sent FROM
  SMTP_PASS  gmail app password (16 chars, no spaces)
  MAIL_TO    where to deliver
"""

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")

SOURCES = [
    {
        "name": "SimplifyJobs",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
               "/dev/.github/scripts/listings.json",
        "repo": "https://github.com/SimplifyJobs/Summer2027-Internships",
    },
    {
        "name": "vanshb03",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships"
               "/dev/.github/scripts/listings.json",
        "repo": "https://github.com/vanshb03/Summer2027-Internships",
    },
]

# Companies polled straight from their own applicant-tracking system, so we see
# a posting the moment it goes live rather than waiting for an aggregator to
# notice it. Only Greenhouse and Ashby are here because both expose a stable,
# unauthenticated JSON board API. Microsoft, Apple, Google and Meta run bespoke
# job sites with no such endpoint — those still come via Simplify only.
GREENHOUSE_BOARDS = {
    "anthropic": "Anthropic", "stripe": "Stripe", "databricks": "Databricks",
    "figma": "Figma", "airbnb": "Airbnb", "coinbase": "Coinbase",
    "robinhood": "Robinhood", "instacart": "Instacart", "scaleai": "Scale AI",
    "discord": "Discord", "reddit": "Reddit", "dropbox": "Dropbox",
    "cloudflare": "Cloudflare",
}
ASHBY_BOARDS = {
    "openai": "OpenAI", "ramp": "Ramp", "linear": "Linear", "notion": "Notion",
    "vercel": "Vercel", "perplexity": "Perplexity", "cursor": "Cursor",
    "sierra": "Sierra",
}

# Simplify tags each listing with a category. These are the ones we care about.
SWE_CATEGORIES = {
    "software",
    "software engineering",
    "ai/ml/data",
    "data science, ai & machine learning",
    "quant",
    "hardware",
    "hardware engineering",
}

# vanshb03 has no category field, so fall back to matching the job title.
TITLE_INCLUDE = re.compile(
    r"\b(software|swe|engineer|engineering|developer|programmer|comput|"
    r"machine learning|deep learning|\bml\b|\bai\b|artificial intelligence|"
    r"data scien|data engineer|research scientist|quant|trading|"
    r"backend|back-end|frontend|front-end|full[ -]?stack|mobile|ios|android|"
    r"infrastructure|platform|systems|embedded|firmware|robotics|security|"
    r"cyber|devops|\bsre\b|cloud|compiler|graphics|silicon|asic|fpga|chip|"
    r"analytic)\b",
    re.I,
)

# Simplify's "AI/ML/Data" bucket is a catch-all and drags in things like
# "Operations Intern". Only these categories are trusted on their own; anything
# tagged AI/ML/Data still has to look technical in the title.
STRONG_CATEGORIES = {"software", "software engineering", "quant", "hardware",
                     "hardware engineering"}

# Things Simplify sometimes files under AI/ML/Data that aren't remotely SWE.
TITLE_EXCLUDE = re.compile(
    r"\b(sales|marketing|recruit|talent acquisition|human resources|\bhr\b|"
    r"accounting|audit|payroll|paralegal|legal|communications|public relations|"
    r"brand|content creator|social media|merchandis|customer success|"
    # campus work-study postings, not industry internships
    r"student worker|work[- ]study|\bfws\b|student assistant|resident assistant)\b",
    re.I,
)

# Must actually be an internship / co-op, not a full-time or new-grad req.
INTERNSHIP_TITLE = re.compile(
    r"\b(intern|interns|internship|co-?op|apprentice|apprenticeship|"
    r"placement|summer analyst|summer associate|trainee|student)\b",
    re.I,
)

# Explicitly *not* for someone still working on a bachelor's.
NOT_UNDERGRAD = re.compile(
    r"\b(ph\.?\s?d|doctoral|doctorate|postdoc|post-doc|\bmba\b|new ?grad|"
    r"graduate program|full[- ]time|experienced hire)\b",
    re.I,
)

TARGET_TERM = "summer 2027"
MAX_LISTINGS_PER_EMAIL = 60
STATE_RETENTION_DAYS = 240
USER_AGENT = "internship-watcher/1.0 (+personal job alert bot)"


# --------------------------------------------------------------------------
# fetching + normalising
# --------------------------------------------------------------------------

def fetch_json(url, attempts=3):
    last = None
    for i in range(attempts):
        try:
            # raw.githubusercontent serves with max-age=300, so a plain GET can
            # hand back a CDN copy up to 5 minutes stale. A throwaway query
            # param plus no-cache headers pulls the current file instead.
            bust = f"{url}{'&' if '?' in url else '?'}cb={int(time.time())}"
            req = urllib.request.Request(bust, headers={
                "User-Agent": USER_AGENT,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # network flake, GitHub hiccup, bad JSON
            last = exc
            if i < attempts - 1:
                time.sleep(3 * (i + 1))
    print(f"  ! fetch failed: {url}\n    {last}", file=sys.stderr)
    return None


def listing_key(company, title, url):
    """Stable identity for a listing, so the two sources don't double-email."""
    clean = (url or "").split("?")[0].split("#")[0].rstrip("/").lower()
    if clean:
        return clean
    return f"{company.strip().lower()}|{title.strip().lower()}"


def is_summer_2027(raw, title):
    """Simplify uses a `terms` list; vanshb03 uses a bare `season` string.

    A plain `terms == ["Summer 2027"]` check is the happy path, but a chunk of
    Simplify's listings are tagged "N/A" or left empty — and a big company
    dropping its summer reqs unexpectedly is exactly the case worth not
    missing. So fall back to reading the title/url when the tag is useless.
    """
    terms = [str(t).strip().lower() for t in (raw.get("terms") or [])]
    if any(t == TARGET_TERM for t in terms):
        return True

    season = str(raw.get("season") or "").strip().lower()
    if season:
        # vanshb03's repo is scoped to the 2027 cycle: bare "Summer" means 2027.
        return season == "summer"

    untagged = not terms or all(t in ("n/a", "", "none") for t in terms)
    if untagged:
        haystack = f"{title} {raw.get('url') or ''}".lower()
        if "2027" in haystack and not re.search(r"\b(2026|2028)\b", title):
            return True
    return False


def is_swe(raw, title):
    if TITLE_EXCLUDE.search(title):
        return False
    if TITLE_INCLUDE.search(title):
        return True
    # No technical signal in the title — only trust an unambiguous category.
    return str(raw.get("category") or "").strip().lower() in STRONG_CATEGORIES


def is_for_current_undergrad(raw, title):
    """Internship-shaped, and open to someone partway through a bachelor's.

    Simplify carries a `degrees` list. When it's populated and Bachelor's isn't
    in it, the role is genuinely closed to an undergrad (42 of the current
    Summer 2027 roles are PhD-only) — drop those rather than pad the inbox.
    An empty list means unknown, which we let through.
    """
    if not INTERNSHIP_TITLE.search(title):
        return False
    if NOT_UNDERGRAD.search(title):
        return False
    degrees = [str(x).strip().lower() for x in (raw.get("degrees") or [])]
    if degrees and not any("bachelor" in d for d in degrees):
        return False
    return True


def normalise(raw, source_name):
    if not raw.get("active") or not raw.get("is_visible", True):
        return None
    title = str(raw.get("title") or "").strip()
    company = str(raw.get("company_name") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not title or not company or not url:
        return None
    if not is_summer_2027(raw, title):
        return None
    if not is_swe(raw, title):
        return None
    if not is_for_current_undergrad(raw, title):
        return None

    locs = raw.get("locations") or []
    if isinstance(locs, str):
        locs = [locs]
    return {
        "key": listing_key(company, title, url),
        "company": company,
        "title": title,
        "url": url,
        "locations": [str(x) for x in locs][:4],
        "category": str(raw.get("category") or "").strip(),
        "sponsorship": str(raw.get("sponsorship") or "").strip(),
        "date_posted": int(raw.get("date_posted") or 0),
        "source": source_name,
    }


def board_listing(company, title, url, posted, location):
    """Shared filter + shape for a role read straight off a company's own board.

    These boards have no season metadata, so the term test is inference: an
    internship posted now, without a competing year in the title, is the
    upcoming summer. Deliberately loose — this list is ~20 companies, so a
    stray alert costs far less than missing the drop we set this up for.
    """
    if not INTERNSHIP_TITLE.search(title) or NOT_UNDERGRAD.search(title):
        return None
    if TITLE_EXCLUDE.search(title) or not TITLE_INCLUDE.search(title):
        return None
    if re.search(r"\b(2026|2028|2029)\b", title):
        return None
    # Boards label off-cycle terms in the title; we only want the summer one.
    if re.search(r"\b(fall|winter|spring|autumn)\b", title, re.I):
        return None
    return {
        "key": listing_key(company, title, url),
        "company": company,
        "title": title,
        "url": url,
        "locations": [location] if location else [],
        "category": "Direct from company board",
        "sponsorship": "",
        "date_posted": posted,
        "source": "company board",
    }


def parse_iso(value):
    if not value:
        return 0
    try:
        text = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return 0


def collect_boards():
    """Poll Greenhouse + Ashby boards directly. Returns (listings, sources_ok)."""
    found, ok = {}, 0

    for token, display in GREENHOUSE_BOARDS.items():
        data = fetch_json(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", attempts=2)
        if not isinstance(data, dict) or "jobs" not in data:
            continue
        ok += 1
        for job in data["jobs"]:
            item = board_listing(
                company=display,
                title=str(job.get("title") or "").strip(),
                url=str(job.get("absolute_url") or "").strip(),
                posted=parse_iso(job.get("updated_at")),
                location=str((job.get("location") or {}).get("name") or "").strip(),
            )
            if item and item["url"]:
                found[item["key"]] = item

    for token, display in ASHBY_BOARDS.items():
        data = fetch_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{token}", attempts=2)
        if not isinstance(data, dict) or "jobs" not in data:
            continue
        ok += 1
        for job in data["jobs"]:
            item = board_listing(
                company=display,
                title=str(job.get("title") or "").strip(),
                url=str(job.get("jobUrl") or "").strip(),
                posted=parse_iso(job.get("publishedAt")),
                location=str(job.get("location") or "").strip(),
            )
            if item and item["url"]:
                found[item["key"]] = item

    print(f"  - company boards: {ok}/{len(GREENHOUSE_BOARDS) + len(ASHBY_BOARDS)} "
          f"reachable -> {len(found)} match filters")
    return found, ok > 0


def collect():
    """Returns (listings_by_key, ok) — ok is False if every source failed."""
    out = {}
    any_ok = False
    for src in SOURCES:
        data = fetch_json(src["url"])
        if not isinstance(data, list) or not data:
            print(f"  - {src['name']}: unavailable, skipping this run", file=sys.stderr)
            continue
        any_ok = True
        kept = 0
        for raw in data:
            item = normalise(raw, src["name"])
            if not item:
                continue
            kept += 1
            prev = out.get(item["key"])
            # Prefer whichever copy has the richer posting date.
            if prev is None or item["date_posted"] > prev["date_posted"]:
                out[item["key"]] = item
        print(f"  - {src['name']}: {len(data)} listings -> {kept} match filters")

    boards, boards_ok = collect_boards()
    for key, item in boards.items():
        # Aggregators and the boards share Greenhouse/Ashby URLs, so a repeat
        # here is the same req — keep whichever copy we already had.
        out.setdefault(key, item)

    return out, (any_ok or boards_ok)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"seeded": False, "seen": {}, "last_run": 0, "runs": 0}
    with open(STATE_PATH) as fh:
        state = json.load(fh)
    state.setdefault("seeded", False)
    state.setdefault("seen", {})
    state.setdefault("runs", 0)
    return state


def save_state(state):
    cutoff = time.time() - STATE_RETENTION_DAYS * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["last_run"] = int(time.time())
    state["runs"] = state.get("runs", 0) + 1
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=0, sort_keys=True)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------

def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_subject(listings):
    n = len(listings)
    companies = []
    for item in listings:
        if item["company"] not in companies:
            companies.append(item["company"])
    head = ", ".join(companies[:3])
    if len(companies) > 3:
        head += f" +{len(companies) - 3} more"
    noun = "internship" if n == 1 else "internships"
    return f"{n} new Summer 2027 SWE {noun} — {head}"


def render(listings, truncated=0):
    when = datetime.now(timezone.utc).astimezone().strftime("%b %d, %Y at %-I:%M %p")

    text = [f"{len(listings)} new Summer 2027 SWE internship posting(s)", ""]
    rows = []
    for item in listings:
        posted = ""
        if item["date_posted"]:
            posted = datetime.fromtimestamp(
                item["date_posted"], timezone.utc).astimezone().strftime("%b %d, %-I:%M %p")
        loc = ", ".join(item["locations"]) or "Location not listed"
        meta = " · ".join(x for x in [loc, item["category"], posted] if x)

        text.append(f"{item['company']} — {item['title']}")
        text.append(f"  {meta}")
        text.append(f"  {item['url']}")
        text.append("")

        rows.append(f"""
      <tr><td style="padding:14px 16px;border-bottom:1px solid #e6e6e6;">
        <div style="font:600 15px/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;">
          {esc(item['company'])}
        </div>
        <div style="font:400 15px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#333;margin:2px 0 6px;">
          <a href="{esc(item['url'])}" style="color:#0b5fff;text-decoration:none;">{esc(item['title'])}</a>
        </div>
        <div style="font:400 12.5px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#777;">
          {esc(meta)}
        </div>
      </td></tr>""")

    note = ""
    if truncated:
        note = (f'<div style="font:400 13px/1.5 sans-serif;color:#777;padding:10px 16px;">'
                f'…and {truncated} more, trimmed to keep this email readable. '
                f'They will not be re-sent.</div>')
        text.append(f"...and {truncated} more (trimmed).")

    html = f"""<!doctype html><html><body style="margin:0;background:#f5f5f7;padding:20px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:640px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;">
    <tr><td style="padding:18px 16px 12px;border-bottom:2px solid #111;">
      <div style="font:700 17px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;">
        {len(listings)} new Summer 2027 SWE internship{'s' if len(listings) != 1 else ''}
      </div>
      <div style="font:400 12.5px/1.4 sans-serif;color:#777;margin-top:3px;">Checked {esc(when)}</div>
    </td></tr>
    {''.join(rows)}
    <tr><td>{note}</td></tr>
    <tr><td style="padding:14px 16px;font:400 11.5px/1.5 sans-serif;color:#999;">
      Sourced from the SimplifyJobs and vanshb03 Summer 2027 internship repos.
    </td></tr>
  </table></body></html>"""
    return "\n".join(text), html


def send(subject, text_body, html_body):
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").replace(" ", "").strip()
    to_addr = os.environ.get("MAIL_TO", "").strip()
    if not (user and password and to_addr):
        raise SystemExit("SMTP_USER, SMTP_PASS and MAIL_TO must all be set.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Internship Bot <{user}>"
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    last = None
    for attempt in range(3):
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=45) as s:
                s.login(user, password)
                s.send_message(msg)
            print(f"  ✓ emailed {to_addr}")
            return
        except smtplib.SMTPAuthenticationError:
            raise SystemExit(
                "Gmail rejected the login. Check that SMTP_PASS is a 16-character "
                "App Password (not your normal Google password) and that 2-Step "
                "Verification is on for that account."
            )
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise SystemExit(f"Could not send mail after 3 attempts: {last}")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    args = ap.parse_args()

    if args.test_email:
        send("Internship bot is live",
             "Your Summer 2027 internship watcher is wired up and can reach your inbox.",
             '<div style="font:400 15px/1.5 sans-serif;padding:16px;">'
             'Your Summer 2027 internship watcher is wired up and can reach your inbox.'
             '</div>')
        return 0

    print(f"Run at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    current, any_ok = collect()

    if not any_ok:
        print("Every source failed — leaving state alone and exiting quietly.")
        return 0
    if not current:
        print("No listings matched the filters. Not touching state.")
        return 0

    state = load_state()
    now = int(time.time())

    if args.seed or not state["seeded"]:
        for key in current:
            state["seen"].setdefault(key, now)
        state["seeded"] = True
        save_state(state)
        print(f"Seeded {len(state['seen'])} existing listings. "
              f"Only postings that appear from now on will be emailed.")
        return 0

    fresh = [item for key, item in current.items() if key not in state["seen"]]
    if not fresh:
        # Deliberately do NOT write state here — an unchanged file means the
        # workflow makes no commit, which keeps the repo history quiet.
        print(f"No new listings ({len(current)} active, all previously seen).")
        return 0

    fresh.sort(key=lambda x: x["date_posted"], reverse=True)
    truncated = max(0, len(fresh) - MAX_LISTINGS_PER_EMAIL)
    shown = fresh[:MAX_LISTINGS_PER_EMAIL]

    subject = build_subject(shown)
    text_body, html_body = render(shown, truncated)

    if args.dry_run:
        print(f"\n--- DRY RUN: would send ---\nSubject: {subject}\n")
        print(text_body)
        print("--- state not modified ---")
        return 0

    send(subject, text_body, html_body)
    for item in fresh:
        state["seen"][item["key"]] = now
    save_state(state)
    print(f"Recorded {len(fresh)} new listing(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
