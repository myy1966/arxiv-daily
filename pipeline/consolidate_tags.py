#!/usr/bin/env python3
"""Consolidate arxiv-daily tags into a smaller canonical vocabulary.

Writes (next to --tags):
  tags_full.json     snapshot of the pre-consolidation list (once)
  tag_mapping.json   old name -> canonical name, for later post rewrites
  tags.json          canonical list used by daily LLM runs

Does not rewrite historical posts unless --rewrite-posts is passed.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUTPUT_TZ = timezone(timedelta(hours=8))
TAG_LINE_RE = re.compile(r"^(- \*\*Tags\*\*: )(.+)$", re.MULTILINE)

TOO_BROAD = {
    "llm",
    "nlp",
    "ai",
    "cv",
    "ml",
    "computer vision",
    "deep learning",
    "machine learning",
    "artificial intelligence",
}

# One-token parents that are too generic to absorb children.
FAMILY_HEADS = {
    "LLM Agent",
    "LLM Evaluation",
    "LLM Reasoning",
    "LLM Inference",
    "LLM Alignment",
    "LLM Hallucination",
    "LLM Security",
    "LLM Safety",
    "LLM Personalization",
    "LLM Training",
    "LLM Application",
    "Agent Evaluation",
    "Agent Security",
    "Agent Memory",
    "Agent Architecture",
    "Code Agent",
    "Code Generation",
    "Interpretability",
    "Self-Correction",
}

GENERIC_PARENTS = {
    "dataset",
    "survey",
    "benchmark",
    "optimization",
    "simulation",
    "analysis",
    "detection",
    "classification",
    "prediction",
    "generation",
    "learning",
    "network",
    "model",
    "system",
    "data",
    "image",
    "video",
    "audio",
    "text",
    "graph",
    "agent",
    "memory",
    "security",
    "evaluation",
    "training",
    "ai",
    "llm",
}


def tokenize(tag: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", tag.lower()))


def norm_key(tag: str) -> str:
    return " ".join(tokenize(tag))


def canonicalize_tag(tag: str, mapping: dict[str, str]) -> str:
    tag = tag.strip()
    if not tag:
        return tag
    return mapping.get(tag, tag)


def canonicalize_tag_list(tags: list, mapping: dict[str, str]) -> list[str]:
    out: list[str] = []
    for tag in tags or []:
        if not isinstance(tag, str):
            continue
        mapped = canonicalize_tag(tag, mapping)
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def load_tag_mapping(tags_path: Path) -> dict[str, str]:
    mapping_path = tags_path.with_name("tag_mapping.json")
    if not mapping_path.exists():
        return {}
    with open(mapping_path, encoding="utf-8") as f:
        data = json.load(f)
    mapping = data.get("mapping") or {}
    return {str(k): str(v) for k, v in mapping.items()}


def collect_post_tag_freq(posts_dir: Path) -> Counter:
    freq: Counter = Counter()
    if not posts_dir.exists():
        return freq
    paths = list(posts_dir.glob("*-cs.AI.md")) + list(posts_dir.glob("*-cs.AR.md"))
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in TAG_LINE_RE.finditer(text):
            freq.update(t.strip() for t in match.group(2).split(",") if t.strip())
    return freq


def _load_vocab(tags_path: Path) -> tuple[list[str], str | None]:
    if not tags_path.exists():
        return [], None
    with open(tags_path, encoding="utf-8") as f:
        data = json.load(f)
    tags = data.get("tags", [])
    if tags and isinstance(tags[0], dict):
        tags = [t.get("name", str(t)) for t in tags]
    return list(tags), data.get("updated_at")


def _preferred_spelling(spellings: list[str], freq: Counter) -> str:
    return sorted(spellings, key=lambda t: (-freq[t], len(t), t))[0]


def _family_target(tag: str) -> str | None:
    """Map fine/alias tags onto a stable family head. None = no family rule."""
    toks = tokenize(tag)
    if not toks:
        return None
    rest = toks[1:]
    joined = " ".join(toks)

    if "judge" in toks:
        return "LLM Evaluation"

    if toks[0] == "llm":
        if any(t in rest for t in ("agent", "planning", "tool", "skill", "memory", "harness", "debate")):
            return "LLM Agent"
        if any(t in rest for t in ("evaluation", "eval", "critic", "ensemble", "calibration", "robustness")):
            return "LLM Evaluation"
        if any(t in rest for t in ("reasoning", "thinking")):
            return "LLM Reasoning"
        if any(
            t in rest
            for t in (
                "inference",
                "serving",
                "quantization",
                "pruning",
                "routing",
                "deployment",
                "optimization",
            )
        ):
            return "LLM Inference"
        if any(t in rest for t in ("alignment", "unlearning", "finetuning")):
            return "LLM Alignment"
        if any(t in rest for t in ("hallucination", "factuality")):
            return "LLM Hallucination"
        if any(
            t in rest
            for t in (
                "security",
                "watermarking",
                "fingerprinting",
                "deception",
                "provenance",
                "jailbreak",
            )
        ):
            return "LLM Security"
        if any(t in rest for t in ("safety", "bias")):
            return "LLM Safety"
        if any(t in rest for t in ("persona", "personality", "personalization")):
            return "LLM Personalization"
        if any(t in rest for t in ("training",)):
            return "LLM Training"
        if "interpretability" in rest:
            return "Interpretability"
        if "correction" in rest:
            return "Self-Correction"
        if len(toks) == 1:
            return None
        return "LLM Application"

    if toks[0] == "agent":
        if any(t in rest for t in ("evaluation", "benchmark")):
            return "Agent Evaluation"
        if any(t in rest for t in ("security", "safety", "governance", "trust", "certification")):
            return "Agent Security"
        if "memory" in rest:
            return "Agent Memory"
        if "architecture" in rest:
            return "Agent Architecture"
        if len(toks) == 1:
            return None
        return "LLM Agent"

    if toks[0] == "code" and rest:
        if rest[0] in {"agent", "agents"}:
            return "Code Agent"
        # Linguistic code-switching is not program synthesis.
        if "switching" in rest:
            return None
        return "Code Generation"

    if joined in {"llm as a judge", "llm as judge"}:
        return "LLM Evaluation"
    return None


def _longest_prefix_parent(
    tag: str,
    parent_tokens: dict[str, tuple[str, ...]],
    parent_freq: Counter,
    min_parent_tokens: int = 2,
) -> str | None:
    child = tokenize(tag)
    best: str | None = None
    best_len = 0
    best_freq = -1
    for parent, ptoks in parent_tokens.items():
        if parent == tag:
            continue
        if len(ptoks) < min_parent_tokens:
            continue
        if len(ptoks) >= len(child):
            continue
        if ptoks[0] in GENERIC_PARENTS and len(ptoks) == 1:
            continue
        if child[: len(ptoks)] == ptoks:
            if len(ptoks) > best_len or (len(ptoks) == best_len and parent_freq[parent] > best_freq):
                best = parent
                best_len = len(ptoks)
                best_freq = parent_freq[parent]
    return best


def build_consolidation(
    vocab: list[str],
    post_freq: Counter,
    min_freq: int = 20,
) -> dict:
    all_tags: list[str] = []
    seen: set[str] = set()
    for tag in list(vocab) + list(post_freq):
        if tag and tag not in seen:
            all_tags.append(tag)
            seen.add(tag)

    by_norm: dict[str, list[str]] = defaultdict(list)
    for tag in all_tags:
        by_norm[norm_key(tag)].append(tag)

    preferred = {key: _preferred_spelling(spells, post_freq) for key, spells in by_norm.items()}
    pref_freq: Counter = Counter()
    for tag, count in post_freq.items():
        pref_freq[preferred[norm_key(tag)]] += count
    for tag in vocab:
        key = norm_key(tag)
        preferred.setdefault(key, tag)
        pref_freq.setdefault(preferred[key], 0)

    reasons: dict[str, str] = {}
    mapping: dict[str, str] = {}

    # Pass 1: spelling / punctuation variants.
    for tag in all_tags:
        mapping[tag] = preferred[norm_key(tag)]
        reasons[tag] = "spelling" if tag != mapping[tag] else "identity"

    # Pass 2: family heads (fine LLM/Agent/Code aliases).
    family_targets = {norm_key(t): t for t in preferred.values()}
    for tag in all_tags:
        current = mapping[tag]
        if pref_freq[current] >= min_freq and norm_key(current) not in TOO_BROAD:
            # Frequent tags stay, except forced judge aliases.
            forced = _family_target(current)
            if forced == "LLM Evaluation" and "judge" in tokenize(current):
                mapping[tag] = forced
                reasons[tag] = "family"
            continue
        target = _family_target(current)
        if not target:
            continue
        target_key = norm_key(target)
        if target_key in family_targets:
            mapping[tag] = family_targets[target_key]
        else:
            mapping[tag] = target
        reasons[tag] = "family"

    # Pass 3: prefix collapse onto frequent 2+ token parents.
    seed = [
        tag
        for tag, count in pref_freq.most_common()
        if count >= min_freq and norm_key(tag) not in TOO_BROAD
    ]
    seed_tokens = {tag: tokenize(tag) for tag in seed}
    for tag in all_tags:
        current = mapping[tag]
        if current in seed_tokens and reasons.get(tag) == "family":
            continue
        parent = _longest_prefix_parent(current, seed_tokens, pref_freq)
        if parent:
            mapping[tag] = parent
            reasons[tag] = "prefix"

    # Resolve mapping through preferred spellings of targets.
    def resolve(tag: str) -> str:
        seen_chain: set[str] = set()
        cur = mapping.get(tag, tag)
        while cur in mapping and mapping[cur] != cur and cur not in seen_chain:
            seen_chain.add(cur)
            cur = mapping[cur]
        return cur

    resolved = {tag: resolve(tag) for tag in all_tags}

    # Canonical = mapping targets with enough mass, minus too-broad names.
    mass: Counter = Counter()
    for tag in all_tags:
        mass[resolved[tag]] += post_freq[tag]

    canonical = [
        tag
        for tag, count in mass.most_common()
        if count >= min_freq and norm_key(tag) not in TOO_BROAD
    ]
    for head in FAMILY_HEADS:
        if mass.get(head, 0) > 0 and head not in canonical and norm_key(head) not in TOO_BROAD:
            canonical.append(head)
    canonical_set = set(canonical)

    # Rare leftovers that still map to a non-canonical name stay identity
    # (rewrite will not change them). Frequent too-broad names also stay identity.
    final_mapping: dict[str, str] = {}
    final_reason: dict[str, str] = {}
    for tag in all_tags:
        target = resolved[tag]
        if target in canonical_set:
            final_mapping[tag] = target
            if tag == target:
                final_reason[tag] = "canonical"
            elif reasons.get(tag) in {"family", "prefix", "spelling"}:
                final_reason[tag] = reasons[tag]
            elif norm_key(tag) == norm_key(target):
                final_reason[tag] = "spelling"
            else:
                final_reason[tag] = reasons.get(tag, "mapped")
        else:
            final_mapping[tag] = tag
            final_reason[tag] = "identity"

    aliases = {src: dst for src, dst in final_mapping.items() if src != dst}
    identity = sorted(
        src for src, dst in final_mapping.items() if src == dst and src not in canonical_set
    )

    return {
        "min_frequency": min_freq,
        "canonical": canonical,
        "mapping": final_mapping,
        "aliases": aliases,
        "identity": identity,
        "reason": final_reason,
        "mass": dict(mass),
        "pref_freq": dict(pref_freq),
        "source_tags": all_tags,
    }


def rewrite_tags_in_markdown(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """Rewrite `- **Tags**:` lines. Returns (new_text, n_lines_changed)."""

    def repl(match: re.Match) -> str:
        prefix, raw = match.group(1), match.group(2)
        original = [t.strip() for t in raw.split(",") if t.strip()]
        rewritten = canonicalize_tag_list(original, mapping)
        return prefix + ", ".join(rewritten)

    new_text, n = TAG_LINE_RE.subn(repl, text)
    changed = 0
    if new_text != text:
        # Count only lines whose tag set actually changed.
        old_lines = TAG_LINE_RE.findall(text)
        new_lines = TAG_LINE_RE.findall(new_text)
        for (_, old_raw), (_, new_raw) in zip(old_lines, new_lines):
            if old_raw != new_raw:
                changed += 1
    return new_text, changed


def rewrite_posts(posts_dir: Path, mapping: dict[str, str], dry_run: bool) -> dict:
    stats = {"files": 0, "files_changed": 0, "tag_lines_changed": 0}
    if not posts_dir.exists():
        return stats
    paths = sorted(list(posts_dir.glob("*-cs.AI.md")) + list(posts_dir.glob("*-cs.AR.md")))
    for path in paths:
        stats["files"] += 1
        original = path.read_text(encoding="utf-8")
        updated, n_changed = rewrite_tags_in_markdown(original, mapping)
        if n_changed:
            stats["files_changed"] += 1
            stats["tag_lines_changed"] += n_changed
            if not dry_run:
                path.write_text(updated, encoding="utf-8")
    return stats


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_consolidate(
    tags_path: Path,
    posts_dir: Path,
    min_freq: int,
    rewrite: bool,
    dry_run: bool,
) -> dict:
    full_path = tags_path.with_name("tags_full.json")
    mapping_path = tags_path.with_name("tag_mapping.json")

    if full_path.exists():
        vocab, source_updated_at = _load_vocab(full_path)
    else:
        vocab, source_updated_at = _load_vocab(tags_path)

    post_freq = collect_post_tag_freq(posts_dir)
    result = build_consolidation(vocab, post_freq, min_freq=min_freq)
    now = datetime.now(OUTPUT_TZ).isoformat()

    if not full_path.exists() and tags_path.exists():
        original = json.loads(tags_path.read_text(encoding="utf-8"))
        original.setdefault("snapshot_of", str(tags_path))
        original.setdefault("snapshotted_at", now)
        _write_json(full_path, original)

    mapping_payload = {
        "version": 1,
        "created_at": now,
        "min_frequency": min_freq,
        "source": {
            "tags_json_updated_at": source_updated_at,
            "vocab_count": len(vocab),
            "union_count": len(result["source_tags"]),
            "post_assignments": int(sum(post_freq.values())),
        },
        "canonical_count": len(result["canonical"]),
        "alias_count": len(result["aliases"]),
        "identity_count": len(result["identity"]),
        "canonical": result["canonical"],
        "aliases": result["aliases"],
        "mapping": result["mapping"],
        "identity": result["identity"],
        "reason": {k: v for k, v in result["reason"].items() if v != "canonical"},
        "notes": (
            "mapping[old] = new. Identity entries are omitted from aliases; "
            "rewrite of historical posts should use mapping and only change "
            "`- **Tags**:` lines (section headers are left unchanged)."
        ),
    }

    tags_payload = {
        "updated_at": now,
        "consolidated_from": str(full_path.name),
        "min_frequency": min_freq,
        "tags": result["canonical"],
    }

    if not dry_run:
        _write_json(mapping_path, mapping_payload)
        _write_json(tags_path, tags_payload)

    rewrite_stats = None
    if rewrite:
        rewrite_stats = rewrite_posts(posts_dir, result["mapping"], dry_run=dry_run)

    return {
        "canonical_count": len(result["canonical"]),
        "alias_count": len(result["aliases"]),
        "identity_count": len(result["identity"]),
        "union_count": len(result["source_tags"]),
        "tags_path": str(tags_path),
        "mapping_path": str(mapping_path),
        "full_path": str(full_path),
        "dry_run": dry_run,
        "rewrite": rewrite_stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidate arxiv-daily tags")
    parser.add_argument(
        "--tags",
        type=Path,
        default=Path.home() / "arxiv-daily/data/tags.json",
        help="Path to tags.json",
    )
    parser.add_argument(
        "--posts",
        type=Path,
        default=Path.home() / "arxiv-daily/_posts",
        help="Path to _posts directory",
    )
    parser.add_argument("--min-freq", type=int, default=20, help="Keep tags seen at least this often")
    parser.add_argument(
        "--rewrite-posts",
        action="store_true",
        help="Also rewrite historical `- **Tags**:` lines (off by default)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute but do not write files")
    args = parser.parse_args()
    summary = run_consolidate(
        tags_path=args.tags,
        posts_dir=args.posts,
        min_freq=args.min_freq,
        rewrite=args.rewrite_posts,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
