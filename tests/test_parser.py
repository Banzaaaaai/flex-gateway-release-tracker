"""
tests/test_parser.py — Flex Gateway Release Tracker
=====================================================
Offline unit tests for parse_releases() and classify_version().
Run with:  pytest tests/test_parser.py -v
"""

from textwrap import dedent

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper import parse_releases, classify_version, find_new_releases


# ── HTML fixtures ─────────────────────────────────────────────────────────────

# Minimal valid page with two release sections
MINIMAL_HTML = dedent("""\
    <html><body>
    <main class="main">
      <h2 id="flex-gateway-1-12-2">1.12.2</h2>
      <div class="sectionbody">
        <p><strong>May 15, 2025</strong></p>
        <p>MuleSoft announces the availability of Flex Gateway 1.12.2.</p>
        <h3>What's New</h3>
        <ul>
          <li>Improved TLS certificate rotation.</li>
          <li>Added support for HTTP/3.</li>
        </ul>
        <h3>Fixed Issues</h3>
        <table>
          <tr><th>Resolution</th><th>ID</th></tr>
          <tr><td>Memory leak in proxy handler</td><td>W-12345678</td></tr>
        </table>
      </div>

      <h2 id="flex-gateway-1-12-0">1.12.0</h2>
      <div class="sectionbody">
        <p><strong>March 10, 2025</strong></p>
        <p>MuleSoft announces the availability of Flex Gateway 1.12.0.</p>
        <h3>What's New</h3>
        <ul>
          <li>New policy engine.</li>
        </ul>
      </div>
    </main>
    </body></html>
""")

# Page with no <main class="main"> — should raise
BAD_STRUCTURE_HTML = "<html><body><div>No main tag here</div></body></html>"

# Page with h2 headings but no sectionbody siblings — entries skipped
NO_SECTIONBODY_HTML = dedent("""\
    <html><body><main class="main">
      <h2 id="v1">1.11.0</h2>
      <p>Not a sectionbody sibling</p>
    </main></body></html>
""")

# Single release, no What's New, no Fixed Issues
SPARSE_HTML = dedent("""\
    <html><body><main class="main">
      <h2 id="flex-gateway-1-10-0">1.10.0</h2>
      <div class="sectionbody">
        <p><strong>January 5, 2025</strong></p>
      </div>
    </main></body></html>
""")


# ── Tests: parse_releases ─────────────────────────────────────────────────────

class TestParseReleases:

    def test_returns_dict(self):
        result = parse_releases(MINIMAL_HTML)
        assert isinstance(result, dict)

    def test_correct_key_count(self):
        result = parse_releases(MINIMAL_HTML)
        assert len(result) == 2

    def test_keys_are_version_strings(self):
        result = parse_releases(MINIMAL_HTML)
        assert "1.12.2" in result
        assert "1.12.0" in result

    def test_required_fields_present(self):
        result = parse_releases(MINIMAL_HTML)
        for entry in result.values():
            for field in ("version", "url", "date", "summary", "whats_new", "fixed_issues"):
                assert field in entry, f"Missing field '{field}'"

    def test_url_contains_anchor(self):
        result = parse_releases(MINIMAL_HTML)
        assert "#flex-gateway-1-12-2" in result["1.12.2"]["url"]

    def test_date_extracted(self):
        result = parse_releases(MINIMAL_HTML)
        assert result["1.12.2"]["date"] == "May 15, 2025"
        assert result["1.12.0"]["date"] == "March 10, 2025"

    def test_whats_new_extracted(self):
        result = parse_releases(MINIMAL_HTML)
        wn = result["1.12.2"]["whats_new"]
        assert len(wn) == 2
        assert any("TLS" in item for item in wn)
        assert any("HTTP/3" in item for item in wn)

    def test_fixed_issues_extracted(self):
        result = parse_releases(MINIMAL_HTML)
        fi = result["1.12.2"]["fixed_issues"]
        assert len(fi) == 1
        assert fi[0]["id"] == "W-12345678"
        assert "Memory leak" in fi[0]["description"]

    def test_no_whats_new_returns_empty_list(self):
        result = parse_releases(SPARSE_HTML)
        assert result["1.10.0"]["whats_new"] == []

    def test_no_fixed_issues_returns_empty_list(self):
        result = parse_releases(SPARSE_HTML)
        assert result["1.10.0"]["fixed_issues"] == []

    def test_missing_main_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="<main>"):
            parse_releases(BAD_STRUCTURE_HTML)

    def test_h2_without_sectionbody_is_skipped(self):
        result = parse_releases(NO_SECTIONBODY_HTML)
        assert len(result) == 0

    def test_version_field_matches_key(self):
        result = parse_releases(MINIMAL_HTML)
        for key, entry in result.items():
            assert entry["version"] == key

    def test_url_is_string_starting_with_http(self):
        result = parse_releases(MINIMAL_HTML)
        for entry in result.values():
            assert isinstance(entry["url"], str)
            assert entry["url"].startswith("http")

    def test_summary_extracted_when_announces_present(self):
        result = parse_releases(MINIMAL_HTML)
        assert "announces" in result["1.12.2"]["summary"].lower()


# ── Tests: classify_version ───────────────────────────────────────────────────

class TestClassifyVersion:

    @pytest.mark.parametrize("version,expected", [
        ("2.0.0", "major"),
        ("1.0.0", "major"),
        ("10.0.0", "major"),
    ])
    def test_major_versions(self, version, expected):
        assert classify_version(version) == expected

    @pytest.mark.parametrize("version,expected", [
        ("1.12.0", "minor"),
        ("1.1.0",  "minor"),
        ("2.3.0",  "minor"),
    ])
    def test_minor_versions(self, version, expected):
        assert classify_version(version) == expected

    @pytest.mark.parametrize("version,expected", [
        ("1.12.2", "patch"),
        ("1.0.1",  "patch"),
        ("2.3.4",  "patch"),
    ])
    def test_patch_versions(self, version, expected):
        assert classify_version(version) == expected

    def test_returns_string(self):
        assert isinstance(classify_version("1.2.3"), str)

    def test_result_is_one_of_three_tiers(self):
        for v in ("1.0.0", "1.1.0", "1.1.1"):
            assert classify_version(v) in ("major", "minor", "patch")


# ── Tests: find_new_releases ──────────────────────────────────────────────────

class TestFindNewReleases:

    def _make_entry(self, version: str) -> dict:
        return {"version": version, "url": f"http://x/{version}", "date": "",
                "summary": "", "whats_new": [], "fixed_issues": [], "full_text_snippet": ""}

    def test_all_new_when_snapshot_empty(self):
        current = {"1.0.0": self._make_entry("1.0.0")}
        result  = find_new_releases(current, {})
        assert len(result) == 1

    def test_no_new_when_snapshot_matches(self):
        entry   = self._make_entry("1.0.0")
        current = {"1.0.0": entry}
        result  = find_new_releases(current, {"1.0.0": entry})
        assert result == []

    def test_only_new_versions_returned(self):
        old     = self._make_entry("1.0.0")
        new     = self._make_entry("1.0.1")
        current = {"1.0.0": old, "1.0.1": new}
        result  = find_new_releases(current, {"1.0.0": old})
        assert len(result) == 1
        assert result[0]["version"] == "1.0.1"

    def test_result_sorted_newest_first(self):
        entries  = {v: self._make_entry(v) for v in ("1.0.0", "1.0.2", "1.0.1")}
        result   = find_new_releases(entries, {})
        versions = [r["version"] for r in result]
        assert versions == sorted(versions, key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)

    def test_returns_list_of_dicts(self):
        current = {"1.0.0": self._make_entry("1.0.0")}
        result  = find_new_releases(current, {})
        assert isinstance(result, list)
        assert all(isinstance(r, dict) for r in result)
