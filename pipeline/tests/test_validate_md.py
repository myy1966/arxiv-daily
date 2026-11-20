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
