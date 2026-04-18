# Anypoint Flex Gateway Release Tracker

Daily tracker that monitors the [Anypoint Flex Gateway Release Notes](https://docs.mulesoft.com/release-notes/flex-gateway/flex-gateway-release-notes) page and sends an HTML email notification whenever new releases are published.

Runs automatically every day at **07:30 UTC** via GitHub Actions.

---

## How it works

1. **Scrapes** `docs.mulesoft.com/release-notes/flex-gateway` and parses every release entry (version, URL, date, What's New, Fixed Issues).
2. **Diffs** against `snapshot.json` (committed in this repo) to identify new versions.
3. **Emails** a formatted HTML report listing new releases with version type badges, change details, and direct links.
4. **Commits** the updated snapshot back to the repo so the next run has an accurate baseline.

---

## Email digest example

Each new release in the email shows:
- Version badge (`MAJOR` / `MINOR` / `PATCH`) with colour coding
- Release date
- **What's New** bullet points
- **Fixed Issues** table with resolution description and case ID
- Direct link to the full release notes section

---

## Setup

### 1. Fork / clone this repo to your GitHub account

### 2. Add the following GitHub Actions secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name     | Value                                             |
|-----------------|---------------------------------------------------|
| `SMTP_HOST`     | e.g. `smtp.gmail.com`                             |
| `SMTP_PORT`     | `587`                                             |
| `SMTP_USER`     | Your sending email address                        |
| `SMTP_PASSWORD` | App password (Gmail) or SMTP token                |
| `EMAIL_TO`      | Recipient(s), comma-separated                     |

#### Gmail app password
1. Enable 2FA on your Google account.
2. Go to **Google Account → Security → App passwords**.
3. Create a password for "Mail / Other".
4. Use that 16-character password as `SMTP_PASSWORD`.

### 3. Enable Actions on the repository

Go to the **Actions** tab → click **Enable Actions** if prompted.

### 4. Test the workflow manually

Go to **Actions → Flex Gateway Release Tracker → Run workflow**.  
Toggle `force_notify = true` on the first run to verify the email arrives.

---

## Version badge classification

| Badge   | Colour | Meaning              |
|---------|--------|----------------------|
| `MAJOR` | 🔴 Red  | x.0.0 — Major release |
| `MINOR` | 🔵 Blue | x.x.0 — Feature release |
| `PATCH` | 🟢 Green | x.x.x — Bug fix / security |

---

## Schedule

| Tracker              | Schedule     | Repo                              |
|----------------------|--------------|-----------------------------------|
| Qualys Release Tracker | 07:00 UTC | qualys-release-tracker            |
| **Flex Gateway Tracker** | **07:30 UTC** | **flex-gateway-release-tracker** |

Staggered by 30 minutes to avoid concurrent GitHub Actions usage.

---

## Files

| File                              | Purpose                            |
|-----------------------------------|------------------------------------|
| `scraper.py`                      | Main scraper + diff + email logic  |
| `snapshot.json`                   | Last-known state (auto-updated)    |
| `requirements.txt`                | Python dependencies                |
| `.github/workflows/tracker.yml`   | GitHub Actions schedule            |

---

## Local testing

```bash
pip install -r requirements.txt

export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=yourapppassword
export EMAIL_TO=you@gmail.com

python scraper.py
```

Delete `snapshot.json` before the first local run to treat all current versions as new (useful for a full email test).

---

## Companion tracker

- [qualys-release-tracker](https://github.com/Banzaaaaai/qualys-release-tracker) — monitors Qualys Suite release notes daily
