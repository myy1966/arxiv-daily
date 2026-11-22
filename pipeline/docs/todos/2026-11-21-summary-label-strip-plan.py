"""Plan the regeneration of the label-strip backlog, one row per post position to change.

Every appearance of a listed paper in _posts is grouped by the abstract it had
that day (from the archived listing pages). Within a group the abstract is the
same, so every position gets the same summary:

- a summary written with the current prompt (a post dated 2026-09-16 or later,
  or one regenerated with --patch on 2026-11-21) that is not a copy is the
  source, and the listed positions and their copies take it ("copy");
- a group without such a summary is regenerated once at its first listed
  position ("regenerate"), and the group's other positions take that result
  ("copy").

A "copy" is a position in a post dated 2026-11-22 or later whose summary is
identical to a listed one of the same group: summary reuse wrote it there.
A listed position whose summary differs from the one at the backlog commit has
been regenerated since and counts as a source, so the plan can be re-run while
the work is in progress. Groups with a different abstract are left alone.

Run from the repository root:
    .venv/bin/python pipeline/docs/todos/2026-11-21-summary-label-strip-plan.py > plan.tsv
Columns: arxiv, field, date, role (listed/copy), action, source (field/date to copy from).

    .venv/bin/python pipeline/docs/todos/2026-11-21-summary-label-strip-plan.py --apply-copies plan.tsv
replaces the Summary line of every "copy" row with its source's summary, leaving
the other lines alone. A row whose source is still listed (not regenerated yet)
is skipped and reported.
"""
import csv
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "pipeline")
import arxiv_daily as a  # noqa: E402

PAGES = Path.home() / ".cache" / "arxiv-update" / "pages"
TSV = Path("pipeline/docs/todos/2026-11-21-summary-label-strip-backlog.tsv")
CURRENT_PROMPT_FROM = "2026-09-16"
REUSE_FROM = "2026-11-22"
# The commit that recorded the backlog list.
BACKLOG_COMMIT = "078b095"
# Commits of 2026-11-21 that regenerated summaries with --patch.
PATCH_COMMITS = ["df40a20", "496d402", "52919c2"]


def post_results_at(rev: str, name: str) -> dict:
    text = subprocess.run(
        ["git", "show", f"{rev}:_posts/{name}"], capture_output=True, text=True, check=True,
    ).stdout
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir) / name
        tmp.write_text(text, encoding="utf-8")
        return a.read_post_results(tmp)


def main() -> None:
    rows = list(csv.DictReader(TSV.open(encoding="utf-8"), delimiter="\t"))
    listed = {(r["field"], r["date"], r["arxiv"]) for r in rows}
    ids = {r["arxiv"] for r in rows}

    regenerated: set[tuple[str, str, str]] = set()
    for commit in PATCH_COMMITS:
        names = subprocess.run(
            ["git", "show", "--name-only", "--format=", commit, "--", "_posts"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        for path in names:
            name = Path(path).name
            date, field = a._POST_NAME_RE.match(name).groups()
            before, after = post_results_at(f"{commit}^", name), post_results_at(commit, name)
            for pid, r in after.items():
                if pid in before and before[pid]["summary"] != r["summary"]:
                    regenerated.add((field, date, pid))

    # The stripped summary of every listed position, as recorded; copies are matched
    # against these even after their listed position has been regenerated.
    stripped: dict[tuple[str, str, str], str] = {}
    done: set[tuple[str, str, str]] = set()
    for name in sorted({f"{d}-{f}.md" for f, d, _ in listed}):
        date, field = a._POST_NAME_RE.match(name).groups()
        then, now = post_results_at(BACKLOG_COMMIT, name), a.read_post_results(Path("_posts") / name)
        for f, d, pid in listed:
            if (f, d) == (field, date):
                stripped[(f, d, pid)] = then[pid]["summary"]
                if then[pid]["summary"] != now.get(pid, {}).get("summary"):
                    done.add((f, d, pid))
    listed -= done
    regenerated = (regenerated - listed) | done

    # (paper, abstract digest) -> positions [(field, date, summary)]
    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for md in sorted(Path("_posts").glob("2026-*-cs.*.md")):
        date, field = a._POST_NAME_RE.match(md.name).groups()
        results = a.read_post_results(md)
        hit = ids & results.keys()
        if not hit:
            continue
        data = a.parse_html((PAGES / field / f"{date}.html").read_text(encoding="utf-8"), field)
        digests = {
            p["id"]: a.abstract_digest(p["abstract"])
            for s in data["sections"] for p in s["papers"] if p.get("abstract")
        }
        for pid in hit:
            groups[(pid, digests.get(pid, "?"))].append((field, date, results[pid]["summary"]))

    out = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    out.writerow(["arxiv", "field", "date", "role", "action", "source"])
    for (pid, _), positions in sorted(groups.items()):
        group_stripped = {stripped[(f, d, pid)] for f, d, _ in positions if (f, d, pid) in stripped}
        if not group_stripped:
            continue
        targets, sources = [], []
        for f, d, s in positions:
            if (f, d, pid) in listed:
                targets.append((f, d, "listed"))
            elif d >= REUSE_FROM and s in group_stripped:
                targets.append((f, d, "copy"))
            elif d >= CURRENT_PROMPT_FROM or (f, d, pid) in regenerated:
                sources.append((f, d))
        if not targets:
            continue
        if sources:
            src = max(sources, key=lambda x: x[1])
            for f, d, role in targets:
                out.writerow([pid, f, d, role, "copy", f"{src[0]}/{src[1]}"])
        else:
            first = targets[0]
            out.writerow([pid, first[0], first[1], first[2], "regenerate", ""])
            for f, d, role in targets[1:]:
                out.writerow([pid, f, d, role, "copy", f"{first[0]}/{first[1]}"])


def apply_copies(plan_path: Path) -> None:
    plan = list(csv.DictReader(plan_path.open(encoding="utf-8"), delimiter="\t"))
    rows = [r for r in plan if r["action"] == "copy"]
    # Sources this plan still has to regenerate; re-run the plan once they are done.
    pending = {(r["arxiv"], f"{r['field']}/{r['date']}") for r in plan if r["action"] == "regenerate"}
    copied = skipped = 0
    for r in rows:
        if (r["arxiv"], r["source"]) in pending:
            print(f"skip {r['arxiv']} {r['field']}/{r['date']}: source {r['source']} not done", file=sys.stderr)
            skipped += 1
            continue
        src_field, src_date = r["source"].split("/")
        summary = a.read_post_results(Path(f"_posts/{src_date}-{src_field}.md"))[r["arxiv"]]["summary"]
        md = Path(f"_posts/{r['date']}-{r['field']}.md")
        text = md.read_text(encoding="utf-8")
        m = re.search(
            rf"^- \*\*arXiv\*\*: \[{re.escape(r['arxiv'])}\].*\n(?:- \*\*(?!Summary)\w+\*\*:.*\n)*(- \*\*Summary\*\*: .*)$",
            text, re.MULTILINE,
        )
        text = text[:m.start(1)] + "- **Summary**: " + a._escape_md_inline(summary) + text[m.end(1):]
        md.write_text(text, encoding="utf-8")
        copied += 1
    print(f"copied {copied}, skipped {skipped}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--apply-copies":
        apply_copies(Path(sys.argv[2]))
    else:
        main()
