---
title: "arxiv cache write recovery review"
date: "2026-05-25"
status: "STATUS: REJECT"
---

## 1. 元数据 (Metadata)
- **Scope**: arxiv
- **审查目标**: 审查 `arxiv-update/scripts/arxiv_daily.py` 中 batch cache 写入、retry 回填与 cache 恢复语义是否可靠。
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`
- **规范来源**: 本项目无 `AGENTS.md` 规范文件，以通用最佳实践为准；以下 Issue 中的判定均基于通用可靠性、失败恢复与数据一致性要求，而非项目强制约定。

## 2. 设计概要 (Design Overview)
- 当前脚本在 Phase 2 对 LLM batch 结果进行缓存：先基于 metadata hash 复用 `batch_*.json`，未命中的 batch 调用 LLM 后写入 batch cache，失败 paper 在 retry 成功后再 patch 回原始 batch cache。
- cache 写入路径已使用临时文件加 `os.replace()` 写 JSON，但 batch cache 的内容语义、retry patch 的合并策略、tag 快照更新，以及 cache 目录级一致性仍存在缺陷。

## 3. 交互历史 (The Thread)

### [Round 1] (2026-05-25)
- **Reviewer**:
  - **状态判决**: `STATUS: REJECT`
    - 当前 cache 写入实现不满足失败恢复语义，尤其是 partial success 与 retry patch 后的 cache replay 会出现重复 ID 或标签状态丢失。
  - **审查摘要**: 本轮聚焦 `arxiv-update/scripts/arxiv_daily.py` 的 cache 写入相关代码。发现两个阻塞问题：初始 batch cache 写入未过滤的原始 LLM 结果，retry patch 又用 append 而非 id upsert；retry patch 只回填 paper，不回填 retry 阶段产生的 tag 状态。另有一个并发/崩溃一致性风险，按当前信息暂列非阻塞。
  - **回应 Executor**: 暂无 Executor 回复。本轮为初始审查。
  - **Issue 优先级与依赖总表**: ISSUE 001 必须先修；ISSUE 002 依赖 ISSUE 001；ISSUE 003 可后置，除非该脚本存在并发触发场景。

#### [ISSUE ID: 001] [状态: RESOLVED] (blocking)
- **定位**: `arxiv-update/scripts/arxiv_daily.py:_write_batch_cache`, `arxiv-update/scripts/arxiv_daily.py:_process_batch`, `arxiv-update/scripts/arxiv_daily.py:_patch_batch_cache`
- **问题描述**: `_process_batch()` 在确认 `diagnostics.total_failed` 之前调用 `_write_batch_cache()`，而 `_write_batch_cache()` 写入的是原始 `result.get("papers", [])`，不是 `match_llm_results()` 已接受的 `matched`。因此 quality failed、duplicate ID、或其它未被接受的 LLM 条目仍可能落入 cache。随后 retry 成功时，`_patch_batch_cache()` 使用 `cached["papers"].extend(new_papers)` 追加结果，而不是按 paper id upsert。下一次读取同一 cache 时，同一 paper id 可能同时存在旧失败条目和新成功条目，`match_llm_results()` 会把重复 ID 判为失败，导致已经通过 retry 修复的 paper 再次 unresolved，cache 不可复用。该判断基于通用失败恢复与缓存一致性要求。
- **严重程度**: 阻塞合并。
- **修复指令**: 初始 batch cache 只能写入 `matched` 中已通过 ID 匹配和质量校验的结果，格式应为 `{"id": pid, **matched[pid]}`。`_patch_batch_cache()` 必须改为按 `id` upsert，而不是 append；新结果应覆盖旧结果，同时保持稳定顺序。补充回归测试：首轮某个 paper quality failed，retry 成功回填 cache，第二次 `run_phase2()` 应直接命中 cache，不应再次调用 LLM，也不应触发 duplicate diagnostics。
- **前置依赖**: 无。
- Executor: [RESOLVED] 同意并执行。
  - 修改 `arxiv-update/scripts/arxiv_daily.py:_write_batch_cache`：签名由 `result: dict` 改为 `matched: dict[str, dict]`，仅序列化 `[{"id": pid, **payload} for pid, payload in matched.items()]`，杜绝 quality failed / duplicate / missing 条目落入 cache。
  - 修改 `arxiv-update/scripts/arxiv_daily.py:_patch_batch_cache`：用 `id` 索引对 `cached["papers"]` 做 upsert，已存在的 id 覆盖原条目，新 id 追加，并保持稳定顺序；空 id 的边缘条目按 append 处理。
  - 修改 `arxiv-update/scripts/arxiv_daily.py:_process_batch` 调用处：改为传 `matched` 而非 `result`，对齐新签名。
  - 同步更新 `tests/test_arxiv_daily.py::test_write_batch_cache_replaces_existing_file` 以匹配新签名；新增 `test_patch_batch_cache_upserts_by_id` 验证同 id 覆盖行为；新增 `test_quality_failure_then_retry_then_cache_replay` 端到端回归：首轮 quality failed → retry 成功回填 → 二次 `run_phase2()` 仅走 cache 路径（`call_llm` 零调用，`MatchDiagnostics` 无 duplicate/missing/quality_failed）。
  - 验证：`.venv/bin/pytest tests/test_arxiv_daily.py -q` → `177 passed, 1 xfailed in 17.36s`。

#### [ISSUE ID: 002] [状态: OPEN] (blocking)
- **定位**: `arxiv-update/scripts/arxiv_daily.py:_patch_batch_cache`, `arxiv-update/scripts/arxiv_daily.py:run_phase2`
- **问题描述**: retry batch 调用 `_process_batch()` 时仍会根据 LLM 返回的 `new_tags` 修改运行时 `current_tags`，但 `_patch_batch_cache()` 只把 retry 成功的 `papers` 写回原始 batch cache，完全不更新该 cache 文件中的 `new_tags` 和 `current_tags`。后续重跑命中该 batch cache 时，代码会执行 `current_tags = cached["current_tags"]`，可能把 `load_tags()` 读到的较新标签状态覆盖成旧快照。结果是 retry 阶段新发现的 tag 可能从后续 prompt 和最终 `tags.json` 中消失，尤其是在“部分 paper retry 成功但整轮仍因其它 paper 失败，cache 未清理，下一次用 `--date` 续跑”的场景。该判断基于通用状态恢复一致性要求。
- **严重程度**: 阻塞合并。
- **修复指令**: `BatchProcessResult` 需要携带本次 LLM 返回的 `new_tags` 以及处理后的 `current_tags` 快照，或提供等价机制让 retry 回填时能更新对应 batch cache 的 tag 状态。`_patch_batch_cache()` 不应只接受 `new_papers`，还应明确处理 `new_tags/current_tags` 的合并或替换策略。修复时必须保留现有“不要让后续 batch cache 的 future tags 泄漏到前序 uncached batch prompt”的约束。补充测试：首轮 batch 部分失败，retry 产生新 tag 并回填 cache，下一轮从 cache 恢复后 `current_tags` 不丢 retry tag，且未提前读取后续 batch cache 的 tags。
- **前置依赖**: ISSUE 001。

#### [ISSUE ID: 003] [状态: OPEN] (non-blocking)
- **定位**: `arxiv-update/scripts/arxiv_daily.py:load_cached_batches`, `arxiv-update/scripts/arxiv_daily.py:run_normal_mode`
- **问题描述**: 单个 `batch_*.json` 文件通过临时文件加 `os.replace()` 写入，但 cache 目录整体没有一致性边界。`metadata_hash` 使用 `hash_file.write_text(current_hash)` 非原子写入；hash 不匹配时直接删除所有 `batch_*.json`；成功后 `run_normal_mode()` 直接 `shutil.rmtree(field_cache)`。如果同一 field 有两个进程并发运行，或进程在 `metadata_hash` 写入中断，可能出现一个进程删除另一个进程刚写好的 batch cache、hash 文件半写导致下次误删有效 cache、或成功进程清掉另一个失败恢复进程正在使用的 cache。该判断基于通用并发与崩溃恢复最佳实践。
- **严重程度**: 信息性（不阻塞合并）；如果该脚本会被 cron、GitHub Actions 或人工重试并发触发，应升级为阻塞合并。
- **修复指令**: 至少将 `metadata_hash` 写入改为 `_atomic_write_text()`。如果需要支持并发运行，应在 `field_cache` 级别增加 lock file，并让 `load_cached_batches()`、batch 写入、retry patch、成功清理处于同一互斥范围。成功清理也应只清理由当前 run 拥有或确认 hash 匹配的 cache，避免跨进程误删。
- **前置依赖**: 无。

## 4. 最终决策 (Final Decision)
（仅在当前轮次给出 `STATUS: PASS` 时填写。）
