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
