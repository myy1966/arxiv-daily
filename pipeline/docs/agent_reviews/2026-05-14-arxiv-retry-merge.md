---
title: "retry 阶段合并 batch 与缓存增补"
date: "2026-05-14"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)

- **原始需求**: 优化 retry 策略——将所有 unresolved papers 合并为新 batch 重试（batch size = BATCH_SIZE//2），并在 retry 成功后将结果增补回原 batch 缓存文件，避免重复重试
- **技术翻译**: 重构 `run_phase2` 的 retry 路径：从"逐原始 batch 重试各自的 unresolved"改为"收集所有 unresolved papers，按 RETRY_BATCH_SIZE(=BATCH_SIZE//2=8) 重新分组统一重试"；每个 retry batch 完成后，将成功匹配的 paper 增补回其所属原始 batch 的缓存文件中，确保下次运行时已成功的 paper 不再重试
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`, `arxiv-update/scripts/tests/test_arxiv_daily.py`

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)

```
run_phase2
  ├── 第一轮：按 BATCH_SIZE(16) 处理所有 papers
  │     └── 记录每个 batch 的 unresolved papers（附带 original batch_num）
  ├── 合并所有 unresolved papers
  │     └── 按 RETRY_BATCH_SIZE(8) 重新分组为 retry batches
  ├── retry 轮：逐 retry batch 调用 LLM
  │     └── 每个 retry batch 完成后：
  │           ├── 更新 results_by_id
  │           └── 将成功的 paper 增补回对应原始 batch 的缓存文件
  └── 仍失败的 paper → placeholder（patch mode 写入 markdown；normal mode 不生成 markdown）
```

### 核心接口定义 (API Contracts)

```python
RETRY_BATCH_SIZE = BATCH_SIZE // 2  # = 8，retry 时使用更小 batch 降低失败概率

def _patch_batch_cache(field_cache: Path, batch_num: int, new_papers: list[dict], logger: logging.Logger) -> None:
    """将 retry 成功的 paper 增补到原始 batch 缓存文件中。

    读取 field_cache/batch_{batch_num:03d}.json，将 new_papers 追加到 papers 列表，
    不修改 current_tags/new_tags 字段，原子写回。若缓存文件不存在则创建。
    """

@dataclass
class UnresolvedPaper:
    paper: dict              # 原始 paper dict
    original_batch_num: int  # 归属的原始 batch 编号（用于回写缓存）
```

### 风险预警 (Risk Assessment)

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

| 风险 | 应对 |
|------|------|
| retry batch 中 papers 来自不同原始 batch，回写缓存时需按 batch_num 分组 | TASK 005 中必须按 `original_batch_num` 分组后逐文件增补 |
| 缓存文件并发写入冲突 | 不存在——脚本单线程执行，无并发 |
| 增补后缓存文件中 papers 顺序可能与 prompt 顺序不一致 | 不影响——缓存加载时走 `match_llm_results` 按 ID 匹配，不依赖顺序 |
| retry batch 全部失败时不应写缓存 | `_patch_batch_cache` 仅在有成功 paper 时被调用 |
| RETRY_BATCH_SIZE 过小导致调用次数增多 | 8 篇仍是合理大小，相比当前 1-2 篇的独立调用已大幅减少次数 |
| `all_unresolved` 变量在 TASK 003 缓存路径和 TASK 005 首轮失败路径中均需使用，但声明位置不明确 | TASK 003 在 `run_phase2` 循环之前声明，TASK 005 使用时该变量已存在 |
| `run_phase2` 返回的 `failed_batches` 语义从"失败 batch 数"变为"失败 paper 数" | TASK 005 显式重定义返回语义，TASK 007 验证 `run_normal_mode` 判断逻辑兼容 |
| `_patch_batch_cache` 覆写 `current_tags` 可能导致后续 batch 缓存中 tags 回退 | `_patch_batch_cache` 仅追加 papers，不覆写 `current_tags` 字段 |
| `--no-cache` 模式下 `_patch_batch_cache` 调用未受控 | TASK 005 中缓存增补逻辑外层必须判断 `save_cache` |

## 3. 交互历史 (The Thread)

### [Round 1]

*Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行。*

#### [TASK ID: 001] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 常量定义区域 + 新增数据类
- **目标描述**: 新增 `RETRY_BATCH_SIZE` 常量和 `UnresolvedPaper` dataclass
- **前置依赖**: 无
- **执行指令与限制**:
  1. 在 `BATCH_SIZE = 16` 之后新增 `RETRY_BATCH_SIZE = BATCH_SIZE // 2`
  2. 在 `BatchProcessResult` 之后新增：
     ```python
     @dataclass
     class UnresolvedPaper:
         paper: dict
         original_batch_num: int
     ```
  3. 仅新增定义，不修改现有代码

#### [TASK ID: 002] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `_process_batch` 函数（第797-806行区域）
- **目标描述**: 首轮部分成功时也写缓存（记录已成功的 paper）
- **前置依赖**: TASK 001
- **执行指令与限制**:
  1. 当前逻辑：`unmatched > 0` 时提前 return，跳过缓存写入。改为：**无论是否有 unmatched，只要有 matched paper 就写缓存**
  2. 修改 `_process_batch` 中的控制流：
     ```python
     # 无论成功与否，有 matched 就写缓存（记录部分成功）
     if save_cache and cache_allowed and matched:
         _write_batch_cache(field_cache, batch_num, result, new_tags, current_tags)

     if unmatched > 0:
         print(f"[...] FAILED ...", file=sys.stderr)
         return BatchProcessResult(matched, _unresolved_papers(batch, matched), True)

     print(f"[...] done ...", file=sys.stderr)
     return BatchProcessResult(matched, [], False)
     ```
  3. **注意**：`_write_batch_cache` 写入的是 LLM 原始响应中的 `result["papers"]`（包含所有 LLM 返回的 paper，无论是否匹配成功）。这不需要修改——后续加载时仍通过 `match_llm_results` 按 ID 匹配，多余的条目会被忽略

#### [TASK ID: 003] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `run_phase2` 函数中加载缓存路径（第916-926行区域）
- **目标描述**: 加载缓存时检测完整性，仅对缺失的 paper 发 LLM 请求
- **前置依赖**: TASK 002
- **执行指令与限制**:
  1. **变量声明**：在 `run_phase2` 的主循环**之前**声明 `all_unresolved: list[UnresolvedPaper] = []`（本 TASK 缓存路径和 TASK 005 首轮失败路径均使用此变量）
  2. 当前逻辑：加载缓存后若 `unmatched > 0`，将整个 batch 加入 `failed_batches`。改为：**仅将 unresolved papers（缓存中未覆盖的）加入 unresolved 列表**
  3. 修改缓存命中路径：
     ```python
     if batch_num in cached_batches:
         cached = cached_batches[batch_num]
         matched, unmatched = match_llm_results(batch, cached.get("papers", []), ...)
         results_by_id.update(matched)
         if cached.get("current_tags"):
             current_tags = cached["current_tags"]

         unresolved_papers = _unresolved_papers(batch, matched)
         if unresolved_papers:
             # 缓存不完整，仅将缺失的 paper 记入 unresolved
             status = f"cached (partial: {len(matched)}/{len(batch)} matched)"
             for paper in unresolved_papers:
                 all_unresolved.append(UnresolvedPaper(paper=paper, original_batch_num=batch_num))
         else:
             status = "cached"
         print(f"[{batch_num}/{total}] {blabel} {status}", file=sys.stderr)
         continue
     ```
  4. **关键语义**：缓存中有 14/16 篇成功 → 仅 2 篇进入 `all_unresolved` → retry 时只请求这 2 篇，不重复消耗已成功的 14 篇的 token
  5. **不再将 partial cache 视为 "failed batch"**——只有完全无缓存的 batch 才走完整 LLM 路径

#### [TASK ID: 004] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 新增 `_patch_batch_cache` 函数
- **目标描述**: 实现将 retry 成功的 paper 增补回原始 batch 缓存文件的逻辑
- **前置依赖**: TASK 001
- **执行指令与限制**:
  1. 放置在 `_write_batch_cache` 函数之后
  2. 函数签名：
     ```python
     def _patch_batch_cache(field_cache: Path, batch_num: int, new_papers: list[dict], logger: logging.Logger) -> None:
     ```
  3. 逻辑：
     - 读取 `field_cache / f"batch_{batch_num:03d}.json"`，若不存在则初始化为 `{"papers": [], "new_tags": [], "current_tags": []}`
     - 将 `new_papers` 追加到 `cached["papers"]` 列表
     - **不覆写 `current_tags` 和 `new_tags` 字段**（避免后续 batch 加载缓存时 tags 回退）
     - 使用 `_atomic_write_json` 原子写回
  4. **必须使用 try-except 包裹读取逻辑**，防止缓存文件损坏时阻断整个 retry 流程（捕获异常后 `logger.warning(...)` 并初始化为空结构继续）

#### [TASK ID: 005] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `run_phase2` 函数的 retry 路径（第939-962行区域）
- **目标描述**: 重写 retry 逻辑为合并 batch + 缓存增补模式
- **前置依赖**: TASK 001, TASK 002, TASK 003, TASK 004
- **执行指令与限制**:
  1. **收集阶段改造**：第一轮循环中，首轮 `_process_batch` 失败时，将 unresolved papers 追加到 TASK 003 中已声明的 `all_unresolved`：
     ```python
     for paper in processed.unresolved:
         all_unresolved.append(UnresolvedPaper(paper=paper, original_batch_num=batch_num))
     ```
  2. **合并分组**：将 `all_unresolved` 按 `RETRY_BATCH_SIZE` 分组为 retry batches：
     ```python
     retry_batches = [
         all_unresolved[i:i+RETRY_BATCH_SIZE]
         for i in range(0, len(all_unresolved), RETRY_BATCH_SIZE)
     ]
     ```
  3. **retry 执行**：逐 retry batch 调用 `_process_batch`，参数说明：
     ```python
     still_failed_papers: list[dict] = []
     for retry_idx, retry_batch in enumerate(retry_batches):
         retry_papers = [u.paper for u in retry_batch]
         processed = _process_batch(
             batch=retry_papers,
             idx=retry_idx,                    # retry batch 自身的 0-based 索引
             total=len(retry_batches),         # retry batches 总数
             current_tags=current_tags,
             api_endpoint=api_endpoint,
             api_key=api_key,
             model=model,
             logger=logger,
             debug_dir=debug_dir,
             field_cache=field_cache,          # 传入但不会被使用（cache_allowed=False）
             label_prefix="retry",            # 区分首轮日志
             save_cache=False,                 # retry 不写独立缓存
             cache_allowed=False,
             exception_status="retry failed",
         )
         results_by_id.update(processed.matched)  # ← 必须将 retry 结果写入
         still_failed_papers.extend(processed.unresolved)
         # ... 缓存增补逻辑（见指令 4）
     ```
  4. **缓存增补**：每个 retry batch 完成后，**仅在 `save_cache=True`（外层参数）时**将 `processed.matched` 中的 paper 按 `original_batch_num` 分组，逐组调用 `_patch_batch_cache`：
     ```python
     if save_cache:
         from collections import defaultdict
         patch_groups: dict[int, list[dict]] = defaultdict(list)
         for u in retry_batch:
             if u.paper["id"] in processed.matched:
                 llm_paper = processed.matched[u.paper["id"]]  # {summary, tags, venue}
                 patch_groups[u.original_batch_num].append({"id": u.paper["id"], **llm_paper})
         for orig_num, papers in patch_groups.items():
             _patch_batch_cache(field_cache, orig_num, papers, logger)
     ```
  5. **仍失败的 paper**：循环结束后，对指令 3 中已收集的 `still_failed_papers` 调用 `_add_summary_failure_placeholders(results_by_id, still_failed_papers, logger, "retry")`（保留原有行为：patch mode 中 placeholder 会被写入 markdown）
  6. **stderr 进度**：打印 `Retrying {n} unresolved paper(s) in {m} batch(es) (batch_size={RETRY_BATCH_SIZE})...`
  7. **移除旧逻辑**：删除原有的逐 batch 重试循环（第939-962行）
  8. **返回值语义变更**：`run_phase2` 的返回值第二项改为 `failed_papers: int`——表示最终仍未解决的 **paper 数量**（而非 batch 数）。调用方 `run_normal_mode` 中 `if failed_batches > 0` 的判断改为 `if failed_papers > 0`，逻辑不变（> 0 则不生成 markdown）。**JSON 输出兼容性**：summary dict 中保留 `"failed_batches"` 字段（值设为 `1 if failed_papers > 0 else 0`，向后兼容），同时新增 `"failed_papers"` 字段表示精确的失败 paper 数量

#### [TASK ID: 006] [状态: OPEN]
- **定位**: `arxiv-update/scripts/tests/test_arxiv_daily.py`
- **目标描述**: 为首轮部分缓存、缓存完整性检测、retry 合并和缓存增补逻辑补充单元测试
- **前置依赖**: TASK 005
- **执行指令与限制**:
  1. 新增测试 `test_process_batch_partial_success_writes_cache`：首轮 batch 14/16 成功时仍写缓存文件
  2. 新增测试 `test_run_phase2_partial_cache_only_retries_missing`：加载缓存后仅对缺失 paper 发 LLM 请求（mock LLM，验证调用参数仅含缺失 paper）
  3. 新增测试 `test_patch_batch_cache_creates_file`：缓存文件不存在时创建
  4. 新增测试 `test_patch_batch_cache_appends`：缓存文件已存在时追加 papers
  5. 新增测试 `test_patch_batch_cache_corrupt_file`：缓存文件损坏时不崩溃，重新初始化
  6. 新增测试 `test_retry_merges_unresolved_across_batches`：验证多个 batch 的 unresolved papers 被合并为按 RETRY_BATCH_SIZE 分组的 retry batches（可 mock `_process_batch`）
  7. 运行 `python3 -m pytest tests/test_arxiv_daily.py -q` 确认全部通过

#### [TASK ID: 007] [状态: OPEN]
- **定位**: 全文验证
- **目标描述**: 确认无回归，整体逻辑一致
- **前置依赖**: TASK 005, TASK 006
- **执行指令与限制**:
  1. `python3 -m py_compile arxiv_daily.py` 通过
  2. 全量测试通过
  3. 确认 `run_phase2` 返回值 `failed_papers` 语义为"最终仍未解决的 paper 数量"，`run_normal_mode` 和 `run_patch_mode` 中 `if failed_papers > 0` 判断逻辑正确
  4. 确认 `--no-cache` 模式下首轮不写缓存、retry 不调用 `_patch_batch_cache`
  5. 验证完整流程场景：首轮部分失败 → 写部分缓存 → 第二次运行加载部分缓存 → 仅 retry 缺失 paper → 增补缓存 → 第三次运行全部从缓存加载

## 4. 最终决策 (Final Decision)

（由 Reviewer 在最终判决 PASS 时填写）
