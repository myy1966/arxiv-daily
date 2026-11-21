#!/usr/bin/env python3
"""Re-derive Venue/Presentation lines of existing posts with the current venue rules.

Two steps, so that editing data/venue_abbreviations.json only needs the second one:

  extract  call the venue LLM once per (arXiv id, comments) that passes the acceptance
           gate (or already shows a venue), appending raw entries to a JSONL file;
           re-running skips entries already in the file.
  apply    recompute every paper's Venue/Presentation from those raw entries (grounding,
           formatting, abbreviation table) and rewrite the Venue, Presentation and
           "已录用论文" lines; --dry-run only writes the report.

Needs the saved listing pages in ~/.cache/arxiv-update/pages and, for extract, the
ARXIV_UPDATE_* environment variables.

Usage:
  python pipeline/venue_backfill.py extract RAW.jsonl [--workers 8]
  python pipeline/venue_backfill.py apply RAW.jsonl [--report REPORT.jsonl] [--dry-run]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import arxiv_daily as a

REPO = Path(__file__).resolve().parent.parent
POSTS = REPO / "_posts"
PAGES = Path(os.path.expanduser("~/.cache/arxiv-update/pages"))
H3 = re.compile(r'^<h3 id="(\d+)">')
FIELD = re.compile(r"^- \*\*(Authors|Subjects|Tags|Venue|Presentation|Code|Summary)\*\*: ?(.*)$")
ORDER = ["Authors", "Subjects", "Tags", "Venue", "Presentation", "Code", "Summary"]
ACCEPTED = re.compile(r"^\*\*已录用论文\*\*：.*$", re.M)


def posts():
    return sorted(POSTS.glob("2026-*-cs.*.md"))


def listing_comments(post: Path) -> dict[str, str]:
    date, field = post.name[:10], post.name[11:-3]
    data = a.parse_html((PAGES / field / f"{date}.html").read_text(encoding="utf-8"), field)
    return {p["id"]: p.get("comments") or "" for s in data["sections"] for p in s["papers"]}


def paper_blocks(lines: list[str]):
    """Yield (num, arxiv_id, first_meta_index, end_index) for every paper block."""
    num, i = None, 0
    while i < len(lines):
        h = H3.match(lines[i])
        if h:
            num = int(h.group(1))
        m = re.match(r"^- \*\*arXiv\*\*: \[([^\]]+)\]", lines[i])
        if m and num is not None:
            j = i + 1
            while j < len(lines) and FIELD.match(lines[j]):
                j += 1
            yield num, m.group(1), i + 1, j
            i = j
            continue
        i += 1


def block_meta(lines, s, e) -> dict[str, str]:
    return {FIELD.match(lines[k]).group(1): lines[k] for k in range(s, e)}


def field_value(meta, key):
    return FIELD.match(meta[key]).group(2).strip() if key in meta else None


def accepted_line(lines: list[str]) -> str:
    """Rebuild "**已录用论文**：..." from the body, as generate_markdown writes it."""
    acc = []
    for num, _, s, e in paper_blocks(lines):
        venue = field_value(block_meta(lines, s, e), "Venue")
        if venue:
            acc.append((num, venue))
    if not acc:
        return "**已录用论文**：无"
    return "**已录用论文**：" + ", ".join(f"[[{n}]](#{n})({v})" for n, v in acc)


def wanted(comments: str, has_venue: bool) -> bool:
    # Early posts got venues from the summary model without the gate, so papers that
    # already show one are re-checked even when the gate misses their comments.
    return a.has_acceptance_signal(comments) or (has_venue and bool(comments.strip()))


def load_raw(path: Path) -> dict[tuple[str, str], dict | None]:
    raw = {}
    if path.exists():
        for line in path.open(encoding="utf-8"):
            rec = json.loads(line)
            raw[(rec["id"], rec["comments"])] = rec["item"]
    return raw


def cmd_extract(args) -> int:
    keys: set[tuple[str, str]] = set()
    for post in posts():
        comments_by_id = listing_comments(post)
        lines = post.read_text(encoding="utf-8").split("\n")
        for _, pid, s, e in paper_blocks(lines):
            comments = comments_by_id.get(pid, "")
            if wanted(comments, "Venue" in block_meta(lines, s, e)):
                keys.add((pid, comments))
    raw_path = Path(args.raw)
    todo = sorted(keys - set(load_raw(raw_path)))
    batches = [todo[i:i + a.VENUE_BATCH_SIZE] for i in range(0, len(todo), a.VENUE_BATCH_SIZE)]
    print(f"{len(keys)} (id, comments) to cover, {len(todo)} new in {len(batches)} batches", flush=True)
    if not batches:
        return 0
    cfg = a.validate_env()
    log = logging.getLogger("venue_backfill")
    log.addHandler(logging.FileHandler(raw_path.with_suffix(".log"), encoding="utf-8"))
    log.setLevel(logging.INFO)

    def run(batch):
        papers = [{"id": f"p{i:02d}", "comments": c} for i, (_, c) in enumerate(batch)]
        result = a.call_venue_llm(a.build_venue_prompt(papers), cfg.api_endpoint, cfg.api_key, cfg.model, log)
        if result is None:
            return batch, None
        by_id = {str(item.get("id")): item for item in result["venues"]}
        return batch, [by_id.get(p["id"]) for p in papers]

    t0, failed = time.monotonic(), 0
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("a", encoding="utf-8") as out, cf.ThreadPoolExecutor(args.workers) as pool:
        for n, (batch, items) in enumerate(pool.map(run, batches), 1):
            if items is None:
                failed += 1
                continue
            for (pid, comments), item in zip(batch, items):
                out.write(json.dumps({"id": pid, "comments": comments, "item": item}, ensure_ascii=False) + "\n")
            out.flush()
            if n % 50 == 0 or n == len(batches):
                print(f"{n}/{len(batches)} batches, {time.monotonic() - t0:.0f}s, {failed} failed", flush=True)
    return 1 if failed else 0


def cmd_apply(args) -> int:
    raw = load_raw(Path(args.raw))
    table = a.load_venue_abbrevs(Path(args.venue_abbrevs))
    known = set(table["full_names"])
    # Pass 1: let the table learn every full name first, so a venue is abbreviated the
    # same way in all posts regardless of which post spells its full name out.
    for (_, comments), item in sorted(raw.items(), key=lambda kv: kv[0]):
        a.resolve_venue_item(item or {}, comments, table)

    report, changed_posts = [], 0
    for post in posts():
        comments_by_id = listing_comments(post)
        lines = post.read_text(encoding="utf-8").split("\n")
        edits = []
        for num, pid, s, e in paper_blocks(lines):
            meta = block_meta(lines, s, e)
            old_v, old_p = field_value(meta, "Venue"), field_value(meta, "Presentation")
            comments = comments_by_id.get(pid, "")
            if not wanted(comments, old_v is not None):
                new = {"venue": None, "presentation": None}
            elif (pid, comments) not in raw:
                report.append({"post": post.name, "num": num, "id": pid, "status": "no_llm_result", "old": old_v})
                continue
            else:
                new = a.resolve_venue_item(raw[(pid, comments)] or {}, comments, table)
            if (new["venue"], new["presentation"]) == (old_v, old_p):
                continue
            report.append({"post": post.name, "num": num, "id": pid, "status": "changed", "old": old_v,
                           "new": new["venue"], "old_p": old_p, "new_p": new["presentation"],
                           "comments": comments[:300]})
            meta.pop("Venue", None)
            meta.pop("Presentation", None)
            if new["venue"]:
                meta["Venue"] = f"- **Venue**: {new['venue']}"
                if new["presentation"]:
                    meta["Presentation"] = f"- **Presentation**: {new['presentation']}"
            edits.append((s, e, [meta[k] for k in ORDER if k in meta]))
        if not edits:
            continue
        for s, e, new_meta in reversed(edits):
            lines[s:e] = new_meta
        text = "\n".join(lines)
        text = ACCEPTED.sub(lambda _: accepted_line(text.split("\n")), text, count=1)
        changed_posts += 1
        if not args.dry_run:
            post.write_text(text, encoding="utf-8")

    added = sorted(set(table["full_names"]) - known)
    if added and not args.dry_run:
        a.save_venue_abbrevs(Path(args.venue_abbrevs), table)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            for r in report:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    changed = sum(r["status"] == "changed" for r in report)
    missing = sum(r["status"] == "no_llm_result" for r in report)
    review = sum(1 for k in added if table["full_names"][k].get("review"))
    print(f"{'dry run: ' if args.dry_run else ''}{changed} papers changed in {changed_posts} posts; "
          f"{missing} without an LLM result; {len(added)} new abbreviations ({review} marked for review)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="call the venue LLM for papers not yet in RAW")
    ex.add_argument("raw")
    ex.add_argument("--workers", type=int, default=8)
    ex.set_defaults(func=cmd_extract)
    apl = sub.add_parser("apply", help="rewrite Venue/Presentation lines from RAW")
    apl.add_argument("raw")
    apl.add_argument("--report", help="write per-paper changes as JSONL")
    apl.add_argument("--dry-run", action="store_true")
    apl.add_argument("--venue-abbrevs", default=str(a.DEFAULT_VENUE_ABBREVS_PATH))
    apl.set_defaults(func=cmd_apply)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
