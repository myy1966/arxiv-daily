---
title: "LLM 流式死循环检测（滑动窗口行重复，覆盖 reasoning_content 与 content）"
date: "2026-05-19"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)

- **原始需求**: 实测发现 glm-5 在流式输出阶段存在两类死循环：reasoning_content 阶段陷入重复推理行、content 阶段陷入重复输出行，均会导致请求耗时失控（>1000s）直至 token 耗尽。需要在流式接收阶段实时检测这两类循环并提前中止请求。
- **技术翻译**: 在 `call_llm` 的 SSE 流式解析循环中，对 `reasoning_content` 和 `content` 两个字段**共享一个 `_StreamLoopDetector` 实例**。每个 chunk 的 delta 中两者互斥（reasoning 阶段只有 `reasoning_content`，content 阶段只有 `content`），因此单实例顺序 feed 即可正确覆盖完整流。检测器触发即关闭 HTTP 连接、抛出 `ValueError`，由现有 `except (... ValueError)` 捕获后重试。
- **Scope**: `arxiv`
- **涉及文件**:
  - `arxiv-update/scripts/arxiv_daily.py`（修改：新增常量、新增 `_StreamLoopDetector` 类、修改 `call_llm` SSE 循环）
  - `arxiv-update/scripts/tests/test_arxiv_daily.py`（修改：新增测试类 `TestStreamLoopDetector` 和 `TestCallLlmLoopDetection`）
  - `arxiv-update/scripts/tests/fake_llm_server.py`（修改：新增 `handle_reasoning_loop` 和 `handle_content_loop` 两个处理器）

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

---

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)

```
call_llm (SSE 解析循环)
  │
  ├─ 实例化单个检测器（每次 attempt 重新创建）
  │     detector = _StreamLoopDetector()
  │
  ├─ 每个 chunk → delta 解析（reasoning_content 与 content 在同一 chunk 中互斥）
  │     ├─ chunk = delta.get("reasoning_content") or delta.get("content") or ""
  │     │     └─ 非空 → detector.feed(chunk)
  │     │           └─ True → resp.close() + raise ValueError("stream loop ...")
  │     │
  │     └─ delta.get("content") 非空且未触发循环 → 追加到 content_parts
  │
  ├─ ValueError 被现有 except (... ValueError) 捕获 → 重试
  │
  └─ 流正常结束 → 继续原有 content 拼接与 JSON 解析逻辑
```

### 核心接口定义 (API Contracts)

```python
# 新增常量（位于现有常量区，BATCH_SIZE 附近）
STREAM_LOOP_WINDOW    = 60   # 滑动窗口大小（保留最近 N 条完整行）
STREAM_LOOP_THRESHOLD =  4   # 同一行在窗口内出现 >= N 次视为循环
STREAM_LOOP_MIN_LEN   =  8   # 短于此字符数的行跳过，不计入检测

class _StreamLoopDetector:
    """Sliding-window line-repetition detector for LLM SSE streams.

    Detects infinite loops in either reasoning_content or content fields
    by tracking repeated lines within a fixed-size window of recently seen
    complete lines. Not thread-safe; create one instance per field per attempt.
    """

    def __init__(
        self,
        window_size: int = STREAM_LOOP_WINDOW,
        threshold: int = STREAM_LOOP_THRESHOLD,
        min_len: int = STREAM_LOOP_MIN_LEN,
    ) -> None: ...

    def feed(self, chunk: str) -> bool:
        """Ingest a text chunk and return True if a loop is detected.

        Args:
            chunk: A partial text fragment from a single SSE delta
                   (may not contain a complete line).

        Returns:
            True  → loop detected; caller should abort the stream immediately.
            False → no loop detected; continue receiving.
        """
        ...

    @property
    def total_chars(self) -> int:
        """Total characters processed so far (for log messages)."""
        ...
```

```python
# call_llm 内 SSE 循环的改动（伪代码，精确位置见 TASK 002）

detector = _StreamLoopDetector()   # 单实例，覆盖 reasoning_content 和 content

for raw_line in resp:
    ...
    delta = choices[0].get("delta", {})

    # 取当前 chunk 有效文本（两者在同一 chunk 中互斥）
    stream_chunk = delta.get("reasoning_content") or delta.get("content") or ""
    if stream_chunk and detector.feed(stream_chunk):
        logger.error(
            f"[Phase2] {log_label} attempt {attempt + 1}: "
            f"stream loop detected after {detector.total_chars} chars, aborting"
        )
        resp.close()
        raise ValueError(f"stream loop after {detector.total_chars} chars")

    # 原有 content 追加逻辑（仅对 content 字段执行，reasoning_content 不追加）
    if delta.get("content"):
        content_parts.append(delta["content"])
```

### 风险预警 (Risk Assessment)

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

| # | 风险 | 应对 |
|---|------|------|
| 1 | `resp.close()` 后继续读取抛出 `OSError`/`ValueError` 以外的异常，被现有重试逻辑漏掉 | `resp.close()` 紧跟 `raise ValueError(...)`，不依赖 close 本身引发何种异常；ValueError 已在现有 `except` 中 |
| 2 | 误报：正常 content 中存在重复的 JSON key 行（如 `"summary": "..."` 出现多次） | JSON content 的行通常含唯一 paper ID，不会在窗口内重复 4 次；`MIN_LINE_LEN=8` 额外过滤极短行 |
| 3 | 误报：正常的长推理中存在合理的重复行（如 "Yes." / "No."） | `MIN_LINE_LEN=8` 过滤极短行；`THRESHOLD=4` 要求 60-line 窗口内出现 4 次，误报概率极低 |
| 4 | 模型不输出 `reasoning_content` 字段（普通模型）时 detector 处理的全是 content | `delta.get("reasoning_content") or delta.get("content")` 保证有值则 feed，无值则跳过；逻辑不变，完全兼容无 reasoning 模型 |
| 5 | 循环发生在行边界之前（chunk 不含 `\n`）导致 buffer 累积 OOM | `_buf` 最大长度 = 一行文本长度；实测循环行约 20-80 字符，无 OOM 风险 |
| 6 | `detector` 实例跨 attempt 复用导致状态污染 | `detector` 在 `with urllib.request.urlopen(...) as resp:` 块内声明，每轮 attempt 重新进入该块，自然重置 |
| 7 | `ValueError` 与现有代码中已有的 `ValueError` 语义冲突 | 现有 `except (urllib.error.URLError, TimeoutError, OSError, ValueError)` 对所有 ValueError 均重试，行为正确 |
| 8 | 测试需覆盖两种循环类型 | TASK 003 新增 `handle_reasoning_loop` 和 `handle_content_loop` 两个 fake server 处理器；TASK 004 新增对应集成测试 |

---

## 3. 交互历史 (The Thread)

### [Round 1]

*(Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行)*

---

#### [TASK ID: 001] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/arxiv_daily.py`
  - 常量区（`BATCH_SIZE` 附近）：新增 3 个常量
  - `_StreamLoopDetector` 类：放置在常量区之后、`_retry_delay` 函数之前
- **目标描述**: 新增循环检测所需的模块级常量和 `_StreamLoopDetector` 类
- **前置依赖**: 无
- **执行指令与限制**:

  1. **常量**：在 `BATCH_SIZE = 16` 所在常量块末尾（`_last_fetch_time` 之前）追加：
     ```python
     STREAM_LOOP_WINDOW    = 60
     STREAM_LOOP_THRESHOLD =  4
     STREAM_LOOP_MIN_LEN   =  8
     ```

  2. **类定义**：在常量块与 `_retry_delay` 之间插入 `_StreamLoopDetector`：
     ```python
     class _StreamLoopDetector:
         """Sliding-window line-repetition detector for LLM SSE streams.

         Detects infinite loops in either reasoning_content or content fields
         by tracking repeated lines within a fixed-size window of recently seen
         complete lines. Not thread-safe; create one instance per field per attempt.
         """

         def __init__(
             self,
             window_size: int = STREAM_LOOP_WINDOW,
             threshold: int = STREAM_LOOP_THRESHOLD,
             min_len: int = STREAM_LOOP_MIN_LEN,
         ) -> None:
             from collections import deque
             self._window: deque[str] = deque(maxlen=window_size)
             self._threshold = threshold
             self._min_len = min_len
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
                 if len(stripped) < self._min_len:
                     continue
                 self._window.append(stripped)
                 if self._window.count(stripped) >= self._threshold:
                     return True
             return False

         @property
         def total_chars(self) -> int:
             """Total characters processed so far."""
             return self._total_chars
     ```

  3. **不修改任何已有代码**；`from collections import deque` 放在 `__init__` 内（`deque` 尚未在顶层单独导入）。

  4. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已在 `arxiv-update/scripts/arxiv_daily.py` 新增 `STREAM_LOOP_WINDOW`、`STREAM_LOOP_THRESHOLD`、`STREAM_LOOP_MIN_LEN` 常量和 `_StreamLoopDetector` 类，未修改后续 `call_llm` 或测试逻辑。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过。

---

#### [TASK ID: 002] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `call_llm` 函数内 SSE 解析循环（当前第 509–535 行区域）
- **目标描述**: 在 SSE 循环中实例化一个共享 `_StreamLoopDetector`，对 `reasoning_content` 和 `content` 顺序 feed，检测到循环时关闭连接并抛出 ValueError
- **前置依赖**: TASK 001
- **执行指令与限制**:

  1. **实例化位置**：在 `with urllib.request.urlopen(req, timeout=300) as resp:` 之后、`for raw_line in resp:` 之前，添加一行：
     ```python
     detector = _StreamLoopDetector()
     ```

  2. **注入位置**：在 `delta = choices[0].get("delta", {})` 之后、`if delta.get("content"):` 之前，插入：
     ```python
     stream_chunk = delta.get("reasoning_content") or delta.get("content") or ""
     if stream_chunk and detector.feed(stream_chunk):
         logger.error(
             f"[Phase2] {log_label} attempt {attempt + 1}: "
             f"stream loop detected after {detector.total_chars} chars, aborting"
         )
         resp.close()
         raise ValueError(f"stream loop after {detector.total_chars} chars")
     ```
     原有 `if delta.get("content"): content_parts.append(delta["content"])` **保持不变、不移动**。

  3. **约束**：
     - 必须使用 `logger.error`，与现有网络/超时错误的日志级别一致。
     - `resp.close()` 必须在 `raise` 之前调用。
     - 不修改任何 `except` 块。
     - **输出路径说明**：循环检测触发后会产生两条 `logger.error` 记录——①检测点的 `"stream loop detected after N chars, aborting"`，②现有 `except (... ValueError)` 中的 `"LLM call attempt X failed: stream loop after N chars"`。不额外向 stderr `print`，与现有网络/超时错误的输出行为一致。

  4. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已在 `arxiv-update/scripts/arxiv_daily.py` 的 `call_llm` SSE 解析循环中按 attempt 创建共享 `_StreamLoopDetector`，并在 `content` 追加前对 `reasoning_content`/`content` 统一检测，触发时记录 `logger.error`、关闭响应并抛出 `ValueError`；未修改任何 `except` 块。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过。

---

#### [TASK ID: 003] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/tests/fake_llm_server.py`
- **目标描述**: 新增 `handle_reasoning_loop` 和 `handle_content_loop` 两个处理器，分别模拟两类死循环的 SSE 流
- **前置依赖**: TASK 001
- **执行指令与限制**:

  1. **`handle_reasoning_loop`**：在现有 `handle_slow_stream` 之后添加。循环行通过 `reasoning_content` 字段发送，`content` 字段始终为 `None`；最终 chunk 发送空 `content` 和 `finish_reason: stop`：
     ```python
     def handle_reasoning_loop(self, repeat_count: int = 20) -> None:
         """Simulate reasoning_content infinite loop (e.g., glm-5 token exhaustion)."""
         model = "fake-reasoning-model"
         req_id = "fake-reasoning-loop-001"
         loop_line = "Let me assume the ID is `2605.17108`? No.\n"

         self._send_response(200, "text/event-stream")
         for _ in range(repeat_count):
             chunk = {
                 "id": req_id, "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {
                     "content": None, "reasoning_content": loop_line,
                 }, "flag": 0}],
             }
             self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
             self.wfile.flush()
         final = {
             "id": req_id, "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop", "flag": 0}],
             "usage": {"prompt_tokens": 100, "completion_tokens": repeat_count,
                       "total_tokens": 100 + repeat_count},
         }
         self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
         self.wfile.write(b"data: [DONE]\n\n")
         self.wfile.flush()
     ```

  2. **`handle_content_loop`**：紧随 `handle_reasoning_loop` 之后添加。循环行通过 `content` 字段发送（无 `reasoning_content`）；最终不发送有效 JSON，仅发送 `finish_reason: stop`：
     ```python
     def handle_content_loop(self, repeat_count: int = 20) -> None:
         """Simulate content field infinite loop."""
         model = "fake-content-loop-model"
         req_id = "fake-content-loop-001"
         loop_line = '"summary": "这是一段重复的摘要内容，模型陷入了循环。"\n'

         self._send_response(200, "text/event-stream")
         for _ in range(repeat_count):
             chunk = {
                 "id": req_id, "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"content": loop_line}, "flag": 0}],
             }
             self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
             self.wfile.flush()
         final = {
             "id": req_id, "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop", "flag": 0}],
             "usage": {"prompt_tokens": 100, "completion_tokens": repeat_count,
                       "total_tokens": 100 + repeat_count},
         }
         self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
         self.wfile.write(b"data: [DONE]\n\n")
         self.wfile.flush()
     ```

  3. **路由注册**：以现有 fake server 路由约定为准，分别为两个处理器注册路由。不要发明新的路由机制。

  4. **必须遵循现有 fake server 的写入模式**（参考 `handle_slow_stream`），确保客户端提前关闭连接时 `BrokenPipeError` 被正确处理。

  5. **`repeat_count` 默认值设为 20**（远大于 `STREAM_LOOP_THRESHOLD=4`，确保检测器在 server 发完所有行之前触发）。

  6. 完成后运行 `python3 -m py_compile arxiv-update/scripts/tests/fake_llm_server.py` 验证语法。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已在 `arxiv-update/scripts/tests/fake_llm_server.py` 新增 `handle_reasoning_loop` 和 `handle_content_loop`，并按现有 fake server 的模块级 `ERROR_HANDLERS` 路由约定注册 `reasoning_loop` 与 `content_loop`；写入模式与 `handle_slow_stream` 保持一致，客户端提前关闭连接时由现有 `do_POST` 外层捕获处理。验证：`python3 -m py_compile arxiv-update/scripts/tests/fake_llm_server.py` 通过。

---

#### [TASK ID: 004] [状态: RESOLVED]

- **定位**: `arxiv-update/scripts/tests/test_arxiv_daily.py`
- **目标描述**: 为 `_StreamLoopDetector` 和 `call_llm` 集成循环检测补充单元测试与集成测试
- **前置依赖**: TASK 001, TASK 002, TASK 003
- **执行指令与限制**:

  1. **新增测试类 `TestStreamLoopDetector`**（单元测试，不依赖 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_no_loop_short_text` | 少量文本，`feed` 全程返回 False |
     | `test_detects_loop_after_threshold` | 同一行重复 `STREAM_LOOP_THRESHOLD` 次后返回 True（分多次 `feed` 调用） |
     | `test_short_lines_ignored` | 长度 < `STREAM_LOOP_MIN_LEN` 的重复行不触发检测 |
     | `test_partial_lines_buffered` | chunk 不含 `\n` 时不误判（行尚未完整） |
     | `test_total_chars_accumulated` | `total_chars` 累积正确 |
     | `test_window_slides` | 超过 `STREAM_LOOP_WINDOW` 条不同行后，早期重复行被驱逐出窗口，不再误触发 |

  2. **新增测试类 `TestCallLlmLoopDetection`**（集成测试，使用 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_reasoning_loop_triggers_retry` | fake server 发送 reasoning_content 循环流，`call_llm` 检测到循环后中止；验证总 attempt 次数 > 1 |
     | `test_content_loop_triggers_retry` | fake server 发送 content 循环流，`call_llm` 检测到循环后中止；验证总 attempt 次数 > 1 |
     | `test_no_false_positive_normal_stream` | fake server 发送正常（无重复）流并给出有效 JSON content；`call_llm` 正常返回，无误触发 |

  3. **约束**：
     - 单元测试直接实例化 `_StreamLoopDetector`，不依赖 fake server。
     - 集成测试中 fake server 的调用次数通过 mock 或 server 内部计数器验证（参考现有 `TestCallLlm*` 测试类的模式）。
     - 必须确保测试结束后 fake server 线程被正确清理（参考现有 fixture）。

  4. 完成后运行：
     ```bash
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestStreamLoopDetector \
                                   tests/test_arxiv_daily.py::TestCallLlmLoopDetection -v
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q
     ```
     全部通过后方可标记 RESOLVED。

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准；已在 `arxiv-update/scripts/tests/test_arxiv_daily.py` 新增 `TestStreamLoopDetector` 单元测试类和 `TestCallLlmLoopDetection` 集成测试类，覆盖重复阈值、短行过滤、partial line buffering、窗口滑动、字符计数、reasoning/content loop 重试和正常流无误报。验证：`python3 -m py_compile arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；在 `arxiv-update/scripts` 下运行 `./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestStreamLoopDetector tests/test_arxiv_daily.py::TestCallLlmLoopDetection -v`，9 passed；运行 `./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q`，161 passed, 1 xfailed。

---

## 4. 最终决策 (Final Decision)

（由 Reviewer 在最终判决 PASS 时填写）
