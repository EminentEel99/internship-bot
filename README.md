# Summer 2027 SWE internship watcher

Checks two community-maintained internship feeds every 5 minutes and emails me
when a new Summer 2027 software / ML / quant / hardware internship is posted.

- [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) — the big one, updates several times an hour
- [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) — secondary coverage

Runs on GitHub Actions. `state.json` records every listing that's already been
emailed, so nothing is ever sent twice.

## Setup

1. Push this repo to GitHub (**public** — private repos burn Actions minutes,
   public ones are unlimited).
2. Create a Gmail App Password at <https://myaccount.google.com/apppasswords>
   (requires 2-Step Verification on that account).
3. Add three repository secrets under **Settings → Secrets and variables →
   Actions**:

   | Secret | Value |
   | --- | --- |
   | `SMTP_USER` | the Gmail address sending the mail |
   | `SMTP_PASS` | the 16-character app password |
   | `MAIL_TO` | where alerts should land |

4. **Actions** tab → enable workflows → run **internship-watch** manually once
   to confirm it's green.

## Running it locally

```bash
python3 bot.py --dry-run      # show what it would email, change nothing
python3 bot.py --test-email   # prove the SMTP credentials work
python3 bot.py --digest 25    # snapshot of the 25 newest, ignores seen state
python3 bot.py --seed         # mark everything currently listed as already seen
python3 bot.py                # real run
```

One new listing means one email, so nothing gets buried inside a digest. If a
single run turns up more than `BURST_THRESHOLD` (12) at once — a company
dropping its whole req list in one go — those collapse into a single digest
rather than 40 separate messages.

Local runs need the same three values in the environment:

```bash
export SMTP_USER=you@gmail.com SMTP_PASS=xxxxxxxxxxxxxxxx MAIL_TO=you@school.edu
```

## Tuning the filter

Everything lives at the top of `bot.py`:

- `TARGET_TERM` — currently `summer 2027`. Change to `fall 2026` etc., or drop
  the check in `is_summer_2027()` to get every term.
- `SWE_CATEGORIES` — Simplify's own tags: Software, AI/ML/Data, Quant, Hardware.
- `TITLE_INCLUDE` / `TITLE_EXCLUDE` — keyword rules, used for the vanshb03 feed
  (which has no category field) and to drop miscategorised sales/marketing roles.

After widening a filter, run `python3 bot.py --seed` and commit `state.json`,
otherwise the next run emails every newly-matching listing at once.

## Notes

- Every source failing (GitHub outage) is a no-op — state is left untouched and
  nothing is lost.
- A single email is capped at 60 listings so a backlog can't produce a wall of text.
- GitHub disables scheduled workflows on repos with ~60 days of no activity. The
  bot commits `state.json` whenever new roles appear, which normally keeps it
  alive; if alerts ever go quiet, check the Actions tab.
