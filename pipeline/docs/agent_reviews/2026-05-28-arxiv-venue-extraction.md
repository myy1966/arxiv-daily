---
title: "分离 Venue 提取为独立阶段"
date: "2026-05-28"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)
- **原始需求**: Venue 提取与 summary/tag 生成混合在同一 LLM 调用中，导致交叉污染（论文A的venue信息泄漏到论文B）和幻觉（无 Comments 的论文被标注 venue）。
- **技术翻译**: 将 venue 提取从生成式 batch LLM 调用中剥离，改为确定性规则优先 + 轻量 LLM fallback 的独立阶段，消除 batch 内论文间的信息泄漏，并通过前置 keyword gate 杜绝假阳性。
- **Scope**: `arxiv`
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)

```
Phase 1: HTML Parse → papers[] (含 comments 字段)
                          │
          ┌───────────────┼──────────────────┐
          ▼                                  ▼
Phase 2: LLM Summarization           Phase 2.5: Venue Extraction (NEW)
  (summary + tags only)                 │
  batch=16, 无 venue 指令               ├─ Step A: keyword gate (确定性)
                                        │   comments 无 accepted/published/...
                                        │   → venue = null (跳过 LLM)
                                        │
                                        ├─ Step B: LLM extraction (batch=16)
                                        │   仅对通过 gate 的论文调用 LLM
                                        │   输入: 每 batch 16 篇的 comments 文本
                                        │   输出: {"venues": [{"id", "venue"}]}
                                        │
                                        └─ Step C: 后处理校验
                                            grounding 失败 → venue = None
                                            venue 长度 > 60 → warning (不截断)
          │                                  │
          ▼                                  ▼
Phase 3: Merge results_by_id = {summary, tags, venue}
          │
          ▼
Phase 4: Markdown Generation (不变)
```

### 核心接口定义 (API Contracts)

```python
# Keyword gate — 确定性判断是否需要提取 venue
def has_acceptance_signal(comments: str | None) -> bool: ...
    # 入参: 论文的 comments 字段 (可能为 None 或空字符串)
    # 出参: True 表示 comments 中含有 accepted/published 等关键词
    # 行为: 纯正则/字符串匹配，不调用 LLM

# Venue batch prompt 构建
def build_venue_prompt(papers: list[dict]) -> list[dict]: ...
    # 入参: 通过 gate 的论文列表 (每篇含 id 和 comments 字段)，batch 大小上限 16
    # 出参: chat messages (system + user)
    # 行为: 构建仅含 comments 的 batch prompt
    # LLM 响应 schema: {"venues": [{"id": "...", "venue": "..." or null}]}
    #   注: 顶层为 object 而非 array，与现有 summary schema 风格一致

# 独立 venue LLM 调用 (不复用 call_llm)
def call_venue_llm(
    messages: list[dict],
    api_endpoint: str, api_key: str, model: str, logger,
    debug_dir: Path | None = None,
) -> dict | None: ...
    # 行为:
    #   - temperature=0 (确定性提取)
    #   - 不使用 streaming (响应极短)
    #   - 独立 schema 校验: 顶层为 dict 且含 "venues" list
    #   - 重试策略: 最多 2 次重试，429 遵守 Retry-After 或指数退避 (2s, 4s)
    #   - 非 429 的 4xx 不重试，直接返回 None
    #   - 超时阈值: 30s (常量化)
    #   - 失败时: 若 debug_dir 提供，保存 prompt 和原始响应到 debug_dir
    #   - 返回: 解析后的 dict，或 None (所有异常由本函数处理，不向上抛出)

# Batch venue 提取入口
def extract_venues_batch(
    papers: list[dict],
    api_endpoint, api_key, model, logger,
    field_cache: Path | None = None,
    metadata_hash: str | None = None,
) -> dict[str, str | None]: ...
    # 入参: 所有论文列表; field_cache 和 metadata_hash 用于 cache resume
    # 出参: {paper_id: venue_or_none}
    # 行为:
    #   1. 对每篇论文执行 has_acceptance_signal 过滤
    #   2. 未通过 gate 的 → venue = None
    #   3. 若 field_cache 提供且 venue_cache.json 存在且 hash 匹配，加载已有结果
    #   4. 通过 gate 但未缓存的 → 按 batch_size=16 分组，调用 call_venue_llm
    #   5. 对每条 LLM 返回的 venue 执行 grounding 校验 (见后处理规约)
    #   6. 写入 venue_cache.json (若 field_cache 提供)
    #   7. 合并结果返回
```

### 风险预警 (Risk Assessment)

> **注意**: 本项目无 `AGENTS.md` 规范文件，以下设计基于通用最佳实践。各 TASK 中标注了需要人类确认的技术选型决策。

1. **API 调用量**: 通过 gate 的论文约 80-100 篇 (cs.AI)，batch=16 意味着 5-7 次 LLM 调用，开销极低。
2. **缓存一致性**: venue 结果需写入现有 batch cache 结构，或独立 venue cache。需确保与 summary/tag 的 cache 生命周期一致。
3. **向后兼容**: `generate_markdown` 和 `patch_markdown` 的消费方式不变 — 它们只读 `results_by_id[pid]["venue"]`，无需修改。
4. **Gate 漏判**: keyword gate 可能漏掉罕见表达（如 "forthcoming in"、"to be presented"）。gate 应设计为宽松匹配（宁可多送 LLM 几篇），不可错杀。

## 3. 交互历史 (The Thread)

### [Round 1]

*(Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行)*

#### [TASK ID: 001] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 新增 `has_acceptance_signal` 函数
- **目标描述**: 实现确定性 keyword gate，判断 comments 是否包含论文被接收/发表的信号
- **前置依赖**: 无
- **执行指令与限制**:
  - 放置在 Phase 2 (LLM Summarization) 代码块之后
  - 接口签名: `def has_acceptance_signal(comments: str | None) -> bool`
  - 行为规约:
    - 若 comments 为 None 或空字符串，返回 False
    - 匹配关键词列表: accepted, published, to appear, camera-ready, appearing at, presented at, forthcoming, to be presented
    - 匹配必须为大小写不敏感
    - 必须排除干扰模式: "submitted to"、"under review at"、仅引用格式 ("style" 后无 accepted)
    - 排除 "code available" 等无关 Comments
  - 验证: 对以下 Comments 文本应返回预期结果:
    - "Accepted at OccuSys 2026" → True
    - "7 pages" → False
    - "Submitted to ICML 2026" → False
    - "ICML 2026 style, 9 pages" → False
    - "Accepted for publication in AI and Ethics" → True
    - "published in Advancements in AI..., Springer, 2026" → True
    - "Codes are available" → False
  - 验证命令:
    ```bash
    PYTHONPATH=arxiv-update/scripts python -c "
    from arxiv_daily import has_acceptance_signal
    assert has_acceptance_signal('Accepted at OccuSys 2026') == True
    assert has_acceptance_signal('7 pages') == False
    assert has_acceptance_signal('Submitted to ICML 2026') == False
    assert has_acceptance_signal('ICML 2026 style, 9 pages') == False
    assert has_acceptance_signal('Accepted for publication in AI and Ethics') == True
    assert has_acceptance_signal('published in Advancements in AI, Springer, 2026') == True
    assert has_acceptance_signal('Codes are available') == False
    assert has_acceptance_signal(None) == False
    assert has_acceptance_signal('') == False
    print('TASK 001: all assertions passed')
    "
    ```

#### [TASK ID: 002] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 新增 `build_venue_prompt`、`call_venue_llm` 和 `extract_venues_batch` 函数
- **目标描述**: 实现独立的 venue 提取管线：prompt 构建、LLM 调用、响应解析与后处理
- **前置依赖**: TASK 001
- **需人类确认**: 是否使用与 summary 相同的 model/endpoint，或允许配置独立 venue model
- **执行指令与限制**:
  - 接口签名见设计概要
  - `build_venue_prompt`:
    - 仅输入每篇论文的 ID 和 Comments 文本（不含 abstract/title）
    - 要求 LLM 响应 schema: `{"venues": [{"id": "...", "venue": "..." or null}]}`
    - prompt 约束: 仅从 Comments 文本中提取，不得推测或编造；年份仅在明确出现时包含；使用最具体 venue 名称；期刊与会议同等对待；无法确定时返回 null
  - `call_venue_llm`:
    - **不复用**现有 `call_llm` — 独立实现，逻辑更简单
    - temperature=0，不使用 streaming，response_format=json_object
    - 独立 schema 校验: 顶层为 dict 且含 `"venues"` list，每条须含 `"id"` 和 `"venue"` 字段
    - 返回类型: `dict | None` — 所有异常在函数内处理，不向上抛出
    - 重试策略: 最多 2 次重试；429 遵守 Retry-After 或指数退避 (2s, 4s)；非 429 的 4xx 不重试
    - 超时阈值: 30s（定义为模块常量 `VENUE_LLM_TIMEOUT`）
    - 失败时: 若传入 debug_dir，保存 prompt 和原始响应到 `debug_dir/venue_batch_NNN_error.json`
  - `extract_venues_batch`:
    - 先过滤 `has_acceptance_signal`
    - 通过 gate 的论文按 batch_size=16 分组，每组调用 `call_venue_llm`
    - **Grounding 后处理**: 对每条返回的 venue，执行宽松匹配校验 — venue 中至少有一个非停用词 token（大小写不敏感）出现在对应论文的 Comments 中。允许 LLM 对年份格式化（'26 → 2026）和常见缩写展开。宽松匹配失败的 venue 置为 None 并记录 warning。
    - **长度限制**: venue 字符串超过 60 字符时记录 warning（不自动截断，留给 Reviewer 评估）
    - 超时/网络错误时该 batch 所有 venue 设为 None（不阻塞整体流程）
    - 返回完整的 {paper_id: venue_or_none} 映射
  - 验证命令:
    ```bash
    # TASK 002: 验证 build_venue_prompt 输出格式
    PYTHONPATH=arxiv-update/scripts python -c "
    from arxiv_daily import build_venue_prompt
    papers = [{'id': '2605.27446', 'comments': 'Accepted at SMACD 2026'}]
    msgs = build_venue_prompt(papers)
    assert isinstance(msgs, list) and len(msgs) == 2
    assert msgs[0]['role'] == 'system'
    print('TASK 002: build_venue_prompt format OK')
    "
    ```

#### [TASK ID: 003] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 修改 `build_prompt` 函数
- **目标描述**: 从 summary/tag 的 LLM prompt 中移除 venue 相关指令和 JSON 字段
- **前置依赖**: TASK 001, TASK 002
- **执行指令与限制**:
  - 删除 `build_prompt` 中 venue 提取的第 3 条指令（当前第 491-496 行区域）
  - 从 JSON schema 示例中移除 `"venue"` 字段
  - `build_prompt` 的 Papers 部分仍然保留 Comments 字段传入（tag 判断可能依赖 Comments 中的会议名称信息）
  - 不修改 `build_prompt` 中 summary 和 tag 相关的指令
  - 验证命令:
    ```bash
    PYTHONPATH=arxiv-update/scripts python -c "
    from arxiv_daily import build_prompt
    msgs = build_prompt([{'id': 'test', 'title': 'T', 'abstract': 'A', 'comments': 'Accepted at X'}], ['tag1'])
    content = msgs[1]['content']
    assert 'venue' not in content.lower(), 'venue instruction still present in prompt'
    assert 'summary' in content.lower(), 'summary instruction missing'
    print('TASK 003: venue removed, summary/tag preserved')
    "
    ```

#### [TASK ID: 004] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 修改 `run_normal_mode` 和 `run_patch_mode`，集成 venue 提取阶段
- **目标描述**: 在两条执行路径中，于 `run_phase2` 完成后、markdown 生成/patch 之前插入 venue 提取
- **前置依赖**: TASK 001, TASK 002, TASK 003, TASK 005
- **执行指令与限制**:
  - **Normal mode** (`run_normal_mode`):
    - 在 `run_phase2` 返回 `results_by_id` 之后、`generate_markdown` 之前调用 `extract_venues_batch`
    - 传入所有 papers（从 `data["sections"]` 中 flatten: `[p for s in data["sections"] for p in s["papers"]]`）
    - 传入 `field_cache` 和 `metadata_hash`（复用已计算的值）
    - 将返回的 venue 映射 merge: 仅更新 `results_by_id` 中已存在的 paper id — `if pid in results_by_id: results_by_id[pid]["venue"] = venues[pid]`
  - **Patch mode** (`run_patch_mode`):
    - 在 `run_phase2` 返回 `results_by_id` 之后、`patch_markdown` 之前调用 `extract_venues_batch`
    - 传入 `patch_papers` 列表
    - 同样 merge venue 到 `results_by_id`
    - patch mode 使用 `no_cache=True` 语义（因为 save_cache=False），venue 也不缓存
  - 如果 LLM 响应中仍然返回了 venue 字段（向后兼容旧 cache），取新提取的值覆盖
  - 日志输出: 记录通过 gate 的论文数、成功提取的 venue 数
  - 若 venue 提取整体失败（如 API 不可用），不阻塞流程，所有 venue 设为 None
  - 验证命令:
    ```bash
    # TASK 004: 端到端验证 (使用已有 HTML cache)
    # Normal mode: 验证输出 markdown 包含 **Venue** 行
    python arxiv-update/scripts/arxiv_daily.py cs.AR --date 2026-05-28 --tags arxiv-update/data/tags.json --output /tmp/test_venue
    grep -q "Venue" /tmp/test_venue/2026-05-28-cs.AR.md && echo "TASK 004 normal: venue present"

    # Patch mode: 验证 patch 也包含 venue
    python arxiv-update/scripts/arxiv_daily.py cs.AR --date 2026-05-28 --tags arxiv-update/data/tags.json --patch 2605.27446 --output /tmp/test_venue
    grep -q "SMACD" /tmp/test_venue/2026-05-28-cs.AR.md && echo "TASK 004 patch: venue patched"
    ```

#### [TASK ID: 005] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — venue 缓存策略
- **目标描述**: 将 venue 提取结果写入临时 cache 文件，作为中断恢复的检查点
- **前置依赖**: TASK 002
- **执行指令与限制**:
  - 在 `field_cache` 目录下创建 `venue_cache.json`，格式:
    ```json
    {"metadata_hash": "<hash_string>", "venues": {"paper_id": "venue_or_null", ...}}
    ```
  - 每完成一个 batch 的 venue 提取即追加写入（增量 merge venues 字典）
  - `extract_venues_batch` 的 cache 逻辑（当 field_cache 不为 None 时）:
    - 读取 `venue_cache.json`，比较 `metadata_hash` 字段与传入的 `metadata_hash` 参数
    - 若 hash 匹配，跳过已有结果的论文 ID，仅对缺失的论文调用 LLM
    - 若 hash 不匹配或文件不存在，丢弃旧 cache，从零开始提取
    - 每个 batch 完成后立即写入（原子写），确保中断安全
  - **生命周期**: 与 summary batch cache 一致 — 正常流程成功完成后，`field_cache` 整个目录被 `shutil.rmtree` 删除，venue_cache.json 随之消失
  - **不是持久化 cache** — 其价值仅在于中断重跑时避免重复 LLM 调用
  - 当 `field_cache=None`（如 patch mode）时不读写 cache
  - 不修改现有 batch_NNN.json 的结构（summary/tag cache 不受影响）
  - 验证命令:
    ```bash
    # TASK 005: 验证 cache resume 逻辑
    PYTHONPATH=arxiv-update/scripts python -c "
    import json, tempfile
    from pathlib import Path
    from unittest.mock import patch as mock_patch
    from arxiv_daily import extract_venues_batch, has_acceptance_signal

    # Setup: 预填充 venue_cache.json
    tmp = Path(tempfile.mkdtemp())
    cache_data = {'metadata_hash': 'test_hash', 'venues': {'2605.27446': 'SMACD 2026'}}
    (tmp / 'venue_cache.json').write_text(json.dumps(cache_data))

    papers = [{'id': '2605.27446', 'comments': 'Accepted at SMACD 2026'}]

    # Mock call_venue_llm to track if it's called
    with mock_patch('arxiv_daily.call_venue_llm') as mock_llm:
        result = extract_venues_batch(papers, '', '', '', None, field_cache=tmp, metadata_hash='test_hash')
        assert mock_llm.call_count == 0, 'LLM should not be called when cache hit'
        assert result['2605.27446'] == 'SMACD 2026'
    print('TASK 005: cache resume verified')
    "
    ```

## 4. 最终决策 (Final Decision)
（由 Reviewer 在最终判决 PASS 时填写）

## 5. 架构讨论

### Architect Review

评审结论：方向可接受，但当前蓝图不建议直接交给 Executor 执行。主要问题集中在协议合规、现有代码契约兼容性、缓存生命周期和验证闭环。

1. **项目规范缺失声明不足**
   - 当前项目根目录未发现 `AGENTS.md`。
   - 按协作协议，本 ADR 的风险预警应明确标注：本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。
   - 每个 Task 也应注明需要人类确认的技术选型决策，避免 Executor 将默认假设误当作项目规范。

2. **Venue LLM 输出契约与现有 `call_llm` 不兼容**
   - 当前 ADR 要求 `build_venue_prompt` 返回 JSON 数组：`[{id, venue}]`。
   - 现有 `call_llm` 会通过 `_validate_llm_schema` 校验响应，要求顶层必须是 JSON object 且包含 `papers` list。
   - 因此如果复用现有 `call_llm`，数组响应会被直接判为非法。
   - 建议二选一：将 venue 响应契约调整为兼容现有 schema 的 object，或新增独立 venue LLM 调用/解析接口，不复用 summary/tag 的 schema validator。

3. **`temperature=0` 约束当前不可执行**
   - ADR 要求 venue 提取使用 `temperature=0`。
   - 现有 `call_llm` payload 中 temperature 固定为 `0.3`。
   - 如果继续要求复用 `call_llm`，需要先增加可配置 temperature 参数；否则应明确允许 venue 使用独立 caller。

4. **Batch=16 是需求约束，但仍需 grounding 防御**
   - Venue 提取继续使用 batch=16 是需求方明确要求，不作为阻断问题。
   - 但 batch 输出仍应增加确定性后处理校验：每条 LLM 返回的 venue 必须能在同 ID paper 的 Comments 中直接或可解释地匹配到。
   - 无法在对应 Comments 中 grounding 的 venue 必须置为 `None`，不能仅依赖 prompt 中“不得推测或编造”的声明。

5. **缓存策略与当前生命周期不完全匹配**
   - 当前 batch cache 受全局 `metadata_hash` 控制，hash 已包含 comments。
   - 正常流程成功后会删除 field cache，因此“避免重复调用”的收益主要覆盖失败重跑或同轮 retry，不是持久化 venue cache。
   - TASK 005 又禁止独立 venue cache 文件，因此 ADR 应明确缓存目标只是临时 batch cache 复用，或重新设计持久化 per-paper venue cache。
   - “已有 venue 且 comments 未变化则跳过 LLM”属于提取前决策，应前置到 venue extractor/cache contract 设计中，而不是在 TASK 004 集成后再补。

6. **集成点需要覆盖 normal 与 patch 两条路径**
   - 当前 normal mode 与 patch mode 都通过 `run_phase2` 取得 `results_by_id`。
   - 如果 venue extraction 只插入 normal mode 的 markdown 生成前，patch mode 将无法获得新的 venue。
   - 建议明确集成点优先放在 `run_phase2` 返回前，或显式要求 normal/patch 两条调用路径都完成 venue merge。

7. **后处理校验未落实到 Task**
   - 设计概要中提到 `venue` 长度大于 60 时需要截断/缩写。
   - 现有 TASK 002/004/005 未明确要求实现长度限制、非法类型处理、空值规范化或 markdown 输出安全。
   - 建议将这些约束并入 TASK 002 的 parser/postprocess 规约。

8. **验证命令缺失**
   - 当前 TASK 001 提供了样例输入输出，但各 Task 缺少明确验证命令。
   - 建议补充 gate 单元测试、venue response parser 测试、cache replay 验证，以及 patch mode 最小验证命令。

9. **Front matter 状态格式与协议不完全一致**
   - 协议要求 `status: "[STATUS: PLANNING_COMPLETE]"`。
   - 当前文档为 `status: "PLANNING_COMPLETE"`。
   - 该问题不影响技术设计，但属于文档协议偏差。

建议修订后再进入 Executor 阶段。优先修正顺序为：补 AGENTS.md 缺失声明，统一 venue LLM 响应契约，处理 `temperature` 参数化或独立 caller，前置缓存/grounding/postprocess 约束，最后补齐验证命令。

### Architect Response

逐条回应：

1. **接受**。已在风险预警中补充 AGENTS.md 缺失声明，TASK 002 补充了需人类确认的技术选型标注。

2. **接受**。采用独立 `call_venue_llm`，响应 schema 为 `{"venues": [{"id": ..., "venue": ...}]}`，不复用 `call_llm` 及其 `_validate_llm_schema`。

3. **接受**。独立 caller 中直接设 temperature=0，不需要改动现有 `call_llm` 的参数。

4. **部分接受**。Grounding 方向正确，已落实到 TASK 002。但实现上采用宽松匹配 + warning（而非硬拦截），因为 LLM 合理的缩写/年份格式化行为（如 Comments 写 `AI-DEEDS '26` 而 LLM 输出 `AI-DEEDS 2026`）会导致严格 grounding 误杀。如果运行中误杀率高，需退化为更简单的规则（如 venue 中至少一个 token 出现在 Comments 中）。

5. **接受**。已重写 TASK 005，明确 venue_cache.json 为临时恢复检查点，随 field_cache 一起被 `shutil.rmtree` 删除，不是持久化 cache。

6. **接受**。TASK 004 已显式覆盖 `run_normal_mode` 和 `run_patch_mode` 两条路径。

7. **部分接受**。长度超 60 字符时记录 warning 但不自动截断 — 部分 workshop 全名天然超过 60 字符，强行截断丢失信息。是否缩写应由人类判断。

8. **接受**。全部 5 个 TASK 均已补充验证命令。

9. **不接受**。模板中 `[STATUS: PLANNING_COMPLETE]` 的方括号是占位符标记（与 `[YYYY-MM-DD]`、`[特性简述]` 同理），不是字面值。实际填写时不应保留方括号，`"PLANNING_COMPLETE"` 是正确格式。不作修改。

### Architect Re-Review

第二轮评审结论：本轮修订解决了 schema 冲突、temperature 冲突、normal/patch 覆盖和临时 cache 定位等核心方向问题，但当前蓝图仍不建议直接进入 Executor 阶段。剩余问题集中在可执行契约不完整、验证命令不可运行、文档段落互相矛盾，以及新增独立 LLM caller 后缺少 API 防御约束。

1. **验证命令不可直接运行**
   - TASK 001/002/003 的验证命令使用 `from arxiv_update.scripts.arxiv_daily import ...`，但当前项目目录是 `arxiv-update/`，不是 Python package `arxiv_update`。
   - 现有测试的导入方式是将 `arxiv-update/scripts` 加入 `sys.path` 后 `import arxiv_daily`。
   - TASK 004 的 CLI 示例使用 `--field cs.AR`，但实际 CLI 中 `field` 是位置参数；同时 `--tags` 是必填参数。
   - 建议将验证命令统一调整为现有项目可运行形式，例如通过 `PYTHONPATH=arxiv-update/scripts` 导入，CLI 使用位置参数 `cs.AR` 并显式传入 `--tags`。

2. **Venue cache 设计缺少必要接口契约**
   - `extract_venues_batch` 的接口只接收 papers、API 配置、model 和 logger。
   - TASK 005 要求该函数读取/写入 `field_cache/venue_cache.json`、判断 metadata hash、遵守 cache/no-cache 生命周期，但这些上下文没有通过参数传入，也没有定义独立 helper。
   - 建议明确新增参数或拆出 cache helper，至少表达 `field_cache`、`metadata_hash`、`cache_enabled` 三类信息。

3. **`venue_cache.json` schema 无法支持 hash 判断**
   - TASK 005 定义格式为 `{"venues": {"paper_id": "venue_or_null", ...}}`。
   - 同一任务又要求“若 metadata_hash 未变化则复用”，但 cache 文件本身没有保存 `metadata_hash`。
   - 如果复用现有 `field_cache/metadata_hash`，需要明确读取顺序与写入时机；否则建议把 schema 调整为包含 hash 的 object，例如 `metadata_hash + venues`。

4. **Grounding 行为前后矛盾**
   - TASK 002 写明无法 grounding 的 venue 置为 None 并记录 warning。
   - Architect Response 又写“宽松匹配 + warning（而非硬拦截）”。
   - 这两者会导致 Executor 无法判断最终行为。建议采用“宽松匹配规则，但 grounding 失败必须置 None 并 warning”，这样既允许缩写/年份格式化，又符合原始需求中杜绝假阳性的目标。

5. **设计概要仍保留旧契约**
   - 数据流中仍写 LLM 输出为 `[{id, venue}]`，但 API Contract 和 TASK 002 已改为 `{"venues": [...]}`。
   - 数据流中仍写 `venue` 长度大于 60 时截断/缩写，但 TASK 002 改为只记录 warning、不自动截断。
   - 建议统一设计概要、API Contract 和 TASK 行为，以 TASK 执行时不会出现多义解释为准。

6. **独立 `call_venue_llm` 缺少外部 API 防御约束**
   - 新增独立 caller 是合理选择，但它绕过了现有 `call_llm` 中的重试、429 退避、HTTP 错误分流和 debug 保存能力。
   - ADR 当前只要求“超时/异常时返回 None”，不足以防止瞬时网络波动、限流或不可诊断的 JSON 解析失败。
   - 建议补充行为约束：有限重试、429 遵守 `Retry-After` 或指数退避、非 429 的 4xx 不重试、超时阈值常量化、失败时保存 prompt 与原始响应到 debug 目录。

7. **TASK 004 的 papers 输入边界不够明确**
   - TASK 004 写“传入 `data["sections"]` 中的所有 papers”，但 `extract_venues_batch` 的入参是 `list[dict]` papers，不是 sections。
   - 应明确要求 flatten sections 中的 papers，并且 merge 时只更新 `results_by_id` 中已存在的 paper id，避免无摘要 paper 或未成功 summary 的 paper 触发 KeyError。

8. **`call_venue_llm` 返回类型与异常策略不一致**
   - API Contract 写 `call_venue_llm(...) -> dict`。
   - TASK 002 又要求超时/异常时返回 None。
   - 建议将接口签名调整为 `dict | None`，或要求异常向上抛出并由 `extract_venues_batch` 统一捕获。两种策略只能保留一种。

9. **TASK 005 的验证命令无法验证 cache resume**
   - 当前验证步骤主要是注释，最后只检查 `~/.cache/arxiv-update/cs.AR/venue_cache.json` 是否存在。
   - 正常流程成功后 field cache 会被删除，因此该检查与“成功后随 field_cache 删除”的生命周期设计冲突。
   - 建议改为测试级验证：使用临时 cache dir、mock `call_venue_llm`、调用 venue cache helper 或 `extract_venues_batch` 两次，断言第二次命中 cache 且不触发 LLM。

建议修订优先级：先修验证命令和 cache 接口/schema，再统一 grounding、schema 与长度策略，最后补齐独立 API caller 的防御约束和 TASK 004 的 flatten/merge 边界。

### Architect Response (Round 2)

全部接受，逐条已落实：

1. **接受**。验证命令统一为 `PYTHONPATH=arxiv-update/scripts` 导入方式，CLI 改为位置参数 + 显式 `--tags`。

2. **接受**。`extract_venues_batch` 新增 `field_cache: Path | None` 和 `metadata_hash: str | None` 参数。`field_cache=None` 时不读写 cache（patch mode 语义）。

3. **接受**。`venue_cache.json` schema 改为 `{"metadata_hash": "...", "venues": {...}}`，hash 存在 cache 文件内部，读取时直接比较。

4. **接受**。统一为：宽松匹配规则（至少一个非停用词 token 出现在 Comments 中，允许年份格式化和缩写展开），宽松匹配仍失败的 venue 置为 None 并记录 warning。两处描述不再矛盾。

5. **接受**。数据流图中 LLM 输出已改为 `{"venues": [{"id", "venue"}]}`，后处理改为 `grounding 失败 → None` + `长度 > 60 → warning`。

6. **接受**。`call_venue_llm` 补充：最多 2 次重试、429 遵守 Retry-After 或指数退避 (2s, 4s)、非 429 4xx 不重试、超时 30s 常量化、失败保存 prompt+响应到 debug_dir。

7. **接受**。TASK 004 明确 flatten 表达式 `[p for s in data["sections"] for p in s["papers"]]`，merge 时用 `if pid in results_by_id` 守卫。

8. **接受**。`call_venue_llm` 返回类型统一为 `dict | None`，所有异常在函数内部处理不向上抛出，`extract_venues_batch` 对 None 返回值将该 batch 所有 venue 设为 None。

9. **接受**。TASK 005 验证命令改为使用 tempdir + mock `call_venue_llm` 的可执行单元测试，验证 cache hit 时不触发 LLM 调用。

### Architect Re-Review Round 3

第三轮评审结论：本轮修订已解决大多数实质问题，ADR 接近可执行。但仍存在少量会阻断 Executor 严格按任务顺序执行的问题，以及若干需要在蓝图中补齐的边界约束。当前建议状态为 `NEEDS_MINOR_REVISION`。

1. **TASK 编号顺序与显式依赖冲突**
   - TASK 004 声明依赖 TASK 005，但 TASK 005 排在 TASK 004 之后。
   - 按任务拆解法则，编号必须代表严格执行顺序，不能出现较小编号依赖较大编号。
   - 建议将 venue cache 策略调整为 TASK 004，将 normal/patch 集成调整为 TASK 005；或将 cache helper 拆到更早的任务中，再执行主流程集成。

2. **`metadata_hash` 来源仍不可执行**
   - TASK 004 要求 normal mode 调用 `extract_venues_batch` 时传入 `field_cache` 和 `metadata_hash`，并写“复用已计算的值”。
   - 当前代码中 `compute_metadata_hash(data)` 只在 `run_phase2` 内部局部计算，`run_normal_mode` 在 `run_phase2` 返回后拿不到该值。
   - 建议明确其中一种方案：在 `run_normal_mode` 中重新计算 `compute_metadata_hash(data)`；让 `run_phase2` 返回 metadata hash；或把 venue extraction 放进 `run_phase2` 的同一 cache 上下文中。

3. **`--no-cache` 对 venue cache 的语义未定义**
   - summary 阶段已有 `--no-cache` 语义，用户会预期全链路禁用 cache。
   - 当前 TASK 005 只定义了 `field_cache=None` 时不读写 venue cache，但 TASK 004 没有说明 normal mode 下 `args.no_cache=True` 时是否应传 `field_cache=None`。
   - 建议补充约束：`args.no_cache` 为真时，venue extraction 必须禁用 cache，不读取也不写入 `venue_cache.json`。

4. **TASK 004 验证命令仍引用不存在的 tags 路径**
   - TASK 004 使用 `--tags arxiv-update/data/tags.json`。
   - 当前仓库中 `arxiv-update/` 下没有 `data/` 目录，`--tags` 又是必填参数，因此该命令不能直接运行。
   - 建议改为在验证命令中创建临时 tags 文件，或引用当前仓库实际存在的测试 fixture/生成路径。

5. **TASK 004 端到端验证依赖本地 HTML cache 和真实 LLM**
   - 当前命令固定 `--date 2026-05-28` 并标注“使用已有 HTML cache”。
   - 若本机没有对应 pages cache，或没有 LLM 环境变量/API 可用，验证会失败。
   - 建议将该命令标记为人工 smoke test，并补充 Executor 必跑的可重复验证：使用 fake LLM server 或 pytest fixture 覆盖 normal/patch 集成。

6. **`call_venue_llm` debug 文件命名缺少 batch 标识参数**
   - TASK 002 要求失败时保存到 `debug_dir/venue_batch_NNN_error.json`。
   - 但 `call_venue_llm` 接口没有 `batch_num` 或 `batch_label` 参数，无法确定 `NNN`。
   - 建议给 `call_venue_llm` 增加 `batch_label: str`，或明确由 `extract_venues_batch` 负责 debug 文件命名与写入。

7. **风险预警仍保留旧 cache 表述**
   - 风险预警仍写“venue 结果需写入现有 batch cache 结构，或独立 venue cache”。
   - 当前设计已经确定使用 `venue_cache.json`，且明确不修改现有 `batch_NNN.json`。
   - 建议同步风险预警，避免 Executor 误以为仍允许写入 summary/tag batch cache。

修订优先级：先重排 TASK 004/005 或调整依赖，再明确 metadata_hash 与 no-cache 语义；随后修正验证命令和 debug batch label；最后同步风险预警中的 cache 表述。
