---
title: "LLM reasoning 死循环检测（滑动窗口行重复）"
date: "2026-05-19"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)

- **原始需求**: batch_003_stream_attempt1.txt 显示 glm-5 在 reasoning 阶段陷入死循环，将全部 65536 reasoning token 耗尽后输出空 content，导致 LLM 请求耗时超过 1000 秒。需要在流式接收阶段提前检测循环并中止请求。
- **技术翻译**: 在 `call_llm` 的 SSE 流式解析循环中，实时对 `reasoning_content` delta 做滑动窗口行重复分析。一旦检测到循环，通过关闭 HTTP 响应连接提前终止流，触发已有重试机制。本次触发的错误类型须被现有 `except (urllib.error.URLError, TimeoutError, OSError, ValueError)` 捕获，无需新增重试分支。
- **Scope**: `arxiv`
- **涉及文件**:
  - `arxiv-update/scripts/arxiv_daily.py`（修改：新增常量、新增 `_ReasoningLoopDetector` 类、修改 `call_llm` SSE 循环）
  - `arxiv-update/scripts/tests/test_arxiv_daily.py`（修改：新增测试类 `TestReasoningLoopDetector` 和 `TestCallLlmLoopDetection`）
  - `arxiv-update/scripts/tests/fake_llm_server.py`（修改：新增 `handle_reasoning_loop` 处理器，模拟 reasoning 无限循环的 SSE 流）

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

---

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)

```
call_llm (SSE 解析循环)
  │
  ├─ 每个 chunk → delta.reasoning_content 非空
  │     └─ 追加到 _ReasoningLoopDetector.feed(chunk)
  │           ├─ 将 chunk 追加到内部 line buffer
  │           ├─ 按 '\n' 分行，将完整行（len >= MIN_LINE_LEN）存入 deque(maxlen=WINDOW_SIZE)
  │           └─ 若任意行的出现次数 >= REPEAT_THRESHOLD → return True（检测到循环）
  │
  ├─ detector.feed() 返回 True
  │     └─ resp.close()  ← 关闭 HTTP 连接，提前终止 SSE 流
  │           └─ 抛出 ValueError("reasoning loop detected, aborting stream")
  │                 └─ 被现有 except (... ValueError) 捕获 → 触发重试
  │
  └─ 流正常结束 → 继续原有 content 拼接与 JSON 解析逻辑
```

### 核心接口定义 (API Contracts)

```python
# 新增常量（位于现有常量区，BATCH_SIZE 附近）
REASONING_LOOP_WINDOW   = 60   # 滑动窗口大小（保留最近 N 条完整行）
REASONING_LOOP_THRESHOLD = 4   # 同一行在窗口内出现 >= N 次视为循环
REASONING_LOOP_MIN_LEN   = 8   # 短于此字符数的行跳过，不计入检测

class _ReasoningLoopDetector:
    """有状态的滑动窗口行重复检测器，供 call_llm 在 SSE 流中实时调用。

    线程不安全，设计为单次 LLM 请求内使用后丢弃。
    """

    def __init__(
        self,
        window_size: int = REASONING_LOOP_WINDOW,
        threshold: int = REASONING_LOOP_THRESHOLD,
        min_len: int = REASONING_LOOP_MIN_LEN,
    ) -> None: ...

    def feed(self, chunk: str) -> bool:
        """将 reasoning_content chunk 追加进缓冲区并检测循环。

        Args:
            chunk: 单个 SSE delta 的 reasoning_content 文本片段（非完整行）。

        Returns:
            True  → 检测到循环，调用方应立即中止流。
            False → 未检测到循环，继续接收。
        """
        ...
```

```python
# call_llm 内 SSE 循环的改动（伪代码，精确位置见 TASK 002）

detector = _ReasoningLoopDetector()          # 每次 attempt 重新实例化

for raw_line in resp:
    ...
    delta = choices[0].get("delta", {})

    # 新增：检测 reasoning_content 循环
    reasoning_chunk = delta.get("reasoning_content") or ""
    if reasoning_chunk and detector.feed(reasoning_chunk):
        resp.close()
        raise ValueError(
            f"[Phase2] {log_label} reasoning loop detected after "
            f"{detector.total_reasoning_chars} reasoning chars, aborting stream"
        )

    if delta.get("content"):
        content_parts.append(delta["content"])
```

```python
# _ReasoningLoopDetector 须暴露的只读属性（供日志使用）
@property
def total_reasoning_chars(self) -> int:
    """已处理的 reasoning_content 字符总数（用于日志记录）。"""
    ...
```

### 风险预警 (Risk Assessment)

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

| # | 风险 | 应对 |
|---|------|------|
| 1 | `resp.close()` 后继续读取抛出 `OSError`/`ValueError` 以外的异常，被现有重试逻辑漏掉 | `resp.close()` 紧跟 `raise ValueError(...)`，不依赖 close 本身引发何种异常；ValueError 已在现有 `except` 中 |
| 2 | 误报：正常的长推理中存在合理的重复行（如 "Yes." / "No."） | `MIN_LINE_LEN=8` 过滤极短行；`THRESHOLD=4` 要求 60-line 窗口内出现 4 次，误报概率极低 |
| 3 | 模型不输出 `reasoning_content` 字段（普通模型）时 detector 永远不触发 | `delta.get("reasoning_content") or ""` 空值直接跳过 feed，零开销，完全兼容无 reasoning 模型 |
| 4 | 循环发生在行边界之前（chunk 不包含 `\n`）导致 buffer 累积 OOM | `_buf` 最大长度不超过一次 reasoning loop 中最长的单行 × 1；实测循环行约 20-50 字符，单次 feed 后 buf 均被清空；无 OOM 风险 |
| 5 | `detector` 实例跨 attempt 复用导致上次循环状态污染 | TASK 002 明确要求每次 `for attempt in range(MAX_RETRIES)` 循环内重新 `detector = _ReasoningLoopDetector()` |
| 6 | `ValueError` 与现有代码中已有的 `ValueError` 语义冲突 | 现有 `except (urllib.error.URLError, TimeoutError, OSError, ValueError)` 对所有 ValueError 均重试，行为正确；loop detection 触发的 ValueError 会被记录到 logger 后重试，不影响其他路径 |
| 7 | 测试中 fake_llm_server 需要模拟 reasoning 循环 SSE 流 | TASK 003 新增 `handle_reasoning_loop`，在 content 之前输出大量重复 reasoning 行，供集成测试验证 abort 行为 |

---

## 3. 交互历史 (The Thread)

### [Round 1]

*(Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行)*

---

#### [TASK ID: 001] [状态: OPEN]

- **定位**: `arxiv-update/scripts/arxiv_daily.py`
  - 常量区（`BATCH_SIZE` 附近）：新增 3 个常量
  - `_ReasoningLoopDetector` 类：放置在常量区之后、`_retry_delay` 函数之前
- **目标描述**: 新增循环检测所需的模块级常量和 `_ReasoningLoopDetector` 类
- **前置依赖**: 无
- **执行指令与限制**:

  1. **常量**：在 `BATCH_SIZE = 16` 所在常量块末尾（`_last_fetch_time` 之前）追加：
     ```python
     REASONING_LOOP_WINDOW    = 60
     REASONING_LOOP_THRESHOLD = 4
     REASONING_LOOP_MIN_LEN   = 8
     ```

  2. **类定义**：在常量块与 `_retry_delay` 之间插入 `_ReasoningLoopDetector`：
     ```python
     class _ReasoningLoopDetector:
         """Sliding-window line-repetition detector for LLM reasoning streams.

         Detects infinite reasoning loops by tracking repeated lines within a
         fixed-size window of recently seen complete lines.
         Not thread-safe; create one instance per LLM request attempt.
         """

         def __init__(
             self,
             window_size: int = REASONING_LOOP_WINDOW,
             threshold: int = REASONING_LOOP_THRESHOLD,
             min_len: int = REASONING_LOOP_MIN_LEN,
         ) -> None:
             from collections import deque
             self._window: deque[str] = deque(maxlen=window_size)
             self._threshold = threshold
             self._min_len = min_len
             self._buf = ""
             self._total_chars = 0

         def feed(self, chunk: str) -> bool:
             """Ingest a reasoning_content chunk and return True if a loop is detected."""
             self._total_chars += len(chunk)
             self._buf += chunk
             lines = self._buf.split("\n")
             self._buf = lines[-1]  # keep incomplete tail for next chunk
             for line in lines[:-1]:
                 stripped = line.strip()
                 if len(stripped) < self._min_len:
                     continue
                 self._window.append(stripped)
                 if self._window.count(stripped) >= self._threshold:
                     return True
             return False

         @property
         def total_reasoning_chars(self) -> int:
             """Total reasoning_content characters processed so far."""
             return self._total_chars
     ```

  3. **不修改任何已有代码**；`from collections import deque` 放在 `__init__` 内（避免顶层重复导入，`collections.defaultdict` 已在顶层以 `from collections import defaultdict` 导入，`deque` 尚未导入）。

  4. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

---

#### [TASK ID: 002] [状态: OPEN]

- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `call_llm` 函数内 SSE 解析循环（当前第 509–535 行区域）
- **目标描述**: 在 SSE 循环中集成 `_ReasoningLoopDetector`，检测到循环时关闭连接并抛出 ValueError
- **前置依赖**: TASK 001
- **执行指令与限制**:

  1. **实例化位置**：在 `with urllib.request.urlopen(req, timeout=300) as resp:` 之后、`for raw_line in resp:` 之前，添加：
     ```python
     detector = _ReasoningLoopDetector()
     ```
     每次 `for attempt in range(MAX_RETRIES)` 迭代均重新实例化（当前代码结构已天然保证：`detector` 在 `with resp:` 块内声明，attempt 循环每轮重新进入该块）。

  2. **注入位置**：在 `delta = choices[0].get("delta", {})` 之后、`if delta.get("content"):` 之前，插入：
     ```python
     reasoning_chunk = delta.get("reasoning_content") or ""
     if reasoning_chunk and detector.feed(reasoning_chunk):
         logger.warning(
             f"[Phase2] {log_label} attempt {attempt + 1}: "
             f"reasoning loop detected after {detector.total_reasoning_chars} "
             "reasoning chars, aborting stream"
         )
         resp.close()
         raise ValueError(
             f"reasoning loop detected after {detector.total_reasoning_chars} chars"
         )
     ```

  3. **约束**：
     - 必须使用 `logger.warning`（非 `logger.error`），因为这是可恢复事件，后续会重试。
     - `resp.close()` 必须在 `raise` 之前调用，确保连接资源被释放。
     - 不修改 `except` 块——`ValueError` 已被现有 `except (urllib.error.URLError, TimeoutError, OSError, ValueError)` 覆盖，行为正确。

  4. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

---

#### [TASK ID: 003] [状态: OPEN]

- **定位**: `arxiv-update/scripts/tests/fake_llm_server.py`
- **目标描述**: 新增 `handle_reasoning_loop` 处理器，模拟 reasoning 陷入死循环的 SSE 流，供集成测试使用
- **前置依赖**: TASK 001（常量定义，用于确认检测参数）
- **执行指令与限制**:

  1. **处理器逻辑**：在现有 `handle_slow_stream` 之后添加：
     ```python
     def handle_reasoning_loop(self, repeat_count: int = 300) -> None:
         """Send a stream where reasoning loops forever, then yields empty content.

         Simulates glm-5 exhausting its reasoning token budget in a tight loop.
         repeat_count controls how many repeated reasoning lines to emit before
         sending the final content chunk with empty string and finish_reason=stop.
         """
         model = "fake-reasoning-model"
         req_id = "fake-loop-001"
         loop_line = "Let me assume the ID is `2605.17108`? No.\n"

         self._send_response(200, "text/event-stream")
         for _ in range(repeat_count):
             chunk = {
                 "id": req_id,
                 "object": "chat.completion.chunk",
                 "model": model,
                 "choices": [{"index": 0, "delta": {
                     "content": None,
                     "reasoning_content": loop_line,
                 }, "flag": 0}],
             }
             self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
             self.wfile.flush()

         # Final chunk: empty content, finish_reason=stop
         final = {
             "id": req_id,
             "object": "chat.completion.chunk",
             "model": model,
             "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop", "flag": 0}],
             "usage": {
                 "prompt_tokens": 100,
                 "completion_tokens": repeat_count,
                 "completion_tokens_details": {"reasoning_tokens": repeat_count},
                 "total_tokens": 100 + repeat_count,
             },
         }
         self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
         self.wfile.write(b"data: [DONE]\n\n")
         self.wfile.flush()
     ```

  2. **路由注册**：在 fake server 的请求路由逻辑中（`do_POST` 或等效分发处），将 URL path `/v1/chat/completions/reasoning_loop` 或通过请求 body 中的特定标记（参考现有 fake server 的路由模式）路由到 `handle_reasoning_loop`。**以现有 fake server 路由约定为准，不要发明新的路由机制。**

  3. **必须使用 `try-except` 包裹** `self.wfile.write`（或遵循现有 fake server 的写入模式），防止客户端提前关闭连接时抛出 `BrokenPipeError` 导致测试 server crash。

  4. 完成后运行 `python3 -m py_compile arxiv-update/scripts/tests/fake_llm_server.py` 验证语法。

---

#### [TASK ID: 004] [状态: OPEN]

- **定位**: `arxiv-update/scripts/tests/test_arxiv_daily.py`
- **目标描述**: 为 `_ReasoningLoopDetector` 和 `call_llm` 集成循环检测补充单元测试与集成测试
- **前置依赖**: TASK 001, TASK 002, TASK 003
- **执行指令与限制**:

  1. **新增测试类 `TestReasoningLoopDetector`**（单元测试，不依赖 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_no_loop_short_text` | 少量 reasoning 文本，`feed` 全程返回 False |
     | `test_detects_loop_after_threshold` | 同一行重复 `REASONING_LOOP_THRESHOLD` 次后返回 True（分多次 `feed` 调用） |
     | `test_short_lines_ignored` | 长度 < `REASONING_LOOP_MIN_LEN` 的重复行不触发检测 |
     | `test_partial_lines_buffered` | chunk 不含 `\n` 时不误判（行尚未完整） |
     | `test_total_chars_accumulated` | `total_reasoning_chars` 累积正确 |
     | `test_window_slides` | 超过 `REASONING_LOOP_WINDOW` 条不同行后，早期的重复行被驱逐出窗口，不再误触发 |

  2. **新增测试类 `TestCallLlmLoopDetection`**（集成测试，使用 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_loop_triggers_retry` | fake server 发送 reasoning 循环流，`call_llm` 检测到循环后中止并重试；验证总 attempt 次数 > 1 |
     | `test_no_false_positive_normal_reasoning` | fake server 发送正常（无重复）reasoning 流后给出有效 JSON content；`call_llm` 正常返回，无误触发 |

  3. **约束**：
     - 单元测试（`TestReasoningLoopDetector`）不得使用 fake server，直接实例化 `_ReasoningLoopDetector` 即可。
     - 集成测试中 fake server 的调用次数通过 mock 或 server 内部计数器验证（参考现有 `TestCallLlm*` 测试类的模式）。
     - 必须确保测试结束后 fake server 线程被正确清理（参考现有 fixture）。

  4. 完成后运行：
     ```bash
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestReasoningLoopDetector \
                                   tests/test_arxiv_daily.py::TestCallLlmLoopDetection -v
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q
     ```
     全部通过后方可标记 RESOLVED。

---

## 4. 最终决策 (Final Decision)

（由 Reviewer 在最终判决 PASS 时填写）
