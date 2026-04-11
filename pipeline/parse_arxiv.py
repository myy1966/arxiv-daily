#!/usr/bin/env python3
"""
Parse arxiv daily listing page and extract structured paper data.

Usage:
    python parse_arxiv.py <field> [--output-dir DIR]

Example:
    python parse_arxiv.py cs.AI
    python parse_arxiv.py cs.CL --output-dir /tmp

Outputs two JSON files in the output directory:
    - metadata.json: paper ID, title, authors, subjects, comments, section info
    - abstracts.json: paper ID -> abstract text mapping
"""

import argparse
import json
import re
import sys
import urllib.request
from html import unescape
from pathlib import Path


def fetch_page(field: str) -> str:
    """Download the arxiv new listings page for the given field."""
    url = f"https://arxiv.org/list/{field}/new"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def clean_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_sections(html: str) -> list[tuple[str, int, int]]:
    """Find section headers and their positions: (name, count, position)."""
    pattern = re.compile(
        r"<h3>(New submissions|Cross submissions|Replacement submissions)"
        r"\s*\(showing \d+ of (\d+) entries\)</h3>",
        re.IGNORECASE,
    )
    sections = []
    for m in pattern.finditer(html):
        name = m.group(1)
        # Normalize section names
        if "Cross" in name:
            name = "Cross submissions"
        elif "Replacement" in name:
            name = "Replacement submissions"
        sections.append((name, int(m.group(2)), m.start()))
    return sections


def parse_listing_date(html: str) -> str | None:
    """Extract the listing date from the page header."""
    m = re.search(
        r"Showing new listings for \w+,\s*(\d+ \w+ \d+)", html
    )
    if m:
        return m.group(1)
    return None


def extract_paper(chunk: str) -> dict | None:
    """Extract a single paper's data from an HTML chunk."""
    paper = {}

    # arXiv ID
    id_match = re.search(r"arXiv:(\d+\.\d+)", chunk)
    if not id_match:
        return None
    paper["id"] = id_match.group(1)

    # Title
    title_match = re.search(
        r"<span class='descriptor'>Title:</span>\s*(.*?)\s*</div>",
        chunk,
        re.DOTALL,
    )
    if title_match:
        paper["title"] = clean_html(title_match.group(1))

    # Authors
    authors_match = re.search(
        r"<div class='list-authors'>(.*?)</div>", chunk, re.DOTALL
    )
    if authors_match:
        names = re.findall(r">([^<]+)</a>", authors_match.group(1))
        paper["authors"] = ", ".join(names)

    # Subjects
    subjects_match = re.search(
        r"<div class='list-subjects'><span class='descriptor'>Subjects:</span>"
        r"\s*(.*?)</div>",
        chunk,
        re.DOTALL,
    )
    if subjects_match:
        paper["subjects"] = clean_html(subjects_match.group(1))

    # Comments
    comments_match = re.search(
        r"<div class='list-comments mathjax'><span class='descriptor'>Comments:</span>"
        r"\s*(.*?)</div>",
        chunk,
        re.DOTALL,
    )
    if comments_match:
        paper["comments"] = clean_html(comments_match.group(1))

    # Abstract
    abstract_match = re.search(
        r"<p class='mathjax'>\s*(.*?)\s*</p>", chunk, re.DOTALL
    )
    if abstract_match:
        paper["abstract"] = clean_html(abstract_match.group(1))

    return paper


def parse_papers(html: str) -> dict:
    """
    Parse all papers from the HTML page.

    Returns a dict with keys:
        - listing_date: str
        - sections: list of {name, count, papers: [{id, title, authors, subjects, comments}]}
        - abstracts: {id: abstract_text}
    """
    listing_date = parse_listing_date(html)
    sections = parse_sections(html)

    if not sections:
        return {"listing_date": listing_date, "sections": [], "abstracts": {}}

    # Find all item positions
    dt_pattern = re.compile(r"<dt>\s*<a name=['\"]item(\d+)['\"]", re.DOTALL)
    all_items = list(dt_pattern.finditer(html))

    def get_section_name(pos: int) -> str:
        for i, (name, _, sec_pos) in enumerate(sections):
            next_pos = sections[i + 1][2] if i + 1 < len(sections) else len(html)
            if sec_pos <= pos < next_pos:
                return name
        return "Unknown"

    # Group papers by section
    section_papers: dict[str, list[dict]] = {}
    abstracts: dict[str, str] = {}

    for i, item_match in enumerate(all_items):
        start = item_match.start()
        end = all_items[i + 1].start() if i + 1 < len(all_items) else len(html)
        chunk = html[start:end]

        paper = extract_paper(chunk)
        if not paper:
            continue

        section_name = get_section_name(start)

        # Separate abstract from metadata
        abstract = paper.pop("abstract", None)
        if abstract:
            abstracts[paper["id"]] = abstract

        section_papers.setdefault(section_name, []).append(paper)

    # Build output sections
    result_sections = []
    for name, count, _ in sections:
        papers = section_papers.get(name, [])
        result_sections.append({"name": name, "count": count, "papers": papers})

    return {
        "listing_date": listing_date,
        "sections": result_sections,
        "abstracts": abstracts,
    }


def main():
    parser = argparse.ArgumentParser(description="Parse arxiv daily listings")
    parser.add_argument("field", help="arXiv field, e.g. cs.AI, cs.CL")
    parser.add_argument(
        "--output-dir",
        default="/tmp/arxiv",
        help="Output directory for JSON files (default: /tmp/arxiv)",
    )
    args = parser.parse_args()

    # Fetch
    print(f"Fetching arxiv.org/list/{args.field}/new ...", file=sys.stderr)
    try:
        html = fetch_page(args.field)
    except Exception as e:
        print(f"Error fetching page: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse
    result = parse_papers(html)
    if not result["sections"]:
        print("No papers found. The field may not exist or no updates today.", file=sys.stderr)
        sys.exit(1)

    # Output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "field": args.field,
        "listing_date": result["listing_date"],
        "sections": result["sections"],
    }
    meta_path = out_dir / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    abs_path = out_dir / "abstracts.json"
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(result["abstracts"], f, ensure_ascii=False, indent=2)

    # Summary
    total = sum(s["count"] for s in result["sections"])
    print(f"Date: {result['listing_date']}", file=sys.stderr)
    print(f"Total: {total} papers", file=sys.stderr)
    for s in result["sections"]:
        print(f"  {s['name']}: {s['count']}", file=sys.stderr)
    print(f"Output: {meta_path}, {abs_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
