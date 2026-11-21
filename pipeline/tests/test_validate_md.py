from pathlib import Path

import validate_md


def write_post(tmp_path: Path, name: str, date_line: str) -> Path:
    path = tmp_path / name
    path.write_text(
        f'---\ntitle: "arXiv cs.AI Daily Update"\n{date_line}\ncategories: [cs.AI]\n---\n\nbody\n',
        encoding="utf-8",
    )
    return path


def date_mismatch_errors(path: Path) -> list[validate_md.Issue]:
    result = validate_md.validate_file(path)
    return [e for e in result.errors if "与文件名日期" in e.message]


def test_matching_date_passes(tmp_path):
    path = write_post(tmp_path, "2026-11-17-cs.AI.md", "date: 2026-11-17 16:00:00 +0800")
    assert date_mismatch_errors(path) == []


def test_mismatched_date_is_an_error_on_the_date_line(tmp_path):
    path = write_post(tmp_path, "2026-11-17-cs.AI.md", "date: 2026-11-18 09:41:19 +0800")
    errors = date_mismatch_errors(path)
    assert len(errors) == 1
    assert errors[0].line == 3
    assert "2026-11-18" in errors[0].message and "2026-11-17" in errors[0].message


def test_filename_without_date_prefix_is_skipped(tmp_path):
    path = write_post(tmp_path, "notes.md", "date: 2026-11-18 09:41:19 +0800")
    assert date_mismatch_errors(path) == []


def write_paper_post(tmp_path: Path, meta_lines: str) -> Path:
    path = tmp_path / "2026-11-17-cs.AI.md"
    path.write_text(
        '---\ntitle: "arXiv cs.AI Daily Update"\ndate: 2026-11-17 16:00:00 +0800\ncategories: [cs.AI]\n---\n\n'
        "cs.AI 领域 2026年11月17日 共有 1 篇论文更新：\n\n"
        "## 新投稿 (1)\n\n"
        '<h3 id="1">[1] Test Paper</h3>\n'
        "- **arXiv**: [2611.00001](https://arxiv.org/abs/2611.00001)\n"
        "- **Authors**: Alice\n- **Subjects**: cs.AI\n- **Tags**: T\n"
        f"{meta_lines}"
        "- **Summary**: 这是一条足够长的测试摘要，用于检查 Venue 与 Presentation 字段的校验规则是否正确。\n",
        encoding="utf-8",
    )
    return path


def venue_warnings(path: Path) -> list[str]:
    result = validate_md.validate_file(path)
    return [w.message for w in result.warnings if "Venue" in w.message or "Presentation" in w.message]


def test_short_venues_pass_even_without_a_year(tmp_path):
    for venue in ("ICML 2026", "ACL 2026 Findings", "NeurIPS 2025 MATH-AI Workshop", "IEEE IoT-J", "TMLR"):
        path = write_paper_post(tmp_path, f"- **Venue**: {venue}\n")
        assert venue_warnings(path) == [], venue


def test_full_venue_name_warns(tmp_path):
    path = write_paper_post(tmp_path, "- **Venue**: 43rd International Conference on Machine Learning\n")
    assert any("全称" in w for w in venue_warnings(path))


def test_presentation_values(tmp_path):
    ok = write_paper_post(tmp_path, "- **Venue**: ICML 2026\n- **Presentation**: Spotlight\n")
    assert venue_warnings(ok) == []
    bad = write_paper_post(tmp_path, "- **Venue**: ICML 2026\n- **Presentation**: Keynote\n")
    assert any("Oral/Spotlight/Poster" in w for w in venue_warnings(bad))
    orphan = write_paper_post(tmp_path, "- **Presentation**: Oral\n")
    assert any("缺少 Venue" in w for w in venue_warnings(orphan))
