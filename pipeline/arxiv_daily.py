#!/usr/bin/env python3
"""
arxiv_daily.py - Download arxiv daily listings, generate LLM summaries, and produce markdown.

Requires: Python >= 3.10

Usage:
    python3 arxiv_daily.py cs.AI \\
      --output ~/arxiv-daily/_posts \\
      --tags ~/arxiv-daily/data/tags.json

Environment variables (all required):
    ARXIV_UPDATE_KEY   - LLM API key
    ARXIV_UPDATE_API   - LLM API endpoint (e.g. https://api.openai.com/v1)
    ARXIV_UPDATE_MODEL - LLM model name (e.g. gpt-4o)

Optional:
    ARXIV_UPDATE_CONTACT    - Contact string included in the arXiv User-Agent
    ARXIV_UPDATE_USER_AGENT - Full User-Agent override for arXiv requests
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

try:
    from json_repair import loads as json_repair_loads
except ImportError:
    json_repair_loads = None


class ArxivDailyError(Exception):
    """Base exception for arxiv_daily pipeline errors."""

    exit_code: int = 1


class ConfigError(ArxivDailyError):
    """Missing environment variables or invalid CLI arguments."""

    exit_code = 2


class FetchError(ArxivDailyError):
    """Network or HTML fetch failure."""

    exit_code = 2


class ParseError(ArxivDailyError):
    """HTML parsing yielded no usable data."""

    exit_code = 2


class PipelineError(ArxivDailyError):
    """LLM or markdown generation pipeline failure."""

    exit_code = 1


@dataclass
class RunResult:
    summary: dict
    exit_code: int = 0


BATCH_SIZE = 16
MAX_RETRIES = 3
RETRY_DELAYS = [2, 4, 8]
MIN_SUMMARY_LEN = 30
SUMMARY_FAILURE_PLACEHOLDER = "[摘要生成失败]"
SUMMARY_FAILURE_TAG = "未分类"
PAPER_SEPARATOR = "\n=====PAPER_SEPARATOR=====\n"
OUTPUT_TZ = timezone(timedelta(hours=8))
ARXIV_FETCH_INTERVAL_SECONDS = 3.0
ARXIV_CONTACT = os.environ.get("ARXIV_UPDATE_CONTACT", "set-ARXIV_UPDATE_CONTACT")
ARXIV_USER_AGENT = os.environ.get(
    "ARXIV_UPDATE_USER_AGENT",
    f"arxiv-daily-bot/1.0 (contact: {ARXIV_CONTACT})",
)
_last_fetch_time: float | None = None


@dataclass
class BatchProcessResult:
    matched: dict[str, dict]
    unresolved: list[dict]
    failed: bool


@dataclass
class MatchDiagnostics:
    missing_ids: list[str]
    duplicate_ids: list[str]
    quality_failed: list[dict]

    @property
    def total_failed(self) -> int:
        return len(self.missing_ids) + len(self.duplicate_ids) + len(self.quality_failed)

    @property
    def all_failed_ids(self) -> list[str]:
        return self.missing_ids + self.duplicate_ids + [
            failure["id"] for failure in self.quality_failed
        ]


@dataclass(frozen=True)
class LlmConfig:
    api_key: str
    api_endpoint: str
    model: str


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

def _wait_for_arxiv_rate_limit() -> None:
    """Apply a courtesy delay before arXiv requests."""
    global _last_fetch_time

    if _last_fetch_time is None:
        delay = ARXIV_FETCH_INTERVAL_SECONDS
    else:
        elapsed = time.monotonic() - _last_fetch_time
        delay = max(0.0, ARXIV_FETCH_INTERVAL_SECONDS - elapsed)

    if delay > 0:
        time.sleep(delay)
    _last_fetch_time = time.monotonic()


def fetch_page(field: str) -> str:
    """Download the arxiv new listings page for the given field."""
    _wait_for_arxiv_rate_limit()
    url = f"https://arxiv.org/list/{field}/new"
    req = urllib.request.Request(url, headers={"User-Agent": ARXIV_USER_AGENT})
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
            "diagnostics": {"missing_title_ids": [...], "missing_abstract_ids": [...]},
        }
    Papers include: id, title, authors, subjects, comments, code_urls, abstract
    """
    listing_date, page_type = parse_listing_date(html)
    sections = parse_sections(html)

    if not sections:
        return {
            "listing_date": listing_date,
            "page_type": page_type,
            "field": field,
            "sections": [],
            "diagnostics": {"missing_title_ids": [], "missing_abstract_ids": []},
        }

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

    all_papers = [paper for section in result_sections for paper in section["papers"]]
    diagnostics = {
        "missing_title_ids": [p["id"] for p in all_papers if not p.get("title")],
        "missing_abstract_ids": [p["id"] for p in all_papers if not p.get("abstract")],
    }

    return {
        "listing_date": listing_date,
        "page_type": page_type,
        "field": field,
        "sections": result_sections,
        "diagnostics": diagnostics,
    }


def log_parse_diagnostics(data: dict, logger: logging.Logger) -> None:
    """Log warnings for partially parsed papers."""
    diagnostics = data.get("diagnostics", {})
    missing_title_ids = diagnostics.get("missing_title_ids") or []
    missing_abstract_ids = diagnostics.get("missing_abstract_ids") or []

    if missing_title_ids:
        logger.warning(
            f"[Phase1] {len(missing_title_ids)} papers missing title: {missing_title_ids[:5]}"
        )
    if missing_abstract_ids:
        logger.warning(
            f"[Phase1] {len(missing_abstract_ids)} papers missing abstract: {missing_abstract_ids[:5]}"
        )


# ---------------------------------------------------------------------------
# Phase 2: LLM Summarization
# ---------------------------------------------------------------------------

def build_prompt(batch_papers: list[dict], current_tags: list[str]) -> list[dict]:
    """Build chat messages for LLM API call."""
    tags_str = ", ".join(current_tags) if current_tags else "(empty)"
    papers_text = []
    total_papers = len(batch_papers)
    for idx, p in enumerate(batch_papers, start=1):
        entry = (
            f"[Paper {idx}/{total_papers}]\n"
            f"ID: {p['id']}\nTitle: {p.get('title', 'N/A')}\nAbstract: {p.get('abstract', 'N/A')}"
        )
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
{PAPER_SEPARATOR.join(papers_text)}

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
            with ExitStack() as stack:
                stream_file = None
                if debug_dir is not None:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    stream_path = debug_dir / f"{label}_stream_attempt{attempt + 1}.txt"
                    stream_file = stack.enter_context(open(stream_path, "w", encoding="utf-8"))
                with urllib.request.urlopen(req, timeout=300) as resp:
                    content_parts = []
                    usage = {}
                    saw_done = False
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
                            saw_done = True
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
                    if not saw_done:
                        logger.warning(
                            f"[Phase2] {log_label} stream ended without [DONE] marker "
                            "(possible network interruption)"
                        )
                        raise ConnectionError("SSE stream incomplete: no [DONE] marker received")

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
) -> tuple[dict[str, dict], MatchDiagnostics]:
    """Match LLM results to batch papers and return structured diagnostics.

    Strategy:
    1. Try exact ID match first
    2. Try normalized ID match (strip version suffix like v1, v2)
    3. Report missing, duplicate, and quality failures separately
    """
    label = batch_label or f"batch_{batch_num:03d}"
    log_label = batch_label or f"Batch {batch_num:03d}"
    attempt_suffix = f"_attempt{attempt}" if attempt > 0 else ""
    results: dict[str, dict] = {}
    batch_ids = [p["id"] for p in batch]
    used_llm_indices: set[int] = set()

    # Build lookup by exact and normalized ID. Duplicate IDs are excluded so
    # they are never silently resolved by "last write wins" dictionary behavior.
    llm_id_counts: dict[str, int] = {}
    for p in llm_papers:
        pid = p.get("id", "")
        llm_id_counts[pid] = llm_id_counts.get(pid, 0) + 1
    duplicate_set = {pid for pid, count in llm_id_counts.items() if count >= 2}
    duplicate_norm_set = {re.sub(r"v\d+$", "", pid) for pid in duplicate_set}

    llm_by_id: dict[str, int] = {}
    llm_by_norm: dict[str, int] = {}
    for i, p in enumerate(llm_papers):
        pid = p.get("id", "")
        norm = re.sub(r"v\d+$", "", pid)
        if pid in duplicate_set or norm in duplicate_norm_set:
            continue
        llm_by_id[pid] = i
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

    # Pass 3: duplicate IDs corresponding to batch papers
    duplicate_ids = []
    for batch_id in batch_ids:
        if batch_id in results:
            continue
        norm = re.sub(r"v\d+$", "", batch_id)
        if batch_id in duplicate_set or norm in duplicate_norm_set:
            duplicate_ids.append(batch_id)

    # Pass 4: quality check - remove papers with missing tags or short summary
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

    missing_ids = [
        bid for bid in batch_ids
        if bid not in results
        and bid not in duplicate_ids
        and bid not in {qf["id"] for qf in quality_failures}
    ]
    diagnostics = MatchDiagnostics(
        missing_ids=missing_ids,
        duplicate_ids=duplicate_ids,
        quality_failed=quality_failures,
    )

    if missing_ids:
        logger.error(
            f"[Phase2] {log_label}: {len(missing_ids)} papers ID not found in LLM response: "
            f"{missing_ids}"
        )
    if duplicate_ids:
        logger.error(
            f"[Phase2] {log_label}: {len(duplicate_ids)} papers ID duplicated in LLM response: "
            f"{duplicate_ids}"
        )
    if quality_failures:
        logger.error(
            f"[Phase2] {log_label}: {len(quality_failures)} papers quality check failed: "
            + ", ".join(f"{qf['id']}({', '.join(qf['issues'])})" for qf in quality_failures)
        )
        if debug_dir is not None and raw_result is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / f"{label}_quality_error{attempt_suffix}.json"
            debug_data = {
                "batch_input_ids": batch_ids,
                "quality_failures": quality_failures,
                "llm_response": raw_result,
                "diagnostics": {
                    "missing_ids": diagnostics.missing_ids,
                    "duplicate_ids": diagnostics.duplicate_ids,
                    "quality_failed": diagnostics.quality_failed,
                },
            }
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            logger.error(f"[Phase2] {log_label} quality debug saved to {debug_path}")

    # Pass 5: write ID mismatch debug info
    unmatched_batch = missing_ids + duplicate_ids
    if unmatched_batch:
        unmatched_llm_ids = [
            llm_papers[i].get("id", "?") for i in range(len(llm_papers)) if i not in used_llm_indices
        ]
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
                "diagnostics": {
                    "missing_ids": diagnostics.missing_ids,
                    "duplicate_ids": diagnostics.duplicate_ids,
                    "quality_failed": diagnostics.quality_failed,
                },
            }
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            logger.error(f"[Phase2] {log_label} debug info saved to {debug_path}")

    return results, diagnostics


def compute_metadata_hash(data: dict) -> str:
    """Compute SHA-256 of prompt-relevant paper metadata for cache validity."""
    hash_input = []
    for section in data["sections"]:
        for paper in section["papers"]:
            hash_input.append({
                "id": paper["id"],
                "title": paper.get("title", ""),
                "abstract": paper.get("abstract", ""),
                "comments": paper.get("comments", ""),
            })
    hash_input.sort(key=lambda x: x["id"])
    return hashlib.sha256(json.dumps(hash_input, sort_keys=True).encode()).hexdigest()


def _unresolved_papers(
    batch: list[dict],
    matched: dict[str, dict],
    failed_ids: list[str] | None = None,
) -> list[dict]:
    """Return batch papers that still lack an accepted LLM result."""
    if failed_ids is not None:
        failed_id_set = set(failed_ids)
        return [paper for paper in batch if paper["id"] in failed_id_set]
    return [paper for paper in batch if paper["id"] not in matched]


def _add_summary_failure_placeholders(
    results_by_id: dict[str, dict],
    papers: list[dict],
    logger: logging.Logger,
    log_label: str,
) -> None:
    """Keep failed papers visible in markdown after retry paths are exhausted."""
    if not papers:
        return
    failed_ids = [paper["id"] for paper in papers]
    logger.error(f"[Phase2] {log_label}: using summary failure placeholders for {failed_ids}")
    for paper in papers:
        results_by_id.setdefault(
            paper["id"],
            {"summary": SUMMARY_FAILURE_PLACEHOLDER, "tags": [SUMMARY_FAILURE_TAG], "venue": None},
        )


def _format_usage(usage: dict) -> str:
    if not usage:
        return ""
    pt = usage.get("prompt_tokens", 0)
    ct = usage.get("completion_tokens", 0)
    tt = usage.get("total_tokens", 0)
    return f", tokens: {pt}+{ct}={tt}"


def _format_match_failures(diagnostics: MatchDiagnostics) -> str:
    parts = []
    if diagnostics.missing_ids:
        parts.append(f"{len(diagnostics.missing_ids)} missing")
    if diagnostics.duplicate_ids:
        parts.append(f"{len(diagnostics.duplicate_ids)} duplicate")
    if diagnostics.quality_failed:
        parts.append(f"{len(diagnostics.quality_failed)} quality")
    return ", ".join(parts)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via temp file and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text via temp file and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _write_batch_cache(field_cache: Path, batch_num: int, result: dict, new_tags: list, current_tags: list) -> None:
    cache_entry = {
        "papers": result.get("papers", []),
        "new_tags": new_tags,
        "current_tags": list(current_tags),
    }
    cache_path = field_cache / f"batch_{batch_num:03d}.json"
    _atomic_write_json(cache_path, cache_entry)


def _process_batch(
    batch: list[dict],
    idx: int,
    total: int,
    current_tags: list,
    api_endpoint: str,
    api_key: str,
    model: str,
    logger: logging.Logger,
    debug_dir: Path,
    field_cache: Path,
    label_prefix: str,
    save_cache: bool,
    cache_allowed: bool,
    exception_status: str,
) -> BatchProcessResult:
    batch_num = idx + 1
    blabel = f"{label_prefix}_{batch_num:03d}"
    t0 = time.monotonic()
    try:
        messages = build_prompt(batch, current_tags)
        result, usage = call_llm(
            messages, api_endpoint, api_key, model, logger, batch_num, debug_dir, batch_label=blabel
        )

        matched, diagnostics = match_llm_results(
            batch, result.get("papers", []), logger, batch_num, result, debug_dir, batch_label=blabel
        )

        new_tags = result.get("new_tags", [])
        for tag in new_tags:
            if tag not in current_tags:
                current_tags.append(tag)

        elapsed = time.monotonic() - t0
        usage_str = _format_usage(usage)
        if diagnostics.total_failed > 0:
            failure_summary = _format_match_failures(diagnostics)
            print(
                f"[{batch_num}/{total}] {blabel} FAILED ({failure_summary}) ({elapsed:.1f}s{usage_str}), see log",
                file=sys.stderr,
            )
            return BatchProcessResult(
                matched,
                _unresolved_papers(batch, matched, diagnostics.all_failed_ids),
                True,
            )

        if save_cache and cache_allowed:
            _write_batch_cache(field_cache, batch_num, result, new_tags, current_tags)
        print(f"[{batch_num}/{total}] {blabel} done ({elapsed:.1f}s{usage_str})", file=sys.stderr)
        return BatchProcessResult(matched, [], False)

    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error(f"[Phase2] {blabel} {exception_status}: {e}")
        print(
            f"[{batch_num}/{total}] {blabel} FAILED ({elapsed:.1f}s), see log",
            file=sys.stderr,
        )
        return BatchProcessResult({}, batch, True)


def load_tags(tags_path: Path) -> list[str]:
    """Load existing tag names, normalizing legacy object-shaped entries."""
    if not tags_path.exists():
        tags_path.parent.mkdir(parents=True, exist_ok=True)
        return []

    with open(tags_path, "r", encoding="utf-8") as f:
        tags_data = json.load(f)
    current_tags = tags_data.get("tags", [])
    if current_tags and isinstance(current_tags[0], dict):
        return [tag.get("name", str(tag)) for tag in current_tags]
    return current_tags


def collect_batches(data: dict, batch_size: int) -> list[list[dict]]:
    """Collect papers with abstracts into fixed-size LLM batches."""
    all_papers = []
    for section in data["sections"]:
        for paper in section["papers"]:
            if paper.get("abstract"):
                all_papers.append(paper)
    return [all_papers[i : i + batch_size] for i in range(0, len(all_papers), batch_size)]


def load_cached_batches(field_cache: Path, hash_file: Path, current_hash: str, no_cache: bool) -> dict[int, dict]:
    """Load valid per-batch cache entries and refresh the metadata hash file."""
    cached_batches: dict[int, dict] = {}
    if not no_cache and hash_file.exists():
        old_hash = hash_file.read_text().strip()
        if old_hash == current_hash:
            for bf in sorted(field_cache.glob("batch_*.json")):
                try:
                    with open(bf, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                    batch_idx = int(bf.stem.split("_")[1])
                    cached_batches[batch_idx] = cached
                except Exception:
                    pass
        else:
            for bf in field_cache.glob("batch_*.json"):
                bf.unlink()
    hash_file.write_text(current_hash)
    return cached_batches


def save_tags(tags_path: Path, current_tags: list) -> None:
    """Persist the current tag list."""
    tags_out = {
        "updated_at": datetime.now(OUTPUT_TZ).isoformat(),
        "tags": current_tags,
    }
    _atomic_write_json(tags_path, tags_out)


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
    current_tags = load_tags(tags_path)
    batches = collect_batches(data, BATCH_SIZE)

    if not batches:
        return {}, 0

    # Cache setup
    field_cache = cache_dir / data["field"]
    field_cache.mkdir(parents=True, exist_ok=True)
    debug_dir = cache_dir / "debug" / data["field"]

    cached_batches: dict[int, dict] = {}
    if save_cache:
        current_hash = compute_metadata_hash(data)
        hash_file = field_cache / "metadata_hash"
        cached_batches = load_cached_batches(field_cache, hash_file, current_hash, no_cache)

    results_by_id: dict[str, dict] = {}
    failed_batches: list[tuple[int, list[dict]]] = []

    for idx, batch in enumerate(batches):
        batch_num = idx + 1
        total = len(batches)
        blabel = f"{batch_prefix}_{batch_num:03d}"

        if batch_num in cached_batches:
            cached = cached_batches[batch_num]
            matched, diagnostics = match_llm_results(
                batch, cached.get("papers", []), logger, batch_num, cached, debug_dir, batch_label=blabel
            )
            results_by_id.update(matched)
            if cached.get("current_tags"):
                current_tags = cached["current_tags"]
            status = (
                "cached"
                if diagnostics.total_failed == 0
                else f"cached failed ({_format_match_failures(diagnostics)})"
            )
            print(f"[{batch_num}/{total}] {blabel} {status}", file=sys.stderr)
            if diagnostics.total_failed > 0:
                failed_batches.append(
                    (idx, _unresolved_papers(batch, matched, diagnostics.all_failed_ids))
                )
            continue

        processed = _process_batch(
            batch, idx, total, current_tags,
            api_endpoint, api_key, model, logger, debug_dir, field_cache,
            batch_prefix, save_cache, cache_allowed=True,
            exception_status="failed after all retries",
        )
        results_by_id.update(processed.matched)
        if processed.failed:
            failed_batches.append((idx, processed.unresolved))

    # Retry failed batches once
    if failed_batches:
        print(f"Retrying {len(failed_batches)} failed batch(es)...", file=sys.stderr)
        still_failed = 0
        for idx, batch in failed_batches:
            original_batch = batches[idx]
            batch_num = idx + 1
            total = len(batches)
            blabel = f"retry_{batch_prefix}_{batch_num:03d}"

            processed = _process_batch(
                batch, idx, total, current_tags,
                api_endpoint, api_key, model, logger, debug_dir, field_cache,
                f"retry_{batch_prefix}", save_cache,
                cache_allowed=len(batch) == len(original_batch),
                exception_status="failed",
            )
            results_by_id.update(processed.matched)
            if processed.failed:
                still_failed += 1
                _add_summary_failure_placeholders(results_by_id, processed.unresolved, logger, blabel)

        failed_batches = still_failed
    else:
        failed_batches = 0

    save_tags(tags_path, current_tags)

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
    now = datetime.now(OUTPUT_TZ)
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

def run_patch_mode(args, logger: logging.Logger, cache_base: Path, llm_config: LlmConfig) -> RunResult:
    if not args.date:
        raise ConfigError("--patch requires --date")

    logger.info(f"[Patch] Starting patch for field={args.field} date={args.date} ids={args.patch}")

    # Load saved HTML
    pages_dir = cache_base / "pages" / args.field
    saved_path = pages_dir / f"{args.date}.html"
    if not saved_path.exists():
        raise ParseError(f"No saved HTML for date {args.date} at {saved_path}")
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
        raise ParseError("No matching papers found in saved HTML")

    papers_without_abstract = [p for p in patch_papers if not p.get("abstract")]
    if papers_without_abstract:
        missing_abstract_ids = [p["id"] for p in papers_without_abstract]
        logger.warning(
            f"[Patch] {len(papers_without_abstract)} papers missing abstract: {missing_abstract_ids}"
        )
        print(
            f"Warning: {len(papers_without_abstract)} patch paper(s) missing abstract: "
            f"{', '.join(missing_abstract_ids)}",
            file=sys.stderr,
        )

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
        api_endpoint=llm_config.api_endpoint,
        api_key=llm_config.api_key,
        model=llm_config.model,
        logger=logger,
        batch_prefix="patch_batch", save_cache=False,
    )

    if not results_by_id:
        raise PipelineError("No papers successfully matched from LLM response")

    # Patch markdown
    output_dir = Path(args.output)
    md_path = output_dir / f"{args.date}-{args.field}.md"
    if not md_path.exists():
        raise ConfigError(f"Markdown file not found: {md_path}")

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
    return RunResult(summary, exit_code=0 if total_failed == 0 else 1)


def run_normal_mode(args, logger: logging.Logger, cache_base: Path, llm_config: LlmConfig) -> RunResult:
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
            raise ParseError(f"No saved HTML for date {args.date} at {saved_path}")
        html = saved_path.read_text(encoding="utf-8")
        logger.info(f"[Phase1] Using saved HTML for {args.date} from {saved_path}")
        print(f"Using saved HTML for {args.date} from {saved_path}", file=sys.stderr)
    else:
        print(f"Fetching arxiv.org/list/{args.field}/new ...", file=sys.stderr)
        try:
            html = fetch_page(args.field)
        except Exception as e:
            logger.error(f"[Phase1] Failed to fetch page: {e}")
            raise FetchError(f"Failed to fetch arxiv page: {e}") from e

    data = parse_html(html, args.field)
    if not data["sections"]:
        logger.error("[Phase1] No sections found in page")
        raise ParseError("No papers found. The field may not exist or no updates today.")

    page_date = data["listing_date"]  # YYYY-MM-DD or None
    page_type = data["page_type"]     # "new", "catchup", or "unknown"

    # Validate actual parsed content
    parsed_papers = sum(len(s["papers"]) for s in data["sections"])
    papers_with_abstract = sum(
        1 for s in data["sections"] for p in s["papers"] if p.get("abstract")
    )
    if parsed_papers == 0:
        logger.error("[Phase1] Sections found but no papers could be parsed from page")
        raise ParseError("No papers could be parsed. Page format may be unsupported.")
    log_parse_diagnostics(data, logger)
    if papers_with_abstract == 0:
        logger.error(f"[Phase1] {parsed_papers} papers parsed but none have abstracts")
        raise ParseError(
            f"{parsed_papers} papers parsed but no abstracts extracted. Page format may be unsupported."
        )

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
            raise ConfigError(f"Invalid --date format: {args.date}, expected YYYY-MM-DD")
        # Validate page date matches --date
        if page_date and page_date != args.date:
            logger.error(
                f"[Phase1] Date mismatch: --date={args.date} but page contains date {page_date} ({page_type})"
            )
            raise ParseError(
                f"Date mismatch: --date={args.date} but page contains date {page_date} ({page_type})"
            )
        date_str = args.date
    elif page_date:
        date_str = page_date
    else:
        logger.error("[Phase1] Unrecognized page format: cannot extract listing date")
        raise ParseError("Unrecognized page format, expected arxiv /list/new or /catchup page.")

    # Save HTML to pages archive (keyed by listing date)
    page_archive_path = pages_dir / f"{date_str}.html"
    if not page_archive_path.exists():
        _atomic_write_text(page_archive_path, html)
        logger.info(f"[Phase1] Saved HTML to {page_archive_path}")
    else:
        logger.info(f"[Phase1] HTML archive already exists: {page_archive_path}")

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
        return RunResult(summary)
    if expected_output.exists() and args.date and not args.no_cache:
        logger.info(f"[Phase1] --date mode overwriting existing output: {expected_output}")
        print(f"Warning: --date mode overwriting existing output: {expected_output}", file=sys.stderr)

    # Phase 2: LLM summarization
    tags_path = Path(args.tags)
    results_by_id, failed_batches = run_phase2(
        data,
        tags_path,
        cache_base,
        args.no_cache,
        llm_config.api_endpoint,
        llm_config.api_key,
        llm_config.model,
        logger,
    )

    # Phase 3: Generate markdown (only if all batches succeeded)
    output_dir = Path(args.output)

    if failed_batches > 0:
        logger.error(f"[Phase2] {failed_batches} batch(es) still failed after retry, skipping markdown generation")
        raise PipelineError(
            f"{failed_batches} batch(es) failed. Markdown not generated. "
            f"Re-run with --date {date_str} to retry (cached batches will be reused)."
        )

    output_file = generate_markdown(data, results_by_id, output_dir, date_str)

    # Clean cache on full success
    if field_cache.exists():
        logger.info(f"[Cache] Removing completed cache directory: {field_cache}")
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
    logger.info(f"[Phase3] Run completed successfully. Output: {output_file}")
    return RunResult(summary)


def validate_env() -> LlmConfig:
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
        raise ConfigError(f"Missing environment variables: {', '.join(missing)}")

    return LlmConfig(api_key=api_key, api_endpoint=api_endpoint, model=model)


def main() -> int:
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

    try:
        llm_config = validate_env()
        logger = setup_logging(cache_base)
        if args.patch:
            result = run_patch_mode(args, logger, cache_base, llm_config)
        else:
            result = run_normal_mode(args, logger, cache_base, llm_config)
        print(json.dumps(result.summary, ensure_ascii=False))
        return result.exit_code
    except ArxivDailyError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(f"Log: {log_path}", file=sys.stderr)
        return e.exit_code

if __name__ == "__main__":
    sys.exit(main())
