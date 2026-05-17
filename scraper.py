"""
Anypoint Flex Gateway Release Tracker — scraper.py
====================================================
Improvements vs v1:
  • Retry logic with exponential backoff       (fetch_page)
  • Failure notification email                 (main try/except → send_failure_email)
  • Snapshot integrity check                   (load_snapshot)
  • Staleness detection                        (check_staleness)
  • Run log in the repo                        (append_run_log)
  • GitHub Pages status badge                  (write_badge)
  • Monthly digest summary                     (send_monthly_digest, MONTHLY_DIGEST env)
  • Snapshot size guard + auto-archive         (snapshot_size_guard)

Environment variables (GitHub Actions secrets):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO
  FORCE_NOTIFY    — "true" to send email even when no new releases
  MONTHLY_DIGEST  — "true" to send monthly stats digest instead of tracker run
  LOG_LEVEL       — DEBUG | INFO (default INFO)
"""

import json
import logging
import os
import smtplib
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────
RELEASE_NOTES_URL = (
    "https://docs.mulesoft.com/release-notes/flex-gateway/flex-gateway-release-notes"
)
SNAPSHOT_PATH    = Path("snapshot.json")
SNAPSHOT_ARCHIVE = Path("snapshot_archive.json")
RUN_LOG_FILE     = Path("run_log.json")
BADGE_FILE       = Path("badge.json")

REQUEST_TIMEOUT  = 30
USER_AGENT       = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0"

# Staleness: alert if no new release detected in this many days
STALENESS_DAYS      = 14           # Flex releases less frequently than Qualys
ARCHIVE_AFTER_DAYS  = 730          # 2 years
SNAPSHOT_WARN_BYTES = 500_000      # 500 KB (snapshot is smaller for Flex)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── 1. Fetch with exponential backoff ─────────────────────────────────────────

def fetch_page(url: str, max_retries: int = 3) -> str:
    """GET url with exponential backoff: 2s → 4s → 8s between attempts."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            log.info("HTTP %s — %d bytes received", resp.status_code, len(resp.text))
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            wait = 2 ** (attempt + 1)       # 2, 4, 8
            if attempt < max_retries - 1:
                log.warning(
                    "Attempt %d/%d failed (%s). Retrying in %ds…",
                    attempt + 1, max_retries, exc, wait
                )
                time.sleep(wait)
    raise RuntimeError(
        f"All {max_retries} fetch attempts failed for {url}"
    ) from last_exc


# ── Parse ──────────────────────────────────────────────────────────────────────

def fetch_releases() -> dict[str, dict]:
    log.info("Fetching %s", RELEASE_NOTES_URL)
    html = fetch_page(RELEASE_NOTES_URL)
    return parse_releases(html)


def parse_releases(html: str) -> dict[str, dict]:
    """
    Parse release entries from the MuleSoft Flex Gateway release notes page.
    Returns dict keyed by version string, e.g. {'1.12.2': {...}, ...}
    Separated from fetch_releases so unit tests can call it directly.
    """
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main", class_="main")
    if not main:
        raise RuntimeError(
            "Could not locate <main> element — page structure may have changed"
        )

    releases: dict[str, dict] = {}

    for h2 in main.find_all("h2"):
        version = h2.get_text(strip=True)
        anchor  = h2.get("id", "")
        url     = f"{RELEASE_NOTES_URL}#{anchor}"

        section = h2.find_next_sibling("div", class_="sectionbody")
        if not section:
            continue

        full_text = section.get_text(separator="\n", strip=True)

        # Release date — first <strong> in first <p>
        date_str = ""
        first_p  = section.find("p")
        if first_p:
            strong = first_p.find("strong")
            if strong:
                date_str = strong.get_text(strip=True)

        # What's New bullet points
        whats_new: list[str] = []
        wn_heading = section.find(
            lambda t: t.name in ("h3", "h4", "p")
            and "what" in t.get_text(strip=True).lower()
            and "new"  in t.get_text(strip=True).lower()
        )
        if wn_heading:
            ul = wn_heading.find_next("ul")
            if ul:
                for li in ul.find_all("li", recursive=False):
                    whats_new.append(li.get_text(separator=" ", strip=True))

        # Fixed Issues table rows
        fixed_issues: list[dict] = []
        fi_heading = section.find(
            lambda t: t.name in ("h3", "h4", "p")
            and "fixed" in t.get_text(strip=True).lower()
        )
        if fi_heading:
            table = fi_heading.find_next("table")
            if table:
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        fixed_issues.append({
                            "description": cells[0].get_text(separator=" ", strip=True),
                            "id":          cells[1].get_text(strip=True),
                        })

        # Summary — first non-date paragraph mentioning "announces"
        summary = ""
        for p in section.find_all("p"):
            t = p.get_text(strip=True)
            if t and t != date_str and "announces" in t.lower():
                summary = t
                break

        releases[version] = {
            "version":            version,
            "url":                url,
            "date":               date_str,
            "summary":            summary,
            "whats_new":          whats_new,
            "fixed_issues":       fixed_issues,
            "full_text_snippet":  full_text[:400],
        }

    log.info("Parsed %d releases from the page", len(releases))
    return releases


# ── 2. Snapshot — load with integrity check ────────────────────────────────────

def load_snapshot() -> dict[str, dict]:
    """Load snapshot.json and validate its structure. Returns {} on missing file."""
    if not SNAPSHOT_PATH.exists():
        log.info("No snapshot found — all releases treated as new on first run")
        return {}

    raw = SNAPSHOT_PATH.read_text(encoding="utf-8")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"snapshot.json is corrupted (invalid JSON): {exc}\n"
            "Delete it and rerun to rebuild from scratch."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            f"snapshot.json must contain a JSON object, got {type(data).__name__}."
        )
    if data and not all(isinstance(k, str) and isinstance(v, dict)
                        for k, v in data.items()):
        raise RuntimeError(
            "snapshot.json structure invalid — expected {str: dict, …}."
        )
    if len(raw) > 100 and len(data) == 0:
        raise RuntimeError(
            "snapshot.json is non-empty but parsed to zero entries — possible truncation."
        )

    log.info("Snapshot loaded — %d known releases", len(data))
    return data


def save_snapshot(releases: dict[str, dict]) -> None:
    SNAPSHOT_PATH.write_text(
        json.dumps(releases, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Snapshot saved — %d releases recorded", len(releases))


# ── 3. Snapshot size guard + archive ──────────────────────────────────────────

def snapshot_size_guard(snapshot: dict) -> dict:
    """
    If snapshot.json exceeds SNAPSHOT_WARN_BYTES, move entries older than
    ARCHIVE_AFTER_DAYS into snapshot_archive.json and return the trimmed dict.
    """
    size = SNAPSHOT_PATH.stat().st_size if SNAPSHOT_PATH.exists() else 0
    if size <= SNAPSHOT_WARN_BYTES:
        return snapshot

    log.warning(
        "snapshot.json is %.1f KB — archiving entries older than %d days.",
        size / 1024, ARCHIVE_AFTER_DAYS
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS)
    keep, archive = {}, {}

    for version, entry in snapshot.items():
        detected = entry.get("detected_at", "")
        try:
            dt = datetime.fromisoformat(detected)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            (archive if dt < cutoff else keep)[version] = entry
        except (ValueError, TypeError):
            keep[version] = entry

    if archive:
        existing: dict = {}
        if SNAPSHOT_ARCHIVE.exists():
            try:
                existing = json.loads(SNAPSHOT_ARCHIVE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        existing.update(archive)
        SNAPSHOT_ARCHIVE.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info(
            "Archived %d old releases → %s. Keeping %d active.",
            len(archive), SNAPSHOT_ARCHIVE, len(keep)
        )

    return keep


# ── 4. Diff ────────────────────────────────────────────────────────────────────

def find_new_releases(
    current: dict[str, dict], previous: dict[str, dict]
) -> list[dict]:
    """Return releases present in current but absent from previous, newest first."""
    new_versions = [v for v in current if v not in previous]
    log.info("%d new release(s) detected: %s", len(new_versions), new_versions or "none")

    def version_key(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0, 0, 0)

    return sorted(
        [current[v] for v in new_versions],
        key=lambda r: version_key(r["version"]),
        reverse=True,
    )


# ── 5. Staleness detection ────────────────────────────────────────────────────

def check_staleness(snapshot: dict) -> bool:
    """
    Return True if snapshot has entries but none were detected within
    the past STALENESS_DAYS. Freshness tracked via 'detected_at' field.
    """
    if not snapshot:
        return False

    timestamps = []
    for entry in snapshot.values():
        ts = entry.get("detected_at")
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                timestamps.append(dt)
            except (ValueError, TypeError):
                pass

    if not timestamps:
        return False

    latest = max(timestamps)
    age = (datetime.now(timezone.utc) - latest).days
    if age >= STALENESS_DAYS:
        log.warning(
            "STALENESS ALERT: No new release detected in %d days "
            "(last seen: %s). Parser may have broken.",
            age, latest.strftime("%Y-%m-%d")
        )
        return True
    return False


# ── 6. Run log ────────────────────────────────────────────────────────────────

def append_run_log(
    *,
    new_count: int,
    total_count: int,
    email_sent: bool,
    stale: bool,
    status: str,
    error: str = "",
    duration_s: float = 0.0,
) -> None:
    entries: list[dict] = []
    if RUN_LOG_FILE.exists():
        try:
            entries = json.loads(RUN_LOG_FILE.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except json.JSONDecodeError:
            entries = []

    entries.append({
        "timestamp":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status":        status,
        "new_releases":  new_count,
        "total_tracked": total_count,
        "email_sent":    email_sent,
        "stale_alert":   stale,
        "duration_s":    round(duration_s, 1),
        "error":         error,
    })

    entries = entries[-365:]
    RUN_LOG_FILE.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Run log updated (%d entries total)", len(entries))


# ── 7. GitHub Pages badge ─────────────────────────────────────────────────────

def write_badge(total_count: int, status: str) -> None:
    color = "brightgreen" if status == "success" else "red"
    badge = {
        "schemaVersion": 1,
        "label":         "flex tracker",
        "message":       f"{total_count} releases · {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "color":         color,
        "namedLogo":     "github-actions",
    }
    BADGE_FILE.write_text(json.dumps(badge, indent=2), encoding="utf-8")
    log.info("Badge written → %s", BADGE_FILE)


# ── Email builders ────────────────────────────────────────────────────────────

VERSION_COLORS = {"major": "#d32f2f", "minor": "#1976d2", "patch": "#388e3c"}
BADGE_STYLE    = (
    "display:inline-block;padding:2px 8px;border-radius:12px;"
    "font-size:11px;font-weight:700;color:#fff;margin-right:4px;"
)


def classify_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) >= 3 and parts[2] == "0":
        return "minor" if parts[1] != "0" else "major"
    return "patch"


def build_release_email_html(new_releases: list[dict], total_known: int) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    release_rows = ""

    for r in new_releases:
        vtype       = classify_version(r["version"])
        badge_color = VERSION_COLORS[vtype]

        wn_html = ""
        if r.get("whats_new"):
            items  = "".join(f"<li style='margin:4px 0'>{i}</li>" for i in r["whats_new"])
            wn_html = f"<div style='margin-top:10px'><strong style='color:#1565c0'>What's New</strong><ul style='margin:6px 0 0;padding-left:18px;color:#37474f'>{items}</ul></div>"

        fi_html = ""
        if r.get("fixed_issues"):
            rows = ""
            for issue in r["fixed_issues"]:
                rows += (
                    f"<tr><td style='padding:5px 10px;border-bottom:1px solid #eceff1;"
                    f"color:#37474f'>{issue['description']}</td>"
                    f"<td style='padding:5px 10px;border-bottom:1px solid #eceff1;"
                    f"color:#78909c;white-space:nowrap'>{issue['id']}</td></tr>"
                )
            fi_html = (
                f"<div style='margin-top:10px'><strong style='color:#1565c0'>Fixed Issues</strong>"
                f"<table style='width:100%;border-collapse:collapse;margin-top:6px;font-size:13px'>"
                f"<tr style='background:#e3f2fd'>"
                f"<th style='padding:5px 10px;text-align:left;color:#1565c0'>Resolution</th>"
                f"<th style='padding:5px 10px;text-align:left;color:#1565c0'>ID</th></tr>"
                f"{rows}</table></div>"
            )

        release_rows += f"""
        <tr>
          <td style='padding:18px;border-bottom:1px solid #e0e0e0;vertical-align:top'>
            <div style='margin-bottom:8px'>
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
            {wn_html}{fi_html}
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style='margin:0;padding:0;font-family:Segoe UI,Arial,sans-serif;background:#f5f5f5'>
  <table width='100%' cellpadding='0' cellspacing='0' style='background:#f5f5f5;padding:24px 0'>
    <tr><td align='center'>
      <table width='680' cellpadding='0' cellspacing='0'
             style='background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08)'>
        <tr>
          <td style='background:#0d47a1;padding:24px 28px'>
            <table width='100%'><tr>
              <td>
                <div style='color:#fff;font-size:22px;font-weight:700'>🚀 Anypoint Flex Gateway — New Release(s)</div>
                <div style='color:#90caf9;font-size:13px;margin-top:4px'>Automated release monitoring · {now_utc}</div>
              </td>
              <td align='right'>
                <div style='background:rgba(255,255,255,0.15);border-radius:50px;padding:6px 14px;
                             color:#fff;font-size:20px;font-weight:800'>{len(new_releases)}</div>
              </td>
            </tr></table>
          </td>
        </tr>
        <tr><td style='padding:14px 18px;background:#e8f5e9'>
          <span style='color:#2e7d32;font-weight:600'>✅ {len(new_releases)} new release(s) detected</span>
          <span style='color:#78909c;font-size:12px'> | {total_known} total tracked | {now_utc}</span>
        </td></tr>
        <tr><td><table width='100%' cellpadding='0' cellspacing='0'>{release_rows}</table></td></tr>
        <tr>
          <td style='padding:16px 18px;background:#fafafa;border-top:1px solid #e0e0e0'>
            <span style='color:#9e9e9e;font-size:11px'>
              Source: <a href='{RELEASE_NOTES_URL}' style='color:#1976d2'>docs.mulesoft.com/release-notes/flex-gateway</a>
              &nbsp;·&nbsp; Flex Gateway Release Tracker · GitHub Actions
            </span>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def build_failure_email_html(error_summary: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:6px;
            box-shadow:0 2px 8px rgba(0,0,0,.09);overflow:hidden;">
  <div style="background:#d32f2f;padding:20px 28px;">
    <h1 style="margin:0;color:#fff;font-size:18px;">⚠️ Flex Gateway Tracker — Run Failed</h1>
    <p style="margin:6px 0 0;color:#ffcdd2;font-size:13px;">{ts}</p>
  </div>
  <div style="padding:24px 28px;">
    <p style="color:#444;font-size:14px;">
      The Flex Gateway Release Tracker encountered an error.
      <strong>No snapshot was updated</strong> and <strong>no release email was sent</strong>.
    </p>
    <div style="background:#fdf2f2;border-left:4px solid #d32f2f;padding:14px 18px;
                border-radius:4px;font-family:monospace;font-size:12px;
                color:#b71c1c;white-space:pre-wrap;word-break:break-word;">
{error_summary}
    </div>
    <p style="color:#888;font-size:12px;margin-top:18px;">
      Check the
      <a href="https://github.com/Banzaaaaai/flex-gateway-release-tracker/actions"
         style="color:#1565c0;">GitHub Actions log</a>
      for full details.
    </p>
  </div>
</div>
</body></html>"""


def build_staleness_email_html(days: int, total: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:6px;
            box-shadow:0 2px 8px rgba(0,0,0,.09);overflow:hidden;">
  <div style="background:#f57c00;padding:20px 28px;">
    <h1 style="margin:0;color:#fff;font-size:18px;">🟡 Flex Gateway Tracker — Staleness Alert</h1>
    <p style="margin:6px 0 0;color:#ffe0b2;font-size:13px;">{ts}</p>
  </div>
  <div style="padding:24px 28px;color:#444;font-size:14px;line-height:1.7;">
    <p>No new Flex Gateway release has been detected in the past <strong>{days} days</strong>.
    This could mean:</p>
    <ul>
      <li>MuleSoft has not published any releases recently (possible — check manually).</li>
      <li>The page structure changed and the parser is no longer extracting entries.</li>
      <li>The HTTP fetch is silently failing or returning an unexpected page.</li>
    </ul>
    <p><strong>{total}</strong> versions are currently in the snapshot.
    Verify the <a href="{RELEASE_NOTES_URL}" style="color:#1565c0;">Flex Gateway release notes page</a>
    and the <a href="https://github.com/Banzaaaaai/flex-gateway-release-tracker/actions"
    style="color:#1565c0;">GitHub Actions log</a>.</p>
  </div>
</div>
</body></html>"""


def build_monthly_digest_html(run_log: list[dict], snapshot: dict) -> str:
    now        = datetime.now(timezone.utc)
    month_name = now.strftime("%B %Y")
    cutoff     = now - timedelta(days=31)

    month_entries = [
        e for e in run_log
        if e.get("timestamp", "") >= cutoff.isoformat(timespec="seconds")
    ]
    total_runs   = len(month_entries)
    success_runs = sum(1 for e in month_entries if e.get("status") == "success")
    new_found    = sum(e.get("new_releases", 0) for e in month_entries)
    emails_sent  = sum(1 for e in month_entries if e.get("email_sent"))
    stale_alerts = sum(1 for e in month_entries if e.get("stale_alert"))

    # Version type breakdown across snapshot
    major = sum(1 for v in snapshot if classify_version(v) == "major")
    minor = sum(1 for v in snapshot if classify_version(v) == "minor")
    patch = sum(1 for v in snapshot if classify_version(v) == "patch")

    def stat_cell(value, label, colour="#0d47a1"):
        return (
            f"<td style='text-align:center;padding:16px 10px;'>"
            f"<div style='font-size:28px;font-weight:700;color:{colour};'>{value}</div>"
            f"<div style='font-size:11px;color:#888;margin-top:4px;'>{label}</div></td>"
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:6px;
            box-shadow:0 2px 8px rgba(0,0,0,.09);overflow:hidden;">
  <div style="background:#0d47a1;padding:24px 28px;">
    <h1 style="margin:0;color:#fff;font-size:20px;">📊 Flex Gateway Tracker — Monthly Digest</h1>
    <p style="margin:6px 0 0;color:#90caf9;font-size:13px;">{month_name}</p>
  </div>
  <div style="padding:20px 28px;">
    <p style="color:#555;font-size:13px;margin-top:0;">Activity summary for the past 30 days.</p>
    <table style="width:100%;border-collapse:collapse;background:#f8fafc;border-radius:6px;">
      <tr>
        {stat_cell(total_runs,   "Runs")}
        {stat_cell(success_runs, "Successful")}
        {stat_cell(new_found,    "New Releases", "#2e7d32")}
        {stat_cell(emails_sent,  "Emails Sent")}
        {stat_cell(stale_alerts, "Staleness Alerts", "#f57c00" if stale_alerts else "#0d47a1")}
      </tr>
    </table>

    <h3 style="font-size:14px;color:#0d47a1;margin:22px 0 10px;">
      Snapshot version type breakdown ({len(snapshot)} total)
    </h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr>
        <td style="padding:8px 12px;background:#ffebee;border-radius:4px;font-weight:700;color:#c62828;">MAJOR releases</td>
        <td style="padding:8px 12px;text-align:right;font-weight:700;">{major}</td>
      </tr>
      <tr>
        <td style="padding:8px 12px;background:#e3f2fd;border-radius:4px;font-weight:700;color:#1565c0;">MINOR releases</td>
        <td style="padding:8px 12px;text-align:right;font-weight:700;">{minor}</td>
      </tr>
      <tr>
        <td style="padding:8px 12px;background:#e8f5e9;border-radius:4px;font-weight:700;color:#2e7d32;">PATCH releases</td>
        <td style="padding:8px 12px;text-align:right;font-weight:700;">{patch}</td>
      </tr>
    </table>
  </div>
  <div style="background:#f0f4f8;padding:12px 28px;font-size:11px;color:#aaa;text-align:center;">
    Automated by <strong>flex-gateway-release-tracker</strong> · GitHub Actions
  </div>
</div>
</body></html>"""


# ── Email sender ───────────────────────────────────────────────────────────────

def _send(subject: str, html_body: str) -> None:
    smtp_host  = os.environ["SMTP_HOST"]
    smtp_port  = int(os.environ["SMTP_PORT"])
    smtp_user  = os.environ["SMTP_USER"]
    smtp_pass  = os.environ["SMTP_PASSWORD"]
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info("Connecting to %s:%d", smtp_host, smtp_port)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipients, msg.as_bytes())
    log.info("Email sent to: %s", ", ".join(recipients))


def send_failure_email(error_summary: str) -> None:
    try:
        _send("[Flex Tracker] ⚠️ Run failed", build_failure_email_html(error_summary))
    except Exception as exc:
        log.error("Could not send failure email: %s", exc)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    t_start      = time.monotonic()
    force_notify = os.getenv("FORCE_NOTIFY",   "").lower() in ("true", "1", "yes")
    monthly      = os.getenv("MONTHLY_DIGEST", "").lower() in ("true", "1", "yes")

    log.info("=== Flex Gateway Release Tracker starting ===")

    # Monthly digest mode — send stats then exit (no fetch needed)
    if monthly:
        log.info("MONTHLY_DIGEST=true — sending digest and exiting")
        snapshot = load_snapshot() if SNAPSHOT_PATH.exists() else {}
        run_log: list[dict] = []
        if RUN_LOG_FILE.exists():
            try:
                run_log = json.loads(RUN_LOG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        month_name = datetime.now(timezone.utc).strftime("%B %Y")
        _send(
            f"[Flex Tracker] 📊 Monthly digest — {month_name}",
            build_monthly_digest_html(run_log, snapshot),
        )
        return

    status     = "success"
    error_msg  = ""
    new: list[dict]     = []
    snapshot: dict      = {}
    email_sent = False
    stale      = False

    try:
        # Load + validate snapshot
        try:
            snapshot = load_snapshot()
        except RuntimeError as exc:
            raise RuntimeError(f"Snapshot integrity check failed: {exc}") from exc

        # Guard snapshot size
        snapshot = snapshot_size_guard(snapshot)

        # Fetch live releases
        current = fetch_releases()

        # Diff
        new = find_new_releases(current, snapshot)

        # Stamp detected_at on new entries
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in new:
            current[r["version"]]["detected_at"] = now_iso

        # Carry forward detected_at from previous snapshot
        for version, entry in snapshot.items():
            if version in current and "detected_at" not in current[version]:
                current[version]["detected_at"] = entry.get("detected_at", now_iso)

        # Staleness check
        stale = check_staleness(current)

        # Send release email
        if new or force_notify:
            sample = new if new else sorted(
                current.values(),
                key=lambda r: tuple(int(x) for x in r["version"].split(".") if x.isdigit()),
                reverse=True
            )[:3]
            subject = (
                f"🚀 Flex Gateway {sample[0]['version']} Released"
                if len(sample) == 1
                else f"🚀 {len(sample)} New Flex Gateway Releases Detected"
            )
            _send(subject, build_release_email_html(sample, len(current)))
            email_sent = True

        if stale and not new:
            _send(
                f"[Flex Tracker] 🟡 Staleness alert — no new release in {STALENESS_DAYS} days",
                build_staleness_email_html(STALENESS_DAYS, len(snapshot)),
            )

        # Save updated snapshot
        save_snapshot(current)

    except Exception as exc:
        status    = "failure"
        error_msg = traceback.format_exc()
        log.error("Run failed:\n%s", error_msg)
        send_failure_email(error_msg)

    finally:
        duration = time.monotonic() - t_start

        append_run_log(
            new_count   = len(new),
            total_count = len(snapshot),
            email_sent  = email_sent,
            stale       = stale,
            status      = status,
            error       = error_msg[:500] if error_msg else "",
            duration_s  = duration,
        )

        write_badge(len(snapshot), status)

        log.info("=== Done in %.1fs (status=%s) ===", duration, status)

    if status == "failure":
        sys.exit(1)


if __name__ == "__main__":
    main()
