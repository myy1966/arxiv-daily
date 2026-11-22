---
title: "摘要「问题：/方法：/结果：」标签清理：待优化清单"
date: "2026-11-21"
status: "BACKLOG"
---

## 1. 背景

2026-05-28 起，摘要提示词改为「问题 → 方法 → 结果」结构（4cd3788）。之后部分模型把这个结构当成小标题写进了摘要，例如：

> 问题：……。方法：提出 X，……。结果：在……上提升 ……。

这类摘要共 2,316 条，集中在 2026-05-28 ~ 2026-09-15。2026-09-16 之后的摘要没有这个问题。

## 2. 已做的处理

| 处理 | 数量 | 说明 |
|---|---:|---|
| 脚本删除标签 | 2,321 | 只删句首或「。；？！，」之后的 7 个标签词：问题、方法、结果、关键结果、贡献、观点、结论。正文里的冒号（如「实验结果：」「采用三阶段训练：」）保留。「方法：/贡献：」后面直接是名称或名词短语时，补上「提出」 |
| 其中重新生成 | 232 | 删标签后读起来生硬的，用 `--patch` 重新生成。只替换 Summary，Tags/Venue 保持原样 |
| **只删了标签（本清单）** | **2,089** | 见 `2026-11-21-summary-label-strip-backlog.tsv` |

232 条重新生成的来源：215 条按规则筛选（删标签后不到 80 字、整句只是名词短语、句首是名词片段）；6 条由 jieba 词性检查找出（整句没有谓语）；11 条由 jieba 检查原「方法：」后面那一句的开头找出（以名词短语开头，后接逗号）。

## 3. 清单里的摘要现在是什么状态

- 内容来自 E2/E3 时期的模型，只去掉了标签，所以写法比当前摘要更简短（中位长度 135 字，当前约 208 字）。
- 随机抽读 40 条，39 条通顺。估计仍有不到 1% 是轻微的电报体，典型形态是「名词短语开头 + 逗号 + 从句」。
- 46 条在删标签时补了「提出」（TSV 中 `inserted_tichu=yes`）。这几条是规则改写的，复查时可优先看。
- 这批摘要都检查过没有挂错论文：与 arXiv 原文（缓存的列表页）比对的错位检测未命中。
- 清单记录之后，cs.AI 2026-06-30 的 2506.00400 和 2506.08774 因为挂错论文，在 52919c2 里重新生成了，已经不是只删了标签的摘要。

## 4. 清单格式

`2026-11-21-summary-label-strip-backlog.tsv`，制表符分隔，带表头：

| 列 | 含义 |
|---|---|
| `field` | `cs.AI` / `cs.AR` |
| `date` | 列表日期，也就是 post 文件名里的日期 |
| `num` | post 内的论文编号（`<h3 id="N">`） |
| `arxiv` | arXiv ID，即 `--patch` 的参数 |
| `inserted_tichu` | 删标签时是否补了「提出」 |

## 5. 同一篇论文的多个版本

清单的 2,089 条对应 2,047 篇论文，其中 42 篇在清单里出现了两次。很多论文后来又提交了新版本：

- 819 篇出现在两个及以上的 post 里，最多一篇出现了 8 次。
- 1,648 篇每次出现时摘要都没变；399 篇的摘要至少改过一次，其中 325 篇有 2 个版本，60 篇有 3 个，14 篇有 4–5 个。

重新生成时，按「论文 + 当天的摘要」分组处理，规则和日常运行的摘要复用（7603b50）一致：

- **摘要相同的位置统一成同一条摘要。** 组内如果已经有用现在的 prompt 写的摘要（2026-09-16 及以后的 post，或 2026-11-21 用 `--patch` 重新生成的），直接复制过来，不调用 LLM。没有的话，在组内第一个清单位置重新生成一次，再复制到其他位置。
- **摘要不同的位置不统一。** 每个 post 的摘要描述的是当天的版本。新版本改了实验数字，摘要里的数字也应该不同。
- **清单外的旧摘要不改。** 摘要相同、但由更早 prompt 写的（例如 2026-05-28 之前），本身没有标签问题，只为统一不值得改。
- **复用产生的副本一起处理。** 2026-11-22 起，日常运行会把摘要没变的论文的摘要从更早的 post 原样复制过来，所以清单里的摘要可能出现在更晚的 post 里。副本指日期在 2026-11-22 及以后、Summary 与组内清单那条完全相同的位置。2026-11-22 之前，清单论文在更晚 post 里出现了 590 次，那些都是当时由 LLM 重新写的，不是副本。

`2026-11-21-summary-label-strip-plan.py` 按这些规则列出每个要改的位置（列：`arxiv`、`field`、`date`、`role`、`action`、`source`）。2026-11-22 的运行结果：

| action | 数量 | 说明 |
|---|---:|---|
| `copy`，来源已有 | 76 | 来源是 2026-09-16 之后的 post（71 条）或 2026-11-21 重新生成的摘要（5 条） |
| `copy`，来源待生成 | 29 | 同组另一个清单位置重新生成后再复制 |
| `regenerate` | 1,982 | 涉及 1,971 篇论文，每组调用一次 LLM |
| 已完成，不列出 | 2 | 上面第 3 节提到的 2506.00400、2506.08774 |

清单位置的 Summary 如果和清单记录时（078b095）不同，脚本就当它已经重新生成过，把它作为来源。所以做到一半时重跑，得到的就是剩下的计划。

## 6. 以后重新生成时的注意事项

1. **列表页缓存**：`--patch` 需要 `~/.cache/arxiv-update/pages/<field>/<date>.html`，清单涉及的日期目前都在。
2. **不要覆盖原缓存的日志**：重试批次的 `retry_*` 和 `venue_batch_*_error.json` 与日常运行同名。用 `--cache-dir` 指向一个独立目录，并把 `pages` 软链接过去：
   ```bash
   P=~/.cache/arxiv-update-patch-<日期>
   mkdir -p "$P" && ln -sfn ~/.cache/arxiv-update/pages "$P/pages"
   ```
3. **先生成计划，只对 `regenerate` 行按日期分组执行**，每个 `(field, date)` 调用一次（用 bash 执行，zsh 不会按空格拆开 `$ids`）：
   ```bash
   .venv/bin/python pipeline/docs/todos/2026-11-21-summary-label-strip-plan.py > "$P/plan.tsv"
   awk -F'\t' 'NR>1 && $5=="regenerate" {k=$2"\t"$3; ids[k]=ids[k]" "$1} END {for (k in ids) print k"\t"ids[k]}' \
     "$P/plan.tsv" |
   while IFS=$'\t' read -r field date ids; do
     .venv/bin/python pipeline/arxiv_daily.py "$field" --date "$date" --patch $ids --cache-dir "$P"
   done
   ```
4. **`--patch` 会同时重写 Tags 和 Venue。**
   - 只想换摘要：按 HEAD 版本还原每篇论文的 Authors/Subjects/Tags/Venue/Code 行，只保留新的 Summary 行。
   - 接受新的 Tags/Venue：post 开头的分类汇总和「已录用论文」行不会自动更新，需要用 `generate_markdown` 按正文重建。还要核对 Venue 关键词门槛的漏判，例如 "Best Paper Award at …"、只写 "ACL 2026"、拼错的 "Accpted"、"MemFM @ ICML 2026"。
5. **`--patch` 会写 `data/tags.json`**，包括 `updated_at` 和模型新造的标签。如果没有采用新 Tags，记得还原，否则这些标签会进入日常运行的提示词标签表。
6. **复制统一的摘要**：处理完第 4、5 条后重新生成计划，再应用 `copy` 行。脚本只替换 Summary 行，其他行不动。来源还没重新生成的行会跳过并打印出来：
   ```bash
   .venv/bin/python pipeline/docs/todos/2026-11-21-summary-label-strip-plan.py > "$P/plan.tsv"
   .venv/bin/python pipeline/docs/todos/2026-11-21-summary-label-strip-plan.py --apply-copies "$P/plan.tsv"
   ```
   重跑后的计划里应该只剩 `copy` 行。如果还有 `regenerate` 行，说明有 `--patch` 失败了，先补做。
7. **验证**：
   - 和 HEAD 对比 `pipeline/validate_md.py` 的输出。
   - 用缓存原文检查新摘要有没有挂到相邻论文上。
   - `bundle exec jekyll b` 后跑 `bundle exec ruby tools/htmlproofer.rb _site --disable-external`。
