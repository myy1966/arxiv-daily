---
title: "validate_md 默认只检查最新日期的 posts"
date: "2026-05-15"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)
- **原始需求**: `validate_md.py --dir` 默认只检查文件名日期最新的几个 post；`--all` 检查所有。
- **技术翻译**: 当前 `--dir` 模式会遍历目录下所有符合命名规范的 `.md` 文件并逐一校验。随着 posts 累积，全量验证耗时增长且大部分历史文件无需反复检查。需增加"按文件名日期筛选最近 N 天"的默认行为，并保留 `--all` 全量模式。
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
parser.add_argument("--recent", type=int, default=3, metavar="N",
                    help="Number of most recent dates to check (default: 3)")
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

#### [TASK ID: 001] [状态: OPEN]
- **定位**: `arxiv-update/scripts/validate_md.py` — `main()` 函数，argparse 定义区域（约 line 398-403）
- **目标描述**: 新增 `--all` 和 `--recent N` 两个命令行参数
- **前置依赖**: 无
- **执行指令与限制**:
  - `--all`: `action="store_true"`，帮助文本注明"默认只检查最近几个日期"
  - `--recent`: `type=int, default=3, metavar="N"`
  - 同时更新文件顶部 docstring 的 Usage 部分，补充新用法示例

#### [TASK ID: 002] [状态: OPEN]
- **定位**: `arxiv-update/scripts/validate_md.py` — `main()` 函数中 `if args.dir:` 分支（约 line 407-416）
- **目标描述**: 将 `--dir` 模式的文件收集逻辑改为"默认取最近 N 个日期，`--all` 取全部"
- **前置依赖**: TASK 001
- **执行指令与限制**:
  - 先收集所有合法文件到临时列表 `all_valid`
  - 如果 `args.all` 为 True，直接使用 `all_valid`
  - 否则，从文件名提取日期（`p.name[:10]`），去重降序排列，取前 `args.recent` 个日期，只保留匹配的文件
  - 保持已有的 `skipped` 逻辑不变
  - 不引入新的 import（`sorted`/`set` 为内置）

## 4. 最终决策 (Final Decision)
（由 Reviewer 在最终判决 PASS 时填写）
