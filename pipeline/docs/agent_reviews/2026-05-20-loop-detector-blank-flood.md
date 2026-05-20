---
title: "流式死循环检测：增加空白字符配额，覆盖纯空白循环"
date: "2026-05-20"
status: "PLANNING_COMPLETE"
---

## 1. 元数据 (Metadata)

- **原始需求**: 上一版 ADR `2026-05-20-loop-detector-unique-lines.md` 已上线"窗口内唯一行数"判据，成功消除模板化重复误报。但实测 `batch_010_stream_attempt1.txt` 显示一种新的循环故障形态：content 字段输出 16241 个纯空白 chunk（共 ~218000 字符的 `\n` 与空格组合），未触发任何循环检测。直接原因是 `feed` 中 `if not stripped: continue` 显式跳过空行，导致空白循环既不入 window，也不计入 unique，永不触发。需要补充对纯空白洪流的独立判据。
- **技术翻译**: 在现有 `_StreamLoopDetector.feed` 中新增"连续空白字符配额"机制：维护一个 `_blank_chars` 计数器，纯空白 chunk 累加该计数器、有效 chunk 重置；累计达到 `STREAM_LOOP_MAX_BLANK_CHARS` 即触发循环并中止。该机制与现有"窗口唯一行数"判据正交并存——空白 chunk 仍按原逻辑跳过 window 入队，不污染 unique 计数。`call_llm` 调用点、错误传播路径、日志格式、重试链路全部不变。
- **Scope**: `arxiv`
- **涉及文件**:
  - `arxiv-update/scripts/arxiv_daily.py`（修改：新增 `STREAM_LOOP_MAX_BLANK_CHARS` 常量、扩展 `_StreamLoopDetector.feed`；`call_llm` 调用点不变）
  - `arxiv-update/scripts/tests/test_arxiv_daily.py`（修改：扩充 `TestStreamLoopDetector` 加入空白配额测试；扩充 `TestCallLlmLoopDetection` 加入纯空白循环集成测试）
  - `arxiv-update/scripts/tests/fake_llm_server.py`（修改：新增 `handle_blank_flood_loop` 处理器）

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

---

## 2. 设计概要 (Design Overview)

### 故障形态回顾

`batch_010_stream_attempt1.txt` 实测：

```
total chunks: 16649
reasoning chars: 8594          ← reasoning 阶段正常完成
content chars: 218405
content chunks: 16249
whitespace-only content chunks: 16241   ← 99.95% 的 content chunk 是纯空白
```

content 内容片段（实际字符）：
```
'{"papers": [{"id":"...", "summary":"...本文审计了..."'  ← 头部正常 JSON 1 段
+ '\n\n\n\n\n\n\n\n\n  \n\n\n\n\n\n\n\n  \n\n\n\n...'  ← 持续到 token 耗尽
```

模型在写出第一段 paper 摘要后陷入"反复输出 `\n` + 空格"的循环，没有任何语义内容。

### 现有算法的盲区

```python
def feed(self, chunk: str) -> bool:
    ...
    for line in lines[:-1]:
        stripped = line.strip()
        if not stripped:
            continue  # ← 空行被跳过，永不入 window
        self._window.append(stripped)
        if len(self._window) == self._window_size and len(set(self._window)) <= self._max_unique:
            return True
    return False
```

空白循环下 `lines[:-1]` 全是空字符串 → 全部 `continue` → window 永不增长 → 永不触发。

### 修复方案：独立的空白字符配额

判据正交叠加，与原 unique 判据互不干扰：

```
判据 1（原）：window 装满后，len(set(window)) <= MAX_UNIQUE  → 语义循环
判据 2（新）：连续空白字符累计 >= MAX_BLANK_CHARS            → 空白洪流
```

任一触发即中止流。

### 数据流向 (Data Flow)

```
detector.feed(chunk)
  │
  ├─ self._total_chars += len(chunk)
  │
  ├─ chunk.strip() == ""？
  │     ├─ 是 → self._blank_chars += len(chunk)
  │     │       └─ self._blank_chars >= STREAM_LOOP_MAX_BLANK_CHARS
  │     │             → return True（空白循环触发）
  │     └─ 否 → self._blank_chars = 0  （遇到有效内容则重置）
  │
  └─ self._buf += chunk
        └─ 按 \n 分行 → 非空行入 window
              └─ window 装满 + unique ≤ MAX_UNIQUE
                    → return True（语义循环触发）
```

注意：纯空白 chunk 仍会进入 `self._buf += chunk` 与 `split("\n")` 的常规路径。这些空行被 `if not stripped: continue` 跳过，不入 window，**与现有行为完全一致**——空白配额是新增的旁路检测，不修改原主流程。

### 核心接口定义 (API Contracts)

```python
# 新增常量（位于现有 STREAM_LOOP_* 同处）
STREAM_LOOP_WINDOW         = 30   # 不变
STREAM_LOOP_MAX_UNIQUE     = 12   # 不变
STREAM_LOOP_MAX_BLANK_CHARS = 200  # 新增：连续空白字符达此值即视为循环

class _StreamLoopDetector:
    """Sliding-window unique-line detector with blank-flood quota for LLM SSE streams."""

    def __init__(
        self,
        window_size: int = STREAM_LOOP_WINDOW,
        max_unique: int = STREAM_LOOP_MAX_UNIQUE,
        max_blank_chars: int = STREAM_LOOP_MAX_BLANK_CHARS,
    ) -> None: ...

    def feed(self, chunk: str) -> bool: ...

    @property
    def total_chars(self) -> int: ...
```

`call_llm` 调用点保持不变——`feed`/`total_chars` 接口签名未变；新参数 `max_blank_chars` 有默认值。

### 参数标定

`STREAM_LOOP_MAX_BLANK_CHARS = 200`：

| 场景 | 连续空白字符数 | 判定 |
|---|---|---|
| 正常 JSON 缩进/分隔（`\n  ` × 论文条目） | 通常 ≤ 50 | 不触发 ✓ |
| 长 JSON 数组中相邻 paper 之间的空白格式化 | 极端 ~100 | 不触发 ✓ |
| batch_010 实测空白循环 | 218000+ | 200 字符即触发，约 < 1 秒 ✓ |

`200` 字符给正常格式化留 ≥ 4× 余量，对真实空白循环延迟极短。

### 风险预警 (Risk Assessment)

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

| # | 风险 | 应对 |
|---|------|------|
| 1 | 正常 JSON 输出中的连续空白（缩进、分隔符）误触发 | `MAX_BLANK_CHARS = 200` 字符相当于 200 个 `\n` 或 50 行带 4 空格缩进的 JSON 块；远超正常格式化需求；TASK 004 中加入 `test_normal_json_indent_no_false_positive` 强制回归 |
| 2 | 模型在合理输出末尾可能输出多个 `\n` 收尾 | 末尾收尾通常 ≤ 5 个 `\n`，且 stream 已接近 `[DONE]`；即便误触发也会被现有 retry 机制吸收，下次成功重试 |
| 3 | `_blank_chars` 重置时机选择 | 任何非空白 chunk 都重置——只有"连续"空白才累加；保证与"循环"语义对齐 |
| 4 | 与原 unique 判据交互导致重复触发或顺序问题 | 两个判据独立 return，先检测空白配额、再走主算法；任一触发立即返回 True，不会重复 |
| 5 | reasoning_content 阶段也可能出现空白循环 | 算法对字段无关，feed 任何字段的空白都会累加；自然覆盖 |
| 6 | 单 chunk 内含大量空白（一次性 1000 个 `\n`） | `len(chunk)` 累加正确，单次 feed 即可触发；不依赖 chunk 数量 |
| 7 | 接口变更影响 `call_llm` 调用方 | `feed(chunk) -> bool` 与 `total_chars` 属性签名保持不变；`__init__` 新增参数有默认值；TASK 002 仅做存在性验证 |
| 8 | 日志无法区分两种触发原因 | TASK 002 的日志消息保持现状（`"stream loop detected after N chars"`）即可；如需细化可在后续 ADR 处理，本次范围克制 |

---

## 3. 交互历史 (The Thread)

### [Round 1]

*(Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行)*

---

#### [TASK ID: 001] [状态: OPEN]

- **定位**: `arxiv-update/scripts/arxiv_daily.py`
  - 常量区（`STREAM_LOOP_*` 同处）：新增 `STREAM_LOOP_MAX_BLANK_CHARS`
  - `_StreamLoopDetector` 类：扩展 `__init__` 与 `feed`
- **目标描述**: 在现有 `_StreamLoopDetector` 上新增空白字符配额机制，对纯空白 chunk 累加计数；超阈值即触发循环
- **前置依赖**: 无
- **执行指令与限制**:

  1. **常量新增**：在 `STREAM_LOOP_MAX_UNIQUE = 12` 之后追加：
     ```python
     STREAM_LOOP_MAX_BLANK_CHARS = 200
     ```

  2. **修改 `__init__`**：新增 `max_blank_chars` 参数与 `_blank_chars` 状态：
     ```python
     def __init__(
         self,
         window_size: int = STREAM_LOOP_WINDOW,
         max_unique: int = STREAM_LOOP_MAX_UNIQUE,
         max_blank_chars: int = STREAM_LOOP_MAX_BLANK_CHARS,
     ) -> None:
         from collections import deque

         self._window: deque[str] = deque(maxlen=window_size)
         self._window_size = window_size
         self._max_unique = max_unique
         self._max_blank_chars = max_blank_chars
         self._buf = ""
         self._total_chars = 0
         self._blank_chars = 0     # 新增：连续空白字符累计
     ```

  3. **修改 `feed`**：在 `self._total_chars += len(chunk)` 之后、`self._buf += chunk` 之前插入空白配额检测逻辑：
     ```python
     def feed(self, chunk: str) -> bool:
         """Ingest a text chunk and return True if a loop is detected."""
         self._total_chars += len(chunk)

         # 空白字符配额检测（与主判据正交）
         if chunk.strip() == "":
             self._blank_chars += len(chunk)
             if self._blank_chars >= self._max_blank_chars:
                 return True
         else:
             self._blank_chars = 0

         # 以下为原主算法，逐字保留
         self._buf += chunk
         lines = self._buf.split("\n")
         self._buf = lines[-1]
         for line in lines[:-1]:
             stripped = line.strip()
             if not stripped:
                 continue
             self._window.append(stripped)
             if (
                 len(self._window) == self._window_size
                 and len(set(self._window)) <= self._max_unique
             ):
                 return True
         return False
     ```

  4. **不修改任何其他已有代码**。docstring 可适度更新以反映新增机制（可选，不强制）。

  5. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

---

#### [TASK ID: 002] [状态: OPEN]

- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `call_llm` 函数内 SSE 解析循环
- **目标描述**: 验证 `call_llm` 内的检测器调用点无需修改
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
  3. **若不一致** → 按上述代码补齐。
  4. **约束**：
     - 必须使用 `logger.error`，与现有网络/超时错误的日志级别一致。
     - `resp.close()` 必须在 `raise` 之前调用。
     - 不修改任何 `except` 块。
     - **输出路径说明**：循环检测触发后产生两条 `logger.error` 记录——①检测点，②现有 `except (... ValueError)` 入口；不向 stderr `print`。

  5. 完成后运行 `python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 验证语法。

---

#### [TASK ID: 003] [状态: OPEN]

- **定位**: `arxiv-update/scripts/tests/fake_llm_server.py`
- **目标描述**: 新增 `handle_blank_flood_loop` 处理器，模拟 batch_010 的纯空白循环场景
- **前置依赖**: TASK 001
- **执行指令与限制**:

  1. **新增 `handle_blank_flood_loop`**：先输出一段正常 content（避免 reasoning 阶段干扰），随后持续输出纯空白 chunk：
     ```python
     def handle_blank_flood_loop(self, head_chars: int = 100, blank_chunks: int = 50) -> None:
         """Simulate content blank-flood loop (e.g., batch_010 production case)."""
         model = "fake-blank-flood-model"
         req_id = "fake-blank-flood-001"

         self._send_response(200, "text/event-stream")

         # 头部：一段正常 content（不会被检测器视为循环）
         head_chunk = {
             "id": req_id, "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {
                 "content": '{"papers": [{"id": "test"}]',
             }, "flag": 0}],
         }
         self.wfile.write(f"data: {json.dumps(head_chunk)}\n\n".encode())
         self.wfile.flush()

         # 空白洪流：每个 chunk 含 ~10 字符空白，累计远超 MAX_BLANK_CHARS=200
         blank_payload = "\n\n\n\n\n  \n\n"  # 10 字符
         for _ in range(blank_chunks):
             chunk = {
                 "id": req_id, "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {
                     "content": blank_payload,
                 }, "flag": 0}],
             }
             self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
             self.wfile.flush()

         # 尾部参考 handle_content_loop：发送 final chunk + [DONE]
     ```
     `blank_chunks=50 × 10 = 500` 字符空白，远超 `MAX_BLANK_CHARS=200`，必触发。

  2. **路由注册**：以现有 fake server 路由约定为准，为新处理器注册路由。

  3. **写入模式**：必须遵循现有 fake server 的 `BrokenPipeError` 处理模式——`call_llm` 触发循环检测后会主动 close 连接，server 后续 write 必须能优雅吞掉异常。

  4. 完成后运行 `python3 -m py_compile arxiv-update/scripts/tests/fake_llm_server.py` 验证语法。

---

#### [TASK ID: 004] [状态: OPEN]

- **定位**: `arxiv-update/scripts/tests/test_arxiv_daily.py`
- **目标描述**: 扩充 `TestStreamLoopDetector` 与 `TestCallLlmLoopDetection`，覆盖空白配额相关场景
- **前置依赖**: TASK 001, TASK 002, TASK 003
- **执行指令与限制**:

  1. **扩充 `TestStreamLoopDetector`**（单元测试，不依赖 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_blank_flood_triggers` | 单次 feed `"\n" * 250`，超过 `MAX_BLANK_CHARS=200`，触发 |
     | `test_blank_flood_accumulates_across_chunks` | 多次 feed 每次 `"\n" * 50`，累计第 5 次（共 250 字符）触发 |
     | `test_non_blank_chunk_resets_blank_counter` | feed 100 空白 → feed `"x"` → feed 150 空白（共 250 但中间被重置）→ **不触发** |
     | `test_blank_flood_via_spaces_and_tabs` | 用 `"   \t  \n  "` 等纯空白组合也能触发（`strip() == ""` 判定） |
     | `test_normal_json_indent_no_false_positive` | feed `'{\n  "key": "val",\n  "k2": "v2"\n}'` 之类的正常 JSON 缩进，**不触发**（连续空白远小于 200） |
     | `test_blank_chunk_does_not_pollute_window` | feed 大量空白后，再 feed 30 行唯一文本，window 内 unique 仍 = 30，主判据不误触发 |
     | `test_custom_max_blank_chars` | 用 `_StreamLoopDetector(max_blank_chars=50)` 构造，验证参数生效 |

  2. **保留并验证现有 `TestStreamLoopDetector` 测试全部通过**——不应受本次新增逻辑影响。

  3. **扩充 `TestCallLlmLoopDetection`**（集成测试，使用 fake server）：

     | 测试方法 | 验证内容 |
     |----------|----------|
     | `test_blank_flood_triggers_retry` | fake server 发送 blank_flood 流，`call_llm` 检测到循环后中止并重试，attempt 次数 > 1 |

  4. **约束**：
     - `test_blank_flood_triggers` 与 `test_non_blank_chunk_resets_blank_counter` **是本次最关键的回归**——前者直接对应 batch_010 故障，后者保证不会误判正常输出尾部的少量空白。
     - 单元测试不依赖 fake server，直接实例化 `_StreamLoopDetector`。
     - 集成测试中 fake server 调用次数通过 mock 或 server 内部计数器验证（参考现有 `TestCallLlm*` 模式）。
     - 必须确保测试结束后 fake server 线程被正确清理（参考现有 fixture）。

  5. 完成后运行：
     ```bash
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestStreamLoopDetector \
                                   tests/test_arxiv_daily.py::TestCallLlmLoopDetection -v
     ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q
     ```
     全部通过后方可标记 RESOLVED。

---

## 4. 最终决策 (Final Decision)

（由 Reviewer 在最终判决 PASS 时填写）
