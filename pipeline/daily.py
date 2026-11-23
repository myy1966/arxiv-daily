#!/usr/bin/env python3
"""Run the daily arXiv update for every field and commit the result.

Runs arxiv_daily.py for each field in turn and stops at the first failure,
checks the posts with validate_md.py, then commits that day's posts together
with the data files the runs changed, in one commit. It never pushes: a push
deploys the site.

    .venv/bin/python pipeline/daily.py              # today's listings, then commit
    .venv/bin/python pipeline/daily.py --no-commit  # leave the result uncommitted
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
SITE_ROOT = PIPELINE_DIR.parent
DEFAULT_FIELDS = ["cs.AR", "cs.AI"]
TAGS_FILE = "data/tags.json"
VENUES_FILE = "data/venue_abbreviations.json"
# The repository's commit hook requires +0800 commit dates.
COMMIT_TZ = "Asia/Singapore"


class DailyError(Exception):
    pass


def git(repo: Path, *args: str, input: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=input, capture_output=True, text=True, env=env,
    )


def git_ok(repo: Path, *args: str) -> str:
    proc = git(repo, *args)
    if proc.returncode != 0:
        raise DailyError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def pending_paths(repo: Path, paths: list[str]) -> list[str]:
    """The paths among `paths` that are untracked or differ from HEAD."""
    out = git_ok(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths)
    return [entry[3:] for entry in out.split("\0") if entry]


def json_at_head(repo: Path, path: str) -> dict:
    proc = git(repo, "show", f"HEAD:{path}")
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run_field(field: str, repo: Path, date: str | None, cache_dir: str | None) -> dict:
    cmd = [
        sys.executable, str(PIPELINE_DIR / "arxiv_daily.py"), field,
        "--output", str(repo / "_posts"),
        "--tags", str(repo / TAGS_FILE),
        "--venue-abbrevs", str(repo / VENUES_FILE),
    ]
    if date:
        cmd += ["--date", date]
    if cache_dir:
        cmd += ["--cache-dir", cache_dir]
    print(f"== {field}", file=sys.stderr, flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise DailyError(f"{field} failed (exit {proc.returncode}), nothing committed")
    lines = proc.stdout.strip().splitlines()
    if not lines:
        raise DailyError(f"{field} printed no run summary, nothing committed")
    return json.loads(lines[-1])


def validate(posts: list[Path]) -> None:
    proc = subprocess.run([sys.executable, str(PIPELINE_DIR / "validate_md.py"), *map(str, posts)])
    if proc.returncode != 0:
        raise DailyError("validate_md.py reported errors, nothing committed")


def describe(repo: Path, summaries: list[dict]) -> list[str]:
    """One line per field, then the tags and venue abbreviations added since HEAD."""
    lines = []
    for s in summaries:
        if s.get("skipped"):
            lines.append(f"{s['field']}: {s['total']} papers (post from an earlier run)")
        else:
            lines.append(f"{s['field']}: {s['total']} papers, {s.get('reused_summaries', 0)} summaries reused")
    old_tags = set(json_at_head(repo, TAGS_FILE).get("tags", []))
    new_tags = [t for t in read_json(repo / TAGS_FILE).get("tags", []) if t not in old_tags]
    if new_tags:
        lines.append(f"New tags: {', '.join(new_tags)}")
    old_venues = json_at_head(repo, VENUES_FILE).get("full_names", {})
    added = {
        k: v for k, v in read_json(repo / VENUES_FILE).get("full_names", {}).items() if k not in old_venues
    }
    if added:
        lines.append("New venue abbreviations: " + ", ".join(
            f"{v.get('abbr')}{' (review)' if v.get('review') else ''}" for v in added.values()
        ))
    return lines


def commit_day(repo: Path, summaries: list[dict], body: list[str]) -> str | None:
    """Commit the day's posts and changed data files; None when there is nothing new."""
    posts = {str(Path(s["output_file"]).resolve().relative_to(repo)): s["listing_date"] for s in summaries}
    pending = pending_paths(repo, list(posts) + [TAGS_FILE, VENUES_FILE])
    new_posts = [p for p in posts if p in pending]
    if not new_posts:
        return None
    verb = "update" if git_ok(repo, "ls-files", "--", *new_posts).split() else "add"
    dates = sorted({posts[p] for p in new_posts})
    message = "\n".join([f"feat(posts): {verb} arXiv posts for {', '.join(dates)}", "", *body, ""])
    git_ok(repo, "add", "--", *pending)
    proc = git(repo, "commit", "-q", "-F", "-", input=message, env={**os.environ, "TZ": COMMIT_TZ})
    if proc.returncode != 0:
        raise DailyError(
            "git commit failed; the files stay staged, fix the problem and commit them by hand:\n"
            + (proc.stderr or proc.stdout).strip()
        )
    return git_ok(repo, "log", "-1", "--format=%h %s").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the daily arXiv update and commit it")
    parser.add_argument("fields", nargs="*", help=f"arXiv fields in run order (default: {' '.join(DEFAULT_FIELDS)})")
    parser.add_argument("--date", help="Use saved HTML for this date (YYYY-MM-DD) instead of fetching")
    parser.add_argument("--cache-dir", help="Cache directory passed to arxiv_daily.py")
    parser.add_argument("--no-commit", action="store_true", help="Run and validate, but do not commit")
    parser.add_argument("--repo", default=str(SITE_ROOT), help="Site repository (default: %(default)s)")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    try:
        if not args.no_commit:
            staged = git_ok(repo, "diff", "--cached", "--name-only").split()
            if staged:
                raise DailyError(
                    "the index already has staged changes (" + ", ".join(staged)
                    + "); commit or unstage them first"
                )
        summaries = [run_field(f, repo, args.date, args.cache_dir) for f in args.fields or DEFAULT_FIELDS]
        validate([Path(s["output_file"]) for s in summaries])
        body = describe(repo, summaries)
        commit = None if args.no_commit else commit_day(repo, summaries, body)
    except DailyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print("\n".join(body))
    if args.no_commit:
        print("Not committed (--no-commit).")
    elif commit is None:
        print("No new posts, nothing committed.")
    else:
        print(f"Committed {commit}. Not pushed; push when the site should update.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
