"""The guard that stops the durable in-repo record going unwritten again.

Between v1.98.0 and v1.184.0, 137 versions shipped to main with no CHANGELOG
entry, and 160 versions across a wider span never got a git tag. Nothing
caught it for months because nothing was checking -- AGENTS.md said to write
the entry and tag the commit, and that was the whole mechanism.

These tests are the mechanism now. They run in the suite, which AGENTS.md
requires before shipping and which `--no-verify` cannot skip, unlike the
pre-push hook that calls the same module.

★ THE DAMAGE TESTS MATTER MOST. A guard that passes is worthless unless it
also fails on the thing it claims to catch, so several tests below feed the
checkers a deliberately damaged COPY of the changelog text -- never the real
file -- and assert they complain.
"""
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

import changelog_guard as guard

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def text():
    return guard.changelog_text()


@pytest.fixture(scope="module")
def shipped():
    if not guard.in_git_repo():
        pytest.skip("not a git repo")
    return guard.shipped_versions()


# --- the real repo must be clean -----------------------------------------

def test_the_current_version_has_a_changelog_entry(text):
    assert guard.check_current_version_has_entry(guard.current_version(), text) == []


def test_every_heading_has_a_sane_date(text):
    assert guard.check_dates(text) == []


def test_headings_are_newest_first_with_no_duplicates(text):
    assert guard.check_order_and_duplicates(text) == []


def test_no_shipped_version_above_the_floor_lacks_an_entry(text, shipped):
    assert guard.check_ratchet(shipped, text) == []


def test_no_shipped_version_above_the_floor_lacks_a_tag(shipped):
    tags = guard.existing_tags()
    if not tags:
        pytest.skip("no tags in this clone")
    assert guard.check_tags(shipped, tags, pending=guard.current_version()) == []


def test_the_pending_release_is_exempt_from_the_tag_check():
    """★ The release order is bump, write the entry, run the suite, push, THEN
    tag -- so the newest version has no tag while the suite runs. Without this
    exemption the guard would fail on every release it exists to protect."""
    tags = {"v1.0.0"}
    assert guard.check_tags(["1.0.0", "2.9.9"], tags, pending="2.9.9") == []
    problems = guard.check_tags(["1.0.0", "2.9.9"], tags, pending=None)
    assert problems and "2.9.9" in problems[0]


def test_the_ratchet_floor_covers_the_whole_reconstructed_history(shipped):
    """The floor is only meaningful if it is actually at the bottom."""
    assert guard.RATCHET_FLOOR == "1.0.0"
    assert min(shipped, key=guard.version_key) == "1.0.0"


# --- ★ the guard must FAIL when the record is damaged --------------------

def strip_entry(text, version):
    """Remove one entry from a COPY of the changelog text."""
    pattern = re.compile(
        r"(?ms)^## \[" + re.escape(version) + r"\].*?(?=^## \[|\Z)")
    damaged, count = pattern.subn("", text)
    assert count == 1, f"expected to remove exactly one {version} entry"
    return damaged


def test_removing_the_current_versions_entry_is_CAUGHT(text):
    """★ The exact failure that went unnoticed for 137 versions."""
    version = guard.current_version()
    damaged = strip_entry(text, version)
    problems = guard.check_current_version_has_entry(version, damaged)
    assert problems, "a missing entry for the shipped version must be reported"
    assert version in problems[0]


def test_removing_any_historical_entry_is_CAUGHT_by_the_ratchet(text, shipped):
    damaged = strip_entry(text, "1.140.0")
    problems = guard.check_ratchet(shipped, damaged)
    assert problems and "1.140.0" in problems[0]


def test_a_whole_era_going_missing_is_CAUGHT_and_counted(text, shipped):
    damaged = text
    for version in ("1.140.0", "1.141.0", "1.142.0"):
        damaged = strip_entry(damaged, version)
    problems = guard.check_ratchet(shipped, damaged)
    assert problems
    assert problems[0].startswith("3 version(s)")


def test_removing_a_TAG_is_CAUGHT(shipped):
    """★ Simulated by withholding a tag from the set handed to the checker --
    the real tag is never deleted."""
    tags = guard.existing_tags()
    if not tags:
        pytest.skip("no tags in this clone")
    without = tags - {"v1.140.0"}
    problems = guard.check_tags(shipped, without)
    assert problems and "1.140.0" in problems[0]


def test_a_future_date_is_CAUGHT(text):
    damaged = text.replace("## [2.9.0] - ", "## [2.9.0] - 2099-01-01 - ", 1)
    damaged = re.sub(r"(?m)^## \[2\.9\.0\].*$", "## [2.9.0] - 2099-01-01", damaged)
    problems = guard.check_dates(damaged)
    assert problems and "in the future" in problems[0]


def test_an_unparseable_date_is_CAUGHT(text):
    damaged = re.sub(r"(?m)^## \[2\.9\.0\].*$", "## [2.9.0] - 2026-13-45", text)
    problems = guard.check_dates(damaged)
    assert problems and "2.9.0" in problems[0]


def test_a_missing_date_is_CAUGHT(text):
    damaged = re.sub(r"(?m)^## \[2\.9\.0\].*$", "## [2.9.0]", text)
    problems = guard.check_dates(damaged)
    assert problems and "no date" in problems[0]


def test_a_duplicate_heading_is_CAUGHT(text):
    damaged = text.replace("## [2.8.0] - ", "## [2.9.0] - ", 1)
    problems = guard.check_order_and_duplicates(damaged)
    assert any("more than once" in p for p in problems)


def test_an_out_of_order_heading_is_CAUGHT(text):
    damaged = text.replace("## [1.0.0]", "## [9.9.9]", 1)
    problems = guard.check_order_and_duplicates(damaged)
    assert any("newest first" in p for p in problems)


# --- the deliberately untagged versions ----------------------------------

def test_the_untagged_versions_are_declared_and_really_have_no_tag():
    tags = guard.existing_tags()
    if not tags:
        pytest.skip("no tags in this clone")
    for version in guard.UNTAGGED_BY_DECISION:
        assert f"v{version}" not in tags, (
            f"v{version} is declared untagged but a tag exists"
        )


def test_each_untagged_version_records_its_reason_in_the_changelog(text):
    """The operator's decision was to RECORD why, not to leave a silent hole."""
    for version in guard.UNTAGGED_BY_DECISION:
        entry = re.search(
            r"(?ms)^## \[" + re.escape(version) + r"\].*?(?=^## \[|\Z)", text)
        assert entry, f"{version} has no entry at all"
        body = entry.group(0)
        assert "No `v" + version + "` git tag exists, deliberately." in body
        assert "Release tagging" in body


def test_the_untagged_versions_are_exactly_the_ones_with_no_bump_commit(shipped):
    """They are untagged for a structural reason, not by preference: each
    shipped inside a commit VERSION records as a different release."""
    if not guard.in_git_repo():
        pytest.skip("not a git repo")
    log = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--format=%H", "--", "VERSION"],
        capture_output=True, text=True, timeout=300).stdout.split()
    bumped = set()
    for sha in log:
        blob = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{sha}:VERSION"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", blob):
            bumped.add(blob)
    # The version being prepared right now has an entry but its VERSION bump
    # is not committed yet, so it legitimately has no bump commit in the log.
    no_commit = {
        v for v in shipped
        if v not in bumped
        and v != guard.current_version()
        and guard.version_key(v) >= (1, 38, 0)
    }
    assert no_commit == set(guard.UNTAGGED_BY_DECISION)


# --- reconstructed entries are marked as such ----------------------------

def test_reconstructed_entries_declare_themselves(text):
    """Nobody should mistake a reconstruction for a contemporaneous record."""
    reconstructed = text.count("> Reconstructed 2026-09-29 from commit")
    assert reconstructed == 137, f"expected 137 reconstructed entries, found {reconstructed}"
    assert text.count("Not a contemporaneous record.") >= reconstructed


def test_every_reconstructed_entry_cites_a_real_commit(text):
    """The provenance line must name a commit that actually exists."""
    if not guard.in_git_repo():
        pytest.skip("not a git repo")
    shas = re.findall(r"> Reconstructed 2026-09-29 from commit `([0-9a-f]+)`", text)
    assert len(shas) == 137
    for sha in sorted(set(shas)):
        done = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "-t", sha],
            capture_output=True, text=True, timeout=60)
        assert done.stdout.strip() == "commit", f"{sha} is not a commit"


# --- the module's own entry points ---------------------------------------

def test_check_head_passes_on_the_real_repo():
    assert guard.check_head() == []


def test_the_module_runs_clean_as_a_script():
    done = subprocess.run(
        ["python3", str(REPO_ROOT / "changelog_guard.py")],
        capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok" in done.stdout


def test_check_tags_SKIPS_when_no_tags_are_present():
    """A shallow clone fetches no tags. That must not be read as 160 missing
    tags -- the check has nothing to say, so it says nothing."""
    assert guard.check_tags(["1.140.0", "2.9.0"], set()) == []


def test_check_tags_ignores_versions_below_the_floor():
    assert guard.check_tags(["0.9.0"], {"v2.9.0"}, floor="1.0.0") == []
