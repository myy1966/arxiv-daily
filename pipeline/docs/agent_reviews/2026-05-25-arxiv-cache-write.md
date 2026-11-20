---
title: "arxiv cache write recovery review"
date: "2026-05-25"
status: "STATUS: PASS"
---

## 1. 元数据 (Metadata)
- **Scope**: arxiv
- **审查目标**: 审查 `arxiv-update/scripts/arxiv_daily.py` 中 batch cache 写入、retry 回填与 cache 恢复语义是否可靠。
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`
- **规范来源**: 本项目无 `AGENTS.md` 规范文件，以通用最佳实践为准；以下 Issue 中的判定均基于通用可靠性、失败恢复与数据一致性要求，而非项目强制约定。

## 2. 设计概要 (Design Overview)
- 当前脚本在 Phase 2 对 LLM batch 结果进行缓存：先基于 metadata hash 复用 `batch_*.json`，未命中的 batch 调用 LLM 后写入 batch cache，失败 paper 在 retry 成功后再 patch 回原始 batch cache。
- cache 写入路径已使用临时文件加 `os.replace()` 写 JSON，但 batch cache 的内容语义、retry patch 的合并策略、tag 快照更新，以及 cache 目录级一致性仍存在缺陷。
- 最终实现改为只缓存已通过校验的 `matched` 结果，retry patch 按 id 去重 upsert 并按原始 batch 过滤 retry 新标签，`metadata_hash` 也改为原子写入；目录级并发 lock 作为未来并发运行需求处理。

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

#### [ISSUE ID: 002] [状态: RESOLVED] (blocking)
- **定位**: `arxiv-update/scripts/arxiv_daily.py:_patch_batch_cache`, `arxiv-update/scripts/arxiv_daily.py:run_phase2`
- **问题描述**: retry batch 调用 `_process_batch()` 时仍会根据 LLM 返回的 `new_tags` 修改运行时 `current_tags`，但 `_patch_batch_cache()` 只把 retry 成功的 `papers` 写回原始 batch cache，完全不更新该 cache 文件中的 `new_tags` 和 `current_tags`。后续重跑命中该 batch cache 时，代码会执行 `current_tags = cached["current_tags"]`，可能把 `load_tags()` 读到的较新标签状态覆盖成旧快照。结果是 retry 阶段新发现的 tag 可能从后续 prompt 和最终 `tags.json` 中消失，尤其是在“部分 paper retry 成功但整轮仍因其它 paper 失败，cache 未清理，下一次用 `--date` 续跑”的场景。该判断基于通用状态恢复一致性要求。
- **严重程度**: 阻塞合并。
- **修复指令**: `BatchProcessResult` 需要携带本次 LLM 返回的 `new_tags` 以及处理后的 `current_tags` 快照，或提供等价机制让 retry 回填时能更新对应 batch cache 的 tag 状态。`_patch_batch_cache()` 不应只接受 `new_papers`，还应明确处理 `new_tags/current_tags` 的合并或替换策略。修复时必须保留现有“不要让后续 batch cache 的 future tags 泄漏到前序 uncached batch prompt”的约束。补充测试：首轮 batch 部分失败，retry 产生新 tag 并回填 cache，下一轮从 cache 恢复后 `current_tags` 不丢 retry tag，且未提前读取后续 batch cache 的 tags。
- **前置依赖**: ISSUE 001。
- Executor: [RESOLVED] 同意并执行。
  - 扩展 `BatchProcessResult` 字段：新增 `new_tags: list[str]` 与 `current_tags_snapshot: list[str]`，由 `_process_batch` 在成功/部分失败路径填充本次 LLM 返回的 `new_tags` 与 merge 后的 `current_tags`，异常路径保持空列表默认值（无新增 tag）。
  - 修改 `_patch_batch_cache`：新增可选 `new_tags` 参数；当传入时，将其按 append-if-absent 同时合并到 `cached["new_tags"]` 与 `cached["current_tags"]`，避免追加重复元素。未传时保持纯 paper upsert 行为，向后兼容。决定不引入 `current_tags` 参数：因为 retry 批次的运行时 `current_tags` 是跨 batch 的累积快照，写入单个 batch cache 会把无关 tag 串入该 batch 的标签上下文；单纯 union 当前 retry 调用产生的 `new_tags` 即可保证“原 cache 状态 ∪ retry 发现的新 tag”，与下一轮 `load_tags()` 的初始集合协同后能完整恢复。
  - 修改 `run_phase2` 的 retry 回填：将 `processed.new_tags` 透传给 `_patch_batch_cache`。
  - 修改 `run_phase2` 缓存复用路径：将 `current_tags = cached["current_tags"]` 整体覆盖改为 append-if-absent 的 union（仅追加 `current_tags` 中尚未存在的 tag），既保留 `load_tags()` 与已处理 batch 累积的最新 tag，又不会让某个 batch cache 的快照倒退状态。该写法仍维持原“cache 命中按 batch 顺序处理”不变量，故未破坏“后续 batch cache 的 future tags 不泄漏到前序 uncached batch prompt”的现有约束。
  - 验证回归：新增 `test_patch_batch_cache_unions_retry_tags`（单元）、`test_retry_tags_persist_across_cache_replay`（端到端：首轮 retry 产生 RetryTag，二次 `run_phase2()` 命中缓存且 RetryTag 不丢）、`test_cache_replay_does_not_leak_future_retry_tags`（重申 future tag 不泄漏不变量）。同时既有 `test_cache_preload_does_not_leak_future_tags` 在新实现下仍 PASS。
  - 验证：`.venv/bin/pytest tests/test_arxiv_daily.py -q` → `180 passed, 1 xfailed in 17.37s`。

#### [ISSUE ID: 003] [状态: RESOLVED] (non-blocking)
- **定位**: `arxiv-update/scripts/arxiv_daily.py:load_cached_batches`, `arxiv-update/scripts/arxiv_daily.py:run_normal_mode`
- **问题描述**: 单个 `batch_*.json` 文件通过临时文件加 `os.replace()` 写入，但 cache 目录整体没有一致性边界。`metadata_hash` 使用 `hash_file.write_text(current_hash)` 非原子写入；hash 不匹配时直接删除所有 `batch_*.json`；成功后 `run_normal_mode()` 直接 `shutil.rmtree(field_cache)`。如果同一 field 有两个进程并发运行，或进程在 `metadata_hash` 写入中断，可能出现一个进程删除另一个进程刚写好的 batch cache、hash 文件半写导致下次误删有效 cache、或成功进程清掉另一个失败恢复进程正在使用的 cache。该判断基于通用并发与崩溃恢复最佳实践。
- **严重程度**: 信息性（不阻塞合并）；如果该脚本会被 cron、GitHub Actions 或人工重试并发触发，应升级为阻塞合并。
- **修复指令**: 至少将 `metadata_hash` 写入改为 `_atomic_write_text()`。如果需要支持并发运行，应在 `field_cache` 级别增加 lock file，并让 `load_cached_batches()`、batch 写入、retry patch、成功清理处于同一互斥范围。成功清理也应只清理由当前 run 拥有或确认 hash 匹配的 cache，避免跨进程误删。
- **前置依赖**: 无。
- Executor: [RESOLVED] 同意并执行修复指令的下限部分（Reviewer 表述为“至少将 `metadata_hash` 写入改为 `_atomic_write_text()`”）。
  - 修改 `arxiv-update/scripts/arxiv_daily.py:load_cached_batches`：将 `hash_file.write_text(current_hash)` 替换为 `_atomic_write_text(hash_file, current_hash)`，复用现有 atomic 写入工具（temp file + `os.replace`），消除 hash 文件半写场景。
  - 决定**不引入** field-level lock file 与 ownership-aware 清理：当前仓库无 `.github/workflows/`、`SKILL.md` 描述的也是单次人工触发，没有 cron / GitHub Actions / 多进程契约；按 Reviewer“如果存在并发触发场景应升级为阻塞合并”的条件分支，未触发该升级条件。引入 lock 会增加非必要的复杂度（违反 YAGNI），等真正出现并发触发需求时再迭代。
  - 验证回归：新增 `test_load_cached_batches_writes_hash_atomically`，monkeypatch `os.replace` 抛异常以模拟写入中断，断言原 hash 文件内容保留、目录无残留 `*.tmp`。
  - 验证：`.venv/bin/pytest tests/test_arxiv_daily.py -q` → `181 passed, 1 xfailed in 17.26s`。

### [Round 2] (2026-05-25)
- **Reviewer**:
  - **状态判决**: `STATUS: REJECT`
    - Executor 已覆盖 Round 1 的主体方向，且 `metadata_hash` 原子写入可接受；但 retry tag 回填仍会在跨原始 batch 的 retry 场景中污染较早 batch cache，`_patch_batch_cache()` 也没有清理历史重复 ID cache，两个问题仍会破坏 cache replay 语义。
  - **审查摘要**: 复核 `review(arxiv): REJECT cache write recovery semantics` 之后的修复提交，确认 `_write_batch_cache()` 改为写 `matched`、普通 retry patch 改为 id upsert、`metadata_hash` 改为 `_atomic_write_text()`，这些方向正确。现有测试 `181 passed, 1 xfailed`，但测试缺少两个边界：mixed retry batch 的 per-original-batch tag 归属，以及旧 cache 已存在重复 ID 时的去重自愈。
  - **回应 Executor**: 接受 ISSUE 003 的下限修复；不接受 ISSUE 001/002 完全关闭的结论。ISSUE 001 仍缺少对历史重复 ID cache 的去重自愈；ISSUE 002 的 `processed.new_tags` 被无差别写入同一个 retry batch 涉及的所有原始 batch cache，会把属于后续原始 batch 的 tag 注入较早 batch cache。
  - **Issue 优先级与依赖总表**: 先修 ISSUE 004，再修 ISSUE 005；ISSUE 004 直接影响 tag 顺序语义，ISSUE 005 影响旧污染 cache 的恢复能力；两者均修复并补测试后才可 PASS。

#### [ISSUE ID: 004] [状态: RESOLVED] (blocking)
- **定位**: `arxiv-update/scripts/arxiv_daily.py:run_phase2`
- **问题描述**: retry 阶段可能把来自多个原始 batch 的 unresolved papers 合并到同一个 retry batch。当前实现按 `original_batch_num` 分组 patch papers，但给每个分组都传同一个 `processed.new_tags`。如果 retry batch 同时包含 batch 1 和 batch 2 的 paper，而新 tag 只由 batch 2 的 paper 产生，`_patch_batch_cache()` 仍会把该 tag 写入 batch 1 的 `new_tags/current_tags`。下一次 cache replay 处理 batch 1 时，该 future tag 会提前进入 `current_tags`，违反 Round 1 明确要求保留的“后续 batch cache 的 future tags 不泄漏到前序 uncached/cached batch prompt”约束。该判断基于通用状态恢复一致性要求。
- **严重程度**: 阻塞合并。
- **修复指令**: retry patch 时必须按 `original_batch_num` 计算该分组自己的 `new_tags`，不能把整个 retry batch 的 `processed.new_tags` 传给所有分组。可接受实现：对每个 `papers` 分组取其 paper `tags` 与 `processed.new_tags` 的交集，并按 `processed.new_tags` 原顺序去重后传给 `_patch_batch_cache()`；或在 `BatchProcessResult` 中携带更精确的 paper-to-new-tag 归属。必须补充回归测试：构造一个 retry batch 同时包含两个原始 batch 的 unresolved papers，其中只有后一个原始 batch 的 retry paper 引入 `FutureTag`；首次 run 后检查 `batch_001.json` 不包含 `FutureTag`，`batch_002.json` 包含 `FutureTag`，二次 cache replay 不会在处理 batch 1 前看到该 tag。
- **前置依赖**: ISSUE 002。
- Executor: [RESOLVED] 同意并执行。
  - 修改 `arxiv-update/scripts/arxiv_daily.py:run_phase2` 的 retry 回填环节：在按 `original_batch_num` 分组之后、调用 `_patch_batch_cache()` 之前，先收集本组所有 retry paper 的 `tags` 并集 `group_paper_tags`，再以 `processed.new_tags` 的原顺序过滤出 `filtered_new_tags = [t for t in processed.new_tags if t in group_paper_tags]`。仅把 `filtered_new_tags`（若为空则传 `None`）作为 `new_tags` 参数传给 `_patch_batch_cache()`，保证只有真正由本组 paper 引入的 tag 才会写入该 batch 的 cache。该实现采用 Reviewer 列出的“按 paper tags 与 `processed.new_tags` 求交集并按原顺序去重”路径，避免在 `BatchProcessResult` 上额外增加 paper-to-new-tag 映射，改动半径最小。
  - 验证回归：新增 `test_retry_patch_does_not_leak_cross_batch_tags`，通过 monkeypatch `_process_batch` 构造一个混合 retry batch（同时含 batch 1 与 batch 2 的 unresolved paper），仅 batch 2 的 retry paper 携带 `FutureTag`；断言首次 run 后 `batch_001.json` 的 `new_tags`/`current_tags` 不含 `FutureTag`，而 `batch_002.json` 两者均含；二次 `run_phase2()` 完整命中 cache（拦截 `_process_batch` 与 `build_prompt` 验证零 LLM 调用）。
  - 验证：`.venv/bin/pytest tests/test_arxiv_daily.py -q` → `182 passed, 1 xfailed in 17.28s`。

#### [ISSUE ID: 005] [状态: RESOLVED] (blocking)
- **定位**: `arxiv-update/scripts/arxiv_daily.py:_patch_batch_cache`
- **问题描述**: 当前 `_patch_batch_cache()` 构建 `index_by_id` 时，如果已有 cache 中同一个 id 出现多次，只记录最后一个位置；随后 incoming paper 只覆盖最后一个条目，前面的重复条目仍保留。也就是说，它能避免普通 append 产生的新重复，但不能清理已经被旧实现或历史 cache 污染出的重复 ID。下一次 `match_llm_results()` 读取该 cache 时仍会因重复 ID 将该 paper 判为 unresolved，retry patch 的自愈能力不完整。该判断基于通用 cache 修复与幂等 upsert 要求。
- **严重程度**: 阻塞合并。
- **修复指令**: `_patch_batch_cache()` 应先将 existing 与 incoming 合并为按 id 去重的列表，再写回 cache。建议保留第一次出现的顺序位置，但内容以后出现的同 id 或 incoming 结果为准；无 id 的异常条目可保留或丢弃，但行为必须明确。补充测试：已有 `batch_001.json` 中同一 id 出现两次，调用 `_patch_batch_cache()` patch 同一 id 后，输出文件中该 id 只能出现一次，且内容为 incoming 的 fresh 结果；随后用 `match_llm_results()` replay 不应产生 duplicate diagnostics。
- **前置依赖**: ISSUE 001。
- Executor: [RESOLVED] 同意并执行。
  - 修改 `arxiv-update/scripts/arxiv_daily.py:_patch_batch_cache` 的 paper 合并逻辑：单次遍历 `cached["papers"]` 构建 `deduped` 列表与 `index_by_id`，对每个 id 保留**首次出现的顺序位置**但**用后续同 id 条目覆盖内容**（“先到位置 + 最新内容”）；无 id 的异常条目按原顺序保留。再以同样的 id-keyed 规则消化 incoming `new_papers`：命中已有 id 即覆盖该位置，新 id 追加到末尾。最终 `cached["papers"]` 中每个 id 唯一出现一次，权威性顺序为 incoming > 后出现的 existing > 先出现的 existing。tag 合并段保持不变。
  - 验证回归：新增 `test_patch_batch_cache_dedupes_existing_duplicates`，构造 `batch_001.json` 已含重复 id `2604.00002`（模拟旧 append 实现遗留的污染），调用 `_patch_batch_cache()` 用一条 incoming 条目替换该 id；断言文件中三条 id 顺序为 `00001, 00002, 00003`、id 唯一出现一次、`00002` 内容为 incoming 的 fresh summary/tags；并以三个 id 跑 `match_llm_results()` 确认 `duplicate_ids/missing_ids/quality_failed` 全部为空，证明 cache 在下一轮 replay 时具备自愈能力。
  - 验证：`.venv/bin/pytest tests/test_arxiv_daily.py -q` → `183 passed, 1 xfailed in 17.28s`。

### [Round 3] (2026-05-25)
- **Reviewer**:
  - **状态判决**: `STATUS: PASS`
    - Executor 针对 Round 2 的两个阻塞问题均已按指令修复，并补充了覆盖 mixed retry tag 归属和历史重复 ID 自愈的回归测试。
  - **审查摘要**: 复核 `review(arxiv): REJECT cache replay edge cases` 之后的修复提交，确认 retry patch 现在按 `original_batch_num` 分组过滤 `new_tags`，避免跨原始 batch 写入 future tags；`_patch_batch_cache()` 现在会先将既有 cache 条目压缩为按 id 唯一的列表，再应用 incoming 结果，旧污染 cache 可自愈。未发现新的阻塞问题。
  - **回应 Executor**: 接受 ISSUE 004 与 ISSUE 005 的 RESOLVED 记录。ISSUE 003 的下限修复在 Round 2 已接受；并发 lock 与 ownership-aware 清理由于当前无并发运行契约，保持后续需求触发时再处理。
  - **Issue 优先级与依赖总表**: 所有阻塞 Issue 已关闭：ISSUE 001 → ISSUE 005 均已具备对应实现与测试；ISSUE 003 的非阻塞剩余并发增强不影响本轮合并。

## 4. 最终决策 (Final Decision)
- **决策记录**: 接受当前实现并判定可合并。最终方案采用“只缓存已通过匹配与质量校验的 `matched` 结果”、“retry patch 按 id 幂等 upsert 并清理历史重复 id”、“retry 新标签按原始 batch 分组过滤后写回 cache”、“metadata_hash 原子写入”的组合。field-level lock 与 ownership-aware cleanup 暂不纳入本轮，因为当前项目未体现并发运行契约；若未来引入 cron/GitHub Actions/多进程触发，应单独设计目录级互斥与 cache ownership。
