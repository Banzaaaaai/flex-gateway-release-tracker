"""
Anypoint Flex Gateway Release Tracker
--------------------------------------
Scrapes https://docs.mulesoft.com/release-notes/flex-gateway/flex-gateway-release-notes
daily, diffs against a committed snapshot, and e-mails an HTML digest of new releases.

Environment variables (set as GitHub Actions secrets):
  SMTP_HOST       – SMTP server hostname  (e.g. smtp.gmail.com)
  SMTP_PORT       – SMTP port             (e.g. 587)
  SMTP_USER       – Sending address
  SMTP_PASSWORD   – SMTP password / app-password
  EMAIL_TO        – Recipient(s), comma-separated
  FORCE_NOTIFY    – Set to "true" to send email even when no new releases found
  LOG_LEVEL       – DEBUG | INFO (default INFO)
"""

import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────
RELEASE_NOTES_URL = (
    "https://docs.mulesoft.com/release-notes/flex-gateway/flex-gateway-release-notes"
)
SNAPSHOT_PATH = Path("snapshot.json")
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Scraper ────────────────────────────────────────────────────────────────────

def fetch_releases() -> dict[str, dict]:
    """
    Returns a dict keyed by version string, e.g. {'1.12.2': {...}, ...}
    Each value contains: version, url, date, summary, fixed_issues, whats_new
    """
    log.info("Fetching %s", RELEASE_NOTES_URL)
    resp = requests.get(
        RELEASE_NOTES_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    log.info("HTTP %s — %d bytes received", resp.status_code, len(resp.text))

    soup = BeautifulSoup(resp.text, "lxml")
    main = soup.find("main", class_="main")
    if not main:
        raise RuntimeError("Could not locate <main> element — page structure may have changed")

    releases: dict[str, dict] = {}

    for h2 in main.find_all("h2"):
        version = h2.get_text(strip=True)
        anchor = h2.get("id", "")
        url = f"{RELEASE_NOTES_URL}#{anchor}"

        # Section body is the div.sectionbody immediately after the h2
        section = h2.find_next_sibling("div", class_="sectionbody")
        if not section:
            continue

        full_text = section.get_text(separator="\n", strip=True)

        # Extract release date (first <strong> in first paragraph)
        date_str = ""
        first_p = section.find("p")
        if first_p:
            strong = first_p.find("strong")
            if strong:
                date_str = strong.get_text(strip=True)

        # Extract "What's New" bullet points
        whats_new: list[str] = []
        wn_heading = section.find(lambda t: t.name in ("h3", "h4", "p") and
                                   "what" in t.get_text(strip=True).lower() and
                                   "new" in t.get_text(strip=True).lower())
        if wn_heading:
            ul = wn_heading.find_next("ul")
            if ul:
                for li in ul.find_all("li", recursive=False):
                    whats_new.append(li.get_text(separator=" ", strip=True))

        # Extract "Fixed Issues" rows from the table
        fixed_issues: list[dict] = []
        fi_heading = section.find(lambda t: t.name in ("h3", "h4", "p") and
                                   "fixed" in t.get_text(strip=True).lower())
        if fi_heading:
            table = fi_heading.find_next("table")
            if table:
                rows = table.find_all("tr")[1:]  # skip header
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        fixed_issues.append({
                            "description": cells[0].get_text(separator=" ", strip=True),
                            "id": cells[1].get_text(strip=True),
                        })

        # Summary = first non-date non-empty paragraph
        summary = ""
        for p in section.find_all("p"):
            t = p.get_text(strip=True)
            if t and t != date_str and "announces" in t.lower():
                summary = t
                break

        releases[version] = {
            "version": version,
            "url": url,
            "date": date_str,
            "summary": summary,
            "whats_new": whats_new,
            "fixed_issues": fixed_issues,
            "full_text_snippet": full_text[:400],
        }

    log.info("Parsed %d releases from the page", len(releases))
    return releases


# ── Snapshot diff ──────────────────────────────────────────────────────────────

def load_snapshot() -> dict[str, dict]:
    if SNAPSHOT_PATH.exists():
        try:
            data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            log.info("Snapshot loaded — %d known releases", len(data))
            return data
        except json.JSONDecodeError:
            log.warning("Snapshot file is corrupt — treating all releases as new")
    else:
        log.info("No snapshot found — all releases will be treated as new on first run")
    return {}


def save_snapshot(releases: dict[str, dict]) -> None:
    SNAPSHOT_PATH.write_text(
        json.dumps(releases, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Snapshot saved — %d releases recorded", len(releases))


def find_new_releases(
    current: dict[str, dict], previous: dict[str, dict]
) -> list[dict]:
    """Return releases present in current but absent from previous, newest first."""
    new_versions = [v for v in current if v not in previous]
    log.info("%d new release(s) detected: %s", len(new_versions), new_versions or "none")
    # Sort by version number descending (semantic-ish, handles 1.12.2 > 1.11.6)
    def version_key(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0, 0, 0)
    return sorted(
        [current[v] for v in new_versions],
        key=lambda r: version_key(r["version"]),
        reverse=True,
    )


# ── Email builder ──────────────────────────────────────────────────────────────

VERSION_COLORS = {
    "major": "#d32f2f",   # x.0.0
    "minor": "#1976d2",   # x.x.0
    "patch": "#388e3c",   # x.x.x
}

BADGE_STYLE = (
    "display:inline-block;padding:2px 8px;border-radius:12px;"
    "font-size:11px;font-weight:700;color:#fff;margin-right:4px;"
)


def classify_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) >= 3 and parts[2] == "0":
        return "minor" if parts[1] != "0" else "major"
    return "patch"


def build_email_html(new_releases: list[dict], total_known: int) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    release_rows = ""

    for r in new_releases:
        vtype = classify_version(r["version"])
        badge_color = VERSION_COLORS[vtype]

        # What's New section
        wn_html = ""
        if r.get("whats_new"):
            items = "".join(f"<li style='margin:4px 0'>{item}</li>" for item in r["whats_new"])
            wn_html = f"""
            <div style='margin-top:10px'>
              <strong style='color:#1565c0'>What's New</strong>
              <ul style='margin:6px 0 0 0;padding-left:18px;color:#37474f'>{items}</ul>
            </div>"""

        # Fixed Issues section
        fi_html = ""
        if r.get("fixed_issues"):
            rows = ""
            for issue in r["fixed_issues"]:
                rows += f"""
                <tr>
                  <td style='padding:5px 10px;border-bottom:1px solid #eceff1;color:#37474f'>{issue['description']}</td>
                  <td style='padding:5px 10px;border-bottom:1px solid #eceff1;color:#78909c;white-space:nowrap'>{issue['id']}</td>
                </tr>"""
            fi_html = f"""
            <div style='margin-top:10px'>
              <strong style='color:#1565c0'>Fixed Issues</strong>
              <table style='width:100%;border-collapse:collapse;margin-top:6px;font-size:13px'>
                <tr style='background:#e3f2fd'>
                  <th style='padding:5px 10px;text-align:left;color:#1565c0'>Resolution</th>
                  <th style='padding:5px 10px;text-align:left;color:#1565c0'>ID</th>
                </tr>
                {rows}
              </table>
            </div>"""

        release_rows += f"""
        <tr>
          <td style='padding:18px;border-bottom:1px solid #e0e0e0;vertical-align:top'>
            <div style='display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px'>
              <a href='{r["url"]}' style='font-size:17px;font-weight:700;color:#0d47a1;text-decoration:none'>
                Flex Gateway {r["version"]}
              </a>
              <span style='{BADGE_STYLE}background:{badge_color}'>{vtype.upper()}</span>
            </div>
            <div style='color:#78909c;font-size:12px;margin-bottom:8px'>
              📅 {r.get("date", "Date not available")} &nbsp;|&nbsp;
              <a href='{r["url"]}' style='color:#1976d2'>View full release notes →</a>
            </div>
            {f'<div style="color:#546e7a;font-size:13px;line-height:1.5">{r["summary"]}</div>' if r.get("summary") else ""}
            {wn_html}
            {fi_html}
          </td>
        </tr>"""

    # Summary stats row
    stats_html = f"""
    <tr>
      <td style='padding:14px 18px;background:#e8f5e9;border-radius:4px'>
        <span style='color:#2e7d32;font-weight:600'>✅ {len(new_releases)} new release(s) detected</span>
        <span style='color:#78909c;font-size:12px'> | {total_known} total releases tracked</span>
        <span style='color:#78909c;font-size:12px'> | Checked: {now_utc}</span>
      </td>
    </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style='margin:0;padding:0;font-family:Segoe UI,Arial,sans-serif;background:#f5f5f5'>
  <table width='100%' cellpadding='0' cellspacing='0' style='background:#f5f5f5;padding:24px 0'>
    <tr><td align='center'>
      <table width='680' cellpadding='0' cellspacing='0'
             style='background:#fff;border-radius:8px;overflow:hidden;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08)'>

        <!-- Header -->
        <tr>
          <td style='background:linear-gradient(135deg,#0d47a1,#1976d2);padding:24px 28px'>
            <table width='100%'><tr>
              <td>
                <div style='color:#fff;font-size:22px;font-weight:700'>
                  🚀 Anypoint Flex Gateway — New Release(s)
                </div>
                <div style='color:#90caf9;font-size:13px;margin-top:4px'>
                  Automated release monitoring · {now_utc}
                </div>
              </td>
              <td align='right'>
                <div style='background:rgba(255,255,255,0.15);border-radius:50px;
                             padding:6px 14px;color:#fff;font-size:20px;font-weight:800'>
                  {len(new_releases)}
                </div>
              </td>
            </tr></table>
          </td>
        </tr>

        <!-- Stats bar -->
        <tr><td style='padding:0 18px 0 18px'>
          <table width='100%' cellpadding='0' cellspacing='0'>
            {stats_html}
          </table>
        </td></tr>

        <!-- Release entries -->
        <tr><td>
          <table width='100%' cellpadding='0' cellspacing='0'>
            {release_rows}
          </table>
        </td></tr>

        <!-- Footer -->
        <tr>
          <td style='padding:16px 18px;background:#fafafa;border-top:1px solid #e0e0e0'>
            <table width='100%'><tr>
              <td>
                <span style='color:#9e9e9e;font-size:11px'>
                  Source: <a href='{RELEASE_NOTES_URL}' style='color:#1976d2'>
                    docs.mulesoft.com/release-notes/flex-gateway
                  </a>
                </span>
              </td>
              <td align='right'>
                <span style='color:#9e9e9e;font-size:11px'>
                  Flex Gateway Release Tracker · GitHub Actions
                </span>
              </td>
            </tr></table>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_no_changes_html(total_known: int) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html>
<body style='font-family:Segoe UI,Arial,sans-serif;background:#f5f5f5;padding:24px'>
  <table width='600' style='background:#fff;border-radius:8px;padding:24px;
                             box-shadow:0 2px 8px rgba(0,0,0,0.08);margin:0 auto'>
    <tr>
      <td style='background:#e8f5e9;border-radius:6px;padding:18px;text-align:center'>
        <div style='font-size:32px'>✅</div>
        <div style='font-size:16px;color:#2e7d32;font-weight:600;margin-top:8px'>
          No new Flex Gateway releases today
        </div>
        <div style='color:#78909c;font-size:13px;margin-top:6px'>
          {total_known} releases tracked · Checked {now_utc}
        </div>
        <div style='margin-top:12px'>
          <a href='{RELEASE_NOTES_URL}' style='color:#1976d2;font-size:13px'>
            View all release notes →
          </a>
        </div>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ── Email sender ───────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASSWORD"]
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info("Connecting to %s:%d", smtp_host, smtp_port)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipients, msg.as_bytes())
    log.info("Email sent to: %s", ", ".join(recipients))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    force_notify = os.getenv("FORCE_NOTIFY", "").lower() in ("true", "1", "yes")

    try:
        current = fetch_releases()
    except Exception as exc:
        log.error("Failed to fetch release notes: %s", exc)
        sys.exit(1)

    previous = load_snapshot()
    new_releases = find_new_releases(current, previous)

    # Always update snapshot
    save_snapshot(current)

    should_email = bool(new_releases) or force_notify

    if not should_email:
        log.info("No new releases and FORCE_NOTIFY is not set — skipping email")
        return

    if new_releases:
        subject = (
            f"🚀 Flex Gateway {new_releases[0]['version']} Released"
            if len(new_releases) == 1
            else f"🚀 {len(new_releases)} New Flex Gateway Releases Detected"
        )
        html = build_email_html(new_releases, len(current))
        log.info("Preparing email: %s", subject)
    else:
        subject = "✅ Flex Gateway Release Tracker — No Changes Today"
        html = build_no_changes_html(len(current))
        log.info("Force-notify: sending no-changes email")

    try:
        send_email(subject, html)
    except KeyError as exc:
        log.error("Missing environment variable: %s — skipping email", exc)
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        sys.exit(1)

    log.info("Done.")


if __name__ == "__main__":
    main()
