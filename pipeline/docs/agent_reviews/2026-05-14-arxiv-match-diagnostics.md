---
title: "match_llm_results 错误诊断细分"
date: "2026-05-14"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)

- **原始需求**: 将 `match_llm_results` 中笼统的 "unmatched" 拆分为三种具体错误类型：ID 未找到、ID 重复、质量不达标；修复 ID 重复时静默选择最后一个的 bug
- **技术翻译**: 重构 `match_llm_results` 的返回值和内部逻辑，引入结构化诊断类型 `MatchDiagnostics`，使 LLM 批量匹配失败原因可观测、可分别处理。同时修复 `llm_by_id[pid] = i` 对重复 ID 静默覆盖的正确性 bug。
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`, `arxiv-update/scripts/tests/test_arxiv_daily.py`

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)

```
match_llm_results(batch, llm_papers, ...)
  ├── 构建 ID 索引，检测重复 → duplicate_ids
  ├── 精确匹配 + 归一化匹配（跳过 duplicate）→ matched
  ├── 质量校验（tags 非空、summary ≥ 30 字符）→ quality_failed
  └── 剩余未匹配 → missing_ids
  返回 (matched, MatchDiagnostics)
```

### 核心接口定义 (API Contracts)

```python
@dataclass
class MatchDiagnostics:
    missing_ids: list[str]       # LLM 响应中找不到的 batch paper ID
    duplicate_ids: list[str]     # LLM 响应中出现 ≥2 次的 ID（对应 batch paper）
    quality_failed: list[dict]   # 匹配成功但质量不达标 [{id, issues, result}]

    @property
    def total_failed(self) -> int:
        return len(self.missing_ids) + len(self.duplicate_ids) + len(self.quality_failed)

    @property
    def all_failed_ids(self) -> list[str]:
        return self.missing_ids + self.duplicate_ids + [q["id"] for q in self.quality_failed]

def match_llm_results(
    batch: list[dict],
    llm_papers: list[dict],
    logger: logging.Logger,
    batch_num: int,
    raw_result: dict | None = None,
    debug_dir: Path | None = None,
    batch_label: str | None = None,
    attempt: int = 0,
) -> tuple[dict[str, dict], MatchDiagnostics]:
    ...
```

### 风险预警 (Risk Assessment)

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

| 风险 | 应对 |
|------|------|
| 返回类型变更导致调用方 `_process_batch`、`run_phase2` 解构失败 | TASK 002 显式更新所有调用方 |
| 重复 ID 直接拒绝匹配可能增加重试次数 | 可接受——重复 ID 意味着 LLM 输出有错，重试是正确行为 |
| debug JSON 格式变化影响外部工具解析 | 保留原 `unmatched_batch_ids` 字段，新增 `diagnostics` 字段 |

## 3. 交互历史 (The Thread)

### [Round 1]

*Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行。*

#### [TASK ID: 001] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `MatchDiagnostics` 定义（在 `BatchProcessResult` 附近）
- **目标描述**: 新增 `MatchDiagnostics` dataclass
- **前置依赖**: 无
- **执行指令与限制**:
  1. 在 `BatchProcessResult` 定义之后新增 `MatchDiagnostics` dataclass，字段如上述接口定义
  2. 包含 `total_failed` 和 `all_failed_ids` 两个 property
  3. 仅新增定义，不修改现有代码

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已在 `arxiv-update/scripts/arxiv_daily.py` 的 `BatchProcessResult` 定义之后新增 `MatchDiagnostics` dataclass，包含 `missing_ids`、`duplicate_ids`、`quality_failed` 字段，以及 `total_failed` 和 `all_failed_ids` 两个 property；未修改任何现有匹配逻辑或调用方。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`python3 - <<'PY' ... PY` 直接断言 `MatchDiagnostics.total_failed` 与 `all_failed_ids` 聚合结果通过。

#### [TASK ID: 002] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `match_llm_results` 函数
- **目标描述**: 重构匹配逻辑，检测重复 ID 并返回 `MatchDiagnostics`
- **前置依赖**: TASK 001
- **执行指令与限制**:
  1. **检测重复 ID**: 遍历 `llm_papers` 构建 ID 计数。出现 ≥2 次的 ID 加入 `duplicate_set`
  2. **构建索引时排除重复**: `llm_by_id` 和 `llm_by_norm` 中不包含 `duplicate_set` 中的 ID
  3. **精确匹配 + 归一化匹配**: 逻辑基本不变，但跳过 duplicate ID
  4. **确定 duplicate 对应的 batch paper**: 若 batch 中某 paper 的 ID（精确或归一化）命中 `duplicate_set`，且未被其他方式匹配到，则归入 `duplicate_ids`
  5. **质量检查**: 逻辑不变，失败的归入 `quality_failed`
  6. **剩余未匹配**: 既不是 duplicate 也不是 quality_failed 的，归入 `missing_ids`
  7. **返回值**: `(matched_dict, MatchDiagnostics(missing_ids, duplicate_ids, quality_failed))`
  8. **日志输出**: 按分类分别 log：
     - `[Phase2] {label}: {n} papers ID not found in LLM response: [ids...]`
     - `[Phase2] {label}: {n} papers ID duplicated in LLM response: [ids...]`
     - `[Phase2] {label}: {n} papers quality check failed: [id(issues)...]`
  9. **debug JSON**: 保留原有 `unmatched_batch_ids` 字段（= missing + duplicate 合集），新增 `diagnostics` 字段包含三类明细
  10. **不要**对重复 ID 尝试启发式选择，直接拒绝匹配

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已重构 `arxiv-update/scripts/arxiv_daily.py:match_llm_results`：返回类型改为 `(matched, MatchDiagnostics)`；构建 LLM ID 计数并将重复 ID 从精确/归一化索引中排除，避免重复 ID 被字典覆盖为“最后一个胜出”；重复 ID 对应的 batch paper 直接归入 `diagnostics.duplicate_ids`，不做启发式选择；质量失败归入 `diagnostics.quality_failed`；其余未匹配归入 `diagnostics.missing_ids`。日志已按 missing、duplicate、quality 三类分别输出；ID mismatch debug JSON 保留 `unmatched_batch_ids` 字段并新增 `diagnostics` 字段，quality debug JSON 也包含同一诊断结构。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`python3 - <<'PY' ... PY` 直接验证 missing/duplicate/quality 三类诊断、`total_failed`、`all_failed_ids` 和归一化重复 ID 判定通过；`python3 - <<'PY' ... PY` 直接验证 debug JSON 保留 `unmatched_batch_ids` 并新增 `diagnostics` 通过。未运行全量 pytest，因为 TASK 003/004/005 尚未适配旧调用方和旧测试，当前处于接口迁移中间态。

#### [TASK ID: 003] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `_process_batch` 函数
- **目标描述**: 适配 `match_llm_results` 新返回类型
- **前置依赖**: TASK 002
- **执行指令与限制**:
  1. 将 `matched, unmatched = match_llm_results(...)` 改为 `matched, diagnostics = match_llm_results(...)`
  2. 用 `diagnostics.total_failed` 替代原 `unmatched > 0` 判断
  3. stderr 进度信息改为细分格式，仅显示非零项。例如：
     - `FAILED (2 missing, 1 duplicate)` 而非 `FAILED (3 unmatched)`
     - 若仅有一种类型：`FAILED (3 quality)`
  4. `_unresolved_papers` 调用使用 `diagnostics.all_failed_ids` 过滤

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已修改 `arxiv-update/scripts/arxiv_daily.py:_process_batch`：将 `match_llm_results` 返回值解构为 `matched, diagnostics`；使用 `diagnostics.total_failed` 判断失败；新增 `_format_match_failures(diagnostics)` 生成仅包含非零分类的 stderr 失败摘要；`_unresolved_papers` 增加可选 `failed_ids` 参数，并在 `_process_batch` 中使用 `diagnostics.all_failed_ids` 过滤 unresolved paper，保持旧调用兼容。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`python3 - <<'PY' ... PY` 直接验证 `_process_batch` 在 missing/duplicate/quality 混合失败时输出 `FAILED (1 missing, 1 duplicate, 1 quality)`，且 unresolved 仅包含诊断失败 ID 通过。未运行全量 pytest，因为 TASK 004/005 尚未适配缓存路径和旧测试，当前仍处于接口迁移中间态。

#### [TASK ID: 004] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `run_phase2` 中对 cached batch 的 `match_llm_results` 调用
- **目标描述**: 适配 `run_phase2` 中缓存路径的返回值解构
- **前置依赖**: TASK 002
- **执行指令与限制**:
  1. 将 `matched, unmatched = match_llm_results(...)` 改为 `matched, diagnostics = match_llm_results(...)`
  2. 用 `diagnostics.total_failed` 替代 `unmatched` 判断
  3. stderr 中 `cached ({unmatched} unmatched)` 改为 `cached ({diagnostics.total_failed} failed)` 或细分显示

#### [TASK ID: 005] [状态: OPEN]
- **定位**: `arxiv-update/scripts/tests/test_arxiv_daily.py`
- **目标描述**: 为三种错误分类补充单元测试
- **前置依赖**: TASK 002
- **执行指令与限制**:
  1. 新增测试用例 `test_match_duplicate_id`：LLM 响应含两个相同 ID → 该 ID 进入 `diagnostics.duplicate_ids`，不在 `matched` 中
  2. 新增测试用例 `test_match_missing_id`：LLM 响应缺少某 batch paper ID → 进入 `diagnostics.missing_ids`
  3. 新增测试用例 `test_match_quality_failed`：LLM 响应 summary 长度 < 30 或 tags 为空 → 进入 `diagnostics.quality_failed`
  4. 验证 `diagnostics.total_failed` 和 `diagnostics.all_failed_ids` 计算正确
  5. 运行 `python3 -m pytest tests/test_arxiv_daily.py -q` 确认全部通过

#### [TASK ID: 006] [状态: OPEN]
- **定位**: 全文验证
- **目标描述**: 确认无回归，无遗漏的旧接口引用
- **前置依赖**: TASK 003, TASK 004, TASK 005
- **执行指令与限制**:
  1. `grep -n "unmatched" arxiv_daily.py` 确认无残留的旧变量名（日志消息中可保留 "unmatched" 一词作为自然语言描述，但变量/判断逻辑应使用 `diagnostics`）
  2. `python3 -m py_compile arxiv_daily.py` 通过
  3. 全量测试通过

## 4. 最终决策 (Final Decision)

（由 Reviewer 在最终判决 PASS 时填写）
