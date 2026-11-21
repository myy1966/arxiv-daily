#!/usr/bin/env python3
"""
arxiv_daily.py - Download arxiv daily listings, generate LLM summaries, and produce markdown.

Requires: Python >= 3.10

Usage:
    python3 pipeline/arxiv_daily.py cs.AI

    --output and --tags default to this site repo's _posts/ and
    data/tags.json; pass them explicitly to write elsewhere.

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
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

try:
    from json_repair import loads as json_repair_loads
except ImportError:
    json_repair_loads = None

from consolidate_tags import (
    DEFAULT_POSTS_DIR,
    DEFAULT_TAGS_PATH,
    canonicalize_tag_list,
    load_tag_mapping,
)


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


BATCH_SIZE = 8
RETRY_BATCH_SIZE = BATCH_SIZE // 2
MAX_RETRIES = 3
RETRY_DELAYS = [2, 4, 8]
MIN_SUMMARY_LEN = 30
# Content check: the LLM sometimes writes a batch neighbour's summary under a
# paper's id. A summary fails when none of its rare English terms (appearing in
# at most CONTENT_RARE_MAX_DF abstracts of the listing) are in its own abstract,
# some are in another batch paper's abstract, and the idf-weighted share of its
# terms found in the own vs. that abstract is <= CONTENT_OWN_MAX vs.
# >= CONTENT_OTHER_MIN, at least CONTENT_MIN_GAP apart. Backtested on the
# 2026-04..11 posts (69.7k summaries): 57/62 labelled swaps caught, 0/26
# labelled-correct summaries flagged, about 0.3 false hits per 10k papers.
CONTENT_RARE_MAX_DF = 2
CONTENT_OWN_MAX = 0.35
CONTENT_OTHER_MIN = 0.6
CONTENT_MIN_GAP = 0.35
SUMMARY_FAILURE_PLACEHOLDER = "[摘要生成失败]"
SUMMARY_FAILURE_TAG = "未分类"
PAPER_SEPARATOR = "\n=====PAPER_SEPARATOR=====\n"
OUTPUT_TZ = timezone(timedelta(hours=8))
# Front-matter hour for catch-up runs (run on a later day than the listing date).
CATCHUP_POST_HOUR = 16
ARXIV_FETCH_INTERVAL_SECONDS = 3.0
ARXIV_CONTACT = os.environ.get("ARXIV_UPDATE_CONTACT", "set-ARXIV_UPDATE_CONTACT")
ARXIV_USER_AGENT = os.environ.get(
    "ARXIV_UPDATE_USER_AGENT",
    f"arxiv-daily-bot/1.0 (contact: {ARXIV_CONTACT})",
)
STREAM_LOOP_WINDOW = 30
STREAM_LOOP_MAX_UNIQUE = 12
STREAM_LOOP_MAX_BLANK_CHARS = 200
_last_fetch_time: float | None = None
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def debug_output_dir(cache_dir: Path, field: str, listing_date: str | None) -> Path:
    """Return debug/<field>/<YYYY-MM-DD>/ so daily runs do not overwrite each other."""
    if listing_date and _DATE_DIR_RE.fullmatch(listing_date):
        date_part = listing_date
    else:
        date_part = datetime.now(OUTPUT_TZ).strftime("%Y-%m-%d")
    field_part = field if field and "/" not in field and "\\" not in field and ".." not in field else "unknown"
    return cache_dir / "debug" / field_part / date_part


class _StreamLoopDetector:
    """Sliding-window unique-line detector with blank-flood quota for LLM SSE streams.

    Detects infinite loops by measuring information entropy in a fixed-size
    line window. When the window contains too few unique lines, the model is
    judged to be looping. Robust against template repetition where the same
    boilerplate line recurs but surrounding context is varied. Also detects
    runs of pure whitespace chunks that never enter the line window.

    Not thread-safe; create one instance per LLM request attempt.
    """

    def __init__(
        self,
        window_size: int = STREAM_LOOP_WINDOW,
        max_unique: int = STREAM_LOOP_MAX_UNIQUE,
        max_blank_chars: int = STREAM_LOOP_MAX_BLANK_CHARS,
    ) -> None:
        from collections import deque

        self._window: deque[str] = deque(maxlen=window_size)
        self._window_size = window_size
        self._max_unique = max_unique
        self._max_blank_chars = max_blank_chars
        self._buf = ""
        self._total_chars = 0
        self._blank_chars = 0

    def feed(self, chunk: str) -> bool:
        """Ingest a text chunk and return True if a loop is detected."""
        self._total_chars += len(chunk)
        if chunk.strip() == "":
            self._blank_chars += len(chunk)
            if self._blank_chars >= self._max_blank_chars:
                return True
        else:
            self._blank_chars = 0

        self._buf += chunk
        lines = self._buf.split("\n")
        self._buf = lines[-1]  # keep incomplete tail for next chunk
        for line in lines[:-1]:
            stripped = line.strip()
            if not stripped:
                continue  # skip blank lines so they don't fill the window
            self._window.append(stripped)
            if (
                len(self._window) == self._window_size
                and len(set(self._window)) <= self._max_unique
            ):
                return True
        return False

    @property
    def total_chars(self) -> int:
        """Total characters processed so far."""
        return self._total_chars


@dataclass
class BatchProcessResult:
    matched: dict[str, dict]
    unresolved: list[dict]
    failed: bool
    new_tags: list[str] = field(default_factory=list)
    current_tags_snapshot: list[str] = field(default_factory=list)


@dataclass
class UnresolvedPaper:
    paper: dict
    original_batch_num: int


@dataclass
class MatchDiagnostics:
    missing_ids: list[str]
    duplicate_ids: list[str]
    quality_failed: list[dict]
    content_mismatch: list[dict] = field(default_factory=list)

    @property
    def total_failed(self) -> int:
        return (
            len(self.missing_ids) + len(self.duplicate_ids)
            + len(self.quality_failed) + len(self.content_mismatch)
        )

    @property
    def all_failed_ids(self) -> list[str]:
        return self.missing_ids + self.duplicate_ids + [
            failure["id"] for failure in self.quality_failed
        ] + [mismatch["id"] for mismatch in self.content_mismatch]


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

    paper_ids = [str(p["id"]) for p in batch_papers]
    ids_csv = ", ".join(paper_ids)
    example_objs = []
    for p in batch_papers[:2]:
        example_tag = "Neuromorphic Photonics" if len(example_objs) == 1 else "LLM Agent"
        example_objs.append(
            f'    {{"id": "{p["id"]}", "summary": "Chinese summary", "tags": ["{example_tag}"]}}'
        )
    example_new_tags = '["Neuromorphic Photonics"]' if len(batch_papers) >= 2 else "[]"
    example_json = (
        "{\n"
        '  "papers": [\n'
        + ",\n".join(example_objs)
        + "\n  ],\n"
        + f'  "new_tags": {example_new_tags}\n'
        + "}"
    )
    user_content = f"""Below are {total_papers} papers.
Copy these IDs verbatim as JSON strings (not numbers): {ids_csv}

Return exactly {total_papers} paper objects. The set of "id" values must be exactly these {total_papers} IDs (order may differ).
Do not drop, invent, reuse, or suffix IDs (_dup, placeholder). Each object must include id, a 1-3 sentence summary, and 1-2 tags.

For each paper:
1. Write a 1-3 sentence Chinese summary following this structure:
   - What problem or gap does the paper address?
   - What is the proposed method, framework, or main contribution?
   - What are the key results or findings? Include quantitative metrics when available.
2. Assign 1-2 tags.
   - Tags are for daily-digest readers to skip or keep a paper, not paper keywords or method names.
   - Prefer an existing tag from the list when it is a genuine fit.
   - Do not stretch a weakly related existing tag onto the paper. If none of the listed tags adequately capture the topic — including a genuinely new research area — create a new tag.
   - Put every newly created tag both in that paper's "tags" and in top-level "new_tags". If nothing was created, return "new_tags": [].
   - New tags must be in English, concise (1-3 words), at the same granularity as existing tags: a recurring research topic, not a paper, method, dataset, or model name.
   - BAD: "LLM", "NLP", "Computer Vision" (too broad); "JEPA", "RAPID", "OpenDiscoveryTrace" (too specific)
   - GOOD existing: "LLM Agent", "LLM Evaluation", "Object Detection", "Code Generation"
   - GOOD new: a missing topic at that same grain, e.g. a distinct subfield that is not in the list

Current tag list: {tags_str}

Papers:
{PAPER_SEPARATOR.join(papers_text)}

Respond in strict JSON format:
{example_json}"""

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


def _normalized_id_set(ids: list) -> set[str]:
    return {
        _normalize_paper_id(coerce_paper_id(pid))
        for pid in ids
        if coerce_paper_id(pid)
    }


def _validate_llm_schema(result: object, expected_ids: list[str] | None = None) -> dict:
    """Validate the top-level LLM response schema before downstream matching."""
    if not isinstance(result, dict):
        raise ValueError(f"LLM response must be a JSON object, got {type(result).__name__}")
    if not isinstance(result.get("papers"), list):
        raise ValueError(f"LLM response missing 'papers' list, got keys: {list(result.keys())}")
    if "new_tags" in result and not isinstance(result["new_tags"], list):
        raise ValueError(f"'new_tags' must be list, got {type(result['new_tags']).__name__}")
    if expected_ids is None:
        return result

    papers = result["papers"]
    expected_n = len(expected_ids)
    if len(papers) != expected_n:
        raise ValueError(f"expected {expected_n} papers, got {len(papers)}")

    got_ids: list[str] = []
    for i, paper in enumerate(papers):
        if not isinstance(paper, dict):
            raise ValueError(f"papers[{i}] must be an object")
        pid = coerce_paper_id(paper.get("id"))
        if _is_junk_llm_id(pid):
            raise ValueError(f"papers[{i}] has invalid id {pid!r}")
        summary = paper.get("summary") if isinstance(paper.get("summary"), str) else ""
        if len(summary) < MIN_SUMMARY_LEN:
            raise ValueError(f"papers[{i}] ({pid}) missing summary")
        tags = paper.get("tags")
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
            raise ValueError(f"papers[{i}] ({pid}) missing tags")
        got_ids.append(pid)

    if len(_normalized_id_set(got_ids)) != expected_n:
        raise ValueError(f"duplicate paper ids in LLM response: {got_ids}")
    expected_set = _normalized_id_set(expected_ids)
    got_set = _normalized_id_set(got_ids)
    if got_set != expected_set:
        raise ValueError(f"paper ids {sorted(got_set)} != expected {sorted(expected_set)}")
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
    expected_ids: list[str] | None = None,
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
            "thinking": {"type": "disabled"},
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
                    detector = _StreamLoopDetector()
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
                            stream_chunk = delta.get("reasoning_content") or delta.get("content") or ""
                            if stream_chunk and detector.feed(stream_chunk):
                                logger.error(
                                    f"[Phase2] {log_label} attempt {attempt + 1}: "
                                    f"stream loop detected after {detector.total_chars} chars, aborting"
                                )
                                resp.close()
                                raise ValueError(f"stream loop after {detector.total_chars} chars")
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
                        return _validate_llm_schema(result, expected_ids), usage
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
            return _validate_llm_schema(result, expected_ids), usage
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


_ID_VERSION_RE = re.compile(r"v\d+$")
_JUNK_ID_RE = re.compile(r"(?:_dup\d*|_placeholder|\bplaceholder\b)", re.I)
_ARXIV_ID_RE = re.compile(r"^(\d{4})\.(\d{4,5})$")


def coerce_paper_id(value: object) -> str:
    """Normalize LLM/batch paper ids to strings (JSON may emit numbers)."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".8f").rstrip("0").rstrip(".")
    return str(value).strip()


def _normalize_paper_id(pid: str) -> str:
    return _ID_VERSION_RE.sub("", pid)


def _is_junk_llm_id(pid: str) -> bool:
    return not pid or bool(_JUNK_ID_RE.search(pid))


def _yymm_one_digit_typo(left: str, right: str) -> bool:
    """True when sequence matches and YYMM differs by exactly one digit."""
    match_left = _ARXIV_ID_RE.fullmatch(_normalize_paper_id(left))
    match_right = _ARXIV_ID_RE.fullmatch(_normalize_paper_id(right))
    if not match_left or not match_right:
        return False
    if match_left.group(2) != match_right.group(2):
        return False
    prefix_left, prefix_right = match_left.group(1), match_right.group(1)
    diffs = 0
    for a, b in zip(prefix_left, prefix_right):
        if a == b:
            continue
        if not (a.isdigit() and b.isdigit()):
            return False
        diffs += 1
        if diffs > 1:
            return False
    return diffs == 1


_CONTENT_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-+][A-Za-z0-9]+)*")
_CONTENT_STOPWORDS = frozenset(
    "the and for with from that this are was were our its into than via can not but has have "
    "using based new also all any sota".split()
)


def content_terms(text: str) -> set[str]:
    """English terms of a text: lowercased, plural -s dropped, hyphenated words also split.

    Chinese summaries keep method, dataset and model names in English, so these
    terms are what ties a summary to the abstract it was written from.
    """
    terms: set[str] = set()
    for token in _CONTENT_TERM_RE.findall(text or ""):
        parts = [token] + (re.split(r"[-+]", token) if ("-" in token or "+" in token) else [])
        for part in parts:
            term = part.lower()
            if len(term) < 3 or term in _CONTENT_STOPWORDS:
                continue
            if len(term) >= 4 and term.endswith("s") and not term.endswith("ss"):
                term = term[:-1]
            terms.add(term)
    return terms


def build_content_index(data: dict) -> dict:
    """Index every listing paper's abstract+title terms and their document frequency."""
    terms_by_id: dict[str, set[str]] = {}
    for section in data.get("sections", []):
        for paper in section.get("papers", []):
            pid = coerce_paper_id(paper.get("id"))
            if pid and pid not in terms_by_id:
                terms_by_id[pid] = content_terms(f"{paper.get('abstract') or ''} {paper.get('title') or ''}")
    df = Counter(term for terms in terms_by_id.values() for term in terms)
    return {"terms": terms_by_id, "df": df, "n": len(terms_by_id)}


def find_content_mismatches(batch: list[dict], results: dict[str, dict], index: dict) -> list[dict]:
    """Return summaries that read like another paper of the same batch (see CONTENT_* constants)."""
    terms_by_id, df, n = index["terms"], index["df"], index["n"]

    def idf(term: str) -> float:
        return math.log((n + 1) / (1 + df.get(term, 0)))

    batch_ids = [coerce_paper_id(p.get("id")) for p in batch]
    mismatches = []
    for pid in batch_ids:
        result = results.get(pid)
        own_terms = terms_by_id.get(pid)
        if not result or own_terms is None:
            continue
        summary_terms = content_terms(result.get("summary", ""))
        rare = {t for t in summary_terms if df.get(t, 0) <= CONTENT_RARE_MAX_DF}
        if not rare or rare & own_terms:
            continue
        weight = sum(idf(t) for t in summary_terms)
        if weight <= 0:
            continue
        own_score = sum(idf(t) for t in summary_terms if t in own_terms) / weight
        for other in batch_ids:
            other_terms = terms_by_id.get(other)
            if other == pid or not other_terms or not (rare & other_terms):
                continue
            other_score = sum(idf(t) for t in summary_terms if t in other_terms) / weight
            if (own_score <= CONTENT_OWN_MAX and other_score >= CONTENT_OTHER_MIN
                    and other_score - own_score >= CONTENT_MIN_GAP):
                mismatches.append({
                    "id": pid,
                    "looks_like": other,
                    "terms": sorted(rare & other_terms),
                    "own_score": round(own_score, 3),
                    "other_score": round(other_score, 3),
                })
                break
    return mismatches


def match_llm_results(
    batch: list[dict], llm_papers: list[dict], logger: logging.Logger, batch_num: int,
    raw_result: dict | None = None, debug_dir: Path | None = None,
    batch_label: str | None = None, attempt: int = 0,
    content_index: dict | None = None, content_strict: bool = True,
) -> tuple[dict[str, dict], MatchDiagnostics]:
    """Match LLM results to batch papers and return structured diagnostics.

    Strategy:
    1. Coerce ids to strings; drop junk ids (_dup, placeholder)
    2. Exact ID match, keeping the first copy when the model repeats an id
    3. Normalized ID match (strip version suffix like v1, v2)
    4. Unique YYMM one-digit typo match for leftovers
    5. Report missing and quality failures separately
    6. With content_index, flag summaries that read like another batch paper:
       strict mode drops them as failures, otherwise they are kept with a warning
    """
    label = batch_label or f"batch_{batch_num:03d}"
    log_label = batch_label or f"Batch {batch_num:03d}"
    attempt_suffix = f"_attempt{attempt}" if attempt > 0 else ""
    results: dict[str, dict] = {}
    batch_ids = [coerce_paper_id(p.get("id")) for p in batch]
    used_llm_indices: set[int] = set()

    normalized_llm: list[dict] = []
    for paper in llm_papers:
        if not isinstance(paper, dict):
            continue
        item = dict(paper)
        item["id"] = coerce_paper_id(item.get("id"))
        normalized_llm.append(item)
    llm_papers = normalized_llm

    llm_by_id: dict[str, int] = {}
    llm_by_norm: dict[str, int] = {}
    extra_copies: dict[str, int] = {}
    for i, paper in enumerate(llm_papers):
        pid = paper.get("id", "")
        if _is_junk_llm_id(pid):
            continue
        if pid in llm_by_id:
            extra_copies[pid] = extra_copies.get(pid, 0) + 1
            continue
        llm_by_id[pid] = i
        llm_by_norm[_normalize_paper_id(pid)] = i

    def _record(batch_id: str, idx: int) -> None:
        paper = llm_papers[idx]
        results[batch_id] = {
            "summary": paper.get("summary", ""),
            "tags": paper.get("tags", []),
            "venue": paper.get("venue"),
        }
        used_llm_indices.add(idx)

    for batch_id in batch_ids:
        if batch_id in llm_by_id:
            _record(batch_id, llm_by_id[batch_id])

    for batch_id in batch_ids:
        if batch_id in results:
            continue
        norm = _normalize_paper_id(batch_id)
        idx = llm_by_norm.get(norm)
        if idx is not None and idx not in used_llm_indices:
            paper = llm_papers[idx]
            _record(batch_id, idx)
            logger.warning(
                f"[Phase2] {log_label}: matched {batch_id} to LLM id {paper.get('id', '')} by normalization"
            )

    unused_indices = [
        i for i, paper in enumerate(llm_papers)
        if i not in used_llm_indices and not _is_junk_llm_id(paper.get("id", ""))
    ]
    unmatched_batch_ids = [bid for bid in batch_ids if bid not in results]
    for batch_id in unmatched_batch_ids:
        candidates = [
            i for i in unused_indices
            if i not in used_llm_indices and _yymm_one_digit_typo(batch_id, llm_papers[i].get("id", ""))
        ]
        if len(candidates) != 1:
            continue
        idx = candidates[0]
        # Ambiguous if another unmatched batch id also wants this leftover.
        other_claimants = [
            bid for bid in unmatched_batch_ids
            if bid != batch_id and bid not in results
            and _yymm_one_digit_typo(bid, llm_papers[idx].get("id", ""))
        ]
        if other_claimants:
            continue
        paper = llm_papers[idx]
        _record(batch_id, idx)
        logger.warning(
            f"[Phase2] {log_label}: matched {batch_id} to LLM id {paper.get('id', '')} by one-digit YYMM typo"
        )

    if extra_copies:
        recovered = [pid for pid in extra_copies if pid in results]
        logger.warning(
            f"[Phase2] {log_label}: dropped extra LLM copies of {len(extra_copies)} id(s), "
            f"kept first: {recovered or list(extra_copies)}"
        )

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

    # Pass 4b: content check - summary written for another paper of the batch
    content_mismatches = find_content_mismatches(batch, results, content_index) if content_index else []
    content_failures: list[dict] = []
    if content_mismatches:
        flagged_summaries = {m["id"]: results[m["id"]].get("summary", "") for m in content_mismatches}
        details = ", ".join(
            f"{m['id']}~{m['looks_like']}({'/'.join(m['terms'][:3])})" for m in content_mismatches
        )
        if content_strict:
            for m in content_mismatches:
                content_failures.append({**m, "result": results.pop(m["id"])})
            logger.error(
                f"[Phase2] {log_label}: {len(content_mismatches)} summaries read like another "
                f"batch paper: {details}"
            )
        else:
            logger.warning(
                f"[Phase2] {log_label}: keeping {len(content_mismatches)} summaries that read like "
                f"another batch paper (non-strict): {details}"
            )
        if debug_dir is not None and raw_result is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / f"{label}_content_mismatch{attempt_suffix}.json"
            debug_data = {
                "batch_input_ids": batch_ids,
                "strict": content_strict,
                "mismatches": [{**m, "summary": flagged_summaries[m["id"]]} for m in content_mismatches],
                "prompt_papers": [
                    {"id": p["id"], "title": p.get("title", ""), "abstract": p.get("abstract", "")[:200]}
                    for p in batch
                ],
                "llm_response": raw_result,
            }
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            log = logger.error if content_strict else logger.warning
            log(f"[Phase2] {log_label} content mismatch debug saved to {debug_path}")

    duplicate_ids: list[str] = []
    missing_ids = [
        bid for bid in batch_ids
        if bid not in results
        and bid not in {qf["id"] for qf in quality_failures}
        and bid not in {cf["id"] for cf in content_failures}
    ]
    diagnostics = MatchDiagnostics(
        missing_ids=missing_ids,
        duplicate_ids=duplicate_ids,
        quality_failed=quality_failures,
        content_mismatch=content_failures,
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
    id_mismatch_batch_ids = missing_ids + duplicate_ids
    if id_mismatch_batch_ids:
        remaining_llm_ids = [
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
                "unmatched_batch_ids": id_mismatch_batch_ids,
                "unmatched_llm_ids": remaining_llm_ids,
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
    if diagnostics.content_mismatch:
        parts.append(f"{len(diagnostics.content_mismatch)} content mismatch")
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


def _write_batch_cache(
    field_cache: Path,
    batch_num: int,
    matched: dict[str, dict],
    new_tags: list,
    current_tags: list,
) -> None:
    cache_entry = {
        "papers": [{"id": pid, **payload} for pid, payload in matched.items()],
        "new_tags": new_tags,
        "current_tags": list(current_tags),
    }
    cache_path = field_cache / f"batch_{batch_num:03d}.json"
    _atomic_write_json(cache_path, cache_entry)


def _patch_batch_cache(
    field_cache: Path,
    batch_num: int,
    new_papers: list[dict],
    logger: logging.Logger,
    new_tags: list[str] | None = None,
) -> None:
    """Upsert retry-successful papers into an original batch cache file by id.

    When new_tags is provided, retry-discovered tags are unioned into both
    cached["new_tags"] and cached["current_tags"] so subsequent cache-replay
    runs do not lose tags first surfaced during retry.
    """
    cache_path = field_cache / f"batch_{batch_num:03d}.json"
    cached = {"papers": [], "new_tags": [], "current_tags": []}
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception as e:
            logger.warning(f"[Cache] Could not read batch cache {cache_path}, recreating: {e}")

    cached.setdefault("papers", [])
    cached.setdefault("new_tags", [])
    cached.setdefault("current_tags", [])

    deduped: list[dict] = []
    index_by_id: dict[str, int] = {}
    for paper in cached["papers"]:
        pid = paper.get("id")
        if pid is None:
            deduped.append(paper)
            continue
        if pid in index_by_id:
            deduped[index_by_id[pid]] = paper
        else:
            index_by_id[pid] = len(deduped)
            deduped.append(paper)
    for paper in new_papers:
        pid = paper.get("id")
        if pid is not None and pid in index_by_id:
            deduped[index_by_id[pid]] = paper
        else:
            if pid is not None:
                index_by_id[pid] = len(deduped)
            deduped.append(paper)
    cached["papers"] = deduped

    if new_tags:
        for tag in new_tags:
            if tag not in cached["new_tags"]:
                cached["new_tags"].append(tag)
            if tag not in cached["current_tags"]:
                cached["current_tags"].append(tag)

    _atomic_write_json(cache_path, cached)


def _apply_tag_mapping(
    matched: dict[str, dict],
    new_tags: list,
    mapping: dict[str, str],
) -> tuple[dict[str, dict], list[str]]:
    """Rewrite paper tags and new_tags through the canonical mapping."""
    if not mapping:
        return matched, list(new_tags or [])
    for rec in matched.values():
        rec["tags"] = canonicalize_tag_list(rec.get("tags") or [], mapping)
    resolved: list[str] = []
    for tag in new_tags or []:
        if not isinstance(tag, str):
            continue
        mapped_tags = canonicalize_tag_list([tag], mapping)
        for mapped in mapped_tags:
            if mapped not in resolved:
                resolved.append(mapped)
    return matched, resolved


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
    tag_mapping: dict[str, str] | None = None,
    content_index: dict | None = None,
    content_strict: bool = True,
) -> BatchProcessResult:
    batch_num = idx + 1
    blabel = f"{label_prefix}_{batch_num:03d}"
    t0 = time.monotonic()
    try:
        messages = build_prompt(batch, current_tags)
        expected_ids = [coerce_paper_id(p.get("id")) for p in batch]
        result, usage = call_llm(
            messages, api_endpoint, api_key, model, logger, batch_num, debug_dir,
            batch_label=blabel, expected_ids=expected_ids,
        )

        matched, diagnostics = match_llm_results(
            batch, result.get("papers", []), logger, batch_num, result, debug_dir, batch_label=blabel,
            content_index=content_index, content_strict=content_strict,
        )

        new_tags = result.get("new_tags", [])
        matched, new_tags = _apply_tag_mapping(matched, new_tags, tag_mapping or {})
        for tag in new_tags:
            if tag not in current_tags:
                current_tags.append(tag)

        elapsed = time.monotonic() - t0
        usage_str = _format_usage(usage)
        if diagnostics.total_failed > 0:
            failure_summary = _format_match_failures(diagnostics)
            logger.error(
                f"[Phase2] {blabel}: rejecting whole batch ({failure_summary})"
            )
            print(
                f"[{batch_num}/{total}] {blabel} FAILED ({failure_summary}) ({elapsed:.1f}s{usage_str}), see log",
                file=sys.stderr,
            )
            return BatchProcessResult(
                {},
                list(batch),
                True,
                new_tags=list(new_tags),
                current_tags_snapshot=list(current_tags),
            )

        if save_cache and cache_allowed and matched:
            _write_batch_cache(field_cache, batch_num, matched, new_tags, current_tags)

        print(f"[{batch_num}/{total}] {blabel} done ({elapsed:.1f}s{usage_str})", file=sys.stderr)
        return BatchProcessResult(
            matched,
            [],
            False,
            new_tags=list(new_tags),
            current_tags_snapshot=list(current_tags),
        )

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
    _atomic_write_text(hash_file, current_hash)
    return cached_batches


def save_tags(tags_path: Path, current_tags: list) -> None:
    """Persist the current tag list, keeping extra metadata fields."""
    existing: dict = {}
    if tags_path.exists():
        try:
            with open(tags_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    tags_out = {
        **{k: v for k, v in existing.items() if k not in {"updated_at", "tags"}},
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
    content_source: dict | None = None,
) -> tuple[dict, int]:
    """Phase 2: LLM summarization.

    content_source is the full listing used for term rarity in the content check;
    it defaults to data (patch mode passes the whole day while summarizing a few papers).

    Returns:
        (results_by_id, failed_papers_count)
        results_by_id: {paper_id: {"summary": ..., "tags": [...], "venue": ...}}
    """
    current_tags = load_tags(tags_path)
    tag_mapping = load_tag_mapping(tags_path)
    batches = collect_batches(data, BATCH_SIZE)

    if not batches:
        return {}, 0

    cache_writes_enabled = save_cache and not no_cache
    content_index = build_content_index(content_source if content_source is not None else data)

    # Cache setup
    field_cache = cache_dir / data["field"]
    field_cache.mkdir(parents=True, exist_ok=True)
    debug_dir = debug_output_dir(cache_dir, data.get("field") or "unknown", data.get("listing_date"))
    debug_dir.mkdir(parents=True, exist_ok=True)

    cached_batches: dict[int, dict] = {}
    if cache_writes_enabled:
        current_hash = compute_metadata_hash(data)
        hash_file = field_cache / "metadata_hash"
        cached_batches = load_cached_batches(field_cache, hash_file, current_hash, no_cache)

    results_by_id: dict[str, dict] = {}
    all_unresolved: list[UnresolvedPaper] = []

    for idx, batch in enumerate(batches):
        batch_num = idx + 1
        total = len(batches)
        blabel = f"{batch_prefix}_{batch_num:03d}"

        if batch_num in cached_batches:
            cached = cached_batches[batch_num]
            # Cached batches already passed the content check or were accepted on retry,
            # so a replay only warns instead of re-queueing the batch every run.
            matched, diagnostics = match_llm_results(
                batch, cached.get("papers", []), logger, batch_num, cached, debug_dir, batch_label=blabel,
                content_index=content_index, content_strict=False,
            )
            if diagnostics.total_failed > 0:
                logger.warning(
                    f"[Phase2] {blabel}: incomplete cache, retrying whole batch"
                )
                for paper in batch:
                    all_unresolved.append(
                        UnresolvedPaper(paper=paper, original_batch_num=batch_num)
                    )
                print(f"[{batch_num}/{total}] {blabel} cached (incomplete, queued)", file=sys.stderr)
                continue
            matched, _ = _apply_tag_mapping(matched, cached.get("new_tags") or [], tag_mapping)
            results_by_id.update(matched)
            for tag in canonicalize_tag_list(cached.get("current_tags") or [], tag_mapping):
                if tag not in current_tags:
                    current_tags.append(tag)
            print(f"[{batch_num}/{total}] {blabel} cached", file=sys.stderr)
            continue

        processed = _process_batch(
            batch, idx, total, current_tags,
            api_endpoint, api_key, model, logger, debug_dir, field_cache,
            batch_prefix, cache_writes_enabled, cache_allowed=True,
            exception_status="failed after all retries",
            tag_mapping=tag_mapping,
            content_index=content_index, content_strict=True,
        )
        results_by_id.update(processed.matched)
        if processed.failed:
            for paper in processed.unresolved:
                all_unresolved.append(
                    UnresolvedPaper(paper=paper, original_batch_num=batch_num)
                )

    still_failed_papers: list[dict] = []
    retry_batches = [
        all_unresolved[i : i + RETRY_BATCH_SIZE]
        for i in range(0, len(all_unresolved), RETRY_BATCH_SIZE)
    ]
    if retry_batches:
        print(
            f"Retrying {len(all_unresolved)} unresolved paper(s) in "
            f"{len(retry_batches)} batch(es) (batch_size={RETRY_BATCH_SIZE})...",
            file=sys.stderr,
        )
        for retry_idx, retry_batch in enumerate(retry_batches):
            retry_papers = [u.paper for u in retry_batch]

            processed = _process_batch(
                retry_papers, retry_idx, len(retry_batches), current_tags,
                api_endpoint, api_key, model, logger, debug_dir, field_cache,
                "retry", save_cache=False,
                cache_allowed=False,
                exception_status="retry failed",
                tag_mapping=tag_mapping,
                # A summary that still trips the content check after a fresh retry is
                # most likely a false positive; keep it with a warning, since the
                # alternative is the failure placeholder.
                content_index=content_index, content_strict=False,
            )
            results_by_id.update(processed.matched)
            still_failed_papers.extend(processed.unresolved)

            if cache_writes_enabled and processed.matched:
                patch_groups: dict[int, list[dict]] = defaultdict(list)
                for unresolved in retry_batch:
                    pid = unresolved.paper["id"]
                    if pid in processed.matched:
                        patch_groups[unresolved.original_batch_num].append(
                            {"id": pid, **processed.matched[pid]}
                        )
                for original_batch_num, papers in patch_groups.items():
                    group_paper_tags: set[str] = set()
                    for paper in papers:
                        for tag in paper.get("tags") or []:
                            group_paper_tags.add(tag)
                    filtered_new_tags = [
                        tag for tag in processed.new_tags if tag in group_paper_tags
                    ]
                    _patch_batch_cache(
                        field_cache,
                        original_batch_num,
                        papers,
                        logger,
                        new_tags=filtered_new_tags or None,
                    )

        _add_summary_failure_placeholders(results_by_id, still_failed_papers, logger, "retry")

    save_tags(tags_path, current_tags)

    return results_by_id, len(still_failed_papers)


# ---------------------------------------------------------------------------
# Phase 2.5: Venue Extraction
# ---------------------------------------------------------------------------

_ACCEPTANCE_SIGNAL_RE = re.compile(
    r"\b(?:ac+e?pt(?:ed)?|accpted|acceped|published|to appear|camera[- ]ready|appearing (?:at|in)|presented at"
    r"|forthcoming|to be presented|award(?:ed)?|best (?:paper|student paper|demo)|selected (?:as|for)"
    r"|oral|spotlight|poster|in proceedings|proceedings of"
    # Comments that only name the venue ("2026 IEEE International Geoscience and Remote
    # Sensing Symposium (IGARSS)", "IEEE Transactions on Learning Technologies, 2026").
    r"|conference|symposium|workshop|journal|transactions|congress|letters"
    r"|findings|full paper|short paper|long paper)\b"
    r"|\bproc\."
    r"|\(\s*(?:19|20)\d{2}\s*\)"  # "Nature Biomedical Engineering (2026)"
    r"|\b[a-z]{2,12}(?:19|20)\d{2}\b"  # glued "cvpr2026", "INTERSPEECH2026"
    r"|@\s*[A-Z][\w\-&]+",
    re.IGNORECASE,
)
# A bare "ACL 2026" / "IJCAI '26" with no other wording also names a venue,
# unless the comments say the paper is only submitted or under review, or only
# name the venue's LaTeX template ("ICML 2026 style").
_VENUE_YEAR_MENTION_RE = re.compile(r"\b[A-Z][A-Za-z\-&]{1,15}\s?(?:'?(?:19|20)\d{2}|'\d{2})\b")
_NOT_ACCEPTED_RE = re.compile(
    r"\b(?:submitted|under review|in submission|submission|preprint|review|style|template|formatted|format|latex)\b",
    re.IGNORECASE,
)


def has_acceptance_signal(comments: str | None) -> bool:
    """Deterministic keyword gate: True if comments may name a venue this paper was accepted to.

    The gate is deliberately loose; the venue LLM still returns null for papers
    that are only submitted or that cite another paper's venue.
    """
    if not comments:
        return False
    if _ACCEPTANCE_SIGNAL_RE.search(comments):
        return True
    return bool(_VENUE_YEAR_MENTION_RE.search(comments)) and not _NOT_ACCEPTED_RE.search(comments)


VENUE_LLM_TIMEOUT = 30
VENUE_BATCH_SIZE = 16
VENUE_MAX_RETRIES = 2
VENUE_RETRY_DELAYS = [2, 4]
_VENUE_STOPWORDS = frozenset({
    "a", "an", "the", "in", "at", "of", "for", "and", "on", "to", "is", "was",
    "by", "with", "from", "or", "as", "it", "its", "that", "this", "are", "be",
})


def build_venue_prompt(papers: list[dict]) -> list[dict]:
    """Build chat messages for venue extraction LLM call."""
    entries = []
    for p in papers:
        entries.append(f"ID: {p['id']}\nComments: {p.get('comments', '')}")
    user_content = f"""Below are {len(papers)} paper comment fields. For each paper, decide whether the Comments say THIS paper was accepted, published or presented at a conference, workshop or journal, and if so describe that venue.

Rules:
- Use ONLY the Comments text. Do NOT guess venues that are not mentioned.
- Set venue to null if the Comments only say the paper is submitted, under review or in submission, or only mention the venue of another paper (e.g. "extends our GECCO 2018 paper").
- venue: the host conference or journal by its common short name (ICML, NeurIPS, ACL, EMNLP, CVPR, TMLR, IEEE TPAMI). Prefer a short name written in the Comments; otherwise use the widely used abbreviation; if the venue has none, give its shortest standard name (e.g. Bioinformatics). Never put the year, track or workshop into venue.
- full_name: the host's full name if the Comments spell it out, otherwise null.
- year: the four-digit year only if the Comments state it ('26 or "IJCAI 26" means 2026), otherwise null.
- category: "main", "findings", "track" or "workshop".
- track: for category "track", a short label such as D&B, Demo, Industry, Position or Short; otherwise null.
- workshop: for category "workshop", the workshop's short name exactly as written in the Comments (e.g. MATH-AI); null if none is written.
- presentation: "Oral", "Spotlight" or "Poster" only if the Comments say so; otherwise null.
- If several venues are listed, describe the one this paper was accepted to.

Papers:
{PAPER_SEPARATOR.join(entries)}

Respond in strict JSON format:
{{"venues": [{{"id": "...", "venue": "ICML", "full_name": null, "year": "2026", "category": "main", "track": null, "workshop": null, "presentation": null}}]}}"""

    return [
        {
            "role": "system",
            "content": (
                "You are a metadata extraction assistant. "
                "Extract venue information from paper comment fields. "
                "Output valid JSON only, no markdown fences."
            ),
        },
        {"role": "user", "content": user_content},
    ]


def call_venue_llm(
    messages: list[dict],
    api_endpoint: str,
    api_key: str,
    model: str,
    logger: logging.Logger,
    debug_dir: Path | None = None,
    debug_label: str = "venue_error",
) -> dict | None:
    """Call LLM for venue extraction with retries. Returns parsed dict or None on failure."""
    url = f"{api_endpoint.rstrip('/')}/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            # Reasoning used up max_tokens and truncated the JSON, as it did for summaries (6167b42).
            "thinking": {"type": "disabled"},
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_error = None
    last_http_body: str | None = None
    last_content: str | None = None
    for attempt in range(VENUE_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=VENUE_LLM_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
            last_http_body = body
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
            last_content = content
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
            result = json.loads(content)
            if not isinstance(result, dict) or not isinstance(result.get("venues"), list):
                logger.warning("[Venue] LLM response schema invalid, expected {\"venues\": [...]}")
                last_error = ValueError("invalid venue schema")
                continue
            for item in result["venues"]:
                if not isinstance(item, dict) or "id" not in item or "venue" not in item:
                    logger.warning("[Venue] LLM response contains malformed venue entry")
                    last_error = ValueError("malformed venue entry")
                    break
            else:
                return result
            continue
        except urllib.error.HTTPError as e:
            last_error = e
            try:
                body_bytes = e.read()
                if body_bytes:
                    last_http_body = body_bytes.decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning(f"[Venue] LLM call attempt {attempt + 1} HTTP {e.code}")
            if 400 <= e.code < 500 and e.code != 429:
                break
            if e.code == 429 and attempt < VENUE_MAX_RETRIES:
                delay = _retry_after_delay(e.headers, VENUE_RETRY_DELAYS[min(attempt, len(VENUE_RETRY_DELAYS) - 1)])
                logger.warning(f"[Venue] rate limited, waiting {delay:g}s")
                time.sleep(delay)
                continue
            if attempt < VENUE_MAX_RETRIES:
                time.sleep(VENUE_RETRY_DELAYS[min(attempt, len(VENUE_RETRY_DELAYS) - 1)])
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError) as e:
            last_error = e
            logger.warning(f"[Venue] LLM call attempt {attempt + 1} failed: {e}")
            if attempt < VENUE_MAX_RETRIES:
                time.sleep(VENUE_RETRY_DELAYS[min(attempt, len(VENUE_RETRY_DELAYS) - 1)])

    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / f"{debug_label}.json"
        debug_data = {
            "error": str(last_error),
            "prompt": messages[-1]["content"] if messages else "",
            "http_body": last_http_body,
            "content": last_content,
        }
        with open(debug_path, "w", encoding="utf-8") as df:
            json.dump(debug_data, df, ensure_ascii=False, indent=2)
    return None


def _venue_grounding_check(venue: str, comments: str) -> bool:
    """Check that at least one core venue token is grounded in the comments.

    A core token is non-stopword and non-numeric. Pure-digit tokens (years) are
    excluded because two unrelated venues can share the same year (e.g.
    "NeurIPS 2026" must not be grounded by "ICML 2026" just because both mention
    2026). When venue has no core tokens (e.g. only stopwords or only digits),
    grounding fails.

    Matching is token-level (not substring) so short tokens like "AI" are not
    accidentally grounded by appearing inside unrelated words like "available".
    """
    # Upper-case acronyms count even when they spell a stopword ("ITS", "AS").
    core_tokens = [
        t.lower() for t in re.findall(r"[A-Za-z0-9]+", venue)
        if not t.isdigit() and (t.lower() not in _VENUE_STOPWORDS or (t.isupper() and len(t) >= 2))
    ]
    if not core_tokens:
        return False
    lowered = comments.lower()
    # Letter runs too, so "SIGKDD2026" grounds "SIGKDD"; venue tokens stay whole words.
    comment_tokens = set(re.findall(r"[a-z0-9]+", lowered)) | set(re.findall(r"[a-z]+", lowered))
    return any(token in comment_tokens for token in core_tokens)


DEFAULT_VENUE_ABBREVS_PATH = DEFAULT_TAGS_PATH.parent / "venue_abbreviations.json"
PRESENTATION_TYPES = ("Oral", "Spotlight", "Poster")
# Track labels appended after "<VENUE> <YEAR>"; keys are lowercased, without a trailing "track".
_VENUE_TRACKS = {
    "d&b": "D&B", "datasets and benchmarks": "D&B", "datasets & benchmarks": "D&B",
    "e&d": "E&D", "evaluations and datasets": "E&D", "evaluations & datasets": "E&D",
    "demo": "Demo", "demos": "Demo", "demonstration": "Demo", "demonstrations": "Demo",
    "system demonstration": "Demo", "system demonstrations": "Demo",
    "industry": "Industry", "position": "Position", "position paper": "Position", "position papers": "Position",
    "short": "Short", "short paper": "Short", "short papers": "Short",
    "applied data science": "ADS", "ads": "ADS",
}
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
# A workshop is named only by an acronym-like single token (MATH-AI, ML4RS,
# GenAI4Health); descriptive names ("Sight and Sound") fall back to "Workshop".
_WORKSHOP_ACRONYM_RE = re.compile(r"^(?=\S*(?:[A-Z]\S*[A-Z]|\d|[a-z][A-Z]))[A-Za-z0-9][A-Za-z0-9\-&+.]{1,24}$")
_ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE)


def venue_name_key(name: str) -> str:
    """Normalize a venue full name for abbreviation-table lookup."""
    s = name.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = _YEAR_RE.sub(" ", s)
    s = re.sub(r"'\d{2}\b", " ", s)
    s = _ORDINAL_RE.sub(" ", s)
    s = re.sub(r"\b(?:the|proceedings of|annual)\b", " ", s)
    s = re.sub(r"[^a-z0-9&+]+", " ", s)
    return " ".join(s.split())


def load_venue_abbrevs(path: Path | None) -> dict:
    """Load the venue abbreviation table ({"aliases": {...}, "full_names": {...}}); empty if absent."""
    table: dict = {"aliases": {}, "full_names": {}}
    if path is None or not path.exists():
        return table
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for key, value in data.items():
        if key in ("aliases", "full_names"):
            table[key].update(value or {})
        else:
            table[key] = value
    return table


def save_venue_abbrevs(path: Path, table: dict) -> None:
    """Persist the abbreviation table with sorted keys so diffs stay small."""
    data = {k: v for k, v in table.items() if k not in ("aliases", "full_names")}
    data["aliases"] = dict(sorted(table.get("aliases", {}).items()))
    data["full_names"] = dict(sorted(table.get("full_names", {}).items()))
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _clean_venue_name(name: str) -> str:
    """Drop years, ordinals and "Proceedings of" from a name that has no abbreviation."""
    s = re.sub(r"\bproceedings of (?:the )?", "", name, flags=re.IGNORECASE)
    s = _ORDINAL_RE.sub("", s)
    s = _YEAR_RE.sub("", s)
    s = re.sub(r"'\d{2}\b", "", s)
    s = re.sub(r"^\s*the\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(\s*\)", "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" ,;:-")


def _word_in(text: str, comments: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(text)}(?![A-Za-z0-9])", comments, re.IGNORECASE) is not None


def _year_grounded(year: str, comments: str) -> bool:
    return year in comments or re.search(rf"(?<!\d)'?{year[2:]}(?!\d)", comments) is not None


def _canonical_venue_host(short: str, full: str, comments: str, table: dict) -> str | None:
    """Short host name: table full-name match, else alias-normalized short name, else cleaned full name.

    A new full name -> short pair is recorded in the table; it is marked for review
    when the short name does not appear in the comments (the model supplied it).
    """
    aliases, full_names = table["aliases"], table["full_names"]
    full_key = venue_name_key(full) if full else ""
    if full_key and full_key in full_names:
        abbr = full_names[full_key]["abbr"]
        return aliases.get(abbr.lower(), abbr)
    if short:
        short_entry = full_names.get(venue_name_key(short))
        if short_entry:
            return aliases.get(short_entry["abbr"].lower(), short_entry["abbr"])
        # Abbreviations already in full_names count as known short names too.
        known = {e["abbr"].lower(): e["abbr"] for e in full_names.values()}
        known.update(aliases)
        host = known.get(short.lower())
        if host is None:
            # "ACM FAccT", "IEEE ICME", "IEEE/CVF CVPR": drop the publisher when the rest is a known
            # venue; canonical names that keep it ("IEEE S&P", "ACM MM") are aliases themselves.
            bare = re.sub(r"^(?:IEEE/CVF|IEEE/ACM|IEEE|ACM)\s+", "", short, flags=re.IGNORECASE)
            host = known.get(bare.lower(), short)
        if full_key and full_key != venue_name_key(host):
            full_names[full_key] = {"abbr": host} if _word_in(host, comments) else {"abbr": host, "review": True}
        return host
    return _clean_venue_name(full) or None


def format_venue_entry(item: dict, comments: str, table: dict | None = None) -> tuple[str | None, str | None]:
    """Build "<VENUE> <YEAR> [Findings|<track>|[<ws>] Workshop]" and the presentation type.

    Only parts grounded in the comments are kept: the year, the workshop's short
    name and the presentation type must all appear there.
    """
    table = table if table is not None else {"aliases": {}, "full_names": {}}
    comments = comments or ""
    short = item.get("venue") if isinstance(item.get("venue"), str) else ""
    full = item.get("full_name") if isinstance(item.get("full_name"), str) else ""
    year = str(item.get("year") or "").strip()
    year_in_short = _YEAR_RE.search(short)
    if year_in_short:
        year = year or year_in_short.group(1)
        short = _YEAR_RE.sub("", short)
    # The model sometimes folds these into the venue name ("ACL 2026 Findings",
    # "EMNLP Main Conference", "ICML (Oral)"); move them to their own fields.
    category_hint = None
    if re.search(r"\bfindings\b", short, re.IGNORECASE):
        category_hint = "findings"
        short = re.sub(r"\bfindings(?:\s+of)?\b", "", short, flags=re.IGNORECASE)
    short = re.sub(r"\bmain(?:\s+(?:conference|track|paper))?\b", "", short, flags=re.IGNORECASE)
    presentation_in_short = re.search(r"\b(oral|spotlight|poster)\b", short, re.IGNORECASE)
    if presentation_in_short:
        short = re.sub(r"\b(?:oral|spotlight|poster)\b", "", short, flags=re.IGNORECASE)
    short = re.sub(r"\(\s*\)", "", short)
    short = re.sub(r"\s{2,}", " ", short).strip(" ,;:-")
    if not (re.fullmatch(r"(?:19|20)\d{2}", year) and _year_grounded(year, comments)):
        year = ""

    category = str(item.get("category") or category_hint or "main").strip().lower()
    if category_hint and category == "main":
        category = category_hint
    workshop = item.get("workshop") if isinstance(item.get("workshop"), str) else ""
    workshop = re.sub(r"\s*workshop$", "", workshop.strip(), flags=re.IGNORECASE)
    workshop_named = bool(workshop and _WORKSHOP_ACRONYM_RE.match(workshop) and workshop.lower() in comments.lower())

    presentation = item.get("presentation") if isinstance(item.get("presentation"), str) else ""
    if not presentation and presentation_in_short:
        presentation = presentation_in_short.group(1)
    presentation = presentation.strip().capitalize()
    if presentation not in PRESENTATION_TYPES or not _word_in(presentation, comments):
        presentation = None

    host = _canonical_venue_host(short, full.strip(), comments, table)
    if not host:
        # A standalone workshop has no host conference: "TPDP 2026 Workshop". Without an
        # acronym its (short) name is the only identifier: "World Modeling 2026 Workshop".
        standalone = workshop if category == "workshop" and workshop.lower() in comments.lower() else ""
        if standalone and (workshop_named or len(standalone) <= 30):
            return " ".join([standalone] + ([year] if year else []) + ["Workshop"]), presentation
        return None, None
    workshop_in_host = False
    if category == "workshop":
        # "Graph Signal Processing Workshop" / "IEEE Globecom Workshops" as the venue: "Workshop"
        # is appended below; "International Workshop on X" already says it, so nothing is added.
        trimmed = re.sub(r"\s*workshops?$", "", host, flags=re.IGNORECASE)
        workshop_in_host = trimmed == host and re.search(r"\bworkshops?\b", host, re.IGNORECASE) is not None
        if trimmed and trimmed != host:
            host = table["aliases"].get(trimmed.lower(), trimmed)
    parts = [host] + ([year] if year else [])
    track = item.get("track") if isinstance(item.get("track"), str) else ""
    track_key = re.sub(r"\s*track$", "", track.strip(), flags=re.IGNORECASE).lower()
    if category == "findings" or track_key == "findings":
        parts.append("Findings")
    elif category == "track" and track_key:
        label = _VENUE_TRACKS.get(track_key)
        if label is None:
            # An unlisted label counts only if the comments call it a track ("AI for Science Track").
            raw_label = re.sub(r"\s*track$", "", track.strip(), flags=re.IGNORECASE)
            if raw_label and re.search(rf"{re.escape(raw_label)}\s+track", comments, re.IGNORECASE):
                label = raw_label
        if label and len(label) <= 20:
            parts.append(label)
    elif category == "workshop" and not workshop_in_host:
        parts.append(f"{workshop} Workshop" if workshop_named and workshop.lower() != host.lower() else "Workshop")
    return " ".join(parts), presentation


def _venue_grounded_by_alias(item: dict, comments: str, table: dict | None) -> bool:
    """Fallbacks for venues the token check misses: the name written verbatim ("A&A"),
    or a comment token that the alias table maps to the same venue ("NeuIPS" -> NeurIPS)."""
    short = item.get("venue") if isinstance(item.get("venue"), str) else ""
    short = _YEAR_RE.sub("", short).strip()
    if not short:
        return False
    if _word_in(short, comments):
        return True
    aliases = (table or {}).get("aliases", {})
    canonical = aliases.get(short.lower(), short)
    lowered = comments.lower()
    tokens = set(re.findall(r"[a-z0-9&\-]+", lowered)) | set(re.findall(r"[a-z]+", lowered))
    if any(aliases.get(t) == canonical for t in tokens):
        return True
    # A misspelled venue the model corrected ("ENMLP" -> EMNLP, "ASPDAC'27" -> ASP-DAC):
    # one edit (or swap) away, for names of at least five letters.
    name = re.sub(r"[^a-z0-9]", "", short.lower())
    if len(name) < 5:
        return False
    squashed = {re.sub(r"[^a-z0-9]", "", t) for t in tokens}
    return any(abs(len(t) - len(name)) <= 1 and _osa_distance(t, name) <= 1 for t in squashed)


def _osa_distance(a: str, b: str) -> int:
    """Optimal string alignment distance (Levenshtein plus adjacent transpositions)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def resolve_venue_item(
    item: dict, comments: str, abbrev_table: dict | None = None,
    logger: logging.Logger | None = None, pid: str = "",
) -> dict:
    """Turn one venue-LLM entry into {"venue", "presentation"}: grounding check, then formatting."""
    raw = " ".join(
        v for v in (item.get("venue"), item.get("full_name"), item.get("workshop"))
        if isinstance(v, str) and v.strip()
    )
    if not raw:
        return {"venue": None, "presentation": None}
    if not (_venue_grounding_check(raw, comments) or _venue_grounded_by_alias(item, comments, abbrev_table)):
        if logger:
            logger.warning(f"[Venue] grounding failed for {pid}: '{raw}' not in comments")
        return {"venue": None, "presentation": None}
    venue_str, presentation = format_venue_entry(item, comments, abbrev_table)
    if logger and venue_str and len(venue_str) > 60:
        logger.warning(f"[Venue] long venue for {pid} ({len(venue_str)} chars): '{venue_str}'")
    return {"venue": venue_str, "presentation": presentation}


def _cached_venue_info(value) -> dict:
    """Accept both cache formats: a venue string (format 1) or {"venue", "presentation"}."""
    if isinstance(value, dict):
        return {"venue": value.get("venue"), "presentation": value.get("presentation")}
    return {"venue": value, "presentation": None}


def extract_venue_info_batch(
    papers: list[dict],
    api_endpoint: str,
    api_key: str,
    model: str,
    logger: logging.Logger,
    field_cache: Path | None = None,
    metadata_hash: str | None = None,
    debug_dir: Path | None = None,
    abbrev_table: dict | None = None,
) -> dict[str, dict]:
    """Extract {"venue", "presentation"} for all papers using keyword gate + LLM.

    abbrev_table (see load_venue_abbrevs) normalizes host names and collects new
    full name -> abbreviation pairs; the caller saves it.
    """
    infos: dict[str, dict] = {}
    gated_papers = []

    for p in papers:
        pid = p["id"]
        if has_acceptance_signal(p.get("comments")):
            gated_papers.append(p)
        else:
            infos[pid] = {"venue": None, "presentation": None}

    logger.info(f"[Venue] {len(gated_papers)}/{len(papers)} papers passed acceptance gate")

    if not gated_papers:
        return infos

    cached_ids: set[str] = set()
    if field_cache is not None and metadata_hash is not None:
        cache_file = field_cache / "venue_cache.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                if cache_data.get("metadata_hash") == metadata_hash:
                    for pid, value in cache_data.get("venues", {}).items():
                        infos[pid] = _cached_venue_info(value)
                        cached_ids.add(pid)
                    logger.info(f"[Venue] loaded {len(cached_ids)} cached venue results")
                else:
                    logger.info("[Venue] cache hash mismatch, discarding old cache")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[Venue] failed to read cache: {e}")

    remaining = [p for p in gated_papers if p["id"] not in cached_ids]
    if not remaining:
        return infos

    venue_debug_dir = debug_dir if debug_dir is not None else field_cache
    batch_count = 0
    for i in range(0, len(remaining), VENUE_BATCH_SIZE):
        batch = remaining[i:i + VENUE_BATCH_SIZE]
        messages = build_venue_prompt(batch)
        result = call_venue_llm(
            messages, api_endpoint, api_key, model, logger,
            debug_dir=venue_debug_dir,
            debug_label=f"venue_batch_{batch_count:03d}_error",
        )
        if result is None:
            for p in batch:
                infos[p["id"]] = {"venue": None, "presentation": None}
            logger.warning(f"[Venue] batch {batch_count:03d} failed, setting all venues to None")
        else:
            items = {coerce_paper_id(item["id"]): item for item in result["venues"]}
            for p in batch:
                pid = p["id"]
                infos[pid] = resolve_venue_item(
                    items.get(coerce_paper_id(pid)) or {}, p.get("comments") or "", abbrev_table, logger, pid,
                )

        if field_cache is not None and metadata_hash is not None:
            cache_file = field_cache / "venue_cache.json"
            field_cache.mkdir(parents=True, exist_ok=True)
            processed_ids = cached_ids | {p["id"] for p in remaining[:i + VENUE_BATCH_SIZE]}
            cache_data = {
                "metadata_hash": metadata_hash,
                "format": 2,
                "venues": {pid: infos[pid] for pid in processed_ids if pid in infos},
            }
            tmp_path = cache_file.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(cache_file)

        batch_count += 1

    successful = sum(1 for info in infos.values() if info["venue"] is not None)
    logger.info(f"[Venue] extraction complete: {successful} venues extracted from {len(papers)} papers")
    return infos


def extract_venues_batch(
    papers: list[dict],
    api_endpoint: str,
    api_key: str,
    model: str,
    logger: logging.Logger,
    field_cache: Path | None = None,
    metadata_hash: str | None = None,
    debug_dir: Path | None = None,
    abbrev_table: dict | None = None,
) -> dict[str, str | None]:
    """Venue strings only; see extract_venue_info_batch for the presentation type."""
    infos = extract_venue_info_batch(
        papers, api_endpoint, api_key, model, logger,
        field_cache=field_cache, metadata_hash=metadata_hash, debug_dir=debug_dir,
        abbrev_table=abbrev_table,
    )
    return {pid: info["venue"] for pid, info in infos.items()}


def _venue_abbrevs_path(args) -> Path | None:
    """--venue-abbrevs from the CLI; None (no table) when the attribute is absent."""
    value = getattr(args, "venue_abbrevs", None)
    return Path(value) if value else None


def apply_venue_extraction(
    papers: list[dict],
    results_by_id: dict[str, dict],
    llm_config: "LlmConfig",
    logger: logging.Logger,
    abbrevs_path: Path | None,
    field_cache: Path | None = None,
    metadata_hash: str | None = None,
    debug_dir: Path | None = None,
) -> None:
    """Phase 2.5: fill venue and presentation into results_by_id, then save new abbreviations.

    abbrevs_path None runs without the abbreviation table (nothing is written).
    """
    table = load_venue_abbrevs(abbrevs_path)
    known = set(table["full_names"])
    try:
        infos = extract_venue_info_batch(
            papers, llm_config.api_endpoint, llm_config.api_key, llm_config.model, logger,
            field_cache=field_cache, metadata_hash=metadata_hash, debug_dir=debug_dir,
            abbrev_table=table,
        )
    except Exception as e:
        logger.warning(f"[Venue] extraction failed, continuing without venues: {e}")
        return
    for pid, info in infos.items():
        if pid in results_by_id:
            results_by_id[pid]["venue"] = info["venue"]
            results_by_id[pid]["presentation"] = info["presentation"]
    added = sorted(set(table["full_names"]) - known)
    if abbrevs_path is not None and added:
        save_venue_abbrevs(abbrevs_path, table)
        review = [k for k in added if table["full_names"][k].get("review")]
        logger.info(
            f"[Venue] added {len(added)} abbreviation(s) to {abbrevs_path} ({len(review)} marked for review): "
            + ", ".join(f"{k} -> {table['full_names'][k]['abbr']}" for k in added)
        )


# ---------------------------------------------------------------------------
# Phase 3: Markdown Generation
# ---------------------------------------------------------------------------


def _escape_md_inline(text: str) -> str:
    """Escape markdown-sensitive characters in inline list text."""
    text = text.replace("|", "\\|")
    text = text.replace("<", "\\<")
    text = text.replace(">", "\\>")
    return text


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


def front_matter_date(listing_date: str | None, now: datetime) -> datetime:
    """Return the post's front-matter date.

    Jekyll derives the post URL (/posts/:year/:month/:day/:title/) from this date,
    so it must fall on the listing date or catch-up runs collide with other days'
    posts. An on-time run (same OUTPUT_TZ calendar day as the listing) keeps the
    run time; a catch-up run uses CATCHUP_POST_HOUR on the listing date.
    """
    now = now.astimezone(OUTPUT_TZ)
    try:
        day = datetime.strptime(listing_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return now
    if now.date() == day:
        return now
    return datetime(day.year, day.month, day.day, CATCHUP_POST_HOUR, tzinfo=OUTPUT_TZ)


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
    post_date = front_matter_date(date_str, datetime.now(OUTPUT_TZ))
    lines.append("---")
    lines.append(f'title: "arXiv {field} Daily Update"')
    lines.append(f"date: {post_date.strftime('%Y-%m-%d %H:%M:%S +0800')}")
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
                if r.get("presentation"):
                    lines.append(f"- **Presentation**: {r['presentation']}")
            if paper.get("code_urls"):
                code_links = ", ".join(f"[code]({u})" for u in paper["code_urls"])
                lines.append(f"- **Code**: {code_links}")
            lines.append(f"- **Summary**: {_escape_md_inline(summary)}")
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
        presentation = r.get("presentation")

        # Find the paper block by arXiv ID link
        # Pattern: from the arXiv line to the next heading or end
        paper_pattern = re.compile(
            rf"(- \*\*arXiv\*\*: \[{re.escape(pid)}\].*\n)"
            rf"((?:- \*\*(?:Authors|Subjects|Tags|Venue|Presentation|Code|Summary)\*\*:.*\n)*)",
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

        # Rebuild in canonical order: Authors, Subjects, Tags, Venue, Presentation, Code, Summary
        new_lines = [arxiv_line]
        if "Authors" in existing:
            new_lines.append(existing["Authors"])
        if "Subjects" in existing:
            new_lines.append(existing["Subjects"])
        if tags:
            new_lines.append(f"- **Tags**: {', '.join(tags)}\n")
        if venue:
            new_lines.append(f"- **Venue**: {venue}\n")
            if presentation:
                new_lines.append(f"- **Presentation**: {presentation}\n")
        if "Code" in existing:
            new_lines.append(existing["Code"])
        new_lines.append(f"- **Summary**: {_escape_md_inline(summary)}\n")

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
    results_by_id, failed_papers = run_phase2(
        patch_data, tags_path, cache_base, no_cache=True,
        api_endpoint=llm_config.api_endpoint,
        api_key=llm_config.api_key,
        model=llm_config.model,
        logger=logger,
        batch_prefix="patch_batch", save_cache=False,
        content_source=full_data,
    )

    if not results_by_id:
        raise PipelineError("No papers successfully matched from LLM response")

    # Phase 2.5: Venue extraction (no cache in patch mode)
    apply_venue_extraction(
        patch_papers, results_by_id, llm_config, logger, _venue_abbrevs_path(args),
        debug_dir=debug_output_dir(cache_base, args.field, args.date),
    )

    # Patch markdown
    output_dir = Path(args.output)
    md_path = output_dir / f"{args.date}-{args.field}.md"
    if not md_path.exists():
        raise ConfigError(f"Markdown file not found: {md_path}")

    patched = patch_markdown(md_path, results_by_id, logger)
    total_failed = len(patch_ids) - len(results_by_id) + failed_papers
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
            "failed_papers": 0,
            "skipped": True,
        }
        return RunResult(summary)
    if expected_output.exists() and args.date and not args.no_cache:
        logger.info(f"[Phase1] --date mode overwriting existing output: {expected_output}")
        print(f"Warning: --date mode overwriting existing output: {expected_output}", file=sys.stderr)

    # Phase 2: LLM summarization
    tags_path = Path(args.tags)
    results_by_id, failed_papers = run_phase2(
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

    if failed_papers > 0:
        logger.error(f"[Phase2] {failed_papers} paper(s) still failed after retry, skipping markdown generation")
        raise PipelineError(
            f"{failed_papers} paper(s) failed. Markdown not generated. "
            f"Re-run with --date {date_str} to retry (cached batches will be reused)."
        )

    # Phase 2.5: Venue extraction
    all_papers = [p for s in data["sections"] for p in s["papers"]]
    venue_cache_dir = None if args.no_cache else field_cache
    venue_hash = compute_metadata_hash(data) if venue_cache_dir else None
    apply_venue_extraction(
        all_papers, results_by_id, llm_config, logger, _venue_abbrevs_path(args),
        field_cache=venue_cache_dir, metadata_hash=venue_hash,
        debug_dir=debug_output_dir(cache_base, args.field, date_str),
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
        "failed_papers": 0,
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
    parser.add_argument(
        "--output",
        default=str(DEFAULT_POSTS_DIR),
        help="Output directory for markdown files (default: %(default)s)",
    )
    parser.add_argument(
        "--tags",
        default=str(DEFAULT_TAGS_PATH),
        help="Path to tags.json (default: %(default)s)",
    )
    parser.add_argument(
        "--venue-abbrevs",
        default=str(DEFAULT_VENUE_ABBREVS_PATH),
        help="Venue abbreviation table, updated with new entries (default: %(default)s)",
    )
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
