"""Tests for arxiv_daily.py."""
import json
import logging
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import arxiv_daily


# ===========================================================================
# TestCleanHtml
# ===========================================================================

class TestCleanHtml:
    def test_strips_tags(self):
        assert arxiv_daily.clean_html("<b>bold</b> text") == "bold text"

    def test_normalizes_whitespace(self):
        assert arxiv_daily.clean_html("  foo   bar  ") == "foo bar"

    def test_unescapes_html_entities(self):
        assert arxiv_daily.clean_html("&amp; &lt; &gt;") == "& < >"

    def test_empty_string(self):
        assert arxiv_daily.clean_html("") == ""

    def test_nested_tags(self):
        assert arxiv_daily.clean_html("<div><span>inner</span></div>") == "inner"

    def test_preserves_text_between_tags(self):
        assert arxiv_daily.clean_html("<a>hello</a> <b>world</b>") == "hello world"


# ===========================================================================
# TestParseListingDate
# ===========================================================================

class TestParseListingDate:
    def test_new_listing_format(self):
        html = '<p>Showing new listings for Friday, 11 April 2025</p>'
        date, ptype = arxiv_daily.parse_listing_date(html)
        assert date == "2025-04-11"
        assert ptype == "new"

    def test_catchup_format(self):
        html = '<p>Total of 16 entries for Fri, 10 Apr 2026</p>'
        date, ptype = arxiv_daily.parse_listing_date(html)
        assert date == "2026-04-10"
        assert ptype == "catchup"

    def test_no_date_found(self):
        html = '<p>Some random content</p>'
        date, ptype = arxiv_daily.parse_listing_date(html)
        assert date is None
        assert ptype == "unknown"

    def test_empty_html(self):
        date, ptype = arxiv_daily.parse_listing_date("")
        assert date is None
        assert ptype == "unknown"

    def test_invalid_month(self):
        html = '<p>Showing new listings for Friday, 11 Foobar 2025</p>'
        date, ptype = arxiv_daily.parse_listing_date(html)
        assert date is None
        assert ptype == "unknown"


# ===========================================================================
# TestParseSections
# ===========================================================================

class TestParseSections:
    def test_all_three_sections(self):
        html = (
            '<h3>New submissions (showing 10 of 12 entries)</h3>'
            '<h3>Cross submissions (showing 3 of 3 entries)</h3>'
            '<h3>Replacement submissions (showing 5 of 5 entries)</h3>'
        )
        sections = arxiv_daily.parse_sections(html)
        assert len(sections) == 3
        assert sections[0] == ("New submissions", 12, html.index("<h3>New"))
        assert sections[1][0] == "Cross submissions"
        assert sections[1][1] == 3
        assert sections[2][0] == "Replacement submissions"
        assert sections[2][1] == 5

    def test_single_section(self):
        html = '<h3>New submissions (showing 5 of 5 entries)</h3>'
        sections = arxiv_daily.parse_sections(html)
        assert len(sections) == 1
        assert sections[0][0] == "New submissions"
        assert sections[0][1] == 5

    def test_no_sections(self):
        html = '<p>No papers today</p>'
        assert arxiv_daily.parse_sections(html) == []

    def test_positions_monotonically_increasing(self):
        html = (
            '<h3>New submissions (showing 10 of 10 entries)</h3>'
            'stuff in between'
            '<h3>Cross submissions (showing 3 of 3 entries)</h3>'
        )
        sections = arxiv_daily.parse_sections(html)
        assert sections[0][2] < sections[1][2]


# ===========================================================================
# TestExtractCodeUrls
# ===========================================================================

class TestExtractCodeUrls:
    def test_github_url(self):
        html = 'Code at <a href="https://github.com/user/repo">link</a>'
        assert arxiv_daily.extract_code_urls(html) == ["https://github.com/user/repo"]

    def test_gitlab_url(self):
        html = '<a href="https://gitlab.com/user/project">link</a>'
        assert arxiv_daily.extract_code_urls(html) == ["https://gitlab.com/user/project"]

    def test_http_upgraded_to_https(self):
        html = '<a href="http://github.com/user/repo">link</a>'
        assert arxiv_daily.extract_code_urls(html) == ["https://github.com/user/repo"]

    def test_no_urls(self):
        assert arxiv_daily.extract_code_urls("no links here") == []

    def test_deduplication(self):
        html = (
            '<a href="https://github.com/user/repo">link1</a>'
            '<a href="https://github.com/user/repo">link2</a>'
        )
        assert arxiv_daily.extract_code_urls(html) == ["https://github.com/user/repo"]

    def test_non_code_urls_excluded(self):
        html = '<a href="https://example.com/paper.pdf">link</a>'
        assert arxiv_daily.extract_code_urls(html) == []

    def test_mixed_urls(self):
        html = (
            '<a href="https://github.com/user/repo">gh</a>'
            '<a href="https://example.com/foo">other</a>'
            '<a href="https://gitlab.com/org/proj">gl</a>'
        )
        result = arxiv_daily.extract_code_urls(html)
        assert "https://github.com/user/repo" in result
        assert "https://gitlab.com/org/proj" in result
        assert len(result) == 2


# ===========================================================================
# TestExtractPaper
# ===========================================================================

class TestExtractPaper:
    def test_full_paper(self, sample_paper_chunk):
        paper = arxiv_daily.extract_paper(sample_paper_chunk)
        assert paper is not None
        assert paper["id"] == "2604.06189"
        assert "ATANT" in paper["title"]
        assert "Alice Smith" in paper["authors"]
        assert "cs.AI" in paper["subjects"]
        assert "Accepted at ACL 2026" in paper["comments"]
        assert "framework" in paper["abstract"]
        assert "https://github.com/example/atant" in paper["code_urls"]

    def test_no_id_returns_none(self):
        chunk = '<div>No arxiv id here</div>'
        assert arxiv_daily.extract_paper(chunk) is None

    def test_empty_chunk(self):
        assert arxiv_daily.extract_paper("") is None

    def test_minimal_paper(self):
        chunk = '<dt><a>arXiv:2604.99999</a></dt>'
        paper = arxiv_daily.extract_paper(chunk)
        assert paper is not None
        assert paper["id"] == "2604.99999"

    def test_code_url_in_abstract(self):
        chunk = '''\
<dt><a>arXiv:2604.11111</a></dt>
<dd><div class="meta">
<div class="list-title mathjax"><span class="descriptor">Title:</span> Test</div>
<p class="mathjax">
  See our code at <a href="https://github.com/org/code">this https URL</a>.
</p>
</div></dd>'''
        paper = arxiv_daily.extract_paper(chunk)
        assert paper is not None
        assert paper["code_urls"] == ["https://github.com/org/code"]


# ===========================================================================
# TestParseHtml
# ===========================================================================

class TestParseHtml:
    def test_full_page(self, sample_listing_html):
        data = arxiv_daily.parse_html(sample_listing_html, "cs.AI")
        assert data["listing_date"] == "2025-04-11"
        assert data["page_type"] == "new"
        assert data["field"] == "cs.AI"
        assert len(data["sections"]) == 3
        assert data["sections"][0]["name"] == "New submissions"
        assert data["sections"][0]["count"] == 2
        assert len(data["sections"][0]["papers"]) == 2
        assert data["sections"][1]["name"] == "Cross submissions"
        assert len(data["sections"][1]["papers"]) == 1
        assert data["sections"][2]["name"] == "Replacement submissions"
        assert len(data["sections"][2]["papers"]) == 1

    def test_empty_page(self):
        html = '<html><body><p>Nothing here</p></body></html>'
        data = arxiv_daily.parse_html(html, "cs.AI")
        assert data["sections"] == []
        assert data["field"] == "cs.AI"

    def test_paper_ids_correct(self, sample_listing_html):
        data = arxiv_daily.parse_html(sample_listing_html, "cs.AI")
        ids = [p["id"] for s in data["sections"] for p in s["papers"]]
        assert "2604.06189" in ids
        assert "2604.07001" in ids
        assert "2604.08123" in ids
        assert "2603.01234" in ids


# ===========================================================================
# TestBuildPrompt
# ===========================================================================

class TestBuildPrompt:
    def test_returns_two_messages(self):
        papers = [{"id": "2604.00001", "title": "Test", "abstract": "Abstract"}]
        messages = arxiv_daily.build_prompt(papers, ["Tag1"])
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_paper_ids_in_content(self):
        papers = [
            {"id": "2604.00001", "title": "T1", "abstract": "A1"},
            {"id": "2604.00002", "title": "T2", "abstract": "A2"},
        ]
        messages = arxiv_daily.build_prompt(papers, [])
        content = messages[1]["content"]
        assert "2604.00001" in content
        assert "2604.00002" in content

    def test_empty_tags(self):
        papers = [{"id": "2604.00001", "title": "T", "abstract": "A"}]
        messages = arxiv_daily.build_prompt(papers, [])
        assert "(empty)" in messages[1]["content"]

    def test_tags_listed(self):
        papers = [{"id": "2604.00001", "title": "T", "abstract": "A"}]
        messages = arxiv_daily.build_prompt(papers, ["LLM Reasoning", "AI Safety"])
        content = messages[1]["content"]
        assert "LLM Reasoning" in content
        assert "AI Safety" in content

    def test_comments_included(self):
        papers = [{"id": "2604.00001", "title": "T", "abstract": "A", "comments": "Accepted at NeurIPS"}]
        messages = arxiv_daily.build_prompt(papers, [])
        assert "Accepted at NeurIPS" in messages[1]["content"]

    def test_no_comments(self):
        papers = [{"id": "2604.00001", "title": "T", "abstract": "A"}]
        messages = arxiv_daily.build_prompt(papers, [])
        assert "Comments:" not in messages[1]["content"]


# ===========================================================================
# TestMatchLlmResults
# ===========================================================================

class TestMatchLlmResults:
    def test_exact_match(self, logger):
        batch = [{"id": "2604.00001"}, {"id": "2604.00002"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * 60, "tags": ["T1"], "venue": None},
            {"id": "2604.00002", "summary": "y" * 60, "tags": ["T2"], "venue": "ACL 2026"},
        ]
        results, unmatched = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 2
        assert unmatched == 0
        assert results["2604.00001"]["tags"] == ["T1"]
        assert results["2604.00002"]["venue"] == "ACL 2026"

    def test_versioned_id_match(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001v2", "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, unmatched = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert "2604.00001" in results
        assert unmatched == 0

    def test_wrong_ids(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "9999.99999", "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, unmatched = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 0
        assert unmatched == 1

    def test_quality_check_short_summary(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "short", "tags": ["T1"], "venue": None},
        ]
        results, unmatched = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 0
        assert unmatched == 1  # quality failures are included in unmatched count

    def test_quality_check_missing_tags(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * 60, "tags": [], "venue": None},
        ]
        results, unmatched = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 0

    def test_empty_llm_papers(self, logger):
        batch = [{"id": "2604.00001"}]
        results, unmatched = arxiv_daily.match_llm_results(batch, [], logger, 1)
        assert len(results) == 0
        assert unmatched == 1

    def test_empty_batch(self, logger):
        results, unmatched = arxiv_daily.match_llm_results([], [], logger, 1)
        assert results == {}
        assert unmatched == 0

    def test_partial_match(self, logger):
        batch = [{"id": "2604.00001"}, {"id": "2604.00002"}, {"id": "2604.00003"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * 60, "tags": ["T1"], "venue": None},
            {"id": "9999.99999", "summary": "y" * 60, "tags": ["T2"], "venue": None},
        ]
        results, unmatched = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 1
        assert unmatched == 2


# ===========================================================================
# TestComputeMetadataHash
# ===========================================================================

class TestComputeMetadataHash:
    def test_deterministic(self, sample_data):
        h1 = arxiv_daily.compute_metadata_hash(sample_data)
        h2 = arxiv_daily.compute_metadata_hash(sample_data)
        assert h1 == h2

    def test_hex_format(self, sample_data):
        h = arxiv_daily.compute_metadata_hash(sample_data)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_data(self, sample_data):
        import copy
        data2 = copy.deepcopy(sample_data)
        data2["sections"][0]["papers"][0]["id"] = "9999.99999"
        assert arxiv_daily.compute_metadata_hash(sample_data) != arxiv_daily.compute_metadata_hash(data2)

    def test_order_independent(self):
        data1 = {"sections": [{"papers": [{"id": "a"}, {"id": "b"}]}]}
        data2 = {"sections": [{"papers": [{"id": "b"}, {"id": "a"}]}]}
        assert arxiv_daily.compute_metadata_hash(data1) == arxiv_daily.compute_metadata_hash(data2)


# ===========================================================================
# TestExtractPaperAbbrev
# ===========================================================================

class TestExtractPaperAbbrev:
    def test_colon_pattern(self):
        assert arxiv_daily.extract_paper_abbrev("ATANT: A Test Framework") == "ATANT"

    def test_dash_pattern(self):
        assert arxiv_daily.extract_paper_abbrev("ATANT - A Test Framework") == "ATANT"

    def test_parenthetical_pattern(self):
        assert arxiv_daily.extract_paper_abbrev("A Test Framework (ATANT)") == "ATANT"

    def test_no_abbreviation(self):
        assert arxiv_daily.extract_paper_abbrev("A paper about something") is None

    def test_lowercase_start_rejected(self):
        assert arxiv_daily.extract_paper_abbrev("lowercase: something") is None

    def test_with_numbers(self):
        assert arxiv_daily.extract_paper_abbrev("GPT4: Something New") == "GPT4"

    def test_chinese_colon(self):
        assert arxiv_daily.extract_paper_abbrev("BERT： 中文标题") == "BERT"


# ===========================================================================
# TestFetchPage
# ===========================================================================

class TestFetchPage:
    def test_url_construction(self):
        with patch("arxiv_daily.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"<html>test</html>"
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = arxiv_daily.fetch_page("cs.AI")
            assert result == "<html>test</html>"

            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert "cs.AI" in req.full_url
            assert "arxiv.org/list/cs.AI/new" in req.full_url

    def test_network_error(self):
        with patch("arxiv_daily.urllib.request.urlopen") as mock_urlopen:
            from urllib.error import URLError
            mock_urlopen.side_effect = URLError("Connection refused")
            with pytest.raises(URLError):
                arxiv_daily.fetch_page("cs.AI")


# ===========================================================================
# TestCallLlm
# ===========================================================================

class TestCallLlm:
    def test_normal_response(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        messages = arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "Test", "abstract": "Abstract text"}],
            ["Tag1"],
        )
        result, usage = arxiv_daily.call_llm(messages, api_url, "test-key", "test-model", logger)
        assert "papers" in result
        assert len(result["papers"]) == 1
        assert result["papers"][0]["id"] == "2604.00001"
        assert usage.get("total_tokens", 0) > 0

    def test_retry_on_500(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["http_500", "http_500", "none"]
        handler_cls.request_counter = 0

        messages = arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "Test", "abstract": "Abstract text"}],
            [],
        )
        result, usage = arxiv_daily.call_llm(messages, api_url, "test-key", "test-model", logger)
        assert "papers" in result

    def test_all_retries_fail(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "http_500"

        messages = arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "Test", "abstract": "Abstract text"}],
            [],
        )
        with pytest.raises(Exception):
            arxiv_daily.call_llm(messages, api_url, "test-key", "test-model", logger)

    def test_markdown_json_stripping(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "markdown_json"

        messages = arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "Test", "abstract": "Abstract text"}],
            [],
        )
        result, usage = arxiv_daily.call_llm(messages, api_url, "test-key", "test-model", logger)
        assert "papers" in result

    def test_debug_files_written(self, fake_llm_server, logger, tmp_path):
        api_url, handler_cls = fake_llm_server
        messages = arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "Test", "abstract": "Abstract text"}],
            [],
        )
        arxiv_daily.call_llm(
            messages, api_url, "test-key", "test-model", logger,
            batch_num=1, debug_dir=tmp_path, batch_label="test_batch",
        )
        stream_files = list(tmp_path.glob("*stream*"))
        assert len(stream_files) > 0


# ===========================================================================
# TestRunPhase2
# ===========================================================================

class TestRunPhase2:
    def test_normal_run(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        results, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert failed == 0
        # Should have results for papers with abstracts
        assert len(results) > 0

    def test_caching(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # First run
        arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )

        # Record request count
        count_after_first = handler_cls.request_counter

        # Second run - should use cache
        results, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )

        # No additional LLM requests should have been made
        assert handler_cls.request_counter == count_after_first

    def test_no_cache_flag(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # First run
        arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        count_after_first = handler_cls.request_counter

        # Second run with no_cache
        arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=True,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert handler_cls.request_counter > count_after_first

    def test_tags_updated(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )

        # Tags file should be updated
        tags_data = json.loads(tags_file.read_text(encoding="utf-8"))
        assert "tags" in tags_data
        assert "updated_at" in tags_data

    def test_empty_sections(self, fake_llm_server, logger, tags_file, tmp_path):
        api_url, handler_cls = fake_llm_server
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        empty_data = {"field": "cs.AI", "sections": [{"name": "New submissions", "count": 0, "papers": []}]}

        results, failed = arxiv_daily.run_phase2(
            empty_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert results == {}
        assert failed == 0


# ===========================================================================
# TestGenerateMarkdown
# ===========================================================================

class TestGenerateMarkdown:
    def test_basic_generation(self, sample_data, tmp_path):
        results_by_id = {
            "2604.06189": {"summary": "摘要一", "tags": ["LLM Reasoning"], "venue": "ACL 2026"},
            "2604.07001": {"summary": "摘要二", "tags": ["Diffusion Models"], "venue": None},
            "2604.08123": {"summary": "摘要三", "tags": ["Transfer Learning"], "venue": None},
        }
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        assert output.exists()
        assert output.name == "2025-04-11-cs.AI.md"

    def test_front_matter(self, sample_data, tmp_path):
        results_by_id = {
            "2604.06189": {"summary": "摘要", "tags": ["T"], "venue": None},
            "2604.07001": {"summary": "摘要", "tags": ["T"], "venue": None},
            "2604.08123": {"summary": "摘要", "tags": ["T"], "venue": None},
        }
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "title:" in content
        assert "date:" in content
        assert "categories:" in content

    def test_section_headers(self, sample_data, tmp_path):
        results_by_id = {}
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "## 新投稿 (2)" in content
        assert "## 跨领域投稿 (1)" in content
        assert "## 替换投稿 (1)" in content

    def test_paper_entries(self, sample_data, tmp_path):
        results_by_id = {
            "2604.06189": {"summary": "论文摘要", "tags": ["LLM"], "venue": "ACL 2026"},
        }
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "2604.06189" in content
        assert "- **Tags**: LLM" in content
        assert "- **Venue**: ACL 2026" in content
        assert "- **Summary**: 论文摘要" in content

    def test_accepted_papers(self, sample_data, tmp_path):
        results_by_id = {
            "2604.06189": {"summary": "x", "tags": ["T"], "venue": "ACL 2026"},
            "2604.07001": {"summary": "y", "tags": ["T"], "venue": None},
            "2604.08123": {"summary": "z", "tags": ["T"], "venue": None},
        }
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "**已录用论文**" in content
        assert "ACL 2026" in content

    def test_open_source_papers(self, sample_data, tmp_path):
        results_by_id = {}
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "**开源论文**" in content
        assert "github.com/example/atant" in content

    def test_cross_listed_annotation(self, sample_data, tmp_path):
        results_by_id = {}
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "(cross-listed)" in content

    def test_replaced_annotation(self, sample_data, tmp_path):
        results_by_id = {}
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "(replaced)" in content

    def test_replacement_section_header(self, sample_data, tmp_path):
        results_by_id = {}
        output = arxiv_daily.generate_markdown(sample_data, results_by_id, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "## 替换投稿 (1)" in content


# ===========================================================================
# TestPatchMarkdown
# ===========================================================================

class TestPatchMarkdown:
    def _make_md(self, tmp_path):
        """Create a minimal markdown file for patching tests."""
        content = """\
---
title: "arXiv cs.AI Daily Update"
date: 2025-04-11 12:00:00 +0800
categories: [cs.AI]
---

## 新投稿 (1)

<h3 id="1">[1] ATANT: A Test Framework</h3>
- **arXiv**: [2604.06189](https://arxiv.org/abs/2604.06189)
- **Authors**: Alice Smith, Bob Jones
- **Subjects**: cs.AI; cs.LG
- **Tags**: Old Tag
- **Summary**: Old summary text here.

"""
        md_path = tmp_path / "2025-04-11-cs.AI.md"
        md_path.write_text(content, encoding="utf-8")
        return md_path

    def test_basic_patch(self, tmp_path, logger):
        md_path = self._make_md(tmp_path)
        results = {
            "2604.06189": {
                "summary": "新的摘要内容",
                "tags": ["New Tag1", "New Tag2"],
                "venue": "NeurIPS 2025",
            },
        }
        patched = arxiv_daily.patch_markdown(md_path, results, logger)
        assert patched == 1
        content = md_path.read_text(encoding="utf-8")
        assert "- **Tags**: New Tag1, New Tag2" in content
        assert "- **Summary**: 新的摘要内容" in content
        assert "- **Venue**: NeurIPS 2025" in content

    def test_preserves_other_fields(self, tmp_path, logger):
        md_path = self._make_md(tmp_path)
        results = {
            "2604.06189": {"summary": "新摘要", "tags": ["T"], "venue": None},
        }
        arxiv_daily.patch_markdown(md_path, results, logger)
        content = md_path.read_text(encoding="utf-8")
        assert "- **Authors**: Alice Smith, Bob Jones" in content
        assert "- **Subjects**: cs.AI; cs.LG" in content

    def test_missing_paper(self, tmp_path, logger):
        md_path = self._make_md(tmp_path)
        results = {
            "9999.99999": {"summary": "x", "tags": ["T"], "venue": None},
        }
        patched = arxiv_daily.patch_markdown(md_path, results, logger)
        assert patched == 0


# ===========================================================================
# TestMainCli
# ===========================================================================

class TestMainCli:
    def test_missing_env_vars(self, monkeypatch):
        monkeypatch.delenv("ARXIV_UPDATE_KEY", raising=False)
        monkeypatch.delenv("ARXIV_UPDATE_API", raising=False)
        monkeypatch.delenv("ARXIV_UPDATE_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["arxiv_daily.py", "cs.AI", "--output", "/tmp/out", "--tags", "/tmp/tags.json"])

        with pytest.raises(SystemExit) as exc_info:
            arxiv_daily.main()
        assert exc_info.value.code == 2

    def test_normal_run(self, monkeypatch, fake_llm_server, tmp_path, sample_listing_html):
        api_url, handler_cls = fake_llm_server
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        tags_path = tmp_path / "tags.json"
        tags_path.write_text('{"tags": ["LLM Reasoning"]}', encoding="utf-8")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Set up saved HTML
        pages_dir = cache_dir / "pages" / "cs.AI"
        pages_dir.mkdir(parents=True)
        (pages_dir / "2025-04-11.html").write_text(sample_listing_html, encoding="utf-8")

        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", api_url)
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(output_dir),
            "--tags", str(tags_path),
            "--date", "2025-04-11",
            "--cache-dir", str(cache_dir),
        ])

        with pytest.raises(SystemExit) as exc_info:
            arxiv_daily.main()
        assert exc_info.value.code == 0

        # Verify markdown output
        md_files = list(output_dir.glob("*.md"))
        assert len(md_files) == 1
        assert "2025-04-11-cs.AI.md" in md_files[0].name
