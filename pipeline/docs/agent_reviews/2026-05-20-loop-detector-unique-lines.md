---
title: "重构流式死循环检测：改用窗口内唯一行数判据"
date: "2026-05-20"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)

- **原始需求**: 上一版基于"滑动窗口内某行重复 N 次"的死循环检测算法（ADR `2026-05-19-reasoning-loop-detection.md`）已合并到 master 并实际运行。但 cs.AI 抓取实测显示：前 5 个 batch 全部在 2000-7000 字符触发"循环"——经核对 stream 日志，全部为模型按论文模板顺序输出（每篇含 `- Venue: null (no acceptance info)` 等模板行）的合法行为，**不是真死循环**。当前算法把"模板化重复"误判为"循环"。需要重构判据，对模板重复免疫，对真实循环（含字面循环与带变化循环）仍能可靠捕获。
- **技术翻译**: 把 `_StreamLoopDetector.feed` 的判据从"滑动窗口内任一行的出现次数 ≥ THRESHOLD"改为"滑动窗口装满后，去重后唯一行数 ≤ MAX_UNIQUE"。核心假设：死循环的本质是信息熵下降——窗口内反复出现同一组句型，唯一内容急剧减少；而模板重复中，虽然某一行（如 venue）会重复，但其余行（title/summary/tags）字面各异，唯一行数仍接近窗口大小。`call_llm` 中的注入点、错误传播路径、日志格式、重试链路全部不变。
- **Scope**: `arxiv`
- **涉及文件**:
  - `arxiv-update/scripts/arxiv_daily.py`（修改：替换 `STREAM_LOOP_*` 常量、重写 `_StreamLoopDetector.feed`；`call_llm` 调用点不变）
  - `arxiv-update/scripts/tests/test_arxiv_daily.py`（修改：重写 `TestStreamLoopDetector` 单元测试；扩充 `TestCallLlmLoopDetection` 增加模板免疫与带变化循环场景）
  - `arxiv-update/scripts/tests/fake_llm_server.py`（修改：保留现有 `handle_reasoning_loop` / `handle_content_loop`；新增 `handle_varying_loop` 与 `handle_template_repetition`）

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

---

## 2. 设计概要 (Design Overview)

### 算法核心：窗口内唯一行数

```
判据：deque(maxlen=WINDOW) 装满后，len(set(deque)) <= MAX_UNIQUE → 死循环

直觉：
  正常输出      → 窗口内多数行字面不同，unique ≈ WINDOW
  字面死循环    → 窗口内全是同一行，unique = 1
  带变化死循环  → 窗口内反复出现 4-5 种句型，unique 很小
  模板化重复    → 论文模板内 1-2 行重复，但 title/summary/tags 字面各异，unique 仍很大
```

### 算法对比表

| 判据 | 字面循环 | 带变化循环 | 模板重复（误报源） | 实现复杂度 |
|---|---|---|---|---|
| 旧：某行计数 ≥ N | ✓ | ✓ | ✗ **生产环境已实证误报** | 低 |
| 候选：前后两窗口字面相等 | ✓ | ✗ 漏检 | ✓ | 低 |
| **新：窗口内 unique ≤ N** | ✓ | ✓ | ✓ | 低 |

### 数据流向 (Data Flow)

```
call_llm (SSE 解析循环) ── 注入点不变，仅算法替换
  │
  ├─ 实例化单个检测器（每次 attempt 重新创建）
  │     detector = _StreamLoopDetector(window_size=30, max_unique=12)
  │
  ├─ 每个 chunk → delta 解析（reasoning_content 与 content 在同一 chunk 中互斥）
  │     ├─ chunk = delta.get("reasoning_content") or delta.get("content") or ""
  │     │     └─ 非空 → detector.feed(chunk)
  │     │           ├─ 累加到 buffer，按 \n 分行
  │     │           ├─ 对每个完整非空行 → window.append(line.strip())
  │     │           └─ 当 len(window) == WINDOW 且 len(set(window)) <= MAX_UNIQUE
  │     │                 → return True
  │     │
  │     └─ 触发 True → resp.close() + raise ValueError("stream loop ...")
  │
  ├─ delta.get("content") 非空 → 追加到 content_parts（原有逻辑，保持不变）
  │
  ├─ ValueError 被现有 except (... ValueError) 捕获 → 重试（行为不变）
  │
  └─ 流正常结束 → 继续原有 content 拼接与 JSON 解析逻辑
```

### 核心接口定义 (API Contracts)

```python
# 替换前一版常量
STREAM_LOOP_WINDOW     = 30   # 滑动窗口行数；装满后开始判断（前一版为 60）
STREAM_LOOP_MAX_UNIQUE = 12   # 窗口内唯一行数 <= 此值 → 视为循环（新增）
# 删除：STREAM_LOOP_THRESHOLD, STREAM_LOOP_MIN_LEN

class _StreamLoopDetector:
    """Sliding-window unique-line detector for LLM SSE streams.

    Detects infinite loops by measuring information entropy in a fixed-size
    line window. When the window contains too few unique lines, the model is
    judged to be looping. Robust against template repetition where the same
    boilerplate line recurs but surrounding context is varied.

    Not thread-safe; create one instance per LLM request attempt.
    """

    def __init__(
        self,
        window_size: int = STREAM_LOOP_WINDOW,
        max_unique: int = STREAM_LOOP_MAX_UNIQUE,
    ) -> None: ...

    def feed(self, chunk: str) -> bool:
        """Ingest a text chunk and return True if a loop is detected."""
        ...

    @property
    def total_chars(self) -> int:
        """Total characters processed so far (for log messages)."""
        ...
```

```python
# call_llm 内 SSE 循环：调用点保持不变（feed/total_chars 接口未变）

detector = _StreamLoopDetector()

for raw_line in resp:
    ...
    delta = choices[0].get("delta", {})

    stream_chunk = delta.get("reasoning_content") or delta.get("content") or ""
    if stream_chunk and detector.feed(stream_chunk):
        logger.error(
            f"[Phase2] {log_label} attempt {attempt + 1}: "
            f"stream loop detected after {detector.total_chars} chars, aborting"
        )
        resp.close()
        raise ValueError(f"stream loop after {detector.total_chars} chars")

    if delta.get("content"):
        content_parts.append(delta["content"])
```

### 参数标定（基于实测数据）

| 场景 | 窗口=30 行内 unique 行数 | 判定 |
|---|---|---|
| batch_001 误报现场（每篇 4 行模板，覆盖约 7 篇） | ≥ 24 | 不触发 ✓ |
| 正常 JSON content（每行不同 paper） | ≈ 30 | 不触发 ✓ |
| batch_003 历史带变化循环（4-5 种句型 + 少量穿插） | ≤ 8 | 触发 ✓ |
| 字面死循环（同一行反复） | 1 | 触发 ✓ |

`MAX_UNIQUE = 12` 取于"模板重复下界 24" 与 "带变化循环上界 8" 的中点附近，向偏严方向留 4 行安全余量。

### 风险预警 (Risk Assessment)

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

| # | 风险 | 应对 |
|---|------|------|
| 1 | 极慢循环（周期 > WINDOW）漏检 | 此类循环 token 消耗速率本身较低，对总耗时影响有限；现有 LLM 超时（300s）兜底 |
| 2 | 巨大循环周期（>30 行）需多周期填满才触发 | 检测仍会触发，仅延迟 1-2 个周期；最坏延迟约 60 行（几千字符），仍远早于 65536 token 耗尽 |
| 3 | 空行频繁出现导致窗口被空行填满，干扰 unique 计数 | `feed` 显式跳过空行（`if not stripped: continue`），仅有意义的行进入窗口 |
| 4 | 短行（如 `.` `,`）在循环段落中反复出现，但语义无害 | 此场景实际有助于触发（短行重复降低 unique），与判据语义一致；不需特殊过滤 |
| 5 | 新算法上线后再次出现误报 | TASK 004 中 `test_template_repetition_no_false_positive` 直接复刻 batch_001 误报场景，作为强制回归测试 |
| 6 | `MAX_UNIQUE = 12` 阈值的鲁棒性 | 数据标定显示真循环上界 8、模板重复下界 24，两者 gap 充足；后续若再发现新误报，仅需调整常量，无需改算法 |
| 7 | `detector` 实例跨 attempt 复用导致状态污染 | `detector` 在 `with urllib.request.urlopen(...) as resp:` 块内声明，每轮 attempt 重新进入该块，自然重置 |
| 8 | 接口变更影响 `call_llm` 调用方 | `feed(chunk) -> bool` 与 `total_chars` 属性签名保持不变；TASK 002 仅做存在性验证 |
| 9 | 现有 fake server 处理器（`handle_reasoning_loop` / `handle_content_loop`）的循环行数（默认 20）小于新窗口（30），可能在 server 发完前不触发 | TASK 003 将 `repeat_count` 默认值从 20 调整为 35，确保现有测试仍能触发 |

---

## 3. 交互历史 (The Thread)

### [Round 1]

*(Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行)*

---

#### [TASK ID: 001] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/arxiv_daily.py`
  - 常量区：替换 `STREAM_LOOP_*` 常量
  - `_StreamLoopDetector` 类：重写 `__init__` 与 `feed`
- **目标描述**: 用窗口内唯一行数判据替换前一版的"窗口内某行计数 ≥ N"判据
- **前置依赖**: 无
- **执行指令与限制**:

  1. **常量替换**：
     - 删除 `STREAM_LOOP_THRESHOLD` 与 `STREAM_LOOP_MIN_LEN`。
     - 修改 `STREAM_LOOP_WINDOW` 默认值由 60 改为 30。
     - 新增 `STREAM_LOOP_MAX_UNIQUE = 12`。
     最终常量块：
     ```python
     STREAM_LOOP_WINDOW     = 30
     STREAM_LOOP_MAX_UNIQUE = 12
     ```

  2. **重写 `_StreamLoopDetector`**：
     ```python
     class _StreamLoopDetector:
         """Sliding-window unique-line detector for LLM SSE streams.

         Detects infinite loops by measuring information entropy in a fixed-size
         line window. When the window contains too few unique lines, the model is
         judged to be looping. Robust against template repetition where the same
         boilerplate line recurs but surrounding context is varied.

         Not thread-safe; create one instance per LLM request attempt.
         """

         def __init__(
             self,
             window_size: int = STREAM_LOOP_WINDOW,
             max_unique: int = STREAM_LOOP_MAX_UNIQUE,
         ) -> None:
             from collections import deque
             self._window: deque[str] = deque(maxlen=window_size)
             self._window_size = window_size
             self._max_unique = max_unique
             self._buf = ""
             self._total_chars = 0

         def feed(self, chunk: str) -> bool:
             """Ingest a text chunk and return True if a loop is detected."""
             self._total_chars += len(chunk)
             self._buf += chunk
             lines = self._buf.split("\n")
             self._buf = lines[-1]  # keep incomplete tail for next chunk
             for line in lines[:-1]:
                 stripped = line.strip()
                 if not stripped:
                     continue  # skip blank lines so they don't fill the window
                 self._window.append(stripped)
                 if (
                     len(self._window) == self._window_size
                     and len(set(self._window)) <= self._max_unique
                 ):
                     return True
             return False

         @property
         def total_chars(self) -> int:
             """Total characters processed so far."""
             return self._total_chars
     ```

  3. **不修改任何其他已有代码**。`from collections import deque` 仍放在 `__init__` 内（与上一版保持一致）。

  4. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已在 `arxiv-update/scripts/arxiv_daily.py` 将 `_StreamLoopDetector` 从单行重复次数判据重构为窗口内唯一行数判据，替换常量为 `STREAM_LOOP_WINDOW = 30` 与 `STREAM_LOOP_MAX_UNIQUE = 12`，并删除旧的 threshold/min_len 参数使用；未修改 `call_llm` 调用点或测试文件。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过。

---

#### [TASK ID: 002] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `call_llm` 函数内 SSE 解析循环
- **目标描述**: 验证 `call_llm` 内的检测器调用点无需修改（`feed` / `total_chars` 接口签名不变）
- **前置依赖**: TASK 001
- **执行指令与限制**:

  1. **检查现有注入代码**应为：
     ```python
     detector = _StreamLoopDetector()  # 在 with urllib.request.urlopen(...) as resp: 之后

     # 在 SSE 循环内：
     stream_chunk = delta.get("reasoning_content") or delta.get("content") or ""
     if stream_chunk and detector.feed(stream_chunk):
         logger.error(
             f"[Phase2] {log_label} attempt {attempt + 1}: "
             f"stream loop detected after {detector.total_chars} chars, aborting"
         )
         resp.close()
         raise ValueError(f"stream loop after {detector.total_chars} chars")
     ```

  2. **若代码已与上述一致** → 本 TASK 标记为 RESOLVED，无需改动。
  3. **若不一致** → 按上述代码补齐，使其与上一版 ADR 完全一致。
  4. **约束**：
     - 必须使用 `logger.error`，与现有网络/超时错误的日志级别一致。
     - `resp.close()` 必须在 `raise` 之前调用。
     - 不修改任何 `except` 块。
     - **输出路径说明**：循环检测触发后产生两条 `logger.error` 记录——①检测点，②现有 `except (... ValueError)` 入口；不向 stderr `print`。

  5. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已核验 `arxiv-update/scripts/arxiv_daily.py` 中 `call_llm` 的 `_StreamLoopDetector()` 实例化位置、`stream_chunk` 取值、`detector.feed(...)` 调用、`logger.error`、`resp.close()` 与 `ValueError` 抛出逻辑均与要求一致；因 `feed` / `total_chars` 接口签名未变，源码无需修改，且未修改任何 `except` 块。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过。

---

#### [TASK ID: 003] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/tests/fake_llm_server.py`
- **目标描述**: 调整现有 `handle_reasoning_loop` / `handle_content_loop` 默认 `repeat_count`；新增 `handle_varying_loop` 与 `handle_template_repetition`
- **前置依赖**: TASK 001
- **执行指令与限制**:

  1. **调整现有处理器**：将 `handle_reasoning_loop` 与 `handle_content_loop` 的 `repeat_count` 默认值由 20 改为 35（≥ `STREAM_LOOP_WINDOW`），确保检测器能在 server 发完前装满窗口并触发。

  2. **新增 `handle_varying_loop`**：模拟 batch_003 历史的带变化循环——4 句型循环（unique=4），无字面重复但唯一行数极少：
     ```python
     def handle_varying_loop(self, repeat_count: int = 12) -> None:
         """Simulate a varying loop: 4 句型 cycled, no literal line repeats consecutively."""
         model = "fake-varying-loop-model"
         req_id = "fake-varying-loop-001"
         loop_lines = [
             "Let me assume the ID is `2605.17108`? No.\n",
             "Wait, I see `ID: 2605.17108`? No.\n",
             "Okay, I will just use the ID `2605.17108`? No.\n",
             "Actually, the ID is `2605.17108`? No.\n",
         ]

         self._send_response(200, "text/event-stream")
         for _ in range(repeat_count):
             for line in loop_lines:
                 chunk = {
                     "id": req_id, "object": "chat.completion.chunk", "model": model,
                     "choices": [{"index": 0, "delta": {
                         "content": None, "reasoning_content": line,
                     }, "flag": 0}],
                 }
                 self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                 self.wfile.flush()
         # 尾部参考 handle_reasoning_loop：发送 final chunk + [DONE]
     ```
     `12 × 4 = 48` 行 ≥ 30；window 装满后 unique=4 ≤ 12，必触发。

  3. **新增 `handle_template_repetition`**：复刻 batch_001 误报场景——10 篇论文 × 4 行模板，仅 venue 行重复：
     ```python
     def handle_template_repetition(self, paper_count: int = 10) -> None:
         """Simulate template repetition (must NOT trigger loop detection)."""
         model = "fake-template-model"
         req_id = "fake-template-001"
         template = [
             "**Paper {i}: Unique Title About Topic {i}**\n",
             "- Summary: This paper {i} proposes a novel method for problem {i}.\n",
             "- Tags: Tag{i}A, Tag{i}B\n",
             "- Venue: null (no acceptance info)\n",  # ← 唯一重复行
         ]

         self._send_response(200, "text/event-stream")
         for i in range(1, paper_count + 1):
             for line_tmpl in template:
                 line = line_tmpl.format(i=i)
                 chunk = {
                     "id": req_id, "object": "chat.completion.chunk", "model": model,
                     "choices": [{"index": 0, "delta": {
                         "content": None, "reasoning_content": line,
                     }, "flag": 0}],
                 }
                 self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                 self.wfile.flush()

         # 输出有效 JSON content 让 call_llm 走完正常路径
         valid = '{"papers": [], "new_tags": []}'
         chunk = {
             "id": req_id, "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {"content": valid}, "flag": 0}],
         }
         self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
         # 尾部 final chunk(finish_reason=stop) + usage + [DONE]
     ```
     40 行模板，window=30 时 unique ≈ 23（每窗口约 7 篇 × 3 唯一行 + 共享 venue 算 1 个），> 12，不应触发。

  4. **路由注册**：以现有 fake server 路由约定为准，分别为两个新处理器注册路由。不要发明新机制。

  5. **写入模式**：所有处理器必须遵循现有 fake server 的 `BrokenPipeError` 处理模式——`call_llm` 触发循环检测后会主动 close 连接，server 后续 write 必须能优雅吞掉异常（参考 `handle_reasoning_loop` 现有写法）。

  6. 完成后运行 `python3 -m py_compile arxiv-update/scripts/tests/fake_llm_server.py` 验证语法。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已在 `arxiv-update/scripts/tests/fake_llm_server.py` 将 `handle_reasoning_loop` 与 `handle_content_loop` 默认 `repeat_count` 调整为 35，新增 `handle_varying_loop` 与 `handle_template_repetition`，并按现有模块级 `ERROR_HANDLERS` 路由约定注册 `varying_loop` 和 `template_repetition`；新增 `_write_sse_final` 复用 final/usage/[DONE] 写入，连接提前关闭仍由现有 `do_POST` 外层 BrokenPipe/连接重置捕获处理。验证：`python3 -m py_compile arxiv-update/scripts/tests/fake_llm_server.py` 通过。

---

#### [TASK ID: 004] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/tests/test_arxiv_daily.py`
- **目标描述**: 重写 `TestStreamLoopDetector` 单元测试以匹配新算法；扩充 `TestCallLlmLoopDetection` 覆盖三类场景（含模板免疫强制回归）
- **前置依赖**: TASK 001, TASK 002, TASK 003
- **执行指令与限制**:

  1. **重写 `TestStreamLoopDetector`**（单元测试，直接实例化，不依赖 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_window_not_full_no_trigger` | feed 不到 `STREAM_LOOP_WINDOW` 行时永不触发，无论行内容是否重复 |
     | `test_literal_loop_triggers` | 同一行重复 `WINDOW` 次后触发（unique=1） |
     | `test_varying_loop_triggers` | 4 句型轮流出现填满窗口，unique=4 ≤ 12，触发 |
     | `test_template_repetition_no_false_positive` | 10 篇论文 × 4 行模板（仅 venue 行字面重复，title/summary/tags 各异），共 40 行 feed 完全程不触发——**对应生产事故场景的强制回归** |
     | `test_normal_diverse_text_no_trigger` | 30+ 行字面均不同，unique≈WINDOW，不触发 |
     | `test_blank_lines_skipped` | 空行不进入窗口；构造"5 个唯一行 + 大量空行"场景，验证不会因空行填满窗口而触发 |
     | `test_partial_lines_buffered` | chunk 不含 `\n` 时不分行不触发 |
     | `test_total_chars_accumulated` | `total_chars` 累积所有 feed 字符（含空行/空白） |
     | `test_window_evicts_old_lines` | 先 feed 30 行唯一行（窗口已满，unique=30 不触发），再追加 1 行重复，验证最早行被驱逐、unique 维持高位仍不触发 |
     | `test_custom_params` | 用 `_StreamLoopDetector(window_size=10, max_unique=3)` 构造，验证参数生效 |

  2. **扩充 `TestCallLlmLoopDetection`**（集成测试，使用 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_reasoning_literal_loop_triggers_retry` | reasoning_content 字面循环 → call_llm 中止并重试，attempt 次数 > 1 |
     | `test_content_literal_loop_triggers_retry` | content 字面循环，同上 |
     | `test_varying_loop_triggers_retry` | 4 句型循环，call_llm 仍能触发循环检测并重试 |
     | `test_template_repetition_no_false_positive` | fake server 发送 10 篇论文模板内容后给出有效 JSON；call_llm 一次性正常返回，**attempt 次数 == 1**——本次最关键的回归 |
     | `test_normal_stream_no_false_positive` | 正常完整 JSON 响应，call_llm 一次性正常返回 |

  3. **约束**：
     - 单元测试不依赖 fake server，直接实例化 `_StreamLoopDetector`。
     - 集成测试中 fake server 调用次数通过 mock 或 server 内部计数器验证（参考现有 `TestCallLlm*` 模式）。
     - `test_template_repetition_no_false_positive`（单元 + 集成两层）**为本次最关键的回归测试**——直接对应导致前一版上线失败的真实场景；必须用至少 10 篇论文（≥ 40 行 ≥ WINDOW + 10）才能验证窗口装满后仍不误触发。
     - 必须确保测试结束后 fake server 线程被正确清理（参考现有 fixture）。

  4. 完成后运行：
     ```bash
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestStreamLoopDetector \
                                   tests/test_arxiv_daily.py::TestCallLlmLoopDetection -v
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q
     ```
     全部通过后方可标记 RESOLVED。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已在 `arxiv-update/scripts/tests/test_arxiv_daily.py` 重写 `TestStreamLoopDetector` 以覆盖窗口未满不触发、字面循环、4 句型低 unique 循环、10 篇论文模板重复免疫、正常多样文本、空行跳过、partial line buffering、字符计数、窗口驱逐和自定义参数；已扩充 `TestCallLlmLoopDetection` 覆盖 reasoning/content 字面循环重试、varying loop 重试、template repetition 单次正常返回和正常流无误报。验证：`python3 -m py_compile arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；在 `arxiv-update/scripts` 下运行 `./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestStreamLoopDetector tests/test_arxiv_daily.py::TestCallLlmLoopDetection -v`，15 passed；运行 `./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q`，167 passed, 1 xfailed。

---

## 4. 最终决策 (Final Decision)

（由 Reviewer 在最终判决 PASS 时填写）
