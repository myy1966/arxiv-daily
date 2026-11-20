"""Tests for tag consolidation and historical mapping."""
from pathlib import Path

from consolidate_tags import (
    build_consolidation,
    canonicalize_tag_list,
    collect_post_tag_freq,
    load_tag_mapping,
    rewrite_posts,
    rewrite_tags_in_markdown,
    run_consolidate,
)


def _freq(pairs: list[tuple[str, int]]):
    from collections import Counter
    return Counter({k: v for k, v in pairs})


class TestBuildConsolidation:
    def test_spelling_variants_merge_to_frequent_form(self):
        vocab = ["Open-Set Recognition", "Open Set Recognition"]
        result = build_consolidation(vocab, _freq([("Open Set Recognition", 30), ("Open-Set Recognition", 2)]))
        assert result["mapping"]["Open-Set Recognition"] == "Open Set Recognition"
        assert "Open Set Recognition" in result["canonical"]
        assert "Open-Set Recognition" not in result["canonical"]

    def test_judge_aliases_map_to_evaluation(self):
        vocab = ["LLM Evaluation", "LLM-as-a-Judge", "LLM-as-Judge"]
        result = build_consolidation(
            vocab,
            _freq([("LLM Evaluation", 100), ("LLM-as-a-Judge", 50), ("LLM-as-Judge", 2)]),
        )
        assert result["mapping"]["LLM-as-a-Judge"] == "LLM Evaluation"
        assert result["mapping"]["LLM-as-Judge"] == "LLM Evaluation"
        assert "LLM-as-a-Judge" not in result["canonical"]

    def test_prefix_child_maps_to_parent(self):
        vocab = ["Knowledge Graph", "Knowledge Graph Completion"]
        result = build_consolidation(
            vocab,
            _freq([("Knowledge Graph", 80), ("Knowledge Graph Completion", 25)]),
        )
        assert result["mapping"]["Knowledge Graph Completion"] == "Knowledge Graph"

    def test_too_broad_not_canonical(self):
        vocab = ["LLM Agent", "LLM", "Computer Vision"]
        result = build_consolidation(
            vocab,
            _freq([("LLM Agent", 100), ("LLM", 40), ("Computer Vision", 40)]),
        )
        assert "LLM Agent" in result["canonical"]
        assert "LLM" not in result["canonical"]
        assert "Computer Vision" not in result["canonical"]
        assert result["mapping"]["LLM"] == "LLM"

    def test_rare_unrelated_tag_stays_identity(self):
        vocab = ["LLM Agent", "Astrolinguistics"]
        result = build_consolidation(
            vocab,
            _freq([("LLM Agent", 100), ("Astrolinguistics", 1)]),
        )
        assert result["mapping"]["Astrolinguistics"] == "Astrolinguistics"
        assert "Astrolinguistics" not in result["canonical"]
        assert "Astrolinguistics" in result["identity"]

    def test_code_switching_not_mapped_to_generation(self):
        vocab = ["Code Generation", "Code-Switching"]
        result = build_consolidation(
            vocab,
            _freq([("Code Generation", 80), ("Code-Switching", 8)]),
        )
        assert result["mapping"]["Code-Switching"] == "Code-Switching"

    def test_code_agents_maps_to_code_agent(self):
        vocab = ["Code Agent", "Code Agents"]
        result = build_consolidation(
            vocab,
            _freq([("Code Agent", 30), ("Code Agents", 5)]),
        )
        assert result["mapping"]["Code Agents"] == "Code Agent"

    def test_posts_only_tags_enter_mapping(self):
        vocab = ["LLM Agent"]
        result = build_consolidation(
            vocab,
            _freq([("LLM Agent", 50), ("LLM-as-a-Judge", 12)]),
        )
        assert result["mapping"]["LLM-as-a-Judge"] == "LLM Evaluation"


class TestRewriteMarkdown:
    def test_rewrites_tag_line_and_dedupes(self):
        mapping = {"LLM-as-a-Judge": "LLM Evaluation", "LLM Evaluation": "LLM Evaluation"}
        text = (
            "- **Authors**: A\n"
            "- **Tags**: LLM-as-a-Judge, LLM Evaluation\n"
            "- **Summary**: 摘要里提到 LLM-as-a-Judge 不应被改\n"
        )
        new, n = rewrite_tags_in_markdown(text, mapping)
        assert n == 1
        assert "- **Tags**: LLM Evaluation\n" in new
        assert "摘要里提到 LLM-as-a-Judge 不应被改" in new

    def test_rewrite_posts_dry_run_does_not_write(self, tmp_path: Path):
        mapping = {"Foo": "Bar"}
        post = tmp_path / "2026-09-09-cs.AI.md"
        post.write_text("- **Tags**: Foo\n", encoding="utf-8")
        stats = rewrite_posts(tmp_path, mapping, dry_run=True)
        assert stats["tag_lines_changed"] == 1
        assert post.read_text(encoding="utf-8") == "- **Tags**: Foo\n"

    def test_rewrite_posts_writes(self, tmp_path: Path):
        mapping = {"Foo": "Bar"}
        post = tmp_path / "2026-09-09-cs.AR.md"
        post.write_text("- **Tags**: Foo, Bar\n", encoding="utf-8")
        stats = rewrite_posts(tmp_path, mapping, dry_run=False)
        assert stats["tag_lines_changed"] == 1
        assert post.read_text(encoding="utf-8") == "- **Tags**: Bar\n"


class TestRunConsolidate:
    def test_writes_full_mapping_and_canonical_tags(self, tmp_path: Path):
        tags_path = tmp_path / "tags.json"
        tags_path.write_text(
            '{"updated_at": "old", "tags": ["LLM Agent", "LLM-as-a-Judge", "Astrolinguistics"]}',
            encoding="utf-8",
        )
        posts = tmp_path / "_posts"
        posts.mkdir()
        (posts / "2026-09-09-cs.AI.md").write_text(
            "- **Tags**: LLM Agent, LLM-as-a-Judge\n- **Tags**: LLM Agent\n",
            encoding="utf-8",
        )
        summary = run_consolidate(tags_path, posts, min_freq=1, rewrite=False, dry_run=False)
        assert (tmp_path / "tags_full.json").exists()
        mapping_path = tmp_path / "tag_mapping.json"
        assert mapping_path.exists()
        payload = load_tag_mapping(tags_path)
        assert payload["LLM-as-a-Judge"] == "LLM Evaluation"
        new_tags = __import__("json").loads(tags_path.read_text())["tags"]
        assert "LLM Agent" in new_tags
        assert "LLM-as-a-Judge" not in new_tags
        assert summary["alias_count"] >= 1

    def test_canonicalize_tag_list_uses_mapping(self):
        mapping = {"A": "B", "B": "B"}
        assert canonicalize_tag_list(["A", "B", "A"], mapping) == ["B"]


class TestCollectFreq:
    def test_collect_post_tag_freq(self, tmp_path: Path):
        (tmp_path / "2026-01-01-cs.AI.md").write_text("- **Tags**: Alpha, Beta\n", encoding="utf-8")
        (tmp_path / "2026-01-01-cs.AR.md").write_text("- **Tags**: Alpha\n", encoding="utf-8")
        (tmp_path / "ignore.md").write_text("- **Tags**: Zeta\n", encoding="utf-8")
        freq = collect_post_tag_freq(tmp_path)
        assert freq["Alpha"] == 2
        assert freq["Beta"] == 1
        assert "Zeta" not in freq
