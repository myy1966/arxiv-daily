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
                                        │   输出: [{id, venue}]
                                        │
                                        └─ Step C: 后处理校验
                                            venue 字符串长度 > 60 → 截断/缩写
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
    # 行为: 构建仅含 comments 的 batch prompt，要求 LLM 返回 [{id, venue}]

# Batch venue 提取入口
def extract_venues_batch(
    papers: list[dict],
    api_endpoint, api_key, model, logger,
) -> dict[str, str | None]: ...
    # 入参: 所有论文列表
    # 出参: {paper_id: venue_or_none}
    # 行为:
    #   1. 对每篇论文执行 has_acceptance_signal 过滤
    #   2. 未通过 gate 的 → venue = None
    #   3. 通过 gate 的 → 按 batch_size=16 分组，调用 LLM 提取 venue
    #   4. 合并结果返回
```

### 风险预警 (Risk Assessment)

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

#### [TASK ID: 002] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 新增 `build_venue_prompt` 和 `extract_venues_batch` 函数
- **目标描述**: 实现 batch venue LLM 提取，对通过 keyword gate 的论文按 batch=16 调用 LLM 提取 venue 名
- **前置依赖**: TASK 001
- **执行指令与限制**:
  - 接口签名见设计概要
  - `build_venue_prompt` 的 LLM prompt 仅输入每篇论文的 ID 和 Comments 文本（不含 abstract/title），要求输出 JSON 数组
  - prompt 中必须包含的约束:
    - 仅从 Comments 文本中提取，不得推测或编造
    - 年份仅在 Comments 中明确出现时才包含
    - 使用最具体的 venue 名称
    - 期刊与会议同等对待
    - 如果从 Comments 中无法确定 venue 名称，返回 null
  - `extract_venues_batch` 必须:
    - 先对所有 papers 执行 `has_acceptance_signal` 过滤
    - 对通过 gate 的论文按 batch_size=16 分组
    - 每个 batch 调用一次 LLM（复用现有 `call_llm` 的调用模式）
    - 超时、网络错误时该 batch 所有 venue 设为 None（不阻塞整体流程）
    - 返回完整的 {paper_id: venue_or_none} 映射（含未通过 gate 的论文，其值为 None）
  - temperature 设为 0（venue 提取是确定性任务）
  - 可使用 streaming（复用现有 call_llm），也可不使用（venue 响应极短）

#### [TASK ID: 003] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 修改 `build_prompt` 函数
- **目标描述**: 从 summary/tag 的 LLM prompt 中移除 venue 相关指令和 JSON 字段
- **前置依赖**: TASK 001, TASK 002
- **执行指令与限制**:
  - 删除 `build_prompt` 中 venue 提取的第 3 条指令（当前第 491-496 行区域）
  - 从 JSON schema 示例中移除 `"venue"` 字段
  - `build_prompt` 的 Papers 部分仍然保留 Comments 字段传入（tag 判断可能依赖 Comments 中的会议名称信息）
  - 不修改 `build_prompt` 中 summary 和 tag 相关的指令

#### [TASK ID: 004] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — 修改主流程，集成 venue 提取阶段
- **目标描述**: 在 Phase 2 (summarization) 完成后、Phase 3 (markdown) 之前插入 venue 提取，并将结果合并到 results_by_id
- **前置依赖**: TASK 001, TASK 002, TASK 003
- **执行指令与限制**:
  - 在 summarization 完成后调用 `extract_venues_batch`
  - 将返回的 venue 映射 merge 到 `results_by_id`：`results_by_id[pid]["venue"] = venues[pid]`
  - 不修改 `match_llm_results` 中的 venue 处理 — 如果 LLM 响应中仍然返回了 venue 字段（向后兼容旧 cache），取新提取的值覆盖
  - 日志输出: 记录通过 gate 的论文数、成功提取的 venue 数
  - 若 venue 提取整体失败（如 API 不可用），不阻塞流程，所有 venue 设为 None

#### [TASK ID: 005] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — venue 缓存策略
- **目标描述**: 将 venue 提取结果纳入现有缓存机制，避免重复调用
- **前置依赖**: TASK 004
- **执行指令与限制**:
  - venue 结果应存入现有 batch cache 的 paper 字典中（保持 `{"id", "summary", "tags", "venue"}` 结构）
  - 从 cache 加载时，如果 cache 中已有 venue 且 comments 未变化，跳过 LLM 调用
  - cache 有效性判断: comments 内容变化时 venue cache 失效（可复用现有 `compute_metadata_hash` 机制）
  - 不创建独立的 venue cache 文件 — 复用现有 batch_NNN.json 结构

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
