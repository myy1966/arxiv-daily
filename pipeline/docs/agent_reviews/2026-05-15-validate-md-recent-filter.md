---
title: "validate_md 默认只检查最新日期的 posts"
date: "2026-05-15"
status: "PASS"
---

## 1. 元数据 (Metadata)
- **原始需求**: `validate_md.py --dir` 默认只检查文件名日期最新的几个 post；`--all` 检查所有。
- **技术翻译**: 当前 `--dir` 模式会遍历目录下所有符合命名规范的 `.md` 文件并逐一校验。随着 posts 累积，全量验证耗时增长且大部分历史文件无需反复检查。需增加"按文件名日期筛选最近 N 天"的默认行为（默认 N=1），并保留 `--all` 全量模式。
- **涉及文件**: `arxiv-update/scripts/validate_md.py`

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)
```
--dir <directory>
    ↓ glob("*.md") + is_valid_filename filter
    ↓ 提取文件名前 10 字符作为日期 (YYYY-MM-DD)
    ↓ 去重排序取最近 N 个日期
    ↓ 只保留属于这些日期的文件
    → 进入原有 validate_file 流程
```

### 核心接口定义 (API Contracts)
```python
# 新增 CLI 参数
parser.add_argument("--all", action="store_true",
                    help="Check all posts (default: only the most recent ones)")
parser.add_argument("--recent", type=int, default=1, metavar="N",
                    help="Number of most recent dates to check (default: 1)")
```

### 日期提取逻辑（伪代码）
```python
# 文件名格式: YYYY-MM-DD-field.SUBFIELD.md
# 同一日期可能有多个 field 的文件（如 2026-05-14-cs.AI.md, 2026-05-14-cs.CL.md）
dates = sorted({p.name[:10] for p in all_valid_files}, reverse=True)
recent_dates = set(dates[:args.recent])  # 取最近 N 个唯一日期
paths = [p for p in all_valid_files if p.name[:10] in recent_dates]
```

### 风险预警 (Risk Assessment)
- **本项目无 AGENTS.md 规范文件**，以下设计基于通用最佳实践。
- **低风险**: 仅修改 `main()` 函数中文件收集逻辑，不涉及校验规则本身。
- **向后兼容**: 直接指定文件名 (`validate_md.py file.md`) 不受影响；`--dir --all` 等价于原行为。
- **边界情况**: 目录为空时、所有文件都不符合命名规范时，行为与现有一致（打印"No files to validate"退出码 2）。

## 3. 交互历史 (The Thread)

### [Round 1]
*(Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行)*

#### [TASK ID: 001] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/validate_md.py` — `main()` 函数，argparse 定义区域（约 line 398-403）
- **目标描述**: 新增 `--all` 和 `--recent N` 两个命令行参数
- **前置依赖**: 无
- **执行指令与限制**:
  - `--all`: `action="store_true"`，帮助文本注明"默认只检查最近几个日期"
  - `--recent`: `type=int, default=1, metavar="N"`
  - 同时更新文件顶部 docstring 的 Usage 部分，补充新用法示例

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。根据 Architect 更新后的 ADR，已将 `arxiv-update/scripts/validate_md.py` 中 `--recent` 参数从 `default=3` 修正为 `default=1`，并同步将 help 文本改为 `Number of most recent dates to check (default: 1)`；`--all` 参数和 Usage 示例已存在且符合本任务要求，本轮未修改 `--dir` 文件收集逻辑。验证：`python3 -m py_compile arxiv-update/scripts/validate_md.py` 通过；`python3 arxiv-update/scripts/validate_md.py --help` 显示 `--recent N ... (default: 1)` 通过；`git diff --check` 通过。

#### [TASK ID: 002] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/validate_md.py` — `main()` 函数中 `if args.dir:` 分支（约 line 407-416）
- **目标描述**: 将 `--dir` 模式的文件收集逻辑改为"默认取最近 N 个日期，`--all` 取全部"
- **前置依赖**: TASK 001
- **执行指令与限制**:
  - 先收集所有合法文件到临时列表 `all_valid`
  - 如果 `args.all` 为 True，直接使用 `all_valid`
  - 否则，从文件名提取日期（`p.name[:10]`），去重降序排列，取前 `args.recent` 个日期，只保留匹配的文件
  - 保持已有的 `skipped` 逻辑不变
  - 不引入新的 import（`sorted`/`set` 为内置）

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。经复核，`arxiv-update/scripts/validate_md.py:main` 的 `--dir` 分支已符合 Architect 更新后的设计：先收集合法文件到 `all_valid`，非规范文件继续进入 `skipped`；`--all` 使用全部 `all_valid`；默认路径从文件名 `p.name[:10]` 提取唯一日期并降序排序，按 `args.recent` 过滤，当前 `TASK-001` 已将默认值修正为 1，因此仅 `--dir` 会检查最新 1 个唯一日期。生产代码本轮无需进一步修改。验证：`python3 -m py_compile arxiv-update/scripts/validate_md.py` 通过；`git diff --check` 通过；`python3 - <<'PY' ... PY` 临时脚本验证仅 `--dir` 检查最新 1 个唯一日期的 2 个文件、`--recent 2` 检查最新 2 个唯一日期的 3 个文件、`--all` 检查全部 4 个合法文件，且 `invalid.md` 仍进入 skipped，均通过。

### [Round 2]
- **Reviewer**:
  - **状态判决**: `[STATUS: PASS]`
  - **审查摘要**:
    改动精准最小化：仅修改 `main()` 的文件收集逻辑，新增 `--all` 和 `--recent N` 参数，不涉及校验规则本身。日期提取使用 `p.name[:10]` 简洁正确（文件名格式 `YYYY-MM-DD-*` 由 `is_valid_filename` 保证）。直接指定文件路径不受影响，`--dir --all` 等价于原行为。
  - **无阻塞 Issue。**
  - **综合评价**:
    - `all_valid` 中间列表清晰分离了收集与过滤，`skipped` 逻辑不受影响。
    - 日期去重降序 + 切片 + 集合过滤，逻辑简洁无冗余。
    - `--all` 与 `--recent` 互不冲突：`--all` 优先级高于 `--recent`，符合直觉。
    - docstring Usage 示例已同步更新。
    - 边界情况安全：空目录 / 无合法文件时 `all_valid` 为空，`dates` 为空，`recent_dates` 为空集，`paths` 为空，走入已有的 "No files to validate" 路径。

> **本项目无 AGENTS.md 规范文件，以通用最佳实践为准。**

## 4. 最终决策 (Final Decision)

- **决策记录**: 代码审查通过，可合并。纯文件收集层改动，不影响校验逻辑，向后兼容。
