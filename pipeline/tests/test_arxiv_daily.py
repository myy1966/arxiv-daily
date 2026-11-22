"""Tests for arxiv_daily.py."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import arxiv_daily


# ===========================================================================
# TestModuleMetadata
# ===========================================================================

class TestModuleMetadata:
    def test_python_version_requirement_is_declared(self):
        assert "Requires: Python >= 3.10" in arxiv_daily.__doc__


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

    def test_no_comments_field(self):
        """Paper without comments div should still parse successfully."""
        chunk = '''\
<dt><a>arXiv:2604.22222</a></dt>
<dd><div class="meta">
<div class="list-title mathjax"><span class="descriptor">Title:</span> No Comments Paper</div>
<div class="list-authors"><a href="/search/?query=Test">Test Author</a></div>
<div class="list-subjects"><span class="descriptor">Subjects:</span>
  <span class="primary-subject">Machine Learning (cs.LG)</span>
</div>
<p class="mathjax">
  Abstract text that is long enough to be useful for testing purposes.
</p>
</div></dd>'''
        paper = arxiv_daily.extract_paper(chunk)
        assert paper is not None
        assert paper["id"] == "2604.22222"
        assert paper["title"] == "No Comments Paper"
        assert "comments" not in paper
        assert "Test Author" in paper["authors"]

    def test_multiple_subject_codes(self):
        """Paper with many subject codes should extract them all."""
        chunk = '''\
<dt><a>arXiv:2604.33333</a></dt>
<dd><div class="meta">
<div class="list-title mathjax"><span class="descriptor">Title:</span> Multi Subject</div>
<div class="list-subjects"><span class="descriptor">Subjects:</span>
  <span class="primary-subject">Artificial Intelligence (cs.AI)</span>; Machine Learning (cs.LG); Computation and Language (cs.CL)
</div>
<p class="mathjax">Abstract here.</p>
</div></dd>'''
        paper = arxiv_daily.extract_paper(chunk)
        assert paper is not None
        assert "cs.AI" in paper["subjects"]
        assert "cs.LG" in paper["subjects"]
        assert "cs.CL" in paper["subjects"]

    def test_full_structure_assertion(self, sample_paper_chunk):
        """Verify complete structure of parsed paper dict."""
        paper = arxiv_daily.extract_paper(sample_paper_chunk)
        assert paper is not None
        assert paper["id"] == "2604.06189"
        assert paper["title"] == "ATANT: A Test Framework for Narrative Truth"
        assert paper["authors"] == "Alice Smith, Bob Jones"
        assert paper["subjects"] == "cs.AI; cs.LG"
        assert paper["comments"] == "Accepted at ACL 2026. Code at this https URL"
        assert paper["code_urls"] == ["https://github.com/example/atant"]
        assert "novel framework" in paper["abstract"]

    @pytest.mark.xfail(reason="extract_paper regex is sensitive to class attribute order", strict=True)
    def test_class_order_variation(self):
        """Title with reversed class order 'mathjax list-title' should still parse."""
        chunk = '''\
<dt><a>arXiv:2604.44444</a></dt>
<dd><div class="meta">
<div class="mathjax list-title"><span class="descriptor">Title:</span> Reversed Class Order</div>
<div class="list-authors"><a href="/search/?query=Test">Test Author</a></div>
<div class="mathjax list-comments"><span class="descriptor">Comments:</span> Some comment</div>
<div class="list-subjects"><span class="descriptor">Subjects:</span>
  <span class="primary-subject">Artificial Intelligence (cs.AI)</span>
</div>
<p class="mathjax">Abstract with reversed class order in title and comments divs.</p>
</div></dd>'''
        paper = arxiv_daily.extract_paper(chunk)
        assert paper is not None
        assert paper["title"] == "Reversed Class Order"
        assert paper["comments"] == "Some comment"


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

    def test_diagnostics_track_missing_title_and_abstract(self):
        html = """\
<p>Showing new listings for Friday, 11 April 2025</p>
<h3>New submissions (showing 2 of 2 entries)</h3>
<dt><a name="item1">[1]</a> <a>arXiv:2604.00001</a></dt>
<dd><div class="meta">
<div class="list-title mathjax"><span class="descriptor">Title:</span> Complete Paper</div>
<p class="mathjax">A complete abstract.</p>
</div></dd>
<dt><a name="item2">[2]</a> <a>arXiv:2604.00002</a></dt>
<dd><div class="meta">
<div class="list-authors"><a href="/search/?query=Test">Test Author</a></div>
</div></dd>
"""
        data = arxiv_daily.parse_html(html, "cs.AI")

        assert data["diagnostics"]["missing_title_ids"] == ["2604.00002"]
        assert data["diagnostics"]["missing_abstract_ids"] == ["2604.00002"]

    def test_log_parse_diagnostics_warns_for_missing_fields(self, caplog):
        data = {
            "diagnostics": {
                "missing_title_ids": ["2604.00002"],
                "missing_abstract_ids": ["2604.00002", "2604.00003"],
            }
        }

        arxiv_daily.log_parse_diagnostics(data, arxiv_daily.logging.getLogger("arxiv_daily.test"))

        assert "[Phase1] 1 papers missing title: ['2604.00002']" in caplog.text
        assert "[Phase1] 2 papers missing abstract: ['2604.00002', '2604.00003']" in caplog.text


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
        assert "[Paper 1/2]" in content
        assert "[Paper 2/2]" in content

    def test_uses_explicit_separator_not_markdown_rule(self):
        papers = [
            {"id": "2604.00001", "title": "T1", "abstract": "A1 --- includes markdown rule"},
            {"id": "2604.00002", "title": "T2", "abstract": "A2"},
        ]
        messages = arxiv_daily.build_prompt(papers, [])
        content = messages[1]["content"]
        assert arxiv_daily.PAPER_SEPARATOR.strip() in content
        assert "\n---\n" not in content
        assert "A1 --- includes markdown rule" in content

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

    def test_allows_new_tag_when_existing_list_does_not_fit(self):
        papers = [
            {"id": "2604.00001", "title": "T", "abstract": "A"},
            {"id": "2604.00002", "title": "T2", "abstract": "A2"},
        ]
        content = arxiv_daily.build_prompt(papers, ["LLM Agent"])[1]["content"]
        assert "from the existing tag list" not in content
        assert "Do not stretch a weakly related existing tag" in content
        assert '"new_tags": ["Neuromorphic Photonics"]' in content
        assert "Prefer an existing tag" in content
        assert "exactly 2 paper objects" in content
        assert '"id": "2604.00001"' in content
        assert "Copy these IDs verbatim" in content

    def test_batches_share_prefix_through_tag_list(self):
        # The API's prefix cache only hits when batches start with identical text.
        tags = ["LLM Agent", "AI Safety"]
        one = arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "T1", "abstract": "A1"}], tags,
        )
        two = arxiv_daily.build_prompt(
            [
                {"id": "2604.00002", "title": "T2", "abstract": "A2"},
                {"id": "2604.00003", "title": "T3", "abstract": "A3"},
            ],
            tags + ["Neuromorphic Photonics"],
        )
        assert one[0] == two[0]
        prefix = os.path.commonprefix([one[1]["content"], two[1]["content"]])
        assert prefix.endswith("Current tag list: LLM Agent, AI Safety")
        assert "Do not stretch a weakly related existing tag" in prefix
        assert "2604.0000" not in prefix


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
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 2
        assert diagnostics.total_failed == 0
        assert diagnostics.all_failed_ids == []
        assert results["2604.00001"]["tags"] == ["T1"]
        assert results["2604.00002"]["venue"] == "ACL 2026"

    def test_versioned_id_match(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001v2", "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert "2604.00001" in results
        assert diagnostics.total_failed == 0

    def test_wrong_ids(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "9999.99999", "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 0
        assert diagnostics.missing_ids == ["2604.00001"]
        assert diagnostics.total_failed == 1

    def test_quality_check_short_summary(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "short", "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 0
        assert [failure["id"] for failure in diagnostics.quality_failed] == ["2604.00001"]
        assert diagnostics.total_failed == 1

    def test_summary_threshold_allows_concise_summaries(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * arxiv_daily.MIN_SUMMARY_LEN, "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 1
        assert diagnostics.total_failed == 0

    def test_quality_check_missing_tags(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * 60, "tags": [], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 0
        assert [failure["id"] for failure in diagnostics.quality_failed] == ["2604.00001"]

    def test_empty_llm_papers(self, logger):
        batch = [{"id": "2604.00001"}]
        results, diagnostics = arxiv_daily.match_llm_results(batch, [], logger, 1)
        assert len(results) == 0
        assert diagnostics.missing_ids == ["2604.00001"]
        assert diagnostics.total_failed == 1

    def test_empty_batch(self, logger):
        results, diagnostics = arxiv_daily.match_llm_results([], [], logger, 1)
        assert results == {}
        assert diagnostics.total_failed == 0

    def test_partial_match(self, logger):
        batch = [{"id": "2604.00001"}, {"id": "2604.00002"}, {"id": "2604.00003"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * 60, "tags": ["T1"], "venue": None},
            {"id": "9999.99999", "summary": "y" * 60, "tags": ["T2"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert len(results) == 1
        assert diagnostics.missing_ids == ["2604.00002", "2604.00003"]
        assert diagnostics.total_failed == 2

    def test_match_duplicate_id(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * 60, "tags": ["T1"], "venue": None},
            {"id": "2604.00001", "summary": "y" * 60, "tags": ["T2"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)

        assert results["2604.00001"]["tags"] == ["T1"]
        assert diagnostics.duplicate_ids == []
        assert diagnostics.missing_ids == []
        assert diagnostics.quality_failed == []
        assert diagnostics.total_failed == 0

    def test_match_numeric_json_id(self, logger):
        batch = [{"id": "2609.12398"}]
        llm_papers = [
            {"id": 2609.12398, "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert "2609.12398" in results
        assert diagnostics.total_failed == 0

    def test_match_ignores_dup_suffix_ids(self, logger):
        batch = [{"id": "2604.00001"}, {"id": "2604.00002"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "x" * 60, "tags": ["T1"], "venue": None},
            {"id": "2604.00001_dup", "summary": "y" * 60, "tags": ["T2"], "venue": None},
            {"id": "2608.00002_placeholder", "summary": "z" * 60, "tags": ["T3"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert list(results) == ["2604.00001"]
        assert diagnostics.missing_ids == ["2604.00002"]

    def test_match_yymm_one_digit_typo(self, logger):
        batch = [{"id": "2609.27875"}]
        llm_papers = [
            {"id": "2608.27875", "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert "2609.27875" in results
        assert diagnostics.total_failed == 0

    def test_match_does_not_typo_neighbor_sequence_ids(self, logger):
        batch = [{"id": "2604.00001"}, {"id": "2604.00002"}]
        llm_papers = [
            {"id": "2604.00002", "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert list(results) == ["2604.00002"]
        assert diagnostics.missing_ids == ["2604.00001"]

    def test_match_missing_id(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [
            {"id": "9999.99999", "summary": "x" * 60, "tags": ["T1"], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)

        assert results == {}
        assert diagnostics.missing_ids == ["2604.00001"]
        assert diagnostics.duplicate_ids == []
        assert diagnostics.quality_failed == []
        assert diagnostics.total_failed == 1
        assert diagnostics.all_failed_ids == ["2604.00001"]

    def test_match_quality_failed(self, logger):
        batch = [{"id": "2604.00001"}, {"id": "2604.00002"}]
        llm_papers = [
            {"id": "2604.00001", "summary": "short", "tags": ["T1"], "venue": None},
            {"id": "2604.00002", "summary": "x" * 60, "tags": [], "venue": None},
        ]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)

        assert results == {}
        assert diagnostics.missing_ids == []
        assert diagnostics.duplicate_ids == []
        assert [failure["id"] for failure in diagnostics.quality_failed] == [
            "2604.00001",
            "2604.00002",
        ]
        assert diagnostics.total_failed == 2
        assert diagnostics.all_failed_ids == ["2604.00001", "2604.00002"]


# ===========================================================================
# TestSummaryEnding
# ===========================================================================

class TestSummaryEnding:
    @pytest.mark.parametrize("summary,expected", [
        ("识别出12项技术挑战，包括规模化、边缘云协同、标准认证等", "识别出12项技术挑战，包括规模化、边缘云协同、标准认证等。"),
        ("方法优于纯RL或纯LLM基线。（注：该论文已撤回）", "方法优于纯RL或纯LLM基线。该论文已撤回。"),
        ("误差增长符合预测。（注：该版本未经全部共同作者同意，已被撤回，不应引用。）",
         "误差增长符合预测。该版本未经全部共同作者同意，已被撤回，不应引用。"),
        ("指出累积进展的机会。(作者后期撤回该手稿)", "指出累积进展的机会。作者后期撤回该手稿。"),
        ("在三个基准上取得最优结果.", "在三个基准上取得最优结果。"),
        ("在 GLUE 上提升 3.2%（详见附录）", "在 GLUE 上提升 3.2%（详见附录）。"),
        ("已经以句号结尾。", "已经以句号结尾。"),
        ("结尾有多余空格。  ", "结尾有多余空格。"),
        ("实验表明该方法有效，并且", "实验表明该方法有效，并且。"),
        ("实验表明该方法有效，", None),
        ("主要贡献包括：", None),
        ("评估能力是否优于生成能力？", None),
        ("", None),
    ])
    def test_normalize_summary_ending(self, summary, expected):
        assert arxiv_daily.normalize_summary_ending(summary) == expected

    def test_match_fixes_missing_full_stop(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [{"id": "2604.00001", "summary": "这是一条内容完整但缺少句号的测试摘要，长度足够通过检查，结尾是汉字",
                       "tags": ["T"], "venue": None}]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert diagnostics.total_failed == 0
        assert results["2604.00001"]["summary"].endswith("结尾是汉字。")

    def test_match_rejects_cut_off_summary(self, logger):
        batch = [{"id": "2604.00001"}]
        llm_papers = [{"id": "2604.00001", "summary": "这是一条在逗号处被截断的足够长的测试摘要，后面本应还有内容，",
                       "tags": ["T"], "venue": None}]
        results, diagnostics = arxiv_daily.match_llm_results(batch, llm_papers, logger, 1)
        assert results == {}
        assert diagnostics.quality_failed[0]["issues"] == ["summary ends with '，' instead of 。"]


# ===========================================================================
# TestContentCheck
# ===========================================================================

CONTENT_PAPERS = [
    {"id": "2604.00001", "title": "FRACTAL: Fractional Recurrent Architecture",
     "abstract": "We propose FRACTAL, a fractional recurrent architecture for temporal modeling with LLM features."},
    {"id": "2604.00002", "title": "SynerDiff: Synergetic Continuous Batching",
     "abstract": "SynerDiff introduces synergetic continuous batching for diffusion serving with LLM schedulers."},
    {"id": "2604.00003", "title": "TriageBench",
     "abstract": "TriageBench evaluates metacognitive control of LLM agents under resource constraints."},
    {"id": "2604.00004", "title": "RS-Claw",
     "abstract": "RS-Claw explores remote sensing tools through hierarchical skill trees for LLM agents."},
]
CONTENT_SUMMARIES = {
    "2604.00001": "本文提出FRACTAL分数递归架构，用于时间序列建模，在长序列任务上取得良好效果。",
    "2604.00002": "本文提出SynerDiff协同连续批处理方法，用于扩散模型服务，显著降低了推理延迟。",
    "2604.00003": "本文提出TriageBench基准，评估资源约束下智能体的前瞻性元认知控制能力。",
    "2604.00004": "本文提出RS-Claw遥感智能体，通过分层技能树渐进式探索工具并压缩输入。",
}


def _content_data(papers=CONTENT_PAPERS):
    return {"sections": [{"name": "New submissions", "count": len(papers), "papers": [dict(p) for p in papers]}]}


def _content_llm_papers(summaries):
    return [{"id": pid, "summary": s, "tags": ["T"], "venue": None} for pid, s in summaries.items()]


def _swapped_summaries():
    s = dict(CONTENT_SUMMARIES)
    s["2604.00001"], s["2604.00002"] = CONTENT_SUMMARIES["2604.00002"], CONTENT_SUMMARIES["2604.00001"]
    return s


class TestContentCheck:
    def test_content_terms_normalizes(self):
        terms = arxiv_daily.content_terms("LLMs use GNN-based Retrieval-Augmented methods, the SOTA class")
        assert {"llm", "gnn", "gnn-based", "retrieval", "augmented", "method", "class"} <= terms
        assert not {"llms", "methods", "the", "based", "sota"} & terms

    def test_detects_swapped_summaries(self):
        index = arxiv_daily.build_content_index(_content_data())
        results = {pid: {"summary": s} for pid, s in _swapped_summaries().items()}
        mismatches = arxiv_daily.find_content_mismatches(CONTENT_PAPERS, results, index)
        assert {(m["id"], m["looks_like"]) for m in mismatches} == {
            ("2604.00001", "2604.00002"), ("2604.00002", "2604.00001"),
        }
        first = next(m for m in mismatches if m["id"] == "2604.00001")
        assert first["terms"] == ["synerdiff"]
        assert first["own_score"] == 0.0 and first["other_score"] == 1.0

    def test_correct_summaries_pass(self):
        index = arxiv_daily.build_content_index(_content_data())
        results = {pid: {"summary": s} for pid, s in CONTENT_SUMMARIES.items()}
        assert arxiv_daily.find_content_mismatches(CONTENT_PAPERS, results, index) == []

    def test_chinese_only_and_common_terms_are_not_evidence(self):
        index = arxiv_daily.build_content_index(_content_data())
        results = {
            "2604.00001": {"summary": "本文提出一种分数递归架构，用于时间序列建模并取得良好效果。"},
            # LLM appears in every abstract, so it is not rare and proves nothing
            "2604.00002": {"summary": "本文研究LLM在扩散模型服务中的调度问题，降低了推理延迟。"},
        }
        assert arxiv_daily.find_content_mismatches(CONTENT_PAPERS, results, index) == []

    def test_plural_and_hyphen_variants_match_own_abstract(self):
        papers = [
            {"id": "2604.00011", "title": "Neurons", "abstract": "We study GNNs for an LFSR-based neuron."},
            {"id": "2604.00012", "title": "Accelerator", "abstract": "A GNN accelerator with an LFSR generator."},
        ]
        index = arxiv_daily.build_content_index(_content_data(papers))
        results = {"2604.00011": {"summary": "本文用GNN研究基于LFSR的随机神经元设计与实现。"}}
        assert arxiv_daily.find_content_mismatches(papers, results, index) == []

    def test_match_strict_drops_swaps_as_failures(self, logger, tmp_path):
        index = arxiv_daily.build_content_index(_content_data())
        raw = {"papers": _content_llm_papers(_swapped_summaries()), "new_tags": []}
        results, diagnostics = arxiv_daily.match_llm_results(
            CONTENT_PAPERS, raw["papers"], logger, 1, raw, tmp_path, content_index=index,
        )
        assert set(results) == {"2604.00003", "2604.00004"}
        assert {m["id"] for m in diagnostics.content_mismatch} == {"2604.00001", "2604.00002"}
        assert diagnostics.missing_ids == []
        assert diagnostics.total_failed == 2
        assert set(diagnostics.all_failed_ids) == {"2604.00001", "2604.00002"}
        debug = json.loads((tmp_path / "batch_001_content_mismatch.json").read_text(encoding="utf-8"))
        assert debug["strict"] is True
        assert {m["id"] for m in debug["mismatches"]} == {"2604.00001", "2604.00002"}
        assert debug["mismatches"][0]["summary"]

    def test_match_non_strict_keeps_swaps(self, logger):
        index = arxiv_daily.build_content_index(_content_data())
        results, diagnostics = arxiv_daily.match_llm_results(
            CONTENT_PAPERS, _content_llm_papers(_swapped_summaries()), logger, 1,
            content_index=index, content_strict=False,
        )
        assert set(results) == set(CONTENT_SUMMARIES)
        assert diagnostics.content_mismatch == []
        assert diagnostics.total_failed == 0

    def test_match_without_index_skips_check(self, logger):
        results, diagnostics = arxiv_daily.match_llm_results(
            CONTENT_PAPERS, _content_llm_papers(_swapped_summaries()), logger, 1,
        )
        assert set(results) == set(CONTENT_SUMMARIES)
        assert diagnostics.total_failed == 0

    @staticmethod
    def _run(monkeypatch, logger, tags_file, tmp_path, sample_data, responses):
        prompt_batches: list[list[str]] = []

        def fake_build_prompt(batch, current_tags):
            prompt_batches.append([paper["id"] for paper in batch])
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0,
                          debug_dir=None, batch_label=None, expected_ids=None):
            summaries = responses[min(len(prompt_batches), len(responses)) - 1]
            ids = prompt_batches[-1]
            return {"papers": _content_llm_papers({pid: summaries[pid] for pid in ids}), "new_tags": []}, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)
        data = {**_content_data(), "field": sample_data["field"], "listing_date": sample_data["listing_date"],
                "page_type": "new"}
        cache_dir = tmp_path / "cache"
        results, failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )
        debug_dir = cache_dir / "debug" / data["field"] / data["listing_date"]
        return results, failed, prompt_batches, debug_dir

    def test_run_phase2_retries_swapped_batch(self, monkeypatch, logger, tags_file, tmp_path, sample_data):
        results, failed, prompt_batches, debug_dir = self._run(
            monkeypatch, logger, tags_file, tmp_path, sample_data,
            [_swapped_summaries(), CONTENT_SUMMARIES],
        )
        assert failed == 0
        assert {pid: r["summary"] for pid, r in results.items()} == CONTENT_SUMMARIES
        ids = [p["id"] for p in CONTENT_PAPERS]
        assert prompt_batches == [ids, ids], "whole batch is rejected and retried"
        assert (debug_dir / "batch_001_content_mismatch.json").exists()

    def test_run_phase2_keeps_retry_result_instead_of_placeholder(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        results, failed, prompt_batches, debug_dir = self._run(
            monkeypatch, logger, tags_file, tmp_path, sample_data,
            [_swapped_summaries(), _swapped_summaries()],
        )
        assert failed == 0
        assert len(prompt_batches) == 2
        assert all(r["summary"] != arxiv_daily.SUMMARY_FAILURE_PLACEHOLDER for r in results.values())
        retry_debug = json.loads((debug_dir / "retry_001_content_mismatch.json").read_text(encoding="utf-8"))
        assert retry_debug["strict"] is False

    def test_patch_mode_uses_full_listing_for_rarity(self, monkeypatch, tmp_path, logger):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        cache_base = tmp_path / "cache"
        (cache_base / "pages" / "cs.AI").mkdir(parents=True)
        (cache_base / "pages" / "cs.AI" / "2025-04-11.html").write_text("<html>saved</html>", encoding="utf-8")
        (output_dir / "2025-04-11-cs.AI.md").write_text(
            "- **arXiv**: [2604.00001](https://arxiv.org/abs/2604.00001)\n- **Summary**: Old.\n", encoding="utf-8"
        )
        tags_path = tmp_path / "tags.json"
        tags_path.write_text('{"tags": []}', encoding="utf-8")
        args = type("Args", (), {})()
        args.date, args.field, args.patch = "2025-04-11", "cs.AI", ["2604.00001"]
        args.tags, args.output = str(tags_path), str(output_dir)
        full = _content_data()
        monkeypatch.setattr(arxiv_daily, "parse_html", lambda html, field: full)
        monkeypatch.setattr(arxiv_daily, "extract_venue_info_batch", lambda *a, **k: {})
        captured = {}

        def fake_run_phase2(data, *args, **kwargs):
            captured["data_ids"] = [p["id"] for s in data["sections"] for p in s["papers"]]
            captured["content_source"] = kwargs.get("content_source")
            return {"2604.00001": {"summary": CONTENT_SUMMARIES["2604.00001"], "tags": ["T"], "venue": None}}, 0

        monkeypatch.setattr(arxiv_daily, "run_phase2", fake_run_phase2)
        arxiv_daily.run_patch_mode(
            args, logger, cache_base,
            arxiv_daily.LlmConfig(api_key="k", api_endpoint="http://unused", model="m"),
        )
        assert captured["data_ids"] == ["2604.00001"]
        assert captured["content_source"] is full


# ===========================================================================
# TestValidateLlmSchema
# ===========================================================================

class TestValidateLlmSchema:
    def test_valid_schema(self):
        result = {"papers": [], "new_tags": []}
        assert arxiv_daily._validate_llm_schema(result) is result

    def test_missing_papers_raises(self):
        with pytest.raises(ValueError, match="missing 'papers' list"):
            arxiv_daily._validate_llm_schema({"foo": "bar"})

    def test_response_must_be_object(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            arxiv_daily._validate_llm_schema([])

    def test_papers_must_be_list(self):
        with pytest.raises(ValueError, match="missing 'papers' list"):
            arxiv_daily._validate_llm_schema({"papers": "not a list"})

    def test_new_tags_must_be_list(self):
        with pytest.raises(ValueError, match="'new_tags' must be list"):
            arxiv_daily._validate_llm_schema({"papers": [], "new_tags": "not a list"})

    def test_expected_ids_accepts_permutation(self):
        long_summary = "x" * 40
        result = {
            "papers": [
                {"id": "2604.00002", "summary": long_summary, "tags": ["T2"]},
                {"id": "2604.00001v2", "summary": long_summary, "tags": ["T1"]},
            ],
            "new_tags": [],
        }
        assert arxiv_daily._validate_llm_schema(
            result, expected_ids=["2604.00001", "2604.00002"]
        ) is result

    def test_expected_ids_rejects_wrong_count(self):
        with pytest.raises(ValueError, match="expected 2 papers"):
            arxiv_daily._validate_llm_schema(
                {"papers": [{"id": "2604.00001", "summary": "x" * 40, "tags": ["T"]}]},
                expected_ids=["2604.00001", "2604.00002"],
            )

    def test_expected_ids_rejects_missing_tags(self):
        with pytest.raises(ValueError, match="missing tags"):
            arxiv_daily._validate_llm_schema(
                {
                    "papers": [
                        {"id": "2604.00001", "summary": "x" * 40, "tags": []},
                    ]
                },
                expected_ids=["2604.00001"],
            )

    def test_expected_ids_rejects_id_mismatch(self):
        with pytest.raises(ValueError, match="!= expected"):
            arxiv_daily._validate_llm_schema(
                {
                    "papers": [
                        {"id": "2604.00099", "summary": "x" * 40, "tags": ["T"]},
                    ]
                },
                expected_ids=["2604.00001"],
            )


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

    def test_prompt_metadata_changes_hash(self, sample_data):
        import copy
        for field_name in ("title", "abstract", "comments"):
            data2 = copy.deepcopy(sample_data)
            data2["sections"][0]["papers"][0][field_name] = f"updated {field_name}"
            assert arxiv_daily.compute_metadata_hash(sample_data) != arxiv_daily.compute_metadata_hash(data2)

    def test_order_independent(self):
        data1 = {
            "sections": [{
                "papers": [
                    {"id": "a", "title": "A", "abstract": "Abstract A", "comments": "Comment A"},
                    {"id": "b", "title": "B", "abstract": "Abstract B", "comments": "Comment B"},
                ],
            }],
        }
        data2 = {
            "sections": [{
                "papers": [
                    {"id": "b", "title": "B", "abstract": "Abstract B", "comments": "Comment B"},
                    {"id": "a", "title": "A", "abstract": "Abstract A", "comments": "Comment A"},
                ],
            }],
        }
        assert arxiv_daily.compute_metadata_hash(data1) == arxiv_daily.compute_metadata_hash(data2)


# ===========================================================================
# TestPhase2Helpers
# ===========================================================================

class TestPhase2Helpers:
    def test_load_tags_normalizes_legacy_objects(self, tmp_path):
        tags_path = tmp_path / "tags.json"
        tags_path.write_text(
            json.dumps({"tags": [{"name": "LLM Reasoning"}, {"name": "Diffusion"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        assert arxiv_daily.load_tags(tags_path) == ["LLM Reasoning", "Diffusion"]

    def test_load_tags_creates_parent_for_missing_file(self, tmp_path):
        tags_path = tmp_path / "nested" / "tags.json"
        assert arxiv_daily.load_tags(tags_path) == []
        assert tags_path.parent.exists()

    def test_apply_tag_mapping_rewrites_paper_and_new_tags(self):
        matched = {
            "1": {"summary": "s", "tags": ["LLM-as-a-Judge", "LLM Evaluation"]},
        }
        mapping = {"LLM-as-a-Judge": "LLM Evaluation", "LLM Evaluation": "LLM Evaluation"}
        out, new_tags = arxiv_daily._apply_tag_mapping(
            matched, ["LLM-as-a-Judge", "Astrolinguistics"], mapping
        )
        assert out["1"]["tags"] == ["LLM Evaluation"]
        assert new_tags == ["LLM Evaluation", "Astrolinguistics"]

    def test_collect_batches_filters_missing_abstracts(self):
        data = {
            "sections": [{
                "papers": [
                    {"id": "a", "abstract": "A"},
                    {"id": "b", "abstract": ""},
                    {"id": "c", "abstract": "C"},
                ],
            }],
        }
        assert arxiv_daily.collect_batches(data, 1) == [[{"id": "a", "abstract": "A"}], [{"id": "c", "abstract": "C"}]]

    def test_load_cached_batches_deletes_stale_cache(self, tmp_path):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        hash_file = field_cache / "metadata_hash"
        hash_file.write_text("old", encoding="utf-8")
        stale_cache = field_cache / "batch_001.json"
        stale_cache.write_text(json.dumps({"papers": []}), encoding="utf-8")

        cached = arxiv_daily.load_cached_batches(field_cache, hash_file, "new", no_cache=False)

        assert cached == {}
        assert hash_file.read_text(encoding="utf-8") == "new"
        assert not stale_cache.exists()

    def test_load_cached_batches_writes_hash_atomically(self, monkeypatch, tmp_path):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        hash_file = field_cache / "metadata_hash"
        hash_file.write_text("good", encoding="utf-8")

        def boom(src, dst):
            raise RuntimeError("simulated crash mid-write")

        monkeypatch.setattr(arxiv_daily.os, "replace", boom)

        with pytest.raises(RuntimeError, match="simulated crash mid-write"):
            arxiv_daily.load_cached_batches(field_cache, hash_file, "good", no_cache=False)

        assert hash_file.read_text(encoding="utf-8") == "good"
        assert list(field_cache.glob("*.tmp")) == []

    def test_write_batch_cache_replaces_existing_file(self, tmp_path):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        cache_path = field_cache / "batch_001.json"
        cache_path.write_text("stale", encoding="utf-8")

        arxiv_daily._write_batch_cache(
            field_cache,
            1,
            {"2604.00001": {"summary": "ok", "tags": ["T1"], "venue": None}},
            ["新标签"],
            ["旧标签", "新标签"],
        )

        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["papers"] == [
            {"id": "2604.00001", "summary": "ok", "tags": ["T1"], "venue": None}
        ]
        assert cached["new_tags"] == ["新标签"]
        assert cached["current_tags"] == ["旧标签", "新标签"]
        assert list(field_cache.glob("*.json.tmp")) == []

    def test_patch_batch_cache_creates_file(self, tmp_path, logger):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()

        arxiv_daily._patch_batch_cache(
            field_cache,
            1,
            [{"id": "2604.00001", "summary": "summary", "tags": ["T1"], "venue": None}],
            logger,
        )

        cached = json.loads((field_cache / "batch_001.json").read_text(encoding="utf-8"))
        assert cached == {
            "papers": [{"id": "2604.00001", "summary": "summary", "tags": ["T1"], "venue": None}],
            "new_tags": [],
            "current_tags": [],
        }

    def test_patch_batch_cache_appends(self, tmp_path, logger):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        cache_path = field_cache / "batch_001.json"
        cache_path.write_text(
            json.dumps(
                {
                    "papers": [{"id": "2604.00001", "summary": "old", "tags": ["T1"], "venue": None}],
                    "new_tags": ["T1"],
                    "current_tags": ["Existing", "T1"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        arxiv_daily._patch_batch_cache(
            field_cache,
            1,
            [{"id": "2604.00002", "summary": "new", "tags": ["T2"], "venue": None}],
            logger,
        )

        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["papers"] == [
            {"id": "2604.00001", "summary": "old", "tags": ["T1"], "venue": None},
            {"id": "2604.00002", "summary": "new", "tags": ["T2"], "venue": None},
        ]
        assert cached["new_tags"] == ["T1"]
        assert cached["current_tags"] == ["Existing", "T1"]

    def test_patch_batch_cache_upserts_by_id(self, tmp_path, logger):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        cache_path = field_cache / "batch_001.json"
        cache_path.write_text(
            json.dumps(
                {
                    "papers": [
                        {"id": "2604.00001", "summary": "old", "tags": ["T1"], "venue": None},
                        {"id": "2604.00002", "summary": "stale", "tags": ["T2"], "venue": None},
                    ],
                    "new_tags": ["T1", "T2"],
                    "current_tags": ["Existing", "T1", "T2"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        arxiv_daily._patch_batch_cache(
            field_cache,
            1,
            [
                {"id": "2604.00002", "summary": "refreshed", "tags": ["T2b"], "venue": None},
                {"id": "2604.00003", "summary": "fresh", "tags": ["T3"], "venue": None},
            ],
            logger,
        )

        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["papers"] == [
            {"id": "2604.00001", "summary": "old", "tags": ["T1"], "venue": None},
            {"id": "2604.00002", "summary": "refreshed", "tags": ["T2b"], "venue": None},
            {"id": "2604.00003", "summary": "fresh", "tags": ["T3"], "venue": None},
        ]
        ids = [p["id"] for p in cached["papers"]]
        assert ids == list(dict.fromkeys(ids)), "patched cache must not contain duplicate ids"

    def test_patch_batch_cache_dedupes_existing_duplicates(self, tmp_path, logger):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        cache_path = field_cache / "batch_001.json"
        long_summary = "这是一篇用于测试的论文摘要，内容足够长，可以通过质量检查并验证缓存自愈。"
        seed_summary = "这是一段长度足以通过质量阈值的论文摘要，用于初始化缓存条目。"
        cache_path.write_text(
            json.dumps(
                {
                    "papers": [
                        {"id": "2604.00001", "summary": seed_summary, "tags": ["T1"], "venue": None},
                        {"id": "2604.00002", "summary": seed_summary, "tags": ["T2"], "venue": None},
                        {"id": "2604.00002", "summary": seed_summary + "重复", "tags": ["T2"], "venue": None},
                        {"id": "2604.00003", "summary": seed_summary, "tags": ["T3"], "venue": None},
                    ],
                    "new_tags": [],
                    "current_tags": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        arxiv_daily._patch_batch_cache(
            field_cache,
            1,
            [
                {"id": "2604.00002", "summary": long_summary, "tags": ["T2b"], "venue": None},
            ],
            logger,
        )

        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        ids = [p["id"] for p in cached["papers"]]
        assert ids == list(dict.fromkeys(ids)), "patched cache must dedupe pre-existing duplicates"
        assert ids == ["2604.00001", "2604.00002", "2604.00003"]
        recovered = next(p for p in cached["papers"] if p["id"] == "2604.00002")
        assert recovered["summary"] == long_summary
        assert recovered["tags"] == ["T2b"]

        batch = [
            {"id": pid, "title": f"T-{pid}", "abstract": "A"}
            for pid in ["2604.00001", "2604.00002", "2604.00003"]
        ]
        _, diagnostics = arxiv_daily.match_llm_results(
            batch, cached["papers"], logger, batch_num=1
        )
        assert diagnostics.duplicate_ids == []
        assert diagnostics.missing_ids == []
        assert diagnostics.quality_failed == []

    def test_patch_batch_cache_corrupt_file(self, tmp_path, logger):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        cache_path = field_cache / "batch_001.json"
        cache_path.write_text("{bad json", encoding="utf-8")

        arxiv_daily._patch_batch_cache(
            field_cache,
            1,
            [{"id": "2604.00001", "summary": "summary", "tags": ["T1"], "venue": None}],
            logger,
        )

        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached == {
            "papers": [{"id": "2604.00001", "summary": "summary", "tags": ["T1"], "venue": None}],
            "new_tags": [],
            "current_tags": [],
        }

    def test_atomic_json_write_cleans_temp_on_failure(self, monkeypatch, tmp_path):
        cache_path = tmp_path / "batch_001.json"
        cache_path.write_text('{"old": true}', encoding="utf-8")

        def raise_dump(*args, **kwargs):
            raise RuntimeError("dump failed")

        monkeypatch.setattr(arxiv_daily.json, "dump", raise_dump)
        with pytest.raises(RuntimeError, match="dump failed"):
            arxiv_daily._atomic_write_json(cache_path, {"new": True})

        assert cache_path.read_text(encoding="utf-8") == '{"old": true}'
        assert list(tmp_path.glob("*.json.tmp")) == []

    def test_atomic_text_write_replaces_existing_file(self, tmp_path):
        text_path = tmp_path / "page.html"
        text_path.write_text("old", encoding="utf-8")

        arxiv_daily._atomic_write_text(text_path, "新 HTML")

        assert text_path.read_text(encoding="utf-8") == "新 HTML"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_save_tags_writes_utf8_tags(self, tmp_path):
        tags_path = tmp_path / "tags.json"
        arxiv_daily.save_tags(tags_path, ["大模型"])
        tags_data = json.loads(tags_path.read_text(encoding="utf-8"))
        assert tags_data["tags"] == ["大模型"]
        assert "updated_at" in tags_data
        assert tags_data["updated_at"].endswith("+08:00")

    def test_save_tags_preserves_consolidation_metadata(self, tmp_path):
        tags_path = tmp_path / "tags.json"
        tags_path.write_text(
            json.dumps({
                "updated_at": "old",
                "consolidated_from": "tags_full.json",
                "min_frequency": 20,
                "tags": ["old"],
            }),
            encoding="utf-8",
        )
        arxiv_daily.save_tags(tags_path, ["new"])
        tags_data = json.loads(tags_path.read_text(encoding="utf-8"))
        assert tags_data["tags"] == ["new"]
        assert tags_data["consolidated_from"] == "tags_full.json"
        assert tags_data["min_frequency"] == 20
        assert tags_data["updated_at"] != "old"

    def test_save_tags_preserves_existing_file_on_write_failure(self, monkeypatch, tmp_path):
        tags_path = tmp_path / "tags.json"
        tags_path.write_text('{"tags": ["old"]}', encoding="utf-8")

        def raise_dump(*args, **kwargs):
            raise RuntimeError("dump failed")

        monkeypatch.setattr(arxiv_daily.json, "dump", raise_dump)
        with pytest.raises(RuntimeError, match="dump failed"):
            arxiv_daily.save_tags(tags_path, ["new"])

        assert tags_path.read_text(encoding="utf-8") == '{"tags": ["old"]}'
        assert list(tmp_path.glob("*.json.tmp")) == []


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
    def test_url_construction_and_arxiv_user_agent(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(arxiv_daily, "_last_fetch_time", None)
        monkeypatch.setattr(arxiv_daily.time, "sleep", sleeps.append)

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
            assert req.get_header("User-agent").startswith("arxiv-daily-bot/1.0 (contact:")
            assert sleeps == [arxiv_daily.ARXIV_FETCH_INTERVAL_SECONDS]

    def test_rate_limit_clamps_elapsed_time(self, monkeypatch):
        sleeps = []
        monotonic_values = iter([101.0, 103.0])
        monkeypatch.setattr(arxiv_daily, "_last_fetch_time", 100.0)
        monkeypatch.setattr(arxiv_daily.time, "monotonic", lambda: next(monotonic_values))
        monkeypatch.setattr(arxiv_daily.time, "sleep", sleeps.append)

        arxiv_daily._wait_for_arxiv_rate_limit()

        assert sleeps == [2.0]
        assert arxiv_daily._last_fetch_time == 103.0

    def test_network_error(self, monkeypatch):
        monkeypatch.setattr(arxiv_daily, "_last_fetch_time", None)
        monkeypatch.setattr(arxiv_daily.time, "sleep", lambda delay: None)

        with patch("arxiv_daily.urllib.request.urlopen") as mock_urlopen:
            from urllib.error import URLError
            mock_urlopen.side_effect = URLError("Connection refused")
            with pytest.raises(URLError):
                arxiv_daily.fetch_page("cs.AI")


# ===========================================================================
# TestStreamLoopDetector
# ===========================================================================

class TestStreamLoopDetector:
    def test_window_not_full_no_trigger(self):
        detector = arxiv_daily._StreamLoopDetector()
        line = "This line repeats but the window is not full yet.\n"

        for _ in range(arxiv_daily.STREAM_LOOP_WINDOW - 1):
            assert detector.feed(line) is False

    def test_literal_loop_triggers(self):
        detector = arxiv_daily._StreamLoopDetector()
        line = "This line is repeating in the stream.\n"

        for _ in range(arxiv_daily.STREAM_LOOP_WINDOW - 1):
            assert detector.feed(line) is False

        assert detector.feed(line) is True

    def test_varying_loop_triggers(self):
        detector = arxiv_daily._StreamLoopDetector()
        lines = [
            "Let me assume the ID is `2605.17108`? No.\n",
            "Wait, I see `ID: 2605.17108`? No.\n",
            "Okay, I will just use the ID `2605.17108`? No.\n",
            "Actually, the ID is `2605.17108`? No.\n",
        ]

        for i in range(arxiv_daily.STREAM_LOOP_WINDOW - 1):
            assert detector.feed(lines[i % len(lines)]) is False

        assert detector.feed(lines[-1]) is True

    def test_template_repetition_no_false_positive(self):
        detector = arxiv_daily._StreamLoopDetector()
        template = [
            "**Paper {i}: Unique Title About Topic {i}**\n",
            "- Summary: This paper {i} proposes a novel method for problem {i}.\n",
            "- Tags: Tag{i}A, Tag{i}B\n",
            "- Venue: null (no acceptance info)\n",
        ]

        for i in range(1, 11):
            for line_tmpl in template:
                assert detector.feed(line_tmpl.format(i=i)) is False

    def test_normal_diverse_text_no_trigger(self):
        detector = arxiv_daily._StreamLoopDetector()

        for i in range(arxiv_daily.STREAM_LOOP_WINDOW + 10):
            assert detector.feed(f"Unique stream line {i} with varied content.\n") is False

    def test_blank_lines_skipped(self):
        detector = arxiv_daily._StreamLoopDetector(
            window_size=10, max_unique=5, max_blank_chars=1000,
        )

        for i in range(5):
            assert detector.feed(f"Unique meaningful line {i}.\n") is False
        for _ in range(50):
            assert detector.feed("\n   \n\t\n") is False

    def test_blank_flood_triggers(self):
        detector = arxiv_daily._StreamLoopDetector()

        assert detector.feed("\n" * 250) is True

    def test_blank_flood_accumulates_across_chunks(self):
        detector = arxiv_daily._StreamLoopDetector()

        for _ in range(4):
            assert detector.feed("\n" * 49) is False

        assert detector.feed("\n" * 4) is True

    def test_non_blank_chunk_resets_blank_counter(self):
        detector = arxiv_daily._StreamLoopDetector()

        assert detector.feed("\n" * 100) is False
        assert detector.feed("x") is False
        assert detector.feed("\n" * 150) is False

    def test_blank_flood_via_spaces_and_tabs(self):
        detector = arxiv_daily._StreamLoopDetector()
        blank_payload = "   \t  \n  "

        while detector.total_chars + len(blank_payload) < arxiv_daily.STREAM_LOOP_MAX_BLANK_CHARS:
            assert detector.feed(blank_payload) is False

        assert detector.feed(blank_payload) is True

    def test_normal_json_indent_no_false_positive(self):
        detector = arxiv_daily._StreamLoopDetector()
        content = '{\n  "key": "val",\n  "k2": "v2"\n}'

        assert detector.feed(content) is False

    def test_blank_chunk_does_not_pollute_window(self):
        detector = arxiv_daily._StreamLoopDetector(max_blank_chars=1000)

        assert detector.feed("\n" * 300) is False
        for i in range(arxiv_daily.STREAM_LOOP_WINDOW):
            assert detector.feed(f"Unique line after blanks {i}.\n") is False

    def test_custom_max_blank_chars(self):
        detector = arxiv_daily._StreamLoopDetector(max_blank_chars=50)

        assert detector.feed(" " * 49) is False
        assert detector.feed("\t") is True

    def test_partial_lines_buffered(self):
        detector = arxiv_daily._StreamLoopDetector(window_size=3, max_unique=1)

        assert detector.feed("This line is incomplete") is False
        assert detector.feed(" and still has no newline") is False
        assert detector.feed("\n") is False

    def test_total_chars_accumulated(self):
        detector = arxiv_daily._StreamLoopDetector()
        chunks = ["abc", "\n\n", "def\n", "   \n"]

        for chunk in chunks:
            assert detector.feed(chunk) is False
        assert detector.total_chars == sum(len(chunk) for chunk in chunks)

    def test_window_evicts_old_lines(self):
        detector = arxiv_daily._StreamLoopDetector()

        for i in range(arxiv_daily.STREAM_LOOP_WINDOW):
            assert detector.feed(f"Unique line {i}.\n") is False
        assert detector.feed("Unique line 1.\n") is False

    def test_custom_params(self):
        detector = arxiv_daily._StreamLoopDetector(window_size=10, max_unique=3)
        lines = ["pattern one\n", "pattern two\n", "pattern three\n"]

        for i in range(9):
            assert detector.feed(lines[i % len(lines)]) is False

        assert detector.feed(lines[0]) is True


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
# TestCallLlmLoopDetection
# ===========================================================================

class TestCallLlmLoopDetection:
    def _build_single_paper_messages(self):
        return arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "Test", "abstract": "Abstract text"}], [],
        )

    def test_reasoning_literal_loop_triggers_retry(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["reasoning_loop", "none"]
        handler_cls.request_counter = 0

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert "papers" in result
        assert handler_cls.request_counter > 1

    def test_content_literal_loop_triggers_retry(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["content_loop", "none"]
        handler_cls.request_counter = 0

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert "papers" in result
        assert handler_cls.request_counter > 1

    def test_varying_loop_triggers_retry(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["varying_loop", "none"]
        handler_cls.request_counter = 0

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert "papers" in result
        assert handler_cls.request_counter > 1

    def test_template_repetition_no_false_positive(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "template_repetition"
        handler_cls.request_counter = 0

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert result == {"papers": [], "new_tags": []}
        assert handler_cls.request_counter == 1

    def test_blank_flood_triggers_retry(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["blank_flood_loop", "none"]
        handler_cls.request_counter = 0

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert "papers" in result
        assert handler_cls.request_counter > 1

    def test_normal_stream_no_false_positive(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "none"
        handler_cls.request_counter = 0

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert "papers" in result
        assert result["papers"][0]["id"] == "2604.00001"
        assert handler_cls.request_counter == 1


# ===========================================================================
# TestRunPhase2
# ===========================================================================

class TestDebugOutputDir:
    def test_uses_listing_date(self, tmp_path):
        path = arxiv_daily.debug_output_dir(tmp_path, "cs.AI", "2026-09-10")
        assert path == tmp_path / "debug" / "cs.AI" / "2026-09-10"

    def test_rejects_invalid_date(self, tmp_path):
        path = arxiv_daily.debug_output_dir(tmp_path, "cs.AI", "not-a-date")
        assert path.parent == tmp_path / "debug" / "cs.AI"
        assert path.name != "not-a-date"
        assert arxiv_daily._DATE_DIR_RE.fullmatch(path.name)

    def test_rejects_path_separators_in_field(self, tmp_path):
        path = arxiv_daily.debug_output_dir(tmp_path, "../etc", "2026-09-10")
        assert path == tmp_path / "debug" / "unknown" / "2026-09-10"


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
        debug_dir = cache_dir / "debug" / sample_data["field"] / sample_data["listing_date"]
        assert debug_dir.is_dir()
        assert list(debug_dir.glob("batch_*_stream_attempt1.txt"))
        assert not (cache_dir / "debug" / sample_data["field"] / "batch_001_stream_attempt1.txt").exists()

    def test_caching(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # First run
        first_results, first_failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert first_failed == 0

        # Verify results cover all papers with abstracts
        expected_ids = {
            p["id"] for s in sample_data["sections"] for p in s["papers"] if p.get("abstract")
        }
        assert set(first_results.keys()) == expected_ids

        # Verify cache files were created
        field_cache = cache_dir / sample_data["field"]
        assert (field_cache / "metadata_hash").exists()
        assert list(field_cache.glob("batch_*.json"))

        # Record request count
        count_after_first = handler_cls.request_counter

        # Second run - should use cache
        second_results, second_failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )

        assert second_failed == 0
        assert second_results == first_results
        # No additional LLM requests should have been made
        assert handler_cls.request_counter == count_after_first

    def test_cache_preload_does_not_leak_future_tags(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        field_cache = cache_dir / sample_data["field"]
        field_cache.mkdir(parents=True)

        papers = [
            {
                "id": f"2604.{i:05d}",
                "title": f"Paper {i}",
                "abstract": f"Abstract {i}",
                "comments": "",
            }
            for i in range(arxiv_daily.BATCH_SIZE + 1)
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "papers": papers}],
        }
        (field_cache / "metadata_hash").write_text(arxiv_daily.compute_metadata_hash(data), encoding="utf-8")
        cached_paper = papers[-1]
        long_summary = (
            "这是一篇用于回归测试的研究论文摘要，内容足够长，可以通过质量检查，"
            "并且不会因为摘要长度阈值导致缓存命中路径被误判为失败。"
        )
        cached_response = {
            "papers": [{
                "id": cached_paper["id"],
                "summary": long_summary,
                "tags": ["FutureTag"],
                "venue": None,
            }],
            "new_tags": ["FutureTag"],
            "current_tags": ["LLM Reasoning", "FutureTag"],
        }
        (field_cache / "batch_002.json").write_text(
            json.dumps(cached_response, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        observed_prompt_tags = []

        def fake_build_prompt(batch, current_tags):
            observed_prompt_tags.append(list(current_tags))
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            return {
                "papers": [
                    {
                        "id": paper["id"],
                        "summary": long_summary,
                        "tags": ["GeneratedTag"],
                        "venue": None,
                    }
                    for paper in papers[:arxiv_daily.BATCH_SIZE]
                ],
                "new_tags": ["GeneratedTag"],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)

        _, failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )

        assert failed == 0
        assert observed_prompt_tags == [["LLM Reasoning", "Diffusion Models", "Transfer Learning", "Robotics"]]

    def test_process_batch_partial_success_writes_cache(self, monkeypatch, logger, tmp_path):
        field_cache = tmp_path / "cache"
        debug_dir = tmp_path / "debug"
        field_cache.mkdir()
        long_summary = "这是一篇用于测试的论文摘要，内容足够长，可以通过质量检查并验证部分成功缓存。"
        papers = [
            {
                "id": f"2604.{i:05d}",
                "title": f"Paper {i}",
                "abstract": f"Abstract {i}",
            }
            for i in range(1, arxiv_daily.BATCH_SIZE + 1)
        ]

        monkeypatch.setattr(
            arxiv_daily,
            "build_prompt",
            lambda batch, current_tags: [{"role": "user", "content": "fake prompt"}],
        )
        monkeypatch.setattr(
            arxiv_daily,
            "call_llm",
            lambda *args, **kwargs: (
                {
                    "papers": [
                        {"id": paper["id"], "summary": long_summary, "tags": ["T1"], "venue": None}
                        for paper in papers[:-2]
                    ],
                    "new_tags": ["T1"],
                },
                {},
            ),
        )

        processed = arxiv_daily._process_batch(
            papers,
            0,
            1,
            [],
            "http://unused",
            "test-key",
            "test-model",
            logger,
            debug_dir,
            field_cache,
            "batch",
            save_cache=True,
            cache_allowed=True,
            exception_status="failed",
        )

        assert processed.failed is True
        assert [paper["id"] for paper in processed.unresolved] == [p["id"] for p in papers]
        assert processed.matched == {}
        assert not (field_cache / "batch_001.json").exists()

    def test_run_phase2_partial_cache_only_retries_missing(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        field_cache = cache_dir / sample_data["field"]
        field_cache.mkdir(parents=True)
        long_summary = "这是一篇用于测试的论文摘要，内容足够长，可以通过质量检查并验证缓存重试。"
        papers = [
            {"id": "2604.00001", "title": "Paper 1", "abstract": "Abstract 1"},
            {"id": "2604.00002", "title": "Paper 2", "abstract": "Abstract 2"},
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": 2, "papers": papers}],
        }
        (field_cache / "metadata_hash").write_text(arxiv_daily.compute_metadata_hash(data), encoding="utf-8")
        (field_cache / "batch_001.json").write_text(
            json.dumps(
                {
                    "papers": [
                        {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    ],
                    "new_tags": ["T1"],
                    "current_tags": ["LLM Reasoning", "T1"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        prompt_batches = []

        def fake_build_prompt(batch, current_tags):
            prompt_batches.append([paper["id"] for paper in batch])
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            return {
                "papers": [
                    {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    {"id": "2604.00002", "summary": long_summary, "tags": ["T2"], "venue": None},
                ],
                "new_tags": ["T2"],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)

        results, failed = arxiv_daily.run_phase2(
            data,
            tags_file,
            cache_dir,
            no_cache=False,
            api_endpoint="http://unused",
            api_key="test-key",
            model="test-model",
            logger=logger,
        )

        assert failed == 0
        assert prompt_batches == [["2604.00001", "2604.00002"]]
        assert set(results) == {"2604.00001", "2604.00002"}

    def test_retries_only_unresolved_papers(self, monkeypatch, logger, tags_file, tmp_path, sample_data):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        long_summary = "这是一篇用于测试的论文摘要，内容足够长，可以通过质量检查并确认重试范围正确。"
        papers = [
            {"id": "2604.00001", "title": "Paper 1", "abstract": "Abstract 1"},
            {"id": "2604.00002", "title": "Paper 2", "abstract": "Abstract 2"},
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": 2, "papers": papers}],
        }
        prompt_batches = []

        def fake_build_prompt(batch, current_tags):
            prompt_batches.append([paper["id"] for paper in batch])
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            if len(prompt_batches) == 1:
                return {
                    "papers": [
                        {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                        {"id": "2604.00002", "summary": "short", "tags": ["T2"], "venue": None},
                    ],
                    "new_tags": [],
                }, {}
            return {
                "papers": [
                    {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    {"id": "2604.00002", "summary": long_summary, "tags": ["T2"], "venue": None},
                ],
                "new_tags": [],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)

        results, failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=True,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )

        assert failed == 0
        assert prompt_batches == [["2604.00001", "2604.00002"], ["2604.00001", "2604.00002"]]
        assert set(results) == {"2604.00001", "2604.00002"}

    def test_retry_merges_unresolved_across_batches(self, monkeypatch, logger, tags_file, tmp_path, sample_data):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        long_summary = "这是一篇用于测试的论文摘要，内容足够长，可以通过质量检查并验证跨批重试。"
        papers = [
            {
                "id": f"2604.{i:05d}",
                "title": f"Paper {i}",
                "abstract": f"Abstract {i}",
                "comments": "",
            }
            for i in range(1, arxiv_daily.BATCH_SIZE + 5)
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": len(papers), "papers": papers}],
        }
        retry_batches = []

        def fake_process_batch(
            batch,
            idx,
            total,
            current_tags,
            api_endpoint,
            api_key,
            model,
            logger,
            debug_dir,
            field_cache,
            label_prefix,
            save_cache,
            cache_allowed,
            exception_status,
            **kwargs,
        ):
            if label_prefix == "retry":
                retry_batches.append([paper["id"] for paper in batch])
                return arxiv_daily.BatchProcessResult(
                    {
                        paper["id"]: {
                            "summary": long_summary,
                            "tags": ["RetryTag"],
                            "venue": None,
                        }
                        for paper in batch
                    },
                    [],
                    False,
                )

            if idx == 0:
                unresolved = batch[-4:]
            else:
                unresolved = batch[:4]
            return arxiv_daily.BatchProcessResult({}, unresolved, True)

        monkeypatch.setattr(arxiv_daily, "_process_batch", fake_process_batch)

        results, failed = arxiv_daily.run_phase2(
            data,
            tags_file,
            cache_dir,
            no_cache=True,
            api_endpoint="http://unused",
            api_key="test-key",
            model="test-model",
            logger=logger,
        )

        unresolved_ids = [paper["id"] for paper in papers[arxiv_daily.BATCH_SIZE - 4 : arxiv_daily.BATCH_SIZE + 4]]
        expected_retry = [
            unresolved_ids[i : i + arxiv_daily.RETRY_BATCH_SIZE]
            for i in range(0, len(unresolved_ids), arxiv_daily.RETRY_BATCH_SIZE)
        ]
        assert failed == 0
        assert retry_batches == expected_retry
        assert set(results) == set(unresolved_ids)

    def test_unresolved_after_retry_gets_summary_placeholder(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        papers = [{"id": "2604.00001", "title": "Paper 1", "abstract": "Abstract 1"}]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": 1, "papers": papers}],
        }

        monkeypatch.setattr(
            arxiv_daily,
            "build_prompt",
            lambda batch, current_tags: [{"role": "user", "content": "fake prompt"}],
        )

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            return {
                "papers": [
                    {"id": "2604.00001", "summary": "short", "tags": ["T1"], "venue": None},
                ],
                "new_tags": [],
            }, {}

        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)

        results, failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=True,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )

        assert failed == 1
        assert results["2604.00001"]["summary"] == arxiv_daily.SUMMARY_FAILURE_PLACEHOLDER
        assert results["2604.00001"]["tags"] == [arxiv_daily.SUMMARY_FAILURE_TAG]

    def test_quality_failure_then_retry_then_cache_replay(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        long_summary = "这是一篇用于回归测试的论文摘要，内容足够长，可以通过质量检查并验证缓存复用。"
        papers = [
            {"id": "2604.00001", "title": "Paper 1", "abstract": "Abstract 1"},
            {"id": "2604.00002", "title": "Paper 2", "abstract": "Abstract 2"},
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": 2, "papers": papers}],
        }

        prompt_batches: list[list[str]] = []

        def fake_build_prompt(batch, current_tags):
            prompt_batches.append([paper["id"] for paper in batch])
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            if len(prompt_batches) == 1:
                return {
                    "papers": [
                        {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                        {"id": "2604.00002", "summary": "short", "tags": ["T2"], "venue": None},
                    ],
                    "new_tags": [],
                }, {}
            return {
                "papers": [
                    {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    {"id": "2604.00002", "summary": long_summary, "tags": ["T2"], "venue": None},
                ],
                "new_tags": [],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)

        first_results, first_failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )
        assert first_failed == 0
        assert set(first_results) == {"2604.00001", "2604.00002"}
        assert prompt_batches == [["2604.00001", "2604.00002"], ["2604.00001", "2604.00002"]]

        field_cache = cache_dir / data["field"]
        cache_path = field_cache / "batch_001.json"
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_ids = [paper["id"] for paper in cached["papers"]]
        assert cached_ids == list(dict.fromkeys(cached_ids)), "cache must not hold duplicate ids"
        assert set(cached_ids) == {"2604.00001", "2604.00002"}

        prompt_batches.clear()
        diagnostics_records: list[arxiv_daily.MatchDiagnostics] = []
        original_match = arxiv_daily.match_llm_results

        def capturing_match(*args, **kwargs):
            matched, diagnostics = original_match(*args, **kwargs)
            diagnostics_records.append(diagnostics)
            return matched, diagnostics

        monkeypatch.setattr(arxiv_daily, "match_llm_results", capturing_match)

        second_results, second_failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )

        assert second_failed == 0
        assert set(second_results) == {"2604.00001", "2604.00002"}
        assert prompt_batches == [], "second run must not invoke LLM"
        assert diagnostics_records, "cache replay should still run match_llm_results"
        for diag in diagnostics_records:
            assert diag.duplicate_ids == []
            assert diag.missing_ids == []
            assert diag.quality_failed == []

    def test_patch_batch_cache_unions_retry_tags(self, tmp_path, logger):
        field_cache = tmp_path / "cache"
        field_cache.mkdir()
        cache_path = field_cache / "batch_001.json"
        cache_path.write_text(
            json.dumps(
                {
                    "papers": [
                        {"id": "2604.00001", "summary": "ok", "tags": ["T1"], "venue": None},
                    ],
                    "new_tags": ["T1"],
                    "current_tags": ["Existing", "T1"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        arxiv_daily._patch_batch_cache(
            field_cache,
            1,
            [{"id": "2604.00002", "summary": "retry", "tags": ["RetryTag"], "venue": None}],
            logger,
            new_tags=["T1", "RetryTag"],
        )

        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["new_tags"] == ["T1", "RetryTag"]
        assert cached["current_tags"] == ["Existing", "T1", "RetryTag"]

    def test_retry_tags_persist_across_cache_replay(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        long_summary = "这是一篇用于回归测试的论文摘要，内容足够长，可以通过质量检查。"
        papers = [
            {"id": "2604.00001", "title": "Paper 1", "abstract": "Abstract 1"},
            {"id": "2604.00002", "title": "Paper 2", "abstract": "Abstract 2"},
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": 2, "papers": papers}],
        }

        prompt_calls: list[list[str]] = []

        def fake_build_prompt(batch, current_tags):
            prompt_calls.append(list(current_tags))
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            if len(prompt_calls) == 1:
                return {
                    "papers": [
                        {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                        {"id": "2604.00002", "summary": "short", "tags": ["T2"], "venue": None},
                    ],
                    "new_tags": ["T1"],
                }, {}
            return {
                "papers": [
                    {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    {"id": "2604.00002", "summary": long_summary, "tags": ["RetryTag"], "venue": None},
                ],
                "new_tags": ["RetryTag"],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)

        first_results, first_failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )
        assert first_failed == 0
        assert set(first_results) == {"2604.00001", "2604.00002"}

        field_cache = cache_dir / data["field"]
        cached = json.loads((field_cache / "batch_001.json").read_text(encoding="utf-8"))
        assert "RetryTag" in cached["new_tags"]
        assert "RetryTag" in cached["current_tags"]

        first_tags = json.loads(tags_file.read_text(encoding="utf-8"))["tags"]
        assert "RetryTag" in first_tags
        assert "T1" in first_tags

        prompt_calls.clear()
        second_results, second_failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )
        assert second_failed == 0
        assert set(second_results) == {"2604.00001", "2604.00002"}
        assert prompt_calls == [], "second run must not invoke LLM"

        second_tags = json.loads(tags_file.read_text(encoding="utf-8"))["tags"]
        assert "RetryTag" in second_tags, "RetryTag must survive cache replay"
        assert "T1" in second_tags

    def test_cache_replay_does_not_leak_future_retry_tags(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        field_cache = cache_dir / sample_data["field"]
        field_cache.mkdir(parents=True)

        long_summary = (
            "这是一篇用于回归测试的研究论文摘要，内容足够长，可以通过质量检查，"
            "并且不会因为摘要长度阈值导致缓存命中路径被误判为失败。"
        )
        papers = [
            {
                "id": f"2604.{i:05d}",
                "title": f"Paper {i}",
                "abstract": f"Abstract {i}",
                "comments": "",
            }
            for i in range(arxiv_daily.BATCH_SIZE + 1)
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "papers": papers}],
        }
        (field_cache / "metadata_hash").write_text(
            arxiv_daily.compute_metadata_hash(data), encoding="utf-8"
        )
        cached_paper = papers[-1]
        cached_response = {
            "papers": [
                {"id": cached_paper["id"], "summary": long_summary, "tags": ["FutureTag"], "venue": None}
            ],
            "new_tags": ["FutureTag"],
            "current_tags": ["LLM Reasoning", "FutureTag"],
        }
        (field_cache / "batch_002.json").write_text(
            json.dumps(cached_response, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        observed_prompt_tags = []

        def fake_build_prompt(batch, current_tags):
            observed_prompt_tags.append(list(current_tags))
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            return {
                "papers": [
                    {"id": paper["id"], "summary": long_summary, "tags": ["GeneratedTag"], "venue": None}
                    for paper in papers[:arxiv_daily.BATCH_SIZE]
                ],
                "new_tags": ["GeneratedTag"],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)

        _, failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )
        assert failed == 0
        assert observed_prompt_tags == [
            ["LLM Reasoning", "Diffusion Models", "Transfer Learning", "Robotics"]
        ]

    def test_retry_patch_does_not_leak_cross_batch_tags(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        long_summary = "这是一篇用于回归测试的论文摘要，内容足够长，可以通过质量检查并验证 retry tag 不跨批污染。"
        monkeypatch.setattr(arxiv_daily, "BATCH_SIZE", 2)
        monkeypatch.setattr(arxiv_daily, "RETRY_BATCH_SIZE", 4)

        papers = [
            {"id": "2604.00001", "title": "P1", "abstract": "A1"},
            {"id": "2604.00002", "title": "P2", "abstract": "A2"},
            {"id": "2604.00003", "title": "P3", "abstract": "A3"},
            {"id": "2604.00004", "title": "P4", "abstract": "A4"},
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": len(papers), "papers": papers}],
        }

        retry_invocations: list[list[str]] = []

        def fake_process_batch(
            batch,
            idx,
            total,
            current_tags,
            api_endpoint,
            api_key,
            model,
            logger,
            debug_dir,
            field_cache,
            label_prefix,
            save_cache,
            cache_allowed,
            exception_status,
            **kwargs,
        ):
            if label_prefix == "retry":
                retry_invocations.append([paper["id"] for paper in batch])
                matched = {
                    "2604.00002": {
                        "summary": long_summary,
                        "tags": ["RetainedTag"],
                        "venue": None,
                    },
                    "2604.00004": {
                        "summary": long_summary,
                        "tags": ["FutureTag"],
                        "venue": None,
                    },
                }
                return arxiv_daily.BatchProcessResult(
                    matched,
                    [],
                    False,
                    new_tags=["RetainedTag", "FutureTag"],
                    current_tags_snapshot=list(current_tags) + ["RetainedTag", "FutureTag"],
                )

            batch_num = idx + 1
            unresolved = [batch[1]]
            if save_cache and cache_allowed:
                arxiv_daily._write_batch_cache(
                    field_cache,
                    batch_num,
                    {batch[0]["id"]: {"summary": long_summary, "tags": ["BaseTag"], "venue": None}},
                    [],
                    list(current_tags),
                )
            return arxiv_daily.BatchProcessResult(
                {batch[0]["id"]: {"summary": long_summary, "tags": ["BaseTag"], "venue": None}},
                unresolved,
                True,
                new_tags=[],
                current_tags_snapshot=list(current_tags),
            )

        monkeypatch.setattr(arxiv_daily, "_process_batch", fake_process_batch)

        results, failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )

        assert failed == 0
        assert set(results) == {p["id"] for p in papers}
        assert retry_invocations == [["2604.00002", "2604.00004"]]

        field_cache = cache_dir / data["field"]
        cache_001 = json.loads((field_cache / "batch_001.json").read_text(encoding="utf-8"))
        cache_002 = json.loads((field_cache / "batch_002.json").read_text(encoding="utf-8"))

        assert "FutureTag" not in cache_001["new_tags"]
        assert "FutureTag" not in cache_001["current_tags"]
        assert "FutureTag" in cache_002["new_tags"]
        assert "FutureTag" in cache_002["current_tags"]

        observed_replay_tags: list[list[str]] = []

        def replay_process_batch(*args, **kwargs):
            raise AssertionError("cache replay must not invoke _process_batch")

        original_match = arxiv_daily.match_llm_results

        def capturing_match(batch, llm_papers, logger, batch_num, *rest, **kw):
            matched, diagnostics = original_match(batch, llm_papers, logger, batch_num, *rest, **kw)
            return matched, diagnostics

        def capturing_build_prompt(batch, current_tags):
            observed_replay_tags.append(list(current_tags))
            return [{"role": "user", "content": "fake"}]

        monkeypatch.setattr(arxiv_daily, "_process_batch", replay_process_batch)
        monkeypatch.setattr(arxiv_daily, "match_llm_results", capturing_match)
        monkeypatch.setattr(arxiv_daily, "build_prompt", capturing_build_prompt)

        _, replay_failed = arxiv_daily.run_phase2(
            data, tags_file, cache_dir, no_cache=False,
            api_endpoint="http://unused", api_key="test-key", model="test-model", logger=logger,
        )
        assert replay_failed == 0
        assert observed_replay_tags == [], "cache replay must not call build_prompt"

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

    def test_no_cache_disables_batch_cache_and_retry_patch(
        self, monkeypatch, logger, tags_file, tmp_path, sample_data
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        long_summary = "这是一篇用于测试的论文摘要，内容足够长，可以通过质量检查并验证禁用缓存。"
        papers = [
            {"id": "2604.00001", "title": "Paper 1", "abstract": "Abstract 1"},
            {"id": "2604.00002", "title": "Paper 2", "abstract": "Abstract 2"},
        ]
        data = {
            "field": sample_data["field"],
            "listing_date": sample_data["listing_date"],
            "page_type": "new",
            "sections": [{"name": "New submissions", "count": 2, "papers": papers}],
        }
        prompt_batches = []
        patch_calls = []

        def fake_build_prompt(batch, current_tags):
            prompt_batches.append([paper["id"] for paper in batch])
            return [{"role": "user", "content": "fake prompt"}]

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None, expected_ids=None):
            if len(prompt_batches) == 1:
                return {
                    "papers": [
                        {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    ],
                    "new_tags": [],
                }, {}
            return {
                "papers": [
                    {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    {"id": "2604.00002", "summary": long_summary, "tags": ["T2"], "venue": None},
                ],
                "new_tags": [],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)
        monkeypatch.setattr(
            arxiv_daily,
            "_patch_batch_cache",
            lambda field_cache, batch_num, new_papers, logger, new_tags=None: patch_calls.append(
                (batch_num, new_papers)
            ),
        )

        results, failed = arxiv_daily.run_phase2(
            data,
            tags_file,
            cache_dir,
            no_cache=True,
            api_endpoint="http://unused",
            api_key="test-key",
            model="test-model",
            logger=logger,
        )

        field_cache = cache_dir / sample_data["field"]
        assert failed == 0
        assert set(results) == {"2604.00001", "2604.00002"}
        assert prompt_batches == [["2604.00001", "2604.00002"], ["2604.00001", "2604.00002"]]
        assert patch_calls == []
        assert not (field_cache / "metadata_hash").exists()
        assert list(field_cache.glob("batch_*.json")) == []

    def test_tags_updated(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Read initial tags for comparison
        initial_tags = json.loads(tags_file.read_text(encoding="utf-8"))["tags"]

        arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=False,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )

        tags_data = json.loads(tags_file.read_text(encoding="utf-8"))
        assert "tags" in tags_data
        assert "updated_at" in tags_data

        # new_tags from fake server should be added
        assert "AI Evaluation" in tags_data["tags"]
        # Existing tags should be preserved
        for tag in initial_tags:
            assert tag in tags_data["tags"]
        # No duplicates
        assert len(tags_data["tags"]) == len(set(tags_data["tags"]))

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
# TestCallLlmErrorModes
# ===========================================================================

class TestCallLlmErrorModes:
    """Tests for call_llm behavior under various fake server error modes."""

    def _build_single_paper_messages(self):
        return arxiv_daily.build_prompt(
            [{"id": "2604.00001", "title": "Test", "abstract": "Abstract text"}], [],
        )

    def test_retry_delay_clamps_to_last_configured_delay(self, monkeypatch):
        monkeypatch.setattr(arxiv_daily, "RETRY_DELAYS", [0.1, 0.2])
        assert arxiv_daily._retry_delay(0) == 0.1
        assert arxiv_daily._retry_delay(1) == 0.2
        assert arxiv_daily._retry_delay(10) == 0.2

    def test_invalid_json_raises(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "invalid_json"
        with pytest.raises(Exception):
            arxiv_daily.call_llm(
                self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
            )

    def test_malformed_sse_raises(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "malformed_sse"
        # Current implementation does not skip bad SSE lines; json.loads raises directly
        with pytest.raises(Exception):
            arxiv_daily.call_llm(
                self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
            )

    def test_partial_stream_raises(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "partial_stream"
        with pytest.raises(ConnectionError, match="SSE stream incomplete"):
            arxiv_daily.call_llm(
                self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
            )

    def test_partial_stream_retries_then_succeeds(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["partial_stream", "none"]
        handler_cls.request_counter = 0

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert "papers" in result
        assert handler_cls.request_counter == 2

    def test_http_429_retries_then_succeeds(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["http_429", "http_429", "none"]
        handler_cls.request_counter = 0
        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )
        assert "papers" in result

    def test_http_429_uses_retry_after(self, fake_llm_server, logger, monkeypatch):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_sequence = ["http_429", "none"]
        handler_cls.retry_after_header = "7"
        sleeps = []
        monkeypatch.setattr(arxiv_daily.time, "sleep", sleeps.append)

        result, _ = arxiv_daily.call_llm(
            self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
        )

        assert "papers" in result
        assert sleeps == [7.0]

    def test_http_400_does_not_retry(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "http_400"
        handler_cls.request_counter = 0
        with pytest.raises(arxiv_daily.urllib.error.HTTPError) as exc_info:
            arxiv_daily.call_llm(
                self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
            )

        assert exc_info.value.code == 400
        assert handler_cls.request_counter == 1

    def test_connection_reset_raises(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "connection_reset"
        with pytest.raises(Exception):
            arxiv_daily.call_llm(
                self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
            )

    def test_missing_papers_schema_retries_then_raises(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "schema_missing_papers"
        with pytest.raises(ValueError, match="missing 'papers' list"):
            arxiv_daily.call_llm(
                self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
            )

    def test_bad_new_tags_schema_retries_then_raises(self, fake_llm_server, logger):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "schema_bad_new_tags"
        with pytest.raises(ValueError, match="'new_tags' must be list"):
            arxiv_daily.call_llm(
                self._build_single_paper_messages(), api_url, "test-key", "test-model", logger,
            )


# ===========================================================================
# TestRunPhase2ErrorModes
# ===========================================================================

class TestRunPhase2ErrorModes:
    """Tests for run_phase2 behavior when fake server returns problematic data."""

    def test_wrong_ids_fails_batch(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "wrong_ids"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=True,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert failed > 0

    def test_empty_papers_fails_batch(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "empty_papers"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=True,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert failed > 0

    def test_missing_fields_fails_batch(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "missing_fields"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=True,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert failed > 0

    def test_versioned_ids_succeed(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "versioned_ids"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        results, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=True,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert len(results) > 0
        assert failed == 0

    def test_duplicate_ids_reject_batch(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        """An extra duplicate ID fails schema (count != N) and the whole batch retries then fails."""
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "duplicate_ids"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        results, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=True,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert failed >= 1
        assert results["2604.06189"]["summary"] == arxiv_daily.SUMMARY_FAILURE_PLACEHOLDER


# ===========================================================================
# TestMarkdownEscaping
# ===========================================================================

class TestMarkdownEscaping:
    def test_escape_md_inline_preserves_plain_chinese(self):
        assert arxiv_daily._escape_md_inline("这是一段普通中文摘要。") == "这是一段普通中文摘要。"

    def test_escape_md_inline_escapes_pipe(self):
        assert arxiv_daily._escape_md_inline("模型 A | 模型 B") == "模型 A \\| 模型 B"

    def test_escape_md_inline_escapes_html_tag_chars(self):
        assert arxiv_daily._escape_md_inline("包含 <tag> 标记") == "包含 \\<tag\\> 标记"

    def test_escape_md_inline_escapes_combined_chars(self):
        text = "方法 <A|B> 在任务 C | D 上有效"
        assert arxiv_daily._escape_md_inline(text) == "方法 \\<A\\|B\\> 在任务 C \\| D 上有效"

    def test_escape_md_inline_preserves_empty_string(self):
        assert arxiv_daily._escape_md_inline("") == ""


# ===========================================================================
# TestFrontMatterDate
# ===========================================================================

class TestFrontMatterDate:
    TZ8 = arxiv_daily.OUTPUT_TZ

    def at(self, *args, tz=None):
        return arxiv_daily.datetime(*args, tzinfo=tz or self.TZ8)

    def test_on_time_run_keeps_run_time(self):
        now = self.at(2026, 11, 20, 22, 52, 13)
        assert arxiv_daily.front_matter_date("2026-11-20", now) == now

    def test_next_morning_catch_up_uses_listing_day_16h(self):
        now = self.at(2026, 11, 18, 9, 41, 19)
        assert arxiv_daily.front_matter_date("2026-11-17", now) == self.at(2026, 11, 17, 16)

    def test_multi_day_catch_up_uses_listing_day_16h(self):
        now = self.at(2026, 10, 17, 12, 0)
        assert arxiv_daily.front_matter_date("2026-10-14", now) == self.at(2026, 10, 14, 16)

    def test_run_just_after_midnight_counts_as_catch_up(self):
        now = self.at(2026, 11, 21, 0, 30)
        assert arxiv_daily.front_matter_date("2026-11-20", now) == self.at(2026, 11, 20, 16)

    def test_same_day_is_judged_in_output_tz(self):
        # 07:26 PST on 11-20 is 23:26 +0800 on 11-20: still an on-time run.
        pst = arxiv_daily.timezone(arxiv_daily.timedelta(hours=-8))
        now = self.at(2026, 11, 20, 7, 26, tz=pst)
        result = arxiv_daily.front_matter_date("2026-11-20", now)
        assert result == now
        assert result.strftime("%Y-%m-%d %H:%M") == "2026-11-20 23:26"

    def test_unparseable_listing_date_falls_back_to_run_time(self):
        now = self.at(2026, 11, 20, 22, 0)
        assert arxiv_daily.front_matter_date(None, now) == now


# ===========================================================================
# TestGenerateMarkdown
# ===========================================================================

class TestGenerateMarkdown:
    def test_presentation_line_follows_venue_and_header_shows_venue_only(self, sample_data, tmp_path):
        results_by_id = {
            "2604.06189": {"summary": "摘要一。", "tags": ["T"], "venue": "ACL 2026", "presentation": "Oral"},
            "2604.07001": {"summary": "摘要二。", "tags": ["T"], "venue": None, "presentation": "Oral"},
            "2604.08123": {"summary": "摘要三。", "tags": ["T"], "venue": None},
        }
        content = arxiv_daily.generate_markdown(
            sample_data, results_by_id, tmp_path, "2025-04-11"
        ).read_text(encoding="utf-8")
        assert "- **Venue**: ACL 2026\n- **Presentation**: Oral\n" in content
        assert content.count("**Presentation**") == 1, "no presentation without a venue"
        assert "(ACL 2026)" in content and "(ACL 2026, Oral)" not in content

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
        assert " +0800" in content
        assert "categories:" in content

    def test_catch_up_front_matter_date_is_listing_day_16h(self, sample_data, tmp_path):
        # 2025-04-11 is always in the past, so this run is a catch-up run.
        output = arxiv_daily.generate_markdown(sample_data, {}, tmp_path, "2025-04-11")
        content = output.read_text(encoding="utf-8")
        assert "\ndate: 2025-04-11 16:00:00 +0800\n" in content

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

    def test_presentation_written_after_venue_and_replaced(self, tmp_path, logger):
        content = """\
<h3 id="1">[1] Test Paper</h3>
- **arXiv**: [2604.06189](https://arxiv.org/abs/2604.06189)
- **Authors**: Alice
- **Subjects**: cs.AI
- **Tags**: Old
- **Venue**: ICML 2025
- **Presentation**: Oral
- **Code**: [code](https://example.com)
- **Summary**: Old summary.

"""
        md_path = tmp_path / "test.md"
        md_path.write_text(content, encoding="utf-8")
        results = {"2604.06189": {"summary": "新摘要。", "tags": ["T"], "venue": "ICML 2026",
                                  "presentation": "Spotlight"}}
        arxiv_daily.patch_markdown(md_path, results, logger)
        lines = md_path.read_text(encoding="utf-8").splitlines()
        assert lines[5:9] == [
            "- **Venue**: ICML 2026", "- **Presentation**: Spotlight",
            "- **Code**: [code](https://example.com)", "- **Summary**: 新摘要。",
        ]

        results["2604.06189"]["presentation"] = None
        arxiv_daily.patch_markdown(md_path, results, logger)
        assert "**Presentation**" not in md_path.read_text(encoding="utf-8")

    def test_venue_removed_when_none(self, tmp_path, logger):
        """Existing Venue line should be removed when new result has venue=None."""
        content = """\
---
title: "test"
---

<h3 id="1">[1] Test Paper</h3>
- **arXiv**: [2604.06189](https://arxiv.org/abs/2604.06189)
- **Authors**: Alice
- **Subjects**: cs.AI
- **Tags**: Old
- **Venue**: ICML 2025
- **Summary**: Old summary.

"""
        md_path = tmp_path / "test.md"
        md_path.write_text(content, encoding="utf-8")
        results = {"2604.06189": {"summary": "New summary here.", "tags": ["T"], "venue": None}}
        arxiv_daily.patch_markdown(md_path, results, logger)
        patched_content = md_path.read_text(encoding="utf-8")
        assert "**Venue**" not in patched_content
        assert "- **Summary**: New summary here." in patched_content

    def test_code_line_preserved(self, tmp_path, logger):
        """Code line should survive patch."""
        content = """\
---
title: "test"
---

<h3 id="1">[1] Test Paper</h3>
- **arXiv**: [2604.06189](https://arxiv.org/abs/2604.06189)
- **Authors**: Alice
- **Subjects**: cs.AI
- **Tags**: Old
- **Code**: [code](https://github.com/example/repo)
- **Summary**: Old summary.

"""
        md_path = tmp_path / "test.md"
        md_path.write_text(content, encoding="utf-8")
        results = {"2604.06189": {"summary": "New summary.", "tags": ["New"], "venue": None}}
        arxiv_daily.patch_markdown(md_path, results, logger)
        patched_content = md_path.read_text(encoding="utf-8")
        assert "github.com/example/repo" in patched_content
        assert "- **Tags**: New" in patched_content

    def test_multi_paper_patch(self, tmp_path, logger):
        """Patch multiple papers in a single file."""
        content = """\
---
title: "test"
---

<h3 id="1">[1] Paper A</h3>
- **arXiv**: [2604.06189](https://arxiv.org/abs/2604.06189)
- **Authors**: Alice
- **Subjects**: cs.AI
- **Tags**: Old A
- **Summary**: Old A.

<h3 id="2">[2] Paper B</h3>
- **arXiv**: [2604.07001](https://arxiv.org/abs/2604.07001)
- **Authors**: Bob
- **Subjects**: cs.LG
- **Tags**: Old B
- **Summary**: Old B.

"""
        md_path = tmp_path / "test.md"
        md_path.write_text(content, encoding="utf-8")
        results = {
            "2604.06189": {"summary": "New A.", "tags": ["Tag A"], "venue": None},
            "2604.07001": {"summary": "New B.", "tags": ["Tag B"], "venue": "ACL 2026"},
        }
        patched = arxiv_daily.patch_markdown(md_path, results, logger)
        assert patched == 2
        patched_content = md_path.read_text(encoding="utf-8")
        assert "- **Summary**: New A." in patched_content
        assert "- **Summary**: New B." in patched_content
        assert "- **Venue**: ACL 2026" in patched_content

    def test_cross_listed_annotation_match(self, tmp_path, logger):
        """arXiv line with (cross-listed) suffix should still be matched."""
        content = """\
---
title: "test"
---

<h3 id="1">[1] Cross Paper</h3>
- **arXiv**: [2604.08123](https://arxiv.org/abs/2604.08123) (cross-listed)
- **Authors**: Dave
- **Subjects**: cs.RO; cs.AI
- **Tags**: Old
- **Summary**: Old summary.

"""
        md_path = tmp_path / "test.md"
        md_path.write_text(content, encoding="utf-8")
        results = {"2604.08123": {"summary": "New cross summary.", "tags": ["New Tag"], "venue": None}}
        patched = arxiv_daily.patch_markdown(md_path, results, logger)
        assert patched == 1
        patched_content = md_path.read_text(encoding="utf-8")
        assert "- **Summary**: New cross summary." in patched_content
        assert "(cross-listed)" in patched_content

    def test_replaced_annotation_match(self, tmp_path, logger):
        """arXiv line with (replaced) suffix should still be matched."""
        content = """\
---
title: "test"
---

<h3 id="1">[1] Replaced Paper</h3>
- **arXiv**: [2603.01234](https://arxiv.org/abs/2603.01234) (replaced)
- **Authors**: Frank
- **Subjects**: cs.CV; cs.AI
- **Tags**: Old
- **Summary**: Old summary.

"""
        md_path = tmp_path / "test.md"
        md_path.write_text(content, encoding="utf-8")
        results = {"2603.01234": {"summary": "New replaced summary.", "tags": ["New Tag"], "venue": None}}
        patched = arxiv_daily.patch_markdown(md_path, results, logger)
        assert patched == 1
        patched_content = md_path.read_text(encoding="utf-8")
        assert "- **Summary**: New replaced summary." in patched_content
        assert "(replaced)" in patched_content


# ===========================================================================
# TestRunPatchMode
# ===========================================================================

class TestRunPatchMode:
    def test_patch_mode_counts_retry_placeholders_as_failed(self, monkeypatch, tmp_path, logger):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        cache_base = tmp_path / "cache"
        pages_dir = cache_base / "pages" / "cs.AI"
        pages_dir.mkdir(parents=True)
        (pages_dir / "2025-04-11.html").write_text("<html>saved</html>", encoding="utf-8")
        md_path = output_dir / "2025-04-11-cs.AI.md"
        md_path.write_text(
            """\
<h3 id="1">[1] Test Paper</h3>
- **arXiv**: [2604.00001](https://arxiv.org/abs/2604.00001)
- **Authors**: Alice
- **Subjects**: cs.AI
- **Tags**: Old
- **Summary**: Old summary.
""",
            encoding="utf-8",
        )
        tags_path = tmp_path / "tags.json"
        tags_path.write_text('{"tags": []}', encoding="utf-8")
        args = type("Args", (), {})()
        args.date = "2025-04-11"
        args.field = "cs.AI"
        args.patch = ["2604.00001"]
        args.tags = str(tags_path)
        args.output = str(output_dir)

        monkeypatch.setattr(
            arxiv_daily,
            "parse_html",
            lambda html, field: {
                "sections": [{
                    "papers": [
                        {"id": "2604.00001", "title": "Test Paper", "abstract": "Abstract"},
                    ],
                }],
            },
        )
        monkeypatch.setattr(
            arxiv_daily,
            "run_phase2",
            lambda *args, **kwargs: (
                {
                    "2604.00001": {
                        "summary": arxiv_daily.SUMMARY_FAILURE_PLACEHOLDER,
                        "tags": [arxiv_daily.SUMMARY_FAILURE_TAG],
                        "venue": None,
                    },
                },
                1,
            ),
        )

        result = arxiv_daily.run_patch_mode(
            args,
            logger,
            cache_base,
            arxiv_daily.LlmConfig(
                api_key="test-key",
                api_endpoint="http://unused",
                model="test-model",
            ),
        )

        assert result.exit_code == 1
        assert result.summary["failed"] == 1
        assert arxiv_daily.SUMMARY_FAILURE_PLACEHOLDER in md_path.read_text(encoding="utf-8")


# ===========================================================================
# TestMainCli
# ===========================================================================

class TestMainCli:
    def test_missing_env_vars(self, monkeypatch):
        monkeypatch.delenv("ARXIV_UPDATE_KEY", raising=False)
        monkeypatch.delenv("ARXIV_UPDATE_API", raising=False)
        monkeypatch.delenv("ARXIV_UPDATE_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["arxiv_daily.py", "cs.AI", "--output", "/tmp/out", "--tags", "/tmp/tags.json"])

        assert arxiv_daily.main() == 2

    def test_main_passes_llm_config_to_normal_mode(self, monkeypatch, tmp_path):
        captured = {}
        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(tmp_path / "output"),
            "--tags", str(tmp_path / "tags.json"),
            "--cache-dir", str(tmp_path / "cache"),
        ])

        def fake_run_normal_mode(args, logger, cache_base, llm_config):
            captured["llm_config"] = llm_config
            return arxiv_daily.RunResult({"ok": True})

        monkeypatch.setattr(arxiv_daily, "run_normal_mode", fake_run_normal_mode)

        assert arxiv_daily.main() == 0

        assert captured["llm_config"] == arxiv_daily.LlmConfig(
            api_key="test-key",
            api_endpoint="http://127.0.0.1:1/v1",
            model="test-model",
        )

    def test_output_and_tags_default_to_site_repo(self, monkeypatch, tmp_path):
        captured = {}
        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--cache-dir", str(tmp_path / "cache"),
        ])

        def fake_run_normal_mode(args, logger, cache_base, llm_config):
            captured["args"] = args
            return arxiv_daily.RunResult({"ok": True})

        monkeypatch.setattr(arxiv_daily, "run_normal_mode", fake_run_normal_mode)

        assert arxiv_daily.main() == 0

        site_root = Path(arxiv_daily.__file__).resolve().parent.parent
        assert Path(captured["args"].output) == site_root / "_posts"
        assert Path(captured["args"].tags) == site_root / "data" / "tags.json"

    def test_main_passes_llm_config_to_patch_mode(self, monkeypatch, tmp_path):
        captured = {}
        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(tmp_path / "output"),
            "--tags", str(tmp_path / "tags.json"),
            "--date", "2025-04-11",
            "--patch", "2604.06189",
            "--cache-dir", str(tmp_path / "cache"),
        ])

        def fake_run_patch_mode(args, logger, cache_base, llm_config):
            captured["llm_config"] = llm_config
            return arxiv_daily.RunResult({"ok": True})

        monkeypatch.setattr(arxiv_daily, "run_patch_mode", fake_run_patch_mode)

        assert arxiv_daily.main() == 0

        assert captured["llm_config"] == arxiv_daily.LlmConfig(
            api_key="test-key",
            api_endpoint="http://127.0.0.1:1/v1",
            model="test-model",
        )

    def test_normal_run(self, monkeypatch, fake_llm_server, tmp_path, sample_listing_html, capsys):
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

        assert arxiv_daily.main() == 0

        # Verify markdown output
        md_files = list(output_dir.glob("*.md"))
        assert len(md_files) == 1
        assert "2025-04-11-cs.AI.md" in md_files[0].name

        # Verify stdout JSON summary
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["listing_date"] == "2025-04-11"
        assert summary["field"] == "cs.AI"
        assert summary["output_file"] == str(md_files[0])
        assert summary["total"] > 0
        assert summary["failed_batches"] == 0
        assert "sections" in summary
        log_text = (cache_dir / "arxiv_daily.log").read_text(encoding="utf-8")
        assert f"[Cache] Removing completed cache directory: {cache_dir / 'cs.AI'}" in log_text

    def test_date_mode_warns_when_overwriting_output(
        self, monkeypatch, tmp_path, sample_listing_html, capsys
    ):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        existing_output = output_dir / "2025-04-11-cs.AI.md"
        existing_output.write_text("old markdown", encoding="utf-8")
        tags_path = tmp_path / "tags.json"
        tags_path.write_text('{"tags": ["LLM Reasoning"]}', encoding="utf-8")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        pages_dir = cache_dir / "pages" / "cs.AI"
        pages_dir.mkdir(parents=True)
        (pages_dir / "2025-04-11.html").write_text(sample_listing_html, encoding="utf-8")

        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(output_dir),
            "--tags", str(tags_path),
            "--date", "2025-04-11",
            "--cache-dir", str(cache_dir),
        ])
        monkeypatch.setattr(
            arxiv_daily,
            "run_phase2",
            lambda *args, **kwargs: ({
                "2604.06189": {
                    "summary": "这是一篇用于测试覆盖输出覆盖提示的摘要，内容足够长。",
                    "tags": ["LLM Reasoning"],
                    "venue": None,
                },
                "2604.07001": {
                    "summary": "这是一篇用于测试覆盖输出覆盖提示的摘要，内容足够长。",
                    "tags": ["Diffusion Models"],
                    "venue": None,
                },
                "2604.08123": {
                    "summary": "这是一篇用于测试覆盖输出覆盖提示的摘要，内容足够长。",
                    "tags": ["Transfer Learning"],
                    "venue": None,
                },
                "2603.01234": {
                    "summary": "这是一篇用于测试覆盖输出覆盖提示的摘要，内容足够长。",
                    "tags": ["Vision Transformer"],
                    "venue": None,
                },
            }, 0),
        )
        monkeypatch.setattr(
            arxiv_daily,
            "_atomic_write_text",
            lambda *args, **kwargs: pytest.fail("existing HTML archive should not be rewritten"),
        )

        assert arxiv_daily.main() == 0
        captured = capsys.readouterr()
        assert "--date mode overwriting existing output" in captured.err
        log_text = (cache_dir / "arxiv_daily.log").read_text(encoding="utf-8")
        assert "--date mode overwriting existing output" in log_text

    def test_date_html_missing(self, monkeypatch, tmp_path):
        """--date with no saved HTML should exit with code 2."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(tmp_path / "output"),
            "--tags", str(tmp_path / "tags.json"),
            "--date", "2099-01-01",
            "--cache-dir", str(cache_dir),
        ])
        assert arxiv_daily.main() == 2

    def test_date_mismatch(self, monkeypatch, tmp_path, sample_listing_html):
        """Page date != --date should exit with code 2."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        pages_dir = cache_dir / "pages" / "cs.AI"
        pages_dir.mkdir(parents=True)
        # sample_listing_html contains date 2025-04-11, but we pass --date 2025-04-12
        (pages_dir / "2025-04-12.html").write_text(sample_listing_html, encoding="utf-8")

        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(tmp_path / "output"),
            "--tags", str(tmp_path / "tags.json"),
            "--date", "2025-04-12",
            "--cache-dir", str(cache_dir),
        ])
        assert arxiv_daily.main() == 2

    def test_patch_without_date(self, monkeypatch, tmp_path):
        """--patch without --date should exit with code 2."""
        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(tmp_path / "output"),
            "--tags", str(tmp_path / "tags.json"),
            "--patch", "2604.06189",
            "--cache-dir", str(tmp_path / "cache"),
        ])
        assert arxiv_daily.main() == 2

    def test_patch_warns_for_missing_abstract(self, monkeypatch, tmp_path, capsys):
        """Patch mode should diagnose parsed target papers without abstracts."""
        cache_dir = tmp_path / "cache"
        pages_dir = cache_dir / "pages" / "cs.AI"
        pages_dir.mkdir(parents=True)
        (pages_dir / "2025-04-11.html").write_text("<html></html>", encoding="utf-8")

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        md_path = output_dir / "2025-04-11-cs.AI.md"
        md_path.write_text("existing", encoding="utf-8")
        tags_path = tmp_path / "tags.json"
        tags_path.write_text('{"tags": []}', encoding="utf-8")

        monkeypatch.setenv("ARXIV_UPDATE_KEY", "test-key")
        monkeypatch.setenv("ARXIV_UPDATE_API", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("ARXIV_UPDATE_MODEL", "test-model")
        monkeypatch.setattr("sys.argv", [
            "arxiv_daily.py", "cs.AI",
            "--output", str(output_dir),
            "--tags", str(tags_path),
            "--date", "2025-04-11",
            "--patch", "2604.06189",
            "--cache-dir", str(cache_dir),
        ])
        monkeypatch.setattr(
            arxiv_daily,
            "parse_html",
            lambda html, field: {
                "field": field,
                "listing_date": "2025-04-11",
                "page_type": "new",
                "sections": [{
                    "name": "New submissions",
                    "count": 1,
                    "papers": [{"id": "2604.06189", "title": "Paper without abstract"}],
                }],
            },
        )
        monkeypatch.setattr(
            arxiv_daily,
            "run_phase2",
            lambda *args, **kwargs: ({
                "2604.06189": {
                    "summary": "新的摘要内容",
                    "tags": ["T"],
                    "venue": None,
                },
            }, 0),
        )
        monkeypatch.setattr(arxiv_daily, "patch_markdown", lambda *args, **kwargs: 1)

        assert arxiv_daily.main() == 0
        captured = capsys.readouterr()
        assert "missing abstract: 2604.06189" in captured.err
        log_text = (cache_dir / "arxiv_daily.log").read_text(encoding="utf-8")
        assert "[Patch] 1 papers missing abstract: ['2604.06189']" in log_text


# ===========================================================================
# TestHasAcceptanceSignal
# ===========================================================================

class TestHasAcceptanceSignal:
    @pytest.mark.parametrize("comments,expected", [
        ("Accepted at OccuSys 2026", True),
        ("Accepted for publication in AI and Ethics", True),
        ("published in Advancements in AI..., Springer, 2026", True),
        ("To appear in NeurIPS 2026", True),
        ("Camera-ready version", True),
        ("camera ready submitted", True),
        ("Appearing at AAAI 2026", True),
        ("Presented at ICML 2026", True),
        ("Forthcoming in JMLR", True),
        ("To be presented at ACL 2026", True),
        ("7 pages", False),
        ("Submitted to ICML 2026", False),
        ("ICML 2026 style, 9 pages", False),
        ("Codes are available", False),
        ("Under review at NeurIPS", False),
        (None, False),
        ("", False),
        ("Best Technical Paper Award at Intelligent Data Analysis (IDA) 2026", True),
        ("ACL 2026", True),
        ("9 pages. Accpted for Publication in IJCAI 26", True),
        ("MemFM @ ICML 2026", True),
        ("Spotlight, NeurIPS 2026", True),
        ("In Proceedings ICLP 2026", True),
        ("Uses the NeurIPS 2026 LaTeX template", False),
        ("ICLR 2026 submission", False),
        ("2026 IEEE International Geoscience and Remote Sensing Symposium (IGARSS)", True),
        ("14 pages, 8 figures, Acccepted to Transactions on Games", True),
        ("18 pages, 8 figures; cvpr2026 paper", True),
        ("Proc. 2026 International Congress of Mathematicians", True),
        ("Bayesian posterior sampling, 7 figures", False),
        ("ACL Findings", True),
        ("IJCNN Full Paper", True),
        ("Nature Biomedical Engineering (2026)", True),
    ])
    def test_acceptance_signal(self, comments, expected):
        assert arxiv_daily.has_acceptance_signal(comments) is expected


# ===========================================================================
# TestBuildVenuePrompt
# ===========================================================================

class TestBuildVenuePrompt:
    def test_only_contains_id_and_comments(self):
        papers = [
            {"id": "001", "title": "My Title", "abstract": "My Abstract", "comments": "Accepted at ICML 2026"},
        ]
        msgs = arxiv_daily.build_venue_prompt(papers)
        user_content = msgs[1]["content"]
        assert "001" in user_content
        assert "Accepted at ICML 2026" in user_content
        assert "My Title" not in user_content
        assert "My Abstract" not in user_content

    def test_returns_system_and_user_messages(self):
        papers = [{"id": "x", "comments": "Accepted at X"}]
        msgs = arxiv_daily.build_venue_prompt(papers)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_multiple_papers(self):
        papers = [
            {"id": "a", "comments": "Accepted at A"},
            {"id": "b", "comments": "Published in B"},
        ]
        msgs = arxiv_daily.build_venue_prompt(papers)
        content = msgs[1]["content"]
        assert "ID: a" in content
        assert "ID: b" in content


# ===========================================================================
# TestVenueGroundingCheck
# ===========================================================================

class TestVenueGroundingCheck:
    def test_matching_token(self):
        assert arxiv_daily._venue_grounding_check("ICML 2026", "Accepted at ICML 2026") is True

    def test_no_matching_token(self):
        assert arxiv_daily._venue_grounding_check("NeurIPS 2025", "Accepted at ICML 2026") is False

    def test_case_insensitive(self):
        assert arxiv_daily._venue_grounding_check("icml", "Accepted at ICML") is True

    def test_no_core_token_fails(self):
        # All stopwords or pure digits should fail (no core venue token).
        assert arxiv_daily._venue_grounding_check("The", "Accepted at ICML") is False
        assert arxiv_daily._venue_grounding_check("2026", "Accepted at ICML 2026") is False

    def test_partial_token_match(self):
        assert arxiv_daily._venue_grounding_check("AI-DEEDS 2026", "AI-DEEDS '26 workshop") is True

    def test_year_format_difference(self):
        assert arxiv_daily._venue_grounding_check("AAAI 2026", "AAAI conference") is True

    def test_year_only_match_rejected(self):
        assert arxiv_daily._venue_grounding_check("NeurIPS 2026", "Accepted at ICML 2026") is False

    def test_exact_venue_match(self):
        assert arxiv_daily._venue_grounding_check("ICML 2026", "Accepted at ICML 2026") is True

    def test_venue_without_year_in_comments(self):
        assert arxiv_daily._venue_grounding_check("AAAI 2026", "Accepted at AAAI conference") is True

    def test_journal_venue_match(self):
        assert arxiv_daily._venue_grounding_check(
            "Journal of AI Research 2026",
            "Accepted for publication in Journal of AI Research",
        ) is True

    def test_substring_match_rejected(self):
        # "AI" should not be grounded by "available" (token-level, not substring).
        assert arxiv_daily._venue_grounding_check(
            "AI 2026",
            "Accepted at Workshop 2026; code available",
        ) is False


# ===========================================================================
# TestCallVenueLlm
# ===========================================================================

class TestCallVenueLlm:
    def _make_http_response(self, content_json: str):
        """Create a mock HTTP response object for call_venue_llm."""
        response_body = json.dumps({
            "choices": [{"message": {"content": content_json}}]
        }).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_valid_response(self, logger):
        content = json.dumps({"venues": [{"id": "001", "venue": "ICML 2026"}]})
        mock_resp = self._make_http_response(content)
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = arxiv_daily.call_venue_llm(messages, "http://fake/v1", "key", "model", logger)
        assert result is not None
        assert result["venues"][0]["venue"] == "ICML 2026"

    def test_invalid_schema_returns_none(self, logger):
        content = json.dumps({"bad": "schema"})
        mock_resp = self._make_http_response(content)
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = arxiv_daily.call_venue_llm(messages, "http://fake/v1", "key", "model", logger)
        assert result is None

    def test_malformed_entry_returns_none(self, logger):
        content = json.dumps({"venues": [{"no_id": "x", "no_venue": "y"}]})
        mock_resp = self._make_http_response(content)
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = arxiv_daily.call_venue_llm(messages, "http://fake/v1", "key", "model", logger)
        assert result is None

    def test_debug_dir_on_failure(self, tmp_path, logger):
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = arxiv_daily.call_venue_llm(
                messages, "http://fake/v1", "key", "model", logger,
                debug_dir=tmp_path,
            )
        assert result is None
        assert (tmp_path / "venue_error.json").exists()

    def test_debug_label_overrides_filename(self, tmp_path, logger):
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            arxiv_daily.call_venue_llm(
                messages, "http://fake/v1", "key", "model", logger,
                debug_dir=tmp_path, debug_label="venue_batch_005_error",
            )
        assert (tmp_path / "venue_batch_005_error.json").exists()
        assert not (tmp_path / "venue_error.json").exists()

    def test_http_error_body_captured(self, tmp_path, logger):
        import urllib.error
        import io
        err = urllib.error.HTTPError(
            "url", 500, "Server Error", {},
            io.BytesIO(b'{"error": "internal"}'),
        )
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.urlopen", side_effect=err):
            arxiv_daily.call_venue_llm(
                messages, "http://fake/v1", "key", "model", logger,
                debug_dir=tmp_path, debug_label="batch_test",
            )
        debug_file = tmp_path / "batch_test.json"
        assert debug_file.exists()
        data = json.loads(debug_file.read_text())
        assert data["http_body"] is not None
        assert "internal" in data["http_body"]

    def test_non_429_4xx_no_retry(self, logger):
        import urllib.error
        err = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.urlopen", side_effect=err) as mock_url:
            result = arxiv_daily.call_venue_llm(messages, "http://fake/v1", "key", "model", logger)
        assert result is None
        assert mock_url.call_count == 1


# ===========================================================================
# TestExtractVenuesBatch
# ===========================================================================

class TestExtractVenuesBatch:
    def test_gate_filters_papers(self, logger):
        papers = [
            {"id": "001", "comments": "Accepted at ICML 2026"},
            {"id": "002", "comments": "7 pages"},
            {"id": "003", "comments": None},
        ]
        mock_resp = {"venues": [{"id": "001", "venue": "ICML 2026"}]}
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp) as mock_llm:
            result = arxiv_daily.extract_venues_batch(papers, "x", "k", "m", logger)
            assert mock_llm.call_count == 1
            assert result["001"] == "ICML 2026"
            assert result["002"] is None
            assert result["003"] is None

    def test_grounding_failure_sets_none(self, logger):
        papers = [{"id": "001", "comments": "Accepted at ICML 2026"}]
        mock_resp = {"venues": [{"id": "001", "venue": "NeurIPS 2025"}]}
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp):
            result = arxiv_daily.extract_venues_batch(papers, "x", "k", "m", logger)
            assert result["001"] is None

    def test_long_venue_warning_no_truncation(self, logger):
        long_venue = "International Conference on Very Long Venue Names for Testing Purposes 2026"
        papers = [{"id": "001", "comments": f"Accepted at {long_venue}"}]
        mock_resp = {"venues": [{"id": "001", "venue": long_venue}]}
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp):
            result = arxiv_daily.extract_venues_batch(papers, "x", "k", "m", logger)
            assert result["001"] == long_venue
            assert len(long_venue) > 60

    def test_api_failure_does_not_block(self, logger):
        papers = [{"id": "001", "comments": "Accepted at ICML 2026"}]
        with patch.object(arxiv_daily, "call_venue_llm", return_value=None):
            result = arxiv_daily.extract_venues_batch(papers, "x", "k", "m", logger)
            assert result["001"] is None

    def test_cache_hit_skips_llm(self, tmp_path, logger):
        papers = [
            {"id": "001", "comments": "Accepted at ICML 2026"},
            {"id": "002", "comments": "7 pages"},
        ]
        cache_data = {"metadata_hash": "h1", "venues": {"001": "ICML 2026"}}
        (tmp_path / "venue_cache.json").write_text(json.dumps(cache_data))
        with patch.object(arxiv_daily, "call_venue_llm") as mock_llm:
            result = arxiv_daily.extract_venues_batch(
                papers, "x", "k", "m", logger, field_cache=tmp_path, metadata_hash="h1"
            )
            assert mock_llm.call_count == 0
            assert result["001"] == "ICML 2026"
            assert result["002"] is None

    def test_cache_mismatch_discards(self, tmp_path, logger):
        papers = [{"id": "001", "comments": "Accepted at ICML 2026"}]
        cache_data = {"metadata_hash": "old", "venues": {"001": "OLD"}}
        (tmp_path / "venue_cache.json").write_text(json.dumps(cache_data))
        mock_resp = {"venues": [{"id": "001", "venue": "ICML 2026"}]}
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp) as mock_llm:
            result = arxiv_daily.extract_venues_batch(
                papers, "x", "k", "m", logger, field_cache=tmp_path, metadata_hash="new"
            )
            assert mock_llm.call_count == 1
            assert result["001"] == "ICML 2026"

    def test_no_cache_when_field_cache_none(self, logger):
        papers = [{"id": "001", "comments": "Accepted at ICML 2026"}]
        mock_resp = {"venues": [{"id": "001", "venue": "ICML 2026"}]}
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp):
            result = arxiv_daily.extract_venues_batch(
                papers, "x", "k", "m", logger, field_cache=None, metadata_hash=None
            )
            assert result["001"] == "ICML 2026"

    def test_atomic_write_after_batch(self, tmp_path, logger):
        papers = [{"id": "001", "comments": "Accepted at ICML 2026"}]
        mock_resp = {"venues": [{"id": "001", "venue": "ICML 2026"}]}
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp):
            arxiv_daily.extract_venues_batch(
                papers, "x", "k", "m", logger, field_cache=tmp_path, metadata_hash="h"
            )
            cache_file = tmp_path / "venue_cache.json"
            assert cache_file.exists()
            data = json.loads(cache_file.read_text())
            assert data["metadata_hash"] == "h"
            assert data["format"] == 2
            assert data["venues"]["001"] == {"venue": "ICML 2026", "presentation": None}


# ===========================================================================
# TestVenueFormatting
# ===========================================================================

def _table(aliases=None, full_names=None):
    return {"aliases": dict(aliases or {}), "full_names": dict(full_names or {})}


class TestVenueFormatting:
    def test_name_key_drops_years_ordinals_and_parentheses(self):
        key = arxiv_daily.venue_name_key("The 43rd International Conference on Machine Learning (ICML 2026)")
        assert key == "international conference on machine learning"
        assert arxiv_daily.venue_name_key("Proceedings of the 2026 Annual Meeting of the ACL") == "meeting of acl"

    @pytest.mark.parametrize("item,comments,expected", [
        ({"venue": "ICML", "year": "2026", "category": "main"},
         "Accepted at ICML 2026", "ICML 2026"),
        ({"venue": "ACL", "year": "2026", "category": "findings"},
         "Findings of ACL 2026", "ACL 2026 Findings"),
        ({"venue": "Findings of EMNLP 2026"}, "Findings of EMNLP 2026", "EMNLP 2026 Findings"),
        ({"venue": "EMNLP 2026 Main Conference"}, "EMNLP 2026, Main Conference", "EMNLP 2026"),
        ({"venue": "NeurIPS", "year": "2026", "category": "track", "track": "Datasets and Benchmarks Track"},
         "NeurIPS 2026 Datasets and Benchmarks Track", "NeurIPS 2026 D&B"),
        ({"venue": "KDD", "year": "2026", "category": "track", "track": "Applied Research"},
         "KDD 2026 Applied Research track", "KDD 2026 Applied Research"),
        ({"venue": "NeurIPS", "year": "2025", "category": "workshop", "workshop": "MATH-AI Workshop"},
         "MATH-AI Workshop at NeurIPS 2025", "NeurIPS 2025 MATH-AI Workshop"),
        ({"venue": "ICLR", "year": "2026", "category": "workshop", "workshop": "LRLLM"},
         "ICLR 2026 Workshop on Logical Reasoning of Large Language Models", "ICLR 2026 Workshop"),
        ({"venue": "ICML", "year": "2026"}, "Accepted at ICML", "ICML"),
        ({"venue": "IJCAI", "year": "2026"}, "Accpted for Publication in IJCAI 26", "IJCAI 2026"),
        ({"venue": "Bioinformatics"}, "Published in Bioinformatics", "Bioinformatics"),
        ({"venue": None, "full_name": "34th IEEE Signal Processing and Communications Applications Conference"},
         "34th IEEE Signal Processing and Communications Applications Conference",
         "IEEE Signal Processing and Communications Applications Conference"),
    ])
    def test_formats_short_venue(self, item, comments, expected):
        venue, _ = arxiv_daily.format_venue_entry(item, comments, _table())
        assert venue == expected

    @pytest.mark.parametrize("item,comments,expected", [
        ({"venue": "CVPR", "year": "2026", "category": "workshop", "workshop": "Sight and Sound"},
         "accepted to CVPR 2026 Workshop on Sight and Sound", "CVPR 2026 Workshop"),
        ({"venue": "ICML", "year": "2026", "category": "workshop", "workshop": "Pluralistic Alignment"},
         "Accepted at the ICML 2026 Workshop on Pluralistic Alignment", "ICML 2026 Workshop"),
        ({"venue": "NeurIPS", "year": "2026", "category": "workshop", "workshop": "GenAI4Health"},
         "Accepted to the NeurIPS 2026 GenAI4Health Workshop", "NeurIPS 2026 GenAI4Health Workshop"),
        ({"venue": "FAccT", "year": "2026", "category": "track", "track": "non-archival"},
         "presented at ACM FAccT 2026 (non-archival)", "FAccT 2026"),
        ({"venue": "SIGKDD", "year": "2026", "category": "track", "track": "AI for Science"},
         "Accepted by SIGKDD2026 AI for Science Track", "KDD 2026 AI for Science"),
    ])
    def test_only_acronym_workshops_and_named_tracks_are_kept(self, item, comments, expected):
        venue, _ = arxiv_daily.format_venue_entry(item, comments, _table(aliases={"sigkdd": "KDD"}))
        assert venue == expected

    @pytest.mark.parametrize("item,comments,expected", [
        ({"venue": "ITS", "year": "2026"}, "Accepted for ITS 2026", "ITS 2026"),
        ({"venue": "A&A"}, "21 pages, 17 figures; accepted in A&A", "A&A"),
        ({"venue": "NeurIPS", "year": "2025", "category": "track", "track": "D&B"},
         "NeuIPS 2025 (Datasets and Benchmarks Track)", "NeurIPS 2025 D&B"),
        ({"venue": None, "year": "2026", "category": "workshop", "workshop": "TPDP"},
         "Accepted to the Theory and Practice of Differential Privacy Workshop (TPDP 2026)", "TPDP 2026 Workshop"),
        ({"venue": "CLPsych", "year": "2026", "category": "workshop", "workshop": "CLPsych"},
         "Accepted at the CLPsych 2026 workshop", "CLPsych 2026 Workshop"),
    ])
    def test_resolve_grounding_fallbacks_and_standalone_workshops(self, item, comments, expected):
        info = arxiv_daily.resolve_venue_item(item, comments, _table(aliases={"neuips": "NeurIPS"}))
        assert info["venue"] == expected

    @pytest.mark.parametrize("item,comments,expected", [
        ({"venue": "EMNLP", "year": "2026", "category": "findings"}, "Accepted to ENMLP 2026 Findings",
         "EMNLP 2026 Findings"),
        ({"venue": "NeurIPS", "year": "2026"}, "Accepted by NeuraIPS 2026", "NeurIPS 2026"),
        ({"venue": "ASP-DAC", "year": "2027"}, "This work is accepted to ASPDAC'27", "ASP-DAC 2027"),
        ({"venue": "ICASSP", "year": "2026"}, "Accepted at ICAPS 2026", None),
    ])
    def test_resolve_tolerates_one_typo_in_long_names(self, item, comments, expected):
        assert arxiv_daily.resolve_venue_item(item, comments, _table())["venue"] == expected

    def test_learned_abbreviations_act_as_aliases(self):
        table = _table(full_names={"acm conference on ai and agentic systems": {"abbr": "CAIS"}})
        venue, _ = arxiv_daily.format_venue_entry(
            {"venue": "ACM CAIS", "year": "2026", "category": "track", "track": "Demo"}, "ACM CAIS 2026 (Demo)", table,
        )
        assert venue == "CAIS 2026 Demo"

    @pytest.mark.parametrize("item,comments,expected", [
        ({"venue": "Graph Signal Processing Workshop", "year": "2026", "category": "workshop"},
         "Accepted by Graph Signal Processing Workshop 2026", "Graph Signal Processing 2026 Workshop"),
        ({"venue": "IEEE Globecom Workshops", "year": "2026", "category": "workshop"},
         "Accepted at IEEE Globecom Workshops 2026", "IEEE Globecom 2026 Workshop"),
        ({"full_name": "International Workshop on Nonmonotonic Reasoning", "year": "2026", "category": "workshop"},
         "Presented at the International Workshop on Nonmonotonic Reasoning 2026",
         "International Workshop on Nonmonotonic Reasoning 2026"),
    ])
    def test_workshop_given_as_venue_is_not_doubled(self, item, comments, expected):
        venue, _ = arxiv_daily.format_venue_entry(item, comments, _table())
        assert venue == expected

    def test_full_name_result_follows_aliases(self):
        table = _table(aliases={"cog": "IEEE CoG"}, full_names={"ieee conference on games": {"abbr": "CoG"}})
        venue, _ = arxiv_daily.format_venue_entry(
            {"venue": "CoG", "full_name": "IEEE Conference on Games", "year": "2026"},
            "Accepted at the IEEE Conference on Games 2026", table,
        )
        assert venue == "IEEE CoG 2026"

    def test_standalone_workshop_names(self):
        named = arxiv_daily.resolve_venue_item(
            {"year": "2026", "category": "workshop", "workshop": "World Modeling Workshop", "presentation": "Poster"},
            "Accepted as a poster at the 2026 World Modeling Workshop", _table(),
        )
        assert named == {"venue": "World Modeling 2026 Workshop", "presentation": "Poster"}
        long_name = "Beyond Private Training: The New Landscape of AI Privacy"
        assert arxiv_daily.resolve_venue_item(
            {"category": "workshop", "workshop": long_name}, f'accepted at "{long_name}" workshop', _table(),
        )["venue"] is None

    def test_resolve_rejects_ungrounded_venue(self):
        info = arxiv_daily.resolve_venue_item({"venue": "NeurIPS", "year": "2026"}, "Accepted at ICML 2026", _table())
        assert info == {"venue": None, "presentation": None}

    def test_grounding_accepts_venue_glued_to_year(self):
        assert arxiv_daily._venue_grounding_check("SIGKDD", "Accepted by SIGKDD2026 AI for Science Track")
        assert not arxiv_daily._venue_grounding_check("AI", "Code is available online")

    def test_table_full_name_wins_over_model_abbreviation(self):
        table = _table(full_names={"international conference on machine learning": {"abbr": "ICML"}})
        item = {"venue": "PMLR-ICML", "full_name": "International Conference on Machine Learning", "year": "2026"}
        venue, _ = arxiv_daily.format_venue_entry(
            item, "43rd International Conference on Machine Learning, 2026", table,
        )
        assert venue == "ICML 2026"

    def test_alias_normalizes_short_name(self):
        venue, _ = arxiv_daily.format_venue_entry(
            {"venue": "NIPS", "year": "2026"}, "NIPS 2026", _table(aliases={"nips": "NeurIPS"}),
        )
        assert venue == "NeurIPS 2026"

    @pytest.mark.parametrize("short,expected", [
        ("ACM FAccT", "FAccT 2026"), ("IEEE/CVF CVPR", "CVPR 2026"),
        ("IEEE S&P", "IEEE S&P 2026"), ("IEEE CAI", "IEEE CAI 2026"),
    ])
    def test_publisher_prefix_dropped_only_for_known_venues(self, short, expected):
        table = _table(aliases={"facct": "FAccT", "cvpr": "CVPR", "ieee s&p": "IEEE S&P", "s&p": "IEEE S&P"})
        venue, _ = arxiv_daily.format_venue_entry({"venue": short, "year": "2026"}, f"{short} 2026", table)
        assert venue == expected

    def test_new_full_names_are_recorded_and_flagged_when_unseen(self):
        table = _table()
        arxiv_daily.format_venue_entry(
            {"venue": "ACIT", "full_name": "International Conference on Advanced Computer Information Technologies",
             "year": "2026"},
            "16th International Conference on Advanced Computer Information Technologies (ACIT 2026)", table,
        )
        arxiv_daily.format_venue_entry(
            {"venue": "IEEE IoT-J", "full_name": "IEEE Internet of Things Journal"},
            "Accepted by IEEE Internet of Things Journal", table,
        )
        assert table["full_names"]["international conference on advanced computer information technologies"] == {
            "abbr": "ACIT"}
        assert table["full_names"]["ieee internet of things journal"] == {"abbr": "IEEE IoT-J", "review": True}

    @pytest.mark.parametrize("item,comments,expected", [
        ({"venue": "ICML", "year": "2026", "presentation": "Spotlight"}, "ICML 2026 (spotlight)", "Spotlight"),
        ({"venue": "ICML", "year": "2026", "presentation": "Oral"}, "Accepted at ICML 2026", None),
        ({"venue": "ICML", "year": "2026", "presentation": "Keynote"}, "ICML 2026 keynote", None),
        ({"venue": "ICML 2026 (Oral)"}, "Oral presentation at ICML 2026", "Oral"),
    ])
    def test_presentation_must_be_grounded(self, item, comments, expected):
        venue, presentation = arxiv_daily.format_venue_entry(item, comments, _table())
        assert venue == "ICML 2026"
        assert presentation == expected

    def test_abbrev_table_roundtrip_is_sorted(self, tmp_path):
        path = tmp_path / "venue_abbreviations.json"
        table = _table(aliases={"nips": "NeurIPS", "acl": "ACL"},
                       full_names={"z conf": {"abbr": "Z"}, "a conf": {"abbr": "A", "review": True}})
        table["description"] = "test"
        arxiv_daily.save_venue_abbrevs(path, table)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert list(data["aliases"]) == ["acl", "nips"]
        assert list(data["full_names"]) == ["a conf", "z conf"]
        assert arxiv_daily.load_venue_abbrevs(path) == table
        assert arxiv_daily.load_venue_abbrevs(tmp_path / "missing.json") == _table()

    def test_extract_info_returns_presentation_and_fills_table(self, logger):
        papers = [{"id": "001", "comments": "Spotlight at the International Conference on Robot Learning (CoRL 2026)"}]
        mock_resp = {"venues": [{
            "id": "001", "venue": "CoRL", "full_name": "International Conference on Robot Learning",
            "year": "2026", "category": "main", "track": None, "workshop": None, "presentation": "Spotlight",
        }]}
        table = _table()
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp):
            infos = arxiv_daily.extract_venue_info_batch(papers, "x", "k", "m", logger, abbrev_table=table)
        assert infos["001"] == {"venue": "CoRL 2026", "presentation": "Spotlight"}
        assert table["full_names"] == {"international conference on robot learning": {"abbr": "CoRL"}}

    def test_apply_saves_table_only_when_entries_are_added(self, tmp_path, logger):
        path = tmp_path / "venue_abbreviations.json"
        arxiv_daily.save_venue_abbrevs(path, _table(aliases={"corl": "CoRL"}))
        before = path.read_text(encoding="utf-8")
        papers = [{"id": "001", "comments": "Accepted at CoRL 2026"}]
        results = {"001": {"summary": "s", "tags": ["T"]}}
        config = arxiv_daily.LlmConfig(api_key="k", api_endpoint="x", model="m")
        with patch.object(arxiv_daily, "call_venue_llm",
                          return_value={"venues": [{"id": "001", "venue": "corl", "year": "2026"}]}):
            arxiv_daily.apply_venue_extraction(papers, results, config, logger, path)
        assert results["001"]["venue"] == "CoRL 2026" and results["001"]["presentation"] is None
        assert path.read_text(encoding="utf-8") == before, "no new full name, so nothing is written"

        with patch.object(arxiv_daily, "call_venue_llm", return_value={"venues": [{
            "id": "001", "venue": "CoRL", "full_name": "Conference on Robot Learning", "year": "2026",
        }]}):
            arxiv_daily.apply_venue_extraction(papers, results, config, logger, path)
        assert json.loads(path.read_text(encoding="utf-8"))["full_names"] == {
            "conference on robot learning": {"abbr": "CoRL"}}

    def test_apply_without_path_writes_nothing(self, tmp_path, logger, monkeypatch):
        monkeypatch.chdir(tmp_path)
        default = arxiv_daily.DEFAULT_VENUE_ABBREVS_PATH
        default_before = default.read_bytes() if default.exists() else None
        papers = [{"id": "001", "comments": "Accepted at the Conference on Robot Learning (CoRL 2026)"}]
        results = {"001": {"summary": "s", "tags": ["T"]}}
        config = arxiv_daily.LlmConfig(api_key="k", api_endpoint="x", model="m")
        with patch.object(arxiv_daily, "call_venue_llm", return_value={"venues": [{
            "id": "001", "venue": "CoRL", "full_name": "Conference on Robot Learning", "year": "2026",
        }]}):
            arxiv_daily.apply_venue_extraction(papers, results, config, logger, None)
        assert results["001"]["venue"] == "CoRL 2026"
        assert list(tmp_path.glob("*.json")) == []
        assert (default.read_bytes() if default.exists() else None) == default_before


# ===========================================================================
# TestBuildPromptNoVenue
# ===========================================================================

class TestBuildPromptNoVenue:
    def test_no_venue_instruction_in_prompt(self):
        papers = [{"id": "001", "title": "T", "abstract": "A", "comments": "Accepted at ICML"}]
        msgs = arxiv_daily.build_prompt(papers, ["LLM"])
        content = msgs[1]["content"]
        assert "venue" not in content.lower()

    def test_comments_still_passed(self):
        papers = [{"id": "001", "title": "T", "abstract": "A", "comments": "Some comments"}]
        msgs = arxiv_daily.build_prompt(papers, ["LLM"])
        content = msgs[1]["content"]
        assert "Comments: Some comments" in content

    def test_summary_and_tags_instructions_present(self):
        papers = [{"id": "001", "title": "T", "abstract": "A"}]
        msgs = arxiv_daily.build_prompt(papers, ["LLM"])
        content = msgs[1]["content"]
        assert "summary" in content.lower()
        assert "tags" in content.lower()


# ===========================================================================
# TestVenueIntegrationMerge
# ===========================================================================

class TestVenueIntegrationMerge:
    def test_merge_only_updates_existing_keys(self, logger):
        results_by_id = {
            "001": {"summary": "s1", "tags": ["T"]},
            "002": {"summary": "s2", "tags": ["T"]},
        }
        papers = [
            {"id": "001", "comments": "Accepted at ICML 2026"},
            {"id": "002", "comments": "7 pages"},
            {"id": "003", "comments": "Accepted at NeurIPS"},
        ]
        mock_resp = {"venues": [
            {"id": "001", "venue": "ICML 2026"},
            {"id": "003", "venue": "NeurIPS"},
        ]}
        with patch.object(arxiv_daily, "call_venue_llm", return_value=mock_resp):
            venues = arxiv_daily.extract_venues_batch(papers, "x", "k", "m", logger)
            for pid, venue in venues.items():
                if pid in results_by_id:
                    results_by_id[pid]["venue"] = venue
        assert results_by_id["001"]["venue"] == "ICML 2026"
        assert results_by_id["002"]["venue"] is None
        assert "003" not in results_by_id

    def test_venue_failure_does_not_crash_integration(self, logger):
        results_by_id = {"001": {"summary": "s1", "tags": ["T"]}}
        papers = [{"id": "001", "comments": "Accepted at ICML"}]
        with patch.object(arxiv_daily, "call_venue_llm", side_effect=Exception("boom")):
            try:
                venues = arxiv_daily.extract_venues_batch(papers, "x", "k", "m", logger)
                for pid, venue in venues.items():
                    if pid in results_by_id:
                        results_by_id[pid]["venue"] = venue
            except Exception:
                pass
        assert "venue" not in results_by_id["001"] or results_by_id["001"].get("venue") is None
