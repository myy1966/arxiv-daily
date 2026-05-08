#!/usr/bin/env python3
"""
arxiv_daily.py - Download arxiv daily listings, generate LLM summaries, and produce markdown.

Usage:
    python3 arxiv_daily.py cs.AI \\
      --output ~/arxiv-daily/_posts \\
      --tags ~/arxiv-daily/data/tags.json

Environment variables (all required):
    ARXIV_UPDATE_KEY   - LLM API key
    ARXIV_UPDATE_API   - LLM API endpoint (e.g. https://api.openai.com/v1)
    ARXIV_UPDATE_MODEL - LLM model name (e.g. gpt-4o)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

try:
    from json_repair import loads as json_repair_loads
except ImportError:
    json_repair_loads = None

BATCH_SIZE = 16
MAX_RETRIES = 3
RETRY_DELAYS = [2, 4, 8]


def _retry_delay(attempt: int) -> float:
    """Return the configured retry delay for an attempt index."""
    return RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]


def _retry_after_delay(headers, fallback: float) -> float:
    """Parse Retry-After seconds, falling back when absent or malformed."""
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after is None:
        return fallback
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return fallback

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(cache_base: Path) -> logging.Logger:
    cache_base.mkdir(parents=True, exist_ok=True)
    log_path = cache_base / "arxiv_daily.log"
    logger = logging.getLogger("arxiv_daily")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    logger.addHandler(fh)
    return logger


# ---------------------------------------------------------------------------
# Phase 1: Download & Parse
# ---------------------------------------------------------------------------

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


def parse_listing_date(html: str) -> tuple[str | None, str]:
    """Extract listing date and page type from HTML.

    Returns:
        (date_str in YYYY-MM-DD or None, page_type "new" or "catchup" or "unknown")
    """
    # /list/{field}/new page: "Showing new listings for Friday, 12 April 2026"
    m = re.search(r"Showing new listings for \w+,\s*(\d+ \w+ \d+)", html)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%d %B %Y")
            return dt.strftime("%Y-%m-%d"), "new"
        except ValueError:
            pass

    # /catchup/{field}/{date} page: "Total of 16 entries for Fri, 10 Apr 2026"
    m = re.search(r"Total of \d+ entries for \w+,\s*(\d+ \w+ \d+)", html)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%d %b %Y")
            return dt.strftime("%Y-%m-%d"), "catchup"
        except ValueError:
            pass

    return None, "unknown"


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
        if "Cross" in name:
            name = "Cross submissions"
        elif "Replacement" in name:
            name = "Replacement submissions"
        sections.append((name, int(m.group(2)), m.start()))
    return sections


def extract_code_urls(html_fragment: str) -> list[str]:
    """Extract code repository URLs from HTML href attributes."""
    urls = []
    href_pattern = re.compile(r'href="(https?://[^"]+)"')
    for m in href_pattern.finditer(html_fragment):
        url = m.group(1)
        if "github.com" in url or "gitlab.com" in url:
            url = re.sub(r"^http://", "https://", url)
            urls.append(url)
    return list(dict.fromkeys(urls))


def extract_paper(chunk: str) -> dict | None:
    """Extract a single paper's data from an HTML chunk."""
    paper = {}

    id_match = re.search(r"arXiv:(\d+\.\d+)", chunk)
    if not id_match:
        return None
    paper["id"] = id_match.group(1)

    title_match = re.search(
        r"""<span class=['"]descriptor['"]>Title:</span>\s*(.*?)\s*</div>""", chunk, re.DOTALL
    )
    if title_match:
        paper["title"] = clean_html(title_match.group(1))

    authors_match = re.search(r"""<div class=['"]list-authors['"]>(.*?)</div>""", chunk, re.DOTALL)
    if authors_match:
        names = re.findall(r">([^<]+)</a>", authors_match.group(1))
        paper["authors"] = ", ".join(names)

    subjects_match = re.search(
        r"""<div class=['"]list-subjects['"]><span class=['"]descriptor['"]>Subjects:</span>\s*(.*?)</div>""",
        chunk, re.DOTALL,
    )
    if subjects_match:
        raw = subjects_match.group(1)
        # Extract abbreviation codes in parentheses, preserving original order
        codes = re.findall(r"\(([a-z-]+\.[A-Z]+)\)", raw)
        paper["subjects"] = "; ".join(codes) if codes else clean_html(raw)

    comments_html = ""
    comments_match = re.search(
        r"""<div class=['"]list-comments mathjax['"]><span class=['"]descriptor['"]>Comments:</span>\s*(.*?)</div>""",
        chunk, re.DOTALL,
    )
    if comments_match:
        comments_html = comments_match.group(1)
        paper["comments"] = clean_html(comments_html)

    abstract_html = ""
    abstract_match = re.search(r"""<p class=['"]mathjax['"]>\s*(.*?)\s*</p>""", chunk, re.DOTALL)
    if abstract_match:
        abstract_html = abstract_match.group(1)
        paper["abstract"] = clean_html(abstract_html)

    # Extract code URLs from both abstract and comments HTML
    code_urls = extract_code_urls(abstract_html + " " + comments_html)
    if code_urls:
        paper["code_urls"] = code_urls

    return paper


def parse_html(html: str, field: str) -> dict:
    """Phase 1: Parse HTML into structured data.

    Returns:
        {
            "listing_date": "YYYY-MM-DD" or None,
            "page_type": "new" or "catchup" or "unknown",
            "field": field,
            "sections": [{"name": ..., "count": ..., "papers": [...]}],
        }
    Papers include: id, title, authors, subjects, comments, code_urls, abstract
    """
    listing_date, page_type = parse_listing_date(html)
    sections = parse_sections(html)

    if not sections:
        return {"listing_date": listing_date, "page_type": page_type, "field": field, "sections": []}

    dt_pattern = re.compile(r"<dt>\s*<a name=['\"]item(\d+)['\"]", re.DOTALL)
    all_items = list(dt_pattern.finditer(html))

    def get_section_name(pos: int) -> str:
        for i, (name, _, sec_pos) in enumerate(sections):
            next_pos = sections[i + 1][2] if i + 1 < len(sections) else len(html)
            if sec_pos <= pos < next_pos:
                return name
        return "Unknown"

    section_papers: dict[str, list[dict]] = {}
    for i, item_match in enumerate(all_items):
        start = item_match.start()
        end = all_items[i + 1].start() if i + 1 < len(all_items) else len(html)
        chunk = html[start:end]
        paper = extract_paper(chunk)
        if not paper:
            continue
        section_name = get_section_name(start)
        section_papers.setdefault(section_name, []).append(paper)

    result_sections = []
    for name, count, _ in sections:
        papers = section_papers.get(name, [])
        result_sections.append({"name": name, "count": count, "papers": papers})

    return {"listing_date": listing_date, "page_type": page_type, "field": field, "sections": result_sections}


# ---------------------------------------------------------------------------
# Phase 2: LLM Summarization
# ---------------------------------------------------------------------------

def build_prompt(batch_papers: list[dict], current_tags: list[str]) -> list[dict]:
    """Build chat messages for LLM API call."""
    tags_str = ", ".join(current_tags) if current_tags else "(empty)"
    papers_text = []
    for p in batch_papers:
        entry = f"ID: {p['id']}\nTitle: {p.get('title', 'N/A')}\nAbstract: {p.get('abstract', 'N/A')}"
        if p.get("comments"):
            entry += f"\nComments: {p['comments']}"
        papers_text.append(entry)

    user_content = f"""Below are {len(batch_papers)} papers. For each paper:
1. Write a 1-3 sentence Chinese summary of the abstract.
2. Assign 1-3 tags from the existing tag list.
   - Tags should be at the level of specific research topics or methods, NOT broad categories.
   - If a broad tag (e.g. "LLM") exists but a more specific subtopic tag (e.g. "LLM Reasoning", "LLM Agent") also exists and fits better, prefer the specific one.
   - If no existing tag adequately captures the paper's specific topic, create a new one.
   - New tags must be in English, concise (1-3 words), and at a similar granularity as existing specific tags.
   - BAD granularity: "LLM", "NLP", "Computer Vision" (too broad)
   - GOOD granularity: "LLM Reasoning", "LLM Alignment", "Object Detection", "Code Generation"
3. If Comments indicate the paper is accepted/published (keywords: accepted, published, to appear, camera-ready, appearing at, presented at), extract the venue abbreviation + year (e.g. "ACL 2026"). Workshop papers should be formatted as "{{Conference}} {{Year}} Workshop". If only submitted/under review/preprint, set venue to null.

Current tag list: {tags_str}

Papers:
---
{"---".join(papers_text)}
---

Respond in strict JSON format:
{{
  "papers": [
    {{"id": "...", "summary": "Chinese summary", "tags": ["tag1", "tag2"], "venue": "VENUE 2026 or null"}}
  ],
  "new_tags": ["new_tag1"]
}}"""

    return [
        {
            "role": "system",
            "content": (
                "You are a research paper summarization and classification assistant. "
                "Output valid JSON only, no markdown fences."
            ),
        },
        {"role": "user", "content": user_content},
    ]


def _validate_llm_schema(result: object) -> dict:
    """Validate the top-level LLM response schema before downstream matching."""
    if not isinstance(result, dict):
        raise ValueError(f"LLM response must be a JSON object, got {type(result).__name__}")
    if not isinstance(result.get("papers"), list):
        raise ValueError(f"LLM response missing 'papers' list, got keys: {list(result.keys())}")
    if "new_tags" in result and not isinstance(result["new_tags"], list):
        raise ValueError(f"'new_tags' must be list, got {type(result['new_tags']).__name__}")
    return result


def call_llm(
    messages: list[dict],
    api_endpoint: str,
    api_key: str,
    model: str,
    logger: logging.Logger,
    batch_num: int = 0,
    debug_dir: Path | None = None,
    batch_label: str | None = None,
) -> tuple[dict, dict]:
    """Call LLM API with retries using streaming mode.

    Returns:
        (parsed_result, usage_info)
        usage_info: {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    """
    label = batch_label or f"batch_{batch_num:03d}"
    log_label = batch_label or f"Batch {batch_num:03d}"
    url = f"{api_endpoint.rstrip('/')}/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 65536,
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            # Set up live stream debug file
            stream_path = None
            stream_file = None
            if debug_dir is not None:
                debug_dir.mkdir(parents=True, exist_ok=True)
                stream_path = debug_dir / f"{label}_stream_attempt{attempt + 1}.txt"
                stream_file = open(stream_path, "w", encoding="utf-8")
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    content_parts = []
                    usage = {}
                    for raw_line in resp:
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        if stream_file is not None:
                            stream_file.write(line + "\n")
                            stream_file.flush()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        chunk = json.loads(data_str)
                        # Extract usage from the final chunk
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        # Extract content delta
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            if delta.get("content"):
                                content_parts.append(delta["content"])
            finally:
                if stream_file is not None:
                    stream_file.close()

            content = "".join(content_parts).strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
            try:
                result = json.loads(content)
            except json.JSONDecodeError as je:
                # Try json_repair as fallback before giving up
                if json_repair_loads is not None:
                    try:
                        result = json_repair_loads(content)
                    except Exception:
                        result = None
                    else:
                        logger.warning(
                            f"[Phase2] {log_label} attempt {attempt + 1}: "
                            f"json.loads failed ({je}), repaired by json_repair"
                        )
                        return _validate_llm_schema(result), usage
                # Save debug info for JSON parse failures
                if debug_dir is not None:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    debug_path = debug_dir / f"{label}_json_error_attempt{attempt + 1}.json"
                    debug_data = {
                        "error": str(je),
                        "prompt": messages[-1]["content"] if messages else "",
                        "raw_response": content,
                        "usage": usage,
                    }
                    with open(debug_path, "w", encoding="utf-8") as df:
                        json.dump(debug_data, df, ensure_ascii=False, indent=2)
                logger.error(f"[Phase2] {log_label} LLM call attempt {attempt + 1} JSON parse failed: {je}")
                if debug_dir is not None:
                    logger.error(f"[Phase2] {log_label} JSON parse debug saved to {debug_path}")
                raise je
            return _validate_llm_schema(result), usage
        except urllib.error.HTTPError as e:
            last_error = e
            logger.error(
                f"[Phase2] {log_label} LLM call attempt {attempt + 1} HTTP {e.code}: {e.reason}"
            )
            try:
                logger.error(f"[Phase2] {log_label} response body: {e.read().decode('utf-8')}")
            except Exception:
                pass
            if 400 <= e.code < 500 and e.code != 429:
                raise
            if attempt < MAX_RETRIES - 1:
                fallback_delay = _retry_delay(attempt)
                if e.code == 429:
                    delay = _retry_after_delay(e.headers, fallback_delay)
                    logger.warning(f"[Phase2] {log_label} rate limited, waiting {delay:g}s")
                else:
                    delay = fallback_delay
                time.sleep(delay)
        except json.JSONDecodeError as e:
            last_error = e
            logger.warning(
                f"[Phase2] {log_label} LLM returned invalid JSON on attempt {attempt + 1}; "
                "retrying to request a fresh JSON response"
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(_retry_delay(attempt))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last_error = e
            logger.error(f"[Phase2] {log_label} LLM call attempt {attempt + 1} failed: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(_retry_delay(attempt))

    raise last_error  # type: ignore[misc]


def match_llm_results(
    batch: list[dict], llm_papers: list[dict], logger: logging.Logger, batch_num: int,
    raw_result: dict | None = None, debug_dir: Path | None = None,
    batch_label: str | None = None, attempt: int = 0,
) -> tuple[dict[str, dict], int]:
    """Match LLM results to batch papers. Returns ({paper_id: {summary, tags, venue}}, unmatched_count).

    Strategy:
    1. Try exact ID match first
    2. Try normalized ID match (strip version suffix like v1, v2)
    3. Fall back to positional matching for remaining unmatched papers
    """
    label = batch_label or f"batch_{batch_num:03d}"
    log_label = batch_label or f"Batch {batch_num:03d}"
    attempt_suffix = f"_attempt{attempt}" if attempt > 0 else ""
    results: dict[str, dict] = {}
    batch_ids = [p["id"] for p in batch]
    used_llm_indices: set[int] = set()

    # Build lookup by exact and normalized ID
    llm_by_id: dict[str, int] = {}
    llm_by_norm: dict[str, int] = {}
    for i, p in enumerate(llm_papers):
        pid = p.get("id", "")
        llm_by_id[pid] = i
        norm = re.sub(r"v\d+$", "", pid)
        llm_by_norm[norm] = i

    # Pass 1: exact match
    for batch_id in batch_ids:
        if batch_id in llm_by_id:
            idx = llm_by_id[batch_id]
            p = llm_papers[idx]
            results[batch_id] = {
                "summary": p.get("summary", ""),
                "tags": p.get("tags", []),
                "venue": p.get("venue"),
            }
            used_llm_indices.add(idx)

    # Pass 2: normalized match for remaining
    for batch_id in batch_ids:
        if batch_id in results:
            continue
        norm = re.sub(r"v\d+$", "", batch_id)
        if norm in llm_by_norm:
            idx = llm_by_norm[norm]
            if idx not in used_llm_indices:
                p = llm_papers[idx]
                results[batch_id] = {
                    "summary": p.get("summary", ""),
                    "tags": p.get("tags", []),
                    "venue": p.get("venue"),
                }
                used_llm_indices.add(idx)
                logger.warning(
                    f"[Phase2] {log_label}: matched {batch_id} to LLM id {p.get('id', '')} by normalization"
                )

    # Pass 3: quality check - remove papers with missing tags or short summary
    MIN_SUMMARY_LEN = 50
    quality_failures: list[dict] = []
    for bid in list(results.keys()):
        r = results[bid]
        issues = []
        if not r.get("tags"):
            issues.append("missing tags")
        if len(r.get("summary", "")) < MIN_SUMMARY_LEN:
            issues.append(f"summary too short ({len(r.get('summary', ''))} chars < {MIN_SUMMARY_LEN})")
        if issues:
            quality_failures.append({"id": bid, "issues": issues, "result": r})
            del results[bid]
    if quality_failures:
        logger.error(
            f"[Phase2] {log_label}: {len(quality_failures)} papers failed quality check: "
            + ", ".join(f"{qf['id']}({', '.join(qf['issues'])})" for qf in quality_failures)
        )
        if debug_dir is not None and raw_result is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / f"{label}_quality_error{attempt_suffix}.json"
            debug_data = {
                "batch_input_ids": batch_ids,
                "quality_failures": quality_failures,
                "llm_response": raw_result,
            }
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            logger.error(f"[Phase2] {log_label} quality debug saved to {debug_path}")

    # Pass 4: report unmatched papers (excluding quality failures already reported)
    quality_failed_ids = {qf["id"] for qf in quality_failures}
    unmatched_batch = [bid for bid in batch_ids if bid not in results and bid not in quality_failed_ids]
    if unmatched_batch:
        unmatched_llm_ids = [
            llm_papers[i].get("id", "?") for i in range(len(llm_papers)) if i not in used_llm_indices
        ]
        logger.error(
            f"[Phase2] {log_label}: {len(unmatched_batch)} papers unmatched "
            f"(batch IDs: {unmatched_batch}, LLM IDs: {unmatched_llm_ids})"
        )
        if debug_dir is not None and raw_result is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / f"{label}_id_mismatch{attempt_suffix}.json"
            debug_data = {
                "batch_input_ids": batch_ids,
                "prompt_papers": [
                    {"id": p["id"], "title": p.get("title", ""), "abstract": p.get("abstract", "")[:200]}
                    for p in batch
                ],
                "llm_response": raw_result,
                "unmatched_batch_ids": unmatched_batch,
                "unmatched_llm_ids": unmatched_llm_ids,
            }
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            logger.error(f"[Phase2] {log_label} debug info saved to {debug_path}")

    return results, len(unmatched_batch) + len(quality_failures)


def compute_metadata_hash(data: dict) -> str:
    """Compute SHA-256 of sorted paper IDs for cache validity."""
    all_ids = []
    for section in data["sections"]:
        for paper in section["papers"]:
            all_ids.append(paper["id"])
    all_ids.sort()
    return hashlib.sha256(json.dumps(all_ids).encode()).hexdigest()


def run_phase2(
    data: dict,
    tags_path: Path,
    cache_dir: Path,
    no_cache: bool,
    api_endpoint: str,
    api_key: str,
    model: str,
    logger: logging.Logger,
    batch_prefix: str = "batch",
    save_cache: bool = True,
) -> tuple[dict, int]:
    """Phase 2: LLM summarization.

    Returns:
        (results_by_id, failed_batches_count)
        results_by_id: {paper_id: {"summary": ..., "tags": [...], "venue": ...}}
    """
    # Load tags
    if tags_path.exists():
        with open(tags_path, "r", encoding="utf-8") as f:
            tags_data = json.load(f)
        current_tags = tags_data.get("tags", [])
        # Handle old format where tags might be objects
        if current_tags and isinstance(current_tags[0], dict):
            current_tags = [t.get("name", str(t)) for t in current_tags]
    else:
        tags_path.parent.mkdir(parents=True, exist_ok=True)
        current_tags = []

    # Collect all papers with abstracts into batches
    all_papers = []
    for section in data["sections"]:
        for paper in section["papers"]:
            if paper.get("abstract"):
                all_papers.append(paper)

    batches = []
    for i in range(0, len(all_papers), BATCH_SIZE):
        batches.append(all_papers[i : i + BATCH_SIZE])

    if not batches:
        return {}, 0

    # Cache setup
    field_cache = cache_dir / data["field"]
    field_cache.mkdir(parents=True, exist_ok=True)
    debug_dir = cache_dir / "debug" / data["field"]

    # Check cache validity
    cached_batches: dict[int, dict] = {}
    if save_cache:
        current_hash = compute_metadata_hash(data)
        hash_file = field_cache / "metadata_hash"
        if not no_cache and hash_file.exists():
            old_hash = hash_file.read_text().strip()
            if old_hash == current_hash:
                for bf in sorted(field_cache.glob("batch_*.json")):
                    try:
                        with open(bf, "r", encoding="utf-8") as f:
                            cached = json.load(f)
                        batch_idx = int(bf.stem.split("_")[1])
                        cached_batches[batch_idx] = cached
                        if cached.get("current_tags"):
                            current_tags = cached["current_tags"]
                    except Exception:
                        pass
            else:
                for bf in field_cache.glob("batch_*.json"):
                    bf.unlink()
        hash_file.write_text(current_hash)

    results_by_id: dict[str, dict] = {}
    failed_indices: list[int] = []

    for idx, batch in enumerate(batches):
        batch_num = idx + 1
        total = len(batches)
        blabel = f"{batch_prefix}_{batch_num:03d}"

        if batch_num in cached_batches:
            cached = cached_batches[batch_num]
            matched, unmatched = match_llm_results(batch, cached.get("papers", []), logger, batch_num, cached, debug_dir, batch_label=blabel)
            results_by_id.update(matched)
            if cached.get("current_tags"):
                current_tags = cached["current_tags"]
            status = "cached" if unmatched == 0 else f"cached ({unmatched} unmatched)"
            print(f"[{batch_num}/{total}] {blabel} {status}", file=sys.stderr)
            continue

        messages = build_prompt(batch, current_tags)
        t0 = time.monotonic()
        try:
            result, usage = call_llm(messages, api_endpoint, api_key, model, logger, batch_num, debug_dir, batch_label=blabel)

            matched, unmatched = match_llm_results(batch, result.get("papers", []), logger, batch_num, result, debug_dir, batch_label=blabel)
            results_by_id.update(matched)

            new_tags = result.get("new_tags", [])
            for t in new_tags:
                if t not in current_tags:
                    current_tags.append(t)

            elapsed = time.monotonic() - t0
            usage_str = ""
            if usage:
                pt = usage.get("prompt_tokens", 0)
                ct = usage.get("completion_tokens", 0)
                tt = usage.get("total_tokens", 0)
                usage_str = f", tokens: {pt}+{ct}={tt}"
            if unmatched > 0:
                failed_indices.append(idx)
                print(
                    f"[{batch_num}/{total}] {blabel} FAILED ({unmatched} unmatched) ({elapsed:.1f}s{usage_str}), see log",
                    file=sys.stderr,
                )
            else:
                if save_cache:
                    cache_entry = {
                        "papers": result.get("papers", []),
                        "new_tags": new_tags,
                        "current_tags": list(current_tags),
                    }
                    cache_path = field_cache / f"batch_{batch_num:03d}.json"
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache_entry, f, ensure_ascii=False, indent=2)
                print(f"[{batch_num}/{total}] {blabel} done ({elapsed:.1f}s{usage_str})", file=sys.stderr)

        except Exception as e:
            elapsed = time.monotonic() - t0
            failed_indices.append(idx)
            logger.error(f"[Phase2] {blabel} failed after all retries: {e}")
            print(
                f"[{batch_num}/{total}] {blabel} FAILED ({elapsed:.1f}s), see log",
                file=sys.stderr,
            )

    # Retry failed batches once
    if failed_indices:
        print(f"Retrying {len(failed_indices)} failed batch(es)...", file=sys.stderr)
        still_failed = 0
        for idx in failed_indices:
            batch = batches[idx]
            batch_num = idx + 1
            total = len(batches)
            blabel = f"retry_{batch_prefix}_{batch_num:03d}"

            messages = build_prompt(batch, current_tags)
            t0 = time.monotonic()
            try:
                result, usage = call_llm(messages, api_endpoint, api_key, model, logger, batch_num, debug_dir, batch_label=blabel)

                matched, unmatched = match_llm_results(batch, result.get("papers", []), logger, batch_num, result, debug_dir, batch_label=blabel)
                results_by_id.update(matched)

                new_tags = result.get("new_tags", [])
                for t in new_tags:
                    if t not in current_tags:
                        current_tags.append(t)

                elapsed = time.monotonic() - t0
                usage_str = ""
                if usage:
                    pt = usage.get("prompt_tokens", 0)
                    ct = usage.get("completion_tokens", 0)
                    tt = usage.get("total_tokens", 0)
                    usage_str = f", tokens: {pt}+{ct}={tt}"
                if unmatched > 0:
                    still_failed += 1
                    print(
                        f"[{batch_num}/{total}] {blabel} FAILED ({unmatched} unmatched) ({elapsed:.1f}s{usage_str}), see log",
                        file=sys.stderr,
                    )
                else:
                    if save_cache:
                        cache_entry = {
                            "papers": result.get("papers", []),
                            "new_tags": new_tags,
                            "current_tags": list(current_tags),
                        }
                        cache_path = field_cache / f"batch_{batch_num:03d}.json"
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump(cache_entry, f, ensure_ascii=False, indent=2)
                    print(f"[{batch_num}/{total}] {blabel} done ({elapsed:.1f}s{usage_str})", file=sys.stderr)

            except Exception as e:
                elapsed = time.monotonic() - t0
                still_failed += 1
                logger.error(f"[Phase2] {blabel} failed: {e}")
                print(
                    f"[{batch_num}/{total}] {blabel} FAILED ({elapsed:.1f}s), see log",
                    file=sys.stderr,
                )

        failed_batches = still_failed
    else:
        failed_batches = 0

    # Save updated tags
    tags_out = {
        "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "tags": current_tags,
    }
    tags_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tags_path, "w", encoding="utf-8") as f:
        json.dump(tags_out, f, ensure_ascii=False, indent=2)

    return results_by_id, failed_batches


# ---------------------------------------------------------------------------
# Phase 3: Markdown Generation
# ---------------------------------------------------------------------------

SECTION_NAME_CN = {
    "New submissions": "新投稿",
    "Cross submissions": "跨领域投稿",
    "Replacement submissions": "替换投稿",
}


def extract_paper_abbrev(title: str) -> str | None:
    """Extract acronym/abbreviation from a paper title, if present.

    Common patterns:
        "ACRONYM: Description..."
        "Description (ACRONYM)"
        "ACRONYM - Description"
    """
    # "ACRONYM: ..."  or  "ACRONYM - ..."
    m = re.match(r"^([A-Z][A-Za-z0-9-]{1,15})[\s]*[:：\-–—]\s", title)
    if m:
        return m.group(1)
    # "... (ACRONYM)" at end or mid-title
    m = re.search(r"\(([A-Z][A-Za-z0-9-]{1,15})\)", title)
    if m:
        return m.group(1)
    return None


def generate_markdown(data: dict, results_by_id: dict, output_dir: Path, date_str: str) -> Path:
    """Phase 3: Generate markdown file. Pure deterministic logic, no LLM."""
    field = data["field"]

    try:
        listing_date = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        listing_date = datetime.now()

    date_display = f"{listing_date.year}年{listing_date.month}月{listing_date.day}日"

    # Assign global numbering
    global_num = 0
    for section in data["sections"]:
        for paper in section["papers"]:
            global_num += 1
            paper["_num"] = global_num
    total = global_num

    # Collect global tag frequencies
    global_tag_freq: dict[str, int] = {}
    for section in data["sections"]:
        for paper in section["papers"]:
            r = results_by_id.get(paper["id"], {})
            for tag in r.get("tags", []):
                global_tag_freq[tag] = global_tag_freq.get(tag, 0) + 1

    # Per-section summary: top-5 tags with representative papers
    section_summaries = []
    for section in data["sections"]:
        if not section["papers"]:
            continue
        sec_tag_papers: dict[str, list[dict]] = {}
        for paper in section["papers"]:
            r = results_by_id.get(paper["id"], {})
            for tag in r.get("tags", []):
                sec_tag_papers.setdefault(tag, []).append(paper)
        top_tags = sorted(sec_tag_papers.items(), key=lambda x: -len(x[1]))[:5]
        cn_name = SECTION_NAME_CN.get(section["name"], section["name"])
        tag_parts = []
        for tag, papers in top_tags:
            # Pick up to 3 representative papers, prefer those with abbreviations
            refs = []
            for p in papers:
                abbrev = extract_paper_abbrev(p.get("title", ""))
                if abbrev:
                    refs.append(f"{abbrev} [[{p['_num']}]](#{p['_num']})")
                if len(refs) >= 3:
                    break
            if len(refs) < 3:
                for p in papers:
                    if len(refs) >= 3:
                        break
                    ref_str = f"[[{p['_num']}]](#{p['_num']})"
                    if not any(ref_str in r for r in refs):
                        refs.append(ref_str)
            tag_parts.append(f"{tag} ({', '.join(refs[:3])})")
        section_summaries.append((cn_name, section["count"], ", ".join(tag_parts)))

    # Overall trend
    top3 = sorted(global_tag_freq.items(), key=lambda x: -x[1])[:3]
    trend_tags = "、".join(t[0] for t in top3) if top3 else "多方向"
    trend_line = f"今日论文主要聚焦于{trend_tags}等方向。"

    # Accepted papers (venue non-null)
    accepted = []
    for section in data["sections"]:
        for paper in section["papers"]:
            r = results_by_id.get(paper["id"], {})
            venue = r.get("venue")
            if venue:
                accepted.append((paper["_num"], venue))

    # Open source papers (code_urls non-empty)
    opensource = []
    for section in data["sections"]:
        for paper in section["papers"]:
            if paper.get("code_urls"):
                opensource.append((paper["_num"], paper["code_urls"]))

    # --- Build markdown ---
    lines = []

    # Front matter
    now = datetime.now(timezone(timedelta(hours=8)))
    lines.append("---")
    lines.append(f'title: "arXiv {field} Daily Update"')
    lines.append(f"date: {now.strftime('%Y-%m-%d %H:%M:%S +0800')}")
    lines.append(f"categories: [{field}]")
    lines.append("---")
    lines.append("")

    # Top summary
    lines.append(f"{field} 领域 {date_display} 共有 {total} 篇论文更新：")
    lines.append("")
    for cn_name, count, tag_desc in section_summaries:
        lines.append(f"- **{count} 篇{cn_name}**：{tag_desc}")
    lines.append("")
    lines.append(f"**整体趋势**：{trend_line}")
    lines.append("")

    # Accepted papers
    if accepted:
        refs = ", ".join(f"[[{num}]](#{num})({venue})" for num, venue in accepted)
        lines.append(f"**已录用论文**：{refs}")
    else:
        lines.append("**已录用论文**：无")
    lines.append("")

    # Open source papers
    if opensource:
        refs = []
        for num, urls in opensource:
            refs.append(f"[[{num}]](#{num})([code]({urls[0]}))")
        lines.append(f"**开源论文**：{', '.join(refs)}")
    else:
        lines.append("**开源论文**：无")
    lines.append("")

    # Paper sections
    for section in data["sections"]:
        if not section["papers"]:
            continue

        lines.append("---")
        lines.append("")
        cn_name = SECTION_NAME_CN.get(section["name"], section["name"])
        lines.append(f"## {cn_name} ({section['count']})")
        lines.append("")

        for paper in section["papers"]:
            num = paper["_num"]
            pid = paper["id"]
            title = paper.get("title", "Untitled")
            authors = paper.get("authors", "")
            subjects = paper.get("subjects", "")
            r = results_by_id.get(pid, {})
            summary = r.get("summary", "")
            tags = r.get("tags", [])

            lines.append(f'<h3 id="{num}">[{num}] {title}</h3>')

            if section["name"] == "Cross submissions":
                lines.append(f"- **arXiv**: [{pid}](https://arxiv.org/abs/{pid}) (cross-listed)")
            elif section["name"] == "Replacement submissions":
                lines.append(f"- **arXiv**: [{pid}](https://arxiv.org/abs/{pid}) (replaced)")
            else:
                lines.append(f"- **arXiv**: [{pid}](https://arxiv.org/abs/{pid})")

            lines.append(f"- **Authors**: {authors}")
            lines.append(f"- **Subjects**: {subjects}")
            if tags:
                lines.append(f"- **Tags**: {', '.join(tags)}")
            venue = r.get("venue")
            if venue:
                lines.append(f"- **Venue**: {venue}")
            if paper.get("code_urls"):
                code_links = ", ".join(f"[code]({u})" for u in paper["code_urls"])
                lines.append(f"- **Code**: {code_links}")
            lines.append(f"- **Summary**: {summary}")
            lines.append("")

    # Write file
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{date_str}-{field}.md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return output_file


def patch_markdown(md_path: Path, results_by_id: dict[str, dict], logger: logging.Logger) -> int:
    """Patch existing markdown file with new LLM results for specific papers.

    Replaces Tags and Summary lines for each paper in results_by_id.
    Returns number of papers successfully patched.
    """
    content = md_path.read_text(encoding="utf-8")
    patched = 0

    for pid, r in results_by_id.items():
        summary = r.get("summary", "")
        tags = r.get("tags", [])
        venue = r.get("venue")

        # Find the paper block by arXiv ID link
        # Pattern: from the arXiv line to the next heading or end
        paper_pattern = re.compile(
            rf"(- \*\*arXiv\*\*: \[{re.escape(pid)}\].*\n)"
            rf"((?:- \*\*(?:Authors|Subjects|Tags|Venue|Code|Summary)\*\*:.*\n)*)",
        )
        m = paper_pattern.search(content)
        if not m:
            logger.warning(f"[Patch] Paper {pid} not found in {md_path}")
            continue

        # Parse existing metadata lines, rebuild in canonical order
        arxiv_line = m.group(1)
        meta_lines = m.group(2)

        # Extract preserved fields
        existing = {}
        for line in meta_lines.splitlines():
            for key in ("Authors", "Subjects", "Code"):
                if line.startswith(f"- **{key}**:"):
                    existing[key] = line + "\n"

        # Rebuild in canonical order: Authors, Subjects, Tags, Venue, Code, Summary
        new_lines = [arxiv_line]
        if "Authors" in existing:
            new_lines.append(existing["Authors"])
        if "Subjects" in existing:
            new_lines.append(existing["Subjects"])
        if tags:
            new_lines.append(f"- **Tags**: {', '.join(tags)}\n")
        if venue:
            new_lines.append(f"- **Venue**: {venue}\n")
        if "Code" in existing:
            new_lines.append(existing["Code"])
        new_lines.append(f"- **Summary**: {summary}\n")

        new_block = "".join(new_lines)
        content = content[:m.start()] + new_block + content[m.end():]
        patched += 1
        logger.info(f"[Patch] Patched paper {pid}")

    md_path.write_text(content, encoding="utf-8")
    return patched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="arxiv daily listing summarizer")
    parser.add_argument("field", help="arXiv field, e.g. cs.AI, cs.CL")
    parser.add_argument("--output", required=True, help="Output directory for markdown files")
    parser.add_argument("--tags", required=True, help="Path to tags.json")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cache, full re-run")
    parser.add_argument("--date", help="Use saved HTML for this date (YYYY-MM-DD) instead of fetching")
    parser.add_argument("--patch", nargs="+", metavar="ID", help="Re-generate specific paper IDs and patch into existing md (requires --date)")
    parser.add_argument("--cache-dir", help="Cache directory (default: ~/.cache/arxiv-update)")
    args = parser.parse_args()

    cache_base = Path(args.cache_dir) if args.cache_dir else Path.home() / ".cache" / "arxiv-update"
    log_path = cache_base / "arxiv_daily.log"

    # Check env vars
    api_key = os.environ.get("ARXIV_UPDATE_KEY")
    api_endpoint = os.environ.get("ARXIV_UPDATE_API")
    model = os.environ.get("ARXIV_UPDATE_MODEL")

    missing = []
    if not api_key:
        missing.append("ARXIV_UPDATE_KEY")
    if not api_endpoint:
        missing.append("ARXIV_UPDATE_API")
    if not model:
        missing.append("ARXIV_UPDATE_MODEL")

    if missing:
        print(
            f"Error: Missing environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        print(f"Log: {log_path}", file=sys.stderr)
        sys.exit(2)

    logger = setup_logging(cache_base)

    # --- Patch mode ---
    if args.patch:
        if not args.date:
            print("Error: --patch requires --date", file=sys.stderr)
            sys.exit(2)

        logger.info(f"[Patch] Starting patch for field={args.field} date={args.date} ids={args.patch}")

        # Load saved HTML
        pages_dir = cache_base / "pages" / args.field
        saved_path = pages_dir / f"{args.date}.html"
        if not saved_path.exists():
            print(f"Error: No saved HTML for date {args.date} at {saved_path}", file=sys.stderr)
            sys.exit(2)
        html = saved_path.read_text(encoding="utf-8")
        full_data = parse_html(html, args.field)

        # Find requested papers and build a minimal data dict for run_phase2
        patch_ids = set(args.patch)
        patch_papers = []
        for section in full_data["sections"]:
            for paper in section["papers"]:
                if paper["id"] in patch_ids:
                    patch_papers.append(paper)
        found_ids = {p["id"] for p in patch_papers}
        missing_ids = patch_ids - found_ids
        if missing_ids:
            print(f"Warning: Papers not found in HTML: {', '.join(sorted(missing_ids))}", file=sys.stderr)
        if not patch_papers:
            print("Error: No matching papers found in saved HTML", file=sys.stderr)
            sys.exit(2)

        print(f"Regenerating {len(patch_papers)} paper(s): {', '.join(p['id'] for p in patch_papers)}", file=sys.stderr)

        # Build a minimal data dict with a single section containing all patch papers
        patch_data = {
            "field": args.field,
            "listing_date": args.date,
            "page_type": "patch",
            "sections": [{"name": "Patch", "count": len(patch_papers), "papers": patch_papers}],
        }

        tags_path = Path(args.tags)
        results_by_id, failed_batches = run_phase2(
            patch_data, tags_path, cache_base, no_cache=True,
            api_endpoint=api_endpoint, api_key=api_key, model=model, logger=logger,
            batch_prefix="patch_batch", save_cache=False,
        )

        if not results_by_id:
            print("Error: No papers successfully matched from LLM response", file=sys.stderr)
            sys.exit(1)

        # Patch markdown
        output_dir = Path(args.output)
        md_path = output_dir / f"{args.date}-{args.field}.md"
        if not md_path.exists():
            print(f"Error: Markdown file not found: {md_path}", file=sys.stderr)
            sys.exit(2)

        patched = patch_markdown(md_path, results_by_id, logger)
        total_failed = len(patch_ids) - len(results_by_id)
        print(f"Patched {patched}/{len(patch_ids)} paper(s) in {md_path}", file=sys.stderr)
        logger.info(f"[Patch] Completed: patched {patched} paper(s)")

        summary = {
            "mode": "patch",
            "listing_date": args.date,
            "field": args.field,
            "output_file": str(md_path),
            "requested": len(patch_ids),
            "patched": patched,
            "failed": total_failed,
        }
        print(json.dumps(summary, ensure_ascii=False))
        sys.exit(0 if total_failed == 0 else 1)

    # --- Normal mode ---
    logger.info(f"[Phase1] Starting run for field={args.field}")

    # Phase 1: Download and parse
    pages_dir = cache_base / "pages" / args.field
    pages_dir.mkdir(parents=True, exist_ok=True)
    field_cache = cache_base / args.field
    field_cache.mkdir(parents=True, exist_ok=True)

    html = None
    if args.date:
        # Load saved HTML for specified date
        saved_path = pages_dir / f"{args.date}.html"
        if not saved_path.exists():
            logger.error(f"[Phase1] No saved HTML for date {args.date} at {saved_path}")
            print(f"Error: No saved HTML for date {args.date} at {saved_path}", file=sys.stderr)
            print(f"Log: {log_path}", file=sys.stderr)
            sys.exit(2)
        html = saved_path.read_text(encoding="utf-8")
        logger.info(f"[Phase1] Using saved HTML for {args.date} from {saved_path}")
        print(f"Using saved HTML for {args.date} from {saved_path}", file=sys.stderr)
    else:
        print(f"Fetching arxiv.org/list/{args.field}/new ...", file=sys.stderr)
        try:
            html = fetch_page(args.field)
        except Exception as e:
            logger.error(f"[Phase1] Failed to fetch page: {e}")
            print(f"Error: Failed to fetch arxiv page: {e}", file=sys.stderr)
            print(f"Log: {log_path}", file=sys.stderr)
            sys.exit(2)

    data = parse_html(html, args.field)
    if not data["sections"]:
        logger.error("[Phase1] No sections found in page")
        print(
            "Error: No papers found. The field may not exist or no updates today.",
            file=sys.stderr,
        )
        print(f"Log: {log_path}", file=sys.stderr)
        sys.exit(2)

    page_date = data["listing_date"]  # YYYY-MM-DD or None
    page_type = data["page_type"]     # "new", "catchup", or "unknown"

    # Validate actual parsed content
    parsed_papers = sum(len(s["papers"]) for s in data["sections"])
    papers_with_abstract = sum(
        1 for s in data["sections"] for p in s["papers"] if p.get("abstract")
    )
    if parsed_papers == 0:
        logger.error("[Phase1] Sections found but no papers could be parsed from page")
        print("Error: No papers could be parsed. Page format may be unsupported.", file=sys.stderr)
        print(f"Log: {log_path}", file=sys.stderr)
        sys.exit(2)
    if papers_with_abstract == 0:
        logger.error(f"[Phase1] {parsed_papers} papers parsed but none have abstracts")
        print(f"Error: {parsed_papers} papers parsed but no abstracts extracted. Page format may be unsupported.", file=sys.stderr)
        print(f"Log: {log_path}", file=sys.stderr)
        sys.exit(2)

    total_papers = sum(s["count"] for s in data["sections"])
    if parsed_papers != total_papers:
        logger.warning(f"[Phase1] Parsed {parsed_papers} of {total_papers} papers, some may be missing")
        print(f"Warning: Parsed {parsed_papers} of {total_papers} papers", file=sys.stderr)

    print(f"Page type: {page_type}, page date: {page_date}", file=sys.stderr)
    print(f"Parsed {parsed_papers}/{total_papers} papers ({papers_with_abstract} with abstracts) in {len(data['sections'])} sections", file=sys.stderr)

    # Determine listing date and validate against --date if provided
    if args.date:
        # Validate --date format
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            logger.error(f"[Phase1] Invalid --date format: {args.date}")
            print(f"Error: Invalid --date format: {args.date}, expected YYYY-MM-DD", file=sys.stderr)
            sys.exit(2)
        # Validate page date matches --date
        if page_date and page_date != args.date:
            logger.error(
                f"[Phase1] Date mismatch: --date={args.date} but page contains date {page_date} ({page_type})"
            )
            print(
                f"Error: Date mismatch: --date={args.date} but page contains date {page_date} ({page_type})",
                file=sys.stderr,
            )
            print(f"Log: {log_path}", file=sys.stderr)
            sys.exit(2)
        date_str = args.date
    elif page_date:
        date_str = page_date
    else:
        logger.error("[Phase1] Unrecognized page format: cannot extract listing date")
        print("Error: Unrecognized page format, expected arxiv /list/new or /catchup page.", file=sys.stderr)
        print(f"Log: {log_path}", file=sys.stderr)
        sys.exit(2)

    # Save HTML to pages archive (keyed by listing date)
    page_archive_path = pages_dir / f"{date_str}.html"
    if not page_archive_path.exists():
        page_archive_path.write_text(html, encoding="utf-8")
        logger.info(f"[Phase1] Saved HTML to {page_archive_path}")

    # Check if this listing date has already been processed
    output_dir = Path(args.output)
    expected_output = output_dir / f"{date_str}-{args.field}.md"
    if expected_output.exists() and not args.no_cache and not args.date:
        logger.info(f"[Phase1] Skipped: {expected_output} already exists (listing date {date_str})")
        print(f"Skipped: {expected_output} already exists (listing date {date_str})", file=sys.stderr)
        summary = {
            "listing_date": date_str,
            "field": args.field,
            "output_file": str(expected_output),
            "sections": {s["name"]: s["count"] for s in data["sections"]},
            "total": total_papers,
            "failed_batches": 0,
            "skipped": True,
        }
        print(json.dumps(summary, ensure_ascii=False))
        sys.exit(0)

    # Phase 2: LLM summarization
    tags_path = Path(args.tags)
    results_by_id, failed_batches = run_phase2(
        data,
        tags_path,
        cache_base,
        args.no_cache,
        api_endpoint,
        api_key,
        model,
        logger,
    )

    # Phase 3: Generate markdown (only if all batches succeeded)
    output_dir = Path(args.output)

    if failed_batches > 0:
        logger.error(f"[Phase2] {failed_batches} batch(es) still failed after retry, skipping markdown generation")
        print(
            f"Error: {failed_batches} batch(es) failed. Markdown not generated. "
            f"Re-run with --date {date_str} to retry (cached batches will be reused).",
            file=sys.stderr,
        )
        print(f"Log: {log_path}", file=sys.stderr)

        summary = {
            "listing_date": date_str,
            "field": args.field,
            "output_file": None,
            "sections": {s["name"]: s["count"] for s in data["sections"]},
            "total": total_papers,
            "failed_batches": failed_batches,
        }
        print(json.dumps(summary, ensure_ascii=False))
        sys.exit(1)

    output_file = generate_markdown(data, results_by_id, output_dir, date_str)

    # Clean cache on full success
    if field_cache.exists():
        shutil.rmtree(field_cache)

    # Build summary JSON for stdout
    sections_summary = {}
    for s in data["sections"]:
        sections_summary[s["name"]] = s["count"]

    summary = {
        "listing_date": date_str,
        "field": args.field,
        "output_file": str(output_file),
        "sections": sections_summary,
        "total": total_papers,
        "failed_batches": 0,
    }
    print(json.dumps(summary, ensure_ascii=False))
    logger.info(f"[Phase3] Run completed successfully. Output: {output_file}")
    sys.exit(0)


if __name__ == "__main__":
    main()
