# Anypoint Flex Gateway Release Tracker

Daily tracker that monitors the [Anypoint Flex Gateway Release Notes](https://docs.mulesoft.com/release-notes/flex-gateway/flex-gateway-release-notes) page and sends an HTML email notification whenever new releases are published.

Runs automatically every day at **09:25 Madrid time** via GitHub Actions (DST-adjusted automatically).

---

## Features

| Feature | Details |
|---|---|
| **Retry logic** | 3 attempts with exponential backoff (2s → 4s → 8s) on HTTP failures |
| **Failure email** | Sends an alert email if the run crashes, so you know immediately |
| **Snapshot integrity** | Validates `snapshot.json` structure on every run; aborts if corrupt |
| **Staleness detection** | Warns by email if no new release is found for 14+ consecutive days |
| **DST auto-adjustment** | `dst_adjuster.yml` patches the cron each March/October — 09:25 Madrid year-round |
| **Run log** | Appends a JSON entry to `run_log.json` after every run (status, counts, duration) |
| **Status badge** | Writes `badge.json` (Shields.io endpoint format) after every run |
| **Monthly digest** | First day of each month: sends a stats summary (runs, new releases, version breakdown) |
| **Snapshot size guard** | When `snapshot.json` exceeds 500 KB, archives entries older than 2 years automatically |
| **Offline unit tests** | `tests/test_parser.py` — 25 tests covering parser, version classifier, and diff logic |

---

## How it works

1. **Scrapes** `docs.mulesoft.com/release-notes/flex-gateway` and parses every release (version, date, What's New, Fixed Issues).
2. **Diffs** against `snapshot.json` to identify new versions.
3. **Emails** a formatted HTML report with version type badge, change details, and fixed issue table.
4. **Commits** the updated snapshot, run log, and badge back to the repo.

---

## Setup

### 1. Fork / clone this repo

### 2. Add GitHub Actions secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Your sending address |
| `SMTP_PASSWORD` | Gmail app password |
| `EMAIL_TO` | Recipients, comma-separated |
| `GH_PAT` | Personal Access Token with `workflow` scope (needed by `dst_adjuster.yml`) |

#### Gmail app password
1. Enable 2FA on your Google account.
2. **Google Account → Security → App passwords**.
3. Create a password for "Mail / Other". Use the 16-character result as `SMTP_PASSWORD`.

#### GH_PAT (for DST adjuster)
1. **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained**.
2. Scope: `Contents: Read & Write` + `Workflows: Read & Write` on this repo only.

### 3. Enable Actions

**Actions** tab → **Enable Actions** if prompted.

### 4. Test manually

**Actions → Flex Gateway Release Tracker → Run workflow → `force_notify = true`**

---

## Version badge classification

| Badge | Colour | Meaning |
|---|---|---|
| `MAJOR` | 🔴 Red | `x.0.0` — Major release |
| `MINOR` | 🔵 Blue | `x.x.0` — Feature release |
| `PATCH` | 🟢 Green | `x.x.x` — Bug fix / security |

---

## Files

| File | Purpose |
|---|---|
| `scraper.py` | Main scraper, diff, email, run log, badge |
| `snapshot.json` | Last-known state (auto-updated) |
| `snapshot_archive.json` | Entries older than 2 years (auto-created when needed) |
| `run_log.json` | Per-run audit log (auto-updated, last 365 entries) |
| `badge.json` | Shields.io endpoint — embed in README as a live badge |
| `requirements.txt` | Python dependencies |
| `.github/workflows/tracker.yml` | Daily + monthly schedule |
| `.github/workflows/dst_adjuster.yml` | Automatic DST cron patching |
| `tests/test_parser.py` | Offline unit tests for parser, classifier, diff |

---

## Status badge

After the first run, add this to any README or dashboard:

```markdown
![Flex Tracker](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Banzaaaaai/flex-gateway-release-tracker/main/badge.json)
```

---

## Monthly digest

Sent automatically on the 1st of each month. Includes: runs, successful runs, new releases found, emails sent, staleness alerts, and a MAJOR/MINOR/PATCH breakdown of the full snapshot. Trigger manually via **Run workflow → `monthly_digest = true`**.

---

## Running tests locally

```bash
pip install -r requirements.txt
pytest tests/test_parser.py -v
```

---

## Local scraper run

```bash
pip install -r requirements.txt

export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=yourapppassword
export EMAIL_TO=you@gmail.com

python scraper.py
```

Delete `snapshot.json` before the first local run to treat all current versions as new.

---

## DST adjuster testing

**Actions → DST Schedule Adjuster → Run workflow**

- `force_offset = 2` → sets CEST (summer, UTC+2) → cron `25 7 * * *`
- `force_offset = 1` → sets CET (winter, UTC+1) → cron `25 8 * * *`

---

## Schedule

| Tracker | Daily cron | Monthly digest |
|---|---|---|
| Qualys Release Tracker | `25 3–8 * * *` (DST-adjusted) | `0 8 1 * *` |
| **Flex Gateway Tracker** | **`25 7–8 * * *` (DST-adjusted)** | **`30 8 1 * *`** |

Staggered by 30 minutes on the monthly digest to avoid concurrent runs.

---

## Companion tracker

- [qualys-release-tracker](https://github.com/Banzaaaaai/qualys-release-tracker) — monitors Qualys Suite release notes daily
