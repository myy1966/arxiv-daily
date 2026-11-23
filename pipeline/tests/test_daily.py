"""Tests for daily.py: run every field, validate, commit the day in one commit."""
import subprocess

import pytest

import daily
from conftest import SAMPLE_LISTING_HTML

DATE = "2025-04-11"
POSTS = {f"_posts/{DATE}-cs.AR.md", f"_posts/{DATE}-cs.AI.md"}


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    ).stdout


def _commit_count(repo):
    return int(_git(repo, "rev-list", "--count", "HEAD"))


@pytest.fixture
def site(tmp_path, monkeypatch, fake_llm_server):
    api_url, _ = fake_llm_server
    repo = tmp_path / "site"
    (repo / "_posts").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "data" / "tags.json").write_text('{"tags": ["LLM Reasoning"]}\n', encoding="utf-8")
    (repo / "data" / "venue_abbreviations.json").write_text(
        '{"aliases": {}, "full_names": {}}\n', encoding="utf-8",
    )
    (repo / "_posts" / ".keep").write_text("", encoding="utf-8")
    (tmp_path / "no-hooks").mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for key, value in [
        ("user.name", "Test"), ("user.email", "test@example.com"),
        ("commit.gpgsign", "false"), ("core.hooksPath", str(tmp_path / "no-hooks")),
    ]:
        _git(repo, "config", key, value)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init")

    cache = tmp_path / "cache"
    for field in ("cs.AI", "cs.AR"):
        (cache / "pages" / field).mkdir(parents=True)
        (cache / "pages" / field / f"{DATE}.html").write_text(SAMPLE_LISTING_HTML, encoding="utf-8")

    monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
    monkeypatch.setenv("ARXIV_UPDATE_API", api_url)
    monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
    return repo, cache


def _run(site, *extra):
    repo, cache = site
    return daily.main([*extra, "--repo", str(repo), "--date", DATE, "--cache-dir", str(cache)])


def _skipped_summaries(repo):
    return [
        {"field": f, "listing_date": DATE, "output_file": str(repo / f"_posts/{DATE}-{f}.md"), "total": 4, "skipped": True}
        for f in ("cs.AR", "cs.AI")
    ]


def test_commits_both_posts_and_changed_data_in_one_commit(site, capsys):
    repo, _ = site

    assert _run(site) == 0

    assert _commit_count(repo) == 2
    files = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert POSTS <= files <= POSTS | {daily.TAGS_FILE, daily.VENUES_FILE}
    assert daily.TAGS_FILE in files
    message = _git(repo, "log", "-1", "--format=%B")
    assert message.startswith(f"feat(posts): add arXiv posts for {DATE}\n\n")
    assert "cs.AR: 4 papers, 0 summaries reused" in message
    # cs.AI runs second on the same listing, so it keeps every cs.AR summary.
    assert "cs.AI: 4 papers, 4 summaries reused" in message
    assert "New tags: AI Evaluation" in message
    assert _git(repo, "log", "-1", "--format=%ad", "--date=iso").strip().endswith("+0800")
    assert _git(repo, "status", "--porcelain") == ""
    assert "Not pushed" in capsys.readouterr().out


def test_staged_changes_stop_the_run_before_any_field(site):
    repo, _ = site
    (repo / "notes.txt").write_text("draft", encoding="utf-8")
    _git(repo, "add", "notes.txt")

    assert _run(site) == 1

    assert _commit_count(repo) == 1
    assert not list((repo / "_posts").glob("*.md"))


def test_failed_field_commits_nothing_and_stops(site):
    repo, cache = site
    (cache / "pages" / "cs.AR" / f"{DATE}.html").unlink()

    assert _run(site) == 1

    assert _commit_count(repo) == 1
    assert not (repo / f"_posts/{DATE}-cs.AI.md").exists()


def test_no_commit_leaves_the_result_in_the_working_tree(site, capsys):
    repo, _ = site

    assert _run(site, "--no-commit") == 0

    assert _commit_count(repo) == 1
    assert {f"_posts/{p.name}" for p in (repo / "_posts").glob("*.md")} == POSTS
    assert "Not committed" in capsys.readouterr().out


def test_posts_left_uncommitted_by_an_earlier_run_are_committed(site):
    repo, _ = site
    assert _run(site, "--no-commit") == 0

    commit = daily.commit_day(repo, _skipped_summaries(repo), ["cs.AR: 4 papers (post from an earlier run)"])

    assert commit is not None and commit.endswith(f"feat(posts): add arXiv posts for {DATE}")
    assert POSTS <= set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())


def test_nothing_is_committed_when_the_posts_are_already_committed(site):
    repo, _ = site
    assert _run(site) == 0

    assert daily.commit_day(repo, _skipped_summaries(repo), []) is None
    assert _commit_count(repo) == 2
