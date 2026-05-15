---
title: "LLM Summary Markdown 特殊字符转义"
date: "2026-05-15"
status: "PASS"
---

## 1. 元数据 (Metadata)
- **原始需求**: LLM 生成的中文 summary 中残留原始英文摘要的 markdown 特殊字符（如 `|`），导致渲染异常（被解析为表格分隔符）。
- **技术翻译**: 在 Phase 3 markdown 输出阶段，summary 文本被原样嵌入 `- **Summary**: {summary}` 行内。当文本含有 `|` 时，部分 markdown 渲染器会将其识别为表格列分隔符，破坏列表项格式。需要在写入时对 summary 进行最小化转义。
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)
```
LLM JSON response → results_by_id[pid]["summary"]
                          ↓
              generate_markdown() line 1338  →  写入 .md
              patch_markdown()    line 1398  →  写入 .md
```

### 核心接口定义 (API Contracts)
```python
def _escape_md_inline(text: str) -> str:
    """Escape markdown-sensitive characters in inline text within list items.

    Target characters:
    - | → \|   (table delimiter, most common offender)
    - < → \<   (HTML tag open, prevents accidental tag interpretation)
    - > → \>   (blockquote / HTML tag close)

    NOT escaped (intentionally preserved):
    - *, _, `, ~  → LLM may legitimately use emphasis/code in summary
    - [], ()      → unlikely in Chinese summary; over-escaping would hurt readability
    """
    text = text.replace("|", "\\|")
    text = text.replace("<", "\\<")
    text = text.replace(">", "\\>")
    return text
```

### 风险预警 (Risk Assessment)
- **本项目无 AGENTS.md 规范文件**，以下设计基于通用最佳实践。
- **低风险改动**: 纯输出层转义，不影响缓存结构、LLM 调用逻辑或 match 逻辑。
- **幂等性**: 转义函数对已转义文本不会二次转义（`\|` 中的 `|` 前已有 `\`，但 `replace("|", "\\|")` 会将 `\|` 变为 `\\|`）—— 但实际场景中 LLM 不会输出 `\|`，且 summary 每次从 JSON 原始值读取而非从已写入的 md 回读，因此无二次转义风险。
- **patch_markdown 的一致性**: patch 模式同样需要转义，否则 patch 写入的 summary 仍会裸露特殊字符。

## 3. 交互历史 (The Thread)

### [Round 1]
*(Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行)*

#### [TASK ID: 001] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py`，在 `generate_markdown` 函数之前（约 line 1179 附近）
- **目标描述**: 新增 `_escape_md_inline(text: str) -> str` 工具函数
- **前置依赖**: 无
- **执行指令与限制**:
  - 放置在 Phase 3 区域开头（`# Phase 3: Markdown Generation` 注释块之后、`SECTION_NAME_CN` 之前）
  - 仅转义 `|`、`<`、`>` 三个字符
  - 不得引入额外依赖

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已在 `arxiv-update/scripts/arxiv_daily.py` 的 Phase 3 区域开头、`SECTION_NAME_CN` 之前新增 `_escape_md_inline(text: str) -> str`，仅依次转义 `|`、`<`、`>` 三个 markdown 敏感字符；未引入额外依赖，未接入 `generate_markdown` 或 `patch_markdown`，以保持本轮只处理 TASK-001。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`git diff --check` 通过。

#### [TASK ID: 002] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` line 1338
- **目标描述**: 在 `generate_markdown` 中对 summary 调用 `_escape_md_inline`
- **前置依赖**: TASK 001
- **执行指令与限制**:
  - 将 `lines.append(f"- **Summary**: {summary}")` 改为 `lines.append(f"- **Summary**: {_escape_md_inline(summary)}")`
  - 仅修改 summary 的输出行，不要动 tags/venue/title 等字段

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已在 `arxiv-update/scripts/arxiv_daily.py:generate_markdown` 中将 summary 输出行改为 `lines.append(f"- **Summary**: {_escape_md_inline(summary)}")`；未修改 tags、venue、title、patch markdown 或其他输出字段。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestGenerateMarkdown -q` 通过，9 passed；`git diff --check` 通过。

#### [TASK ID: 003] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` line 1398
- **目标描述**: 在 `patch_markdown` 中对 summary 同样调用 `_escape_md_inline`
- **前置依赖**: TASK 001
- **执行指令与限制**:
  - 将 `new_lines.append(f"- **Summary**: {summary}\n")` 改为 `new_lines.append(f"- **Summary**: {_escape_md_inline(summary)}\n")`

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已在 `arxiv-update/scripts/arxiv_daily.py:patch_markdown` 中将 summary 重写行改为 `new_lines.append(f"- **Summary**: {_escape_md_inline(summary)}\n")`；未修改 tags、venue、code、authors 或其他 patch 字段。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestPatchMarkdown tests/test_arxiv_daily.py::TestRunPatchMode -q` 通过，9 passed；`git diff --check` 通过。

#### [TASK ID: 004] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/tests/`
- **目标描述**: 为 `_escape_md_inline` 添加单元测试
- **前置依赖**: TASK 001
- **执行指令与限制**:
  - 测试用例需覆盖：纯中文无特殊字符（不变）、含 `|`、含 `<tag>`、含组合字符、空字符串
  - 遵循项目已有测试风格（检查 `tests/` 目录下已有文件的命名与结构）

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已在 `arxiv-update/scripts/tests/test_arxiv_daily.py` 的 Markdown 相关测试区块前新增 `TestMarkdownEscaping`，覆盖 `_escape_md_inline` 对纯中文无特殊字符保持不变、`|` 转义、`<tag>` 的 `<`/`>` 转义、`|`/`<`/`>` 组合字符转义，以及空字符串保持为空。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMarkdownEscaping tests/test_arxiv_daily.py::TestGenerateMarkdown tests/test_arxiv_daily.py::TestPatchMarkdown -q` 通过，22 passed；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q` 通过，152 passed, 1 xfailed；`git diff --check` 通过。

### [Round 2]
- **Reviewer**:
  - **状态判决**: `[STATUS: PASS]`
  - **审查摘要**:
    改动精准最小化：新增 `_escape_md_inline` 转义 `|`/`<`/`>`，在 `generate_markdown` 和 `patch_markdown` 两处 summary 输出点接入。不影响缓存、LLM 调用或 match 逻辑。测试覆盖 5 个用例，全量 152 passed, 1 xfailed。
  - **无阻塞 Issue。**
  - **综合评价**:
    - 转义策略保守正确：仅处理 `|`/`<`/`>` 三个高频破坏字符，不过度转义 `*`/`_`/`` ` `` 等 LLM 可能合理使用的强调语法。
    - `generate_markdown` 和 `patch_markdown` 两条写入路径均已覆盖，无遗漏。
    - 无二次转义风险：summary 始终从 `results_by_id` 的原始 JSON 值读取，不从已写入的 `.md` 文件回读。
    - 测试用例覆盖纯中文、单字符、组合字符、空字符串边界。

> **本项目无 AGENTS.md 规范文件，以通用最佳实践为准。**

## 4. 最终决策 (Final Decision)

- **决策记录**: 代码审查通过，可合并。纯输出层转义，改动最小化且无副作用，测试全部通过。
