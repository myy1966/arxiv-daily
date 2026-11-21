---
title: "Venue 简称化：待审条目与待定规则"
date: "2026-11-21"
status: "BACKLOG"
---

## 1. 背景

2026-11-21 起，Venue 统一写成简称，格式为「会议+年份在前」：

| 类型 | 写法 |
|---|---|
| 主会 | `ICML 2026` |
| Findings | `ACL 2026 Findings` |
| track | `NeurIPS 2026 D&B`、`EMNLP 2026 Industry`、`ICML 2026 Position` |
| workshop | `NeurIPS 2025 MATH-AI Workshop`；没有简称时写 `ICLR 2026 Workshop` |
| 期刊 | `TMLR`、`IEEE TPAMI`（年份只在 comments 写了时才加） |

报告形式（Oral/Spotlight/Poster）单独写在 Venue 下面的 `Presentation` 行。

全称到简称的对应关系维护在 `data/venue_abbreviations.json`：

- `aliases`：小写的简称或变体 → 规范写法，例如 `nips` → `NeurIPS`、`tkdd` → `ACM TKDD`。
- `full_names`：规范化后的全称 → `{"abbr": 简称}`。规范化方法见 `pipeline/arxiv_daily.py` 中的 `venue_name_key`。

日常运行时，模型遇到新的全称会自动写进表里。如果这个简称没有在论文 comments 里出现过（也就是模型自己给的），会标上 `"review": true`。

## 2. 待审：模型拟定的简称（152 条）

列出这些条目：

```bash
.venv/bin/python -c "import json; t=json.load(open('data/venue_abbreviations.json'))['full_names']; [print(k, '->', v['abbr']) for k, v in t.items() if v.get('review')]"
```

审核方法：确认简称正确后删掉 `"review": true`；简称不对就改 `abbr`。已经发现的问题：

- `ieee transactions on affective computing -> IEEE TAC`：和 IEEE Transactions on Automatic Control 撞了简称，通用写法是 `IEEE TAFFC`。
- `acm on programming languages -> OOPSLA`：这本期刊（PACMPL）不只收 OOPSLA，可能应写 `PACMPL`。

## 3. 待补：仍是全称的 Venue（132 种，156 行）

这些大多是确实没有通用简称的期刊或小众会议，例如 `Archives of Computational Methods in Engineering`、`Annual Review of Biomedical Data Science`，所以保留了原名。`validate_md.py` 会对它们给出「Venue 可能是全称而非简称」的 warning：

```bash
.venv/bin/python pipeline/validate_md.py --dir _posts --all | grep '全称'
```

需要简称的，在 `full_names` 里加一条，key 用 `venue_name_key(全称)`。

## 4. 待定：会议论文的扩展版要不要显示 Venue（26 篇）

现在的规则：如果 comments 说 arXiv 上这一版是某篇会议论文的扩展版，例如「extended version of a paper appearing at KR 2026」，就不显示 Venue，理由是这一版本身没有被录用。以前的 post 会显示原会议。按现行规则被删掉 Venue 的有下面这些：

| post | # | arXiv | 原 Venue | comments 摘录 |
|---|---:|---|---|---|
| 2026-04-16-cs.AI.md | 91 | 2604.13432 | CVPR 2026 Findings | 20 pages. Extended version of CVPR 2026 Findings paper. Neurocomputing (Elsevier |
| 2026-04-24-cs.AI.md | 169 | 2604.21603 | KR 2026 | This is an extended version of a paper appearing at the 23rd International Confe |
| 2026-04-27-cs.AI.md | 75 | 2604.22422 | KR 2026 | Long version of KR'26 paper |
| 2026-04-28-cs.AI.md | 250 | 2604.23701 | ICASSP 2026 | This work is an expanded version of our prior paper published in the IEEE ICASSP |
| 2026-04-29-cs.AI.md | 188 | 2603.00376 | NICE 2026 | This is an expanded version of the paper titled "NeuroHex: Highly Efficient Hex  |
| 2026-04-29-cs.AR.md | 8 | 2604.25338 | APCCAS 2025 | Extended manuscript of APCCAS 2025 |
| 2026-05-01-cs.AI.md | 90 | 2604.28112 | KR 2026 | Extended version of a paper presented at the 23rd International Conference on Pr |
| 2026-05-01-cs.AI.md | 112 | 2604.26977 | KR 2026 | 13 pages. Extended version of a paper to be presented at KR 2926 |
| 2026-05-04-cs.AI.md | 187 | 2603.08965 | GRAAI 2026 Workshop | v2: extended companion of GRAAI 2026 workshop paper; full proofs of Lemmas A.1-A |
| 2026-05-05-cs.AI.md | 585 | 2604.21603 | KR 2026 | This is an extended version of a paper appearing at the 23rd International Confe |
| 2026-05-08-cs.AI.md | 359 | 2507.01833 | IJCAI 2022 | 76 pages. This article is a significantly extended version of a paper presented  |
| 2026-05-15-cs.AI.md | 71 | 2605.14666 | IJCAI 2026 | This is the extended version of a paper accepted to IJCAI 2026 |
| 2026-05-18-cs.AI.md | 167 | 2605.16026 | ACL 2026 Findings | Submitted to IEEE/ACM TASLP. This work extends S2ST-Omni, accepted to Findings o |
| 2026-05-28-cs.AI.md | 133 | 2605.28566 | SoCS 2026 | Extended version of the SoCS 2026 paper. Includes appendices omitted from the pr |
| 2026-06-02-cs.AI.md | 885 | 2604.26977 | KR 2926 | 13 pages. Extended version of a paper presented at KR 2926 |
| 2026-06-24-cs.AI.md | 52 | 2606.24618 | KR 2025 | Extended version of a paper published in the proceedings of KR 2025 |
| 2026-06-30-cs.AI.md | 246 | 2606.28994 | DSC Next 2026 | 10 pages, 1 figure. Extended version of the talk presented at DSC Next 2026, Ams |
| 2026-06-30-cs.AI.md | 592 | 2604.26977 | KR 2926 | 13 pages. Extended version of a paper presented at KR 2926 |
| 2026-07-16-cs.AI.md | 8 | 2607.13172 | ICAART 2026 | 42 pages, 18 figures. Extended version of a paper presented at ICAART 2026; subm |
| 2026-07-16-cs.AI.md | 90 | 2607.13370 | ICAART 2026 | Extended version of a paper presented at ICAART 2026. Manuscript under review at |
| 2026-07-29-cs.AI.md | 72 | 2607.25484 | ICAPS 2026 | Extended version of a paper accepted at ICAPS 2026 (doi: https://doi.org/10.1609 |
| 2026-09-09-cs.AI.md | 375 | 2609.07206 | KDD 2026 | This paper is an extended version of REFINE, which was published at KDD 2026 |
| 2026-09-17-cs.AI.md | 89 | 2609.17601 | IEEE Conference on Decision and Control, 2026 | Extended version of the paper: S. T. Kiremitci and M. O. Sayin, "Decentralized o |
| 2026-10-21-cs.AI.md | 437 | 2609.17601 | IEEE Conference on Decision and Control 2026 | Extended version of S. T. Kiremitci and M. O. Sayin, "Decentralized optimal equi |
| 2026-10-28-cs.AI.md | 84 | 2610.37375 | NeurIPS 2026 Workshop on Resource-Aware Agentic AI | 17 pages, 4 figures, 11 tables. Extended version of a paper accepted at the Neur |
| 2026-11-18-cs.AI.md | 400 | 2611.21001 | ML4H 2026; GenAI4H Workshop at NeurIPS 2026 | Extended version of papers accepted at ML4H 2026 (Proceedings) and GenAI4H works |

如果想改成显示原会议，就修改 `pipeline/arxiv_daily.py` 中 `build_venue_prompt` 的规则，把「扩展版视为原会议」写进去。然后在 `venue_raw.jsonl` 里删掉这些论文对应的行，再按下一节重新抽取并应用。

## 5. 改表之后同步到已有 post

模型的原始抽取结果在 `~/.cache/arxiv-update-venue-backfill-2026-11-21/venue_raw.jsonl`，共 14,262 条。只改简称表的话，不用重新调用模型：

```bash
# 先干跑，看看会改哪些
.venv/bin/python pipeline/venue_backfill.py apply ~/.cache/arxiv-update-venue-backfill-2026-11-21/venue_raw.jsonl --dry-run --report /tmp/venue_report.jsonl
# 确认后写入：改写 Venue/Presentation 行和「已录用论文」行
.venv/bin/python pipeline/venue_backfill.py apply ~/.cache/arxiv-update-venue-backfill-2026-11-21/venue_raw.jsonl
```

改了提示词，或者新增了 post，需要先补抽：

```bash
.venv/bin/python pipeline/venue_backfill.py extract ~/.cache/arxiv-update-venue-backfill-2026-11-21/venue_raw.jsonl
```

`extract` 会跳过已经抽取过的条目。运行需要 `ARXIV_UPDATE_*` 环境变量。
