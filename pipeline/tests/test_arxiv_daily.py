"""Tests for arxiv_daily.py."""
import json
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

        assert "2604.00001" not in results
        assert diagnostics.duplicate_ids == ["2604.00001"]
        assert diagnostics.missing_ids == []
        assert diagnostics.quality_failed == []
        assert diagnostics.total_failed == 1
        assert diagnostics.all_failed_ids == ["2604.00001"]

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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
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
                        for paper in papers[:14]
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
        assert [paper["id"] for paper in processed.unresolved] == ["2604.00015", "2604.00016"]
        cached = json.loads((field_cache / "batch_001.json").read_text(encoding="utf-8"))
        assert [paper["id"] for paper in cached["papers"]] == [paper["id"] for paper in papers[:14]]

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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
            return {
                "papers": [
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
        assert prompt_batches == [["2604.00002"]]
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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
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
        assert prompt_batches == [["2604.00001", "2604.00002"], ["2604.00002"]]
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

        expected_retry = [paper["id"] for paper in papers[arxiv_daily.BATCH_SIZE - 4 : arxiv_daily.BATCH_SIZE + 4]]
        assert failed == 0
        assert retry_batches == [expected_retry]
        assert set(results) == set(expected_retry)

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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
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
        assert prompt_batches == [["2604.00001", "2604.00002"], ["2604.00002"]]

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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
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

        def fake_call_llm(messages, api_endpoint, api_key, model, logger, batch_num=0, debug_dir=None, batch_label=None):
            if len(prompt_batches) == 1:
                return {
                    "papers": [
                        {"id": "2604.00001", "summary": long_summary, "tags": ["T1"], "venue": None},
                    ],
                    "new_tags": [],
                }, {}
            return {
                "papers": [
                    {"id": "2604.00002", "summary": long_summary, "tags": ["T2"], "venue": None},
                ],
                "new_tags": [],
            }, {}

        monkeypatch.setattr(arxiv_daily, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(arxiv_daily, "call_llm", fake_call_llm)
        monkeypatch.setattr(
            arxiv_daily,
            "_patch_batch_cache",
            lambda field_cache, batch_num, new_papers, logger: patch_calls.append(
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
        assert prompt_batches == [["2604.00001", "2604.00002"], ["2604.00002"]]
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

    def test_duplicate_ids_are_rejected(self, fake_llm_server, logger, tags_file, tmp_path, sample_data):
        """Duplicate IDs in LLM response should fail instead of silently choosing one."""
        api_url, handler_cls = fake_llm_server
        handler_cls.error_mode = "duplicate_ids"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        results, failed = arxiv_daily.run_phase2(
            sample_data, tags_file, cache_dir, no_cache=True,
            api_endpoint=api_url, api_key="test-key", model="test-model", logger=logger,
        )
        assert failed == 1
        assert results["2604.06189"]["summary"] == arxiv_daily.SUMMARY_FAILURE_PLACEHOLDER
        assert results["2604.06189"]["tags"] == [arxiv_daily.SUMMARY_FAILURE_TAG]


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
        assert " +0800" in content
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
