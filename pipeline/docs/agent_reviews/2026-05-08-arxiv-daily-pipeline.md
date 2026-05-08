---
title: "arxiv_daily.py 全局架构审查"
date: "2026-05-08"
status: "REFACTOR_NEEDED"
---

## 1. 元数据 (Metadata)
- **审查目标**: 对 arxiv_daily.py 进行全局性深度代码审查，覆盖 LLM 容错、幂等性、爬虫礼仪和 DRY 四个维度
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`

## 2. 变更摘要 (Change Summary)
- 当前分支实现了一个三阶段流水线：Phase1（HTML 抓取与解析） → Phase2（LLM 批量摘要） → Phase3（Markdown 生成）
- 包含缓存机制（SHA-256 元数据哈希校验）、流式 SSE 读取、重试逻辑、patch 模式
- 已有 json_repair 容错、debug 文件落盘、quality check 等防御性措施

## 3. 交互历史 (The Thread)

### [Round 1]
- **Reviewer**:
  - **状态判决**: `[STATUS: REFACTOR_NEEDED]`

  ---

  #### [ISSUE ID: 001] LLM 429/5xx 无区分处理，重试策略对非幂等错误不安全
  - **定位**: `arxiv_daily.py:call_llm` (L321-409)
  - **问题描述**: 当前重试循环对**所有** Exception 一视同仁地执行退避重试（L397-407）。然而：
    1. HTTP 4xx（如 400 Bad Request、401 Unauthorized）重试无意义，白白消耗延时。
    2. 未区分 HTTP 429（Rate Limit）的 `Retry-After` 头——如果 API 返回了此头，固定延时 `[2, 4, 8]` 秒可能远低于所需等待时间，导致连续 429 打满重试次数后彻底失败。
    3. `json.JSONDecodeError` 被 `raise` 后仍然被外层 `except Exception` 捕获并进入下一次重试（L398），意味着 JSON 解析失败也会触发完整的 LLM API 重新调用——这可能是有意为之（期望 LLM 重新生成有效 JSON），但应显式标注意图。
  - **修复指令**:
    ```python
    # 在 except 块中区分错误类型:
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = int(e.headers.get("Retry-After", RETRY_DELAYS[attempt]))
            logger.warning(f"Rate limited, waiting {retry_after}s")
            time.sleep(retry_after)
        elif 400 <= e.code < 500 and e.code != 429:
            raise  # 客户端错误不重试
        else:
            time.sleep(RETRY_DELAYS[attempt])
    except json.JSONDecodeError:
        # 显式重试：期望 LLM 重新生成有效 JSON
        time.sleep(RETRY_DELAYS[attempt])
    except (urllib.error.URLError, TimeoutError, OSError):
        time.sleep(RETRY_DELAYS[attempt])
    ```

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:call_llm` 的异常分派：HTTP 429 读取 `Retry-After` 后再退避，HTTP 400/401 等非 429 客户端错误直接失败不重试，HTTP 5xx/网络错误继续按退避策略重试，`json.JSONDecodeError` 单独记录为“请求 LLM 重新生成 JSON”的显式重试路径。补充 `_retry_delay` 与 `_retry_after_delay`，避免退避参数解析散落在异常块中；新增 fake LLM 的 HTTP 400/可配置 Retry-After 模式，并补充 429 遵守 Retry-After、400 不重试的测试用例。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/fake_llm_server.py arxiv-update/scripts/tests/test_arxiv_daily.py arxiv-update/scripts/tests/conftest.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestCallLlmErrorModes -q` 通过，9 passed。

  ---

  #### [ISSUE ID: 002] 流式读取未处理 SSE 中途断流（Partial Stream）
  - **定位**: `arxiv_daily.py:call_llm` (L335-356)
  - **问题描述**: 流式 SSE 读取循环在收到 `[DONE]` 时正常终止。但如果网络中断导致连接在收到 `[DONE]` 之前断开，循环会静默退出（`for raw_line in resp` 迭代器耗尽），此时 `content_parts` 包含**截断的 JSON fragment**。后续 `json.loads(content)` 会抛 `JSONDecodeError` 并触发重试——这虽然会"修复"问题，但：
    1. 无法区分"LLM 返回了畸形 JSON"与"网络断流"两种根因，日志中丢失了关键诊断信息。
    2. 如果恰好截断点产生了合法但不完整的 JSON（例如 `{"papers": [`），`json_repair` 可能会"修复"为空数组，导致静默数据丢失。
  - **修复指令**:
    ```python
    saw_done = False
    for raw_line in resp:
        # ... existing logic ...
        if data_str == "[DONE]":
            saw_done = True
            break
    if not saw_done:
        logger.warning(f"[Phase2] {log_label} stream ended without [DONE] marker (possible network interruption)")
        raise ConnectionError("SSE stream incomplete: no [DONE] marker received")
    ```

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:call_llm` 的 SSE 读取逻辑，新增 `saw_done` 状态追踪；当流迭代结束但未收到 `[DONE]` marker 时，记录明确 warning 并抛出 `ConnectionError("SSE stream incomplete: no [DONE] marker received")`，避免截断内容继续进入 `json.loads` 或 `json_repair` 造成静默数据丢失。该异常会沿用 Issue 001 已拆分的网络错误退避路径进行重试。补充测试：永久 partial stream 断言抛出明确 `ConnectionError`，短暂 partial stream 断言重试后成功。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestCallLlmErrorModes -q` 通过，10 passed。

  ---

  #### [ISSUE ID: 003] `fetch_page` 无爬虫礼仪延时，多字段串行运行也无 rate limit
  - **定位**: `arxiv_daily.py:fetch_page` (L59-64)
  - **问题描述**: `fetch_page` 直接发起请求，无任何 `time.sleep()` 间隔。虽然当前脚本单次运行只抓取一个 field 的一个页面，但：
    1. arXiv 的 robots.txt 和 Terms of Service 建议爬虫间隔不低于 3 秒。
    2. 如果未来通过 shell 循环对多个 field 串行调用此脚本，会对 arXiv 形成高频请求。
    3. 当前 User-Agent 为通用 `Mozilla/5.0`，不符合 arXiv 建议的 bot 标识规范（应包含联系邮箱）。
  - **修复指令**:
    1. 在 `fetch_page` 顶部加入模块级变量 `_last_fetch_time` 和至少 3 秒的间隔控制。
    2. User-Agent 改为 `arxiv-daily-bot/1.0 (contact: <your-email>)` 格式，便于 arXiv 管理员识别。

  Executor: [DISPUTE] 修正评审意见。仅按建议加入 `_last_fetch_time` 无法覆盖文中提到的 shell 循环多进程场景，因为每次脚本启动都会重置内存状态，仍可能连续打到 arXiv。已修改 `arxiv-update/scripts/arxiv_daily.py:fetch_page` 周边实现：新增 `ARXIV_FETCH_INTERVAL_SECONDS = 3.0`、`ARXIV_USER_AGENT`、`_last_fetch_time` 和 `_wait_for_arxiv_rate_limit()`；首次 arXiv 请求前也执行 3 秒 courtesy delay，进程内后续请求按 `_last_fetch_time` 补足间隔。User-Agent 改为 `arxiv-daily-bot/1.0 (contact: ...)`，支持 `ARXIV_UPDATE_CONTACT` 和 `ARXIV_UPDATE_USER_AGENT` 环境变量注入真实联系信息，避免硬编码虚假邮箱。补充 `TestFetchPage` 覆盖 URL、User-Agent、首次延时和进程内延时夹取。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestFetchPage tests/test_arxiv_daily.py::TestMainCli -q` 通过，10 passed。

  ---

  #### [ISSUE ID: 004] Phase2 重试逻辑大量代码复制（DRY 违规）
  - **定位**: `arxiv_daily.py:run_phase2` (L617-738)
  - **问题描述**: `run_phase2` 中"首次执行"循环（L617-677）与"失败重试"循环（L680-738）的代码高度重复：两段均包含 `build_prompt → call_llm → match_llm_results → 更新 tags → 打印状态 → 写缓存` 的完整流程，差异仅在于：
    - batch_label 前缀（`batch_prefix` vs `retry_batch_prefix`）
    - 打印消息略有不同
    
    这导致约 **60 行几乎相同的代码**被复制粘贴。任何对 LLM 调用后处理逻辑的修改都必须同步两处，极易遗漏。
  - **修复指令**:
    抽取 `_process_batch(batch, idx, total, current_tags, ..., label_prefix) -> (matched, new_tags, success)` 辅助函数，主循环和重试循环只需分别调用此函数。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:run_phase2`，抽取 `_process_batch` 统一执行 `build_prompt -> call_llm -> match_llm_results -> 更新 current_tags -> 打印状态 -> 可选写缓存`，并补充 `BatchProcessResult`、`_format_usage`、`_write_batch_cache` 等小型辅助结构。首轮处理和 retry 处理现在调用同一批处理函数，仅通过 `label_prefix`、`cache_allowed` 和异常日志文案表达差异；Issue 008 引入的子集重试仍保留“不写完整 batch cache”的约束。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestRunPhase2 tests/test_arxiv_daily.py::TestRunPhase2ErrorModes -q` 通过，13 passed；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q` 通过，118 passed, 1 xfailed。

  ---

  #### [ISSUE ID: 005] 缓存写入非原子操作，进程中断可能生成损坏的 JSON 缓存文件
  - **定位**: `arxiv_daily.py:run_phase2` (L659-667, L716-724)
  - **问题描述**: 缓存写入直接 `open(cache_path, "w")` 后 `json.dump`。如果进程在写入过程中被 SIGKILL/断电中断，缓存文件会处于截断状态。下次运行时 `json.load(bf)` 会抛异常，当前代码用 `except Exception: pass`（L607）跳过了损坏的缓存——但这意味着该 batch 会被重新发送给 LLM，浪费 Token 成本。
  - **修复指令**:
    使用 write-to-temp-then-rename 原子写入模式：
    ```python
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=field_cache, suffix=".json.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(cache_entry, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, cache_path)  # atomic on POSIX
    except:
        os.unlink(tmp_path)
        raise
    ```

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py` 的 batch cache 写入路径：新增 `_atomic_write_json(path, data)`，使用 `tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")` 写入临时文件，再通过 `os.replace` 原子替换目标文件；异常路径会清理残留临时文件并保留旧缓存文件不变。`_write_batch_cache` 已改为调用该原子写入 helper。补充 `TestPhase2Helpers` 测试正常替换、UTF-8 内容写入、异常时清理 `.json.tmp` 且不破坏既有 cache。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestPhase2Helpers tests/test_arxiv_daily.py::TestRunPhase2 -q` 通过，15 passed。

  ---

  #### [ISSUE ID: 006] `stream_file` 在异常路径下可能泄漏文件描述符
  - **定位**: `arxiv_daily.py:call_llm` (L330-359)
  - **问题描述**: `stream_file = open(stream_path, "w", ...)` 在 L330 打开，如果 `urllib.request.urlopen` (L332) 抛出异常（如连接被拒绝），控制流跳到 `finally` 块（L357-359）会正确关闭文件。然而，如果 `open(stream_path, ...)` 本身失败（如磁盘满），`stream_file` 仍为 `None`，`finally` 中 `if stream_file is not None` 条件跳过——这没问题。但更大的问题是：**`open` 调用在 `try` 块外部**（L330 在 L331 `try` 之前），所以如果 `open` 成功但紧接着在 L331 之前发生异常，`finally` 块不会被执行。
    
    实际上仔细看代码，`stream_file = open(...)` 在 L330，`try:` 在 L331，`finally: stream_file.close()` 在 L357-359。如果在 L330 和 L331 之间不会有异常（它们是连续的），这个问题理论上不存在。但这种模式不够健壮——建议将 `open` 也放入 `try` 块内或使用 `contextmanager`。
  - **修复指令**:
    将 stream_file 的生命周期管理统一到内层 try/finally 中，或使用 `contextlib.ExitStack`。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:call_llm`，使用 `contextlib.ExitStack` 管理可选的 stream debug 文件句柄，文件打开、SSE 读取和异常退出均处于同一上下文生命周期内，消除手写 `stream_file = None` + 外层 `finally` 的脆弱模式。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestCallLlm tests/test_arxiv_daily.py::TestCallLlmErrorModes -q` 通过，15 passed。

  ---

  #### [ISSUE ID: 007] 幂等性风险：HTML 保存逻辑有条件竞争窗口
  - **定位**: `arxiv_daily.py:main` (L1229-1231)
  - **问题描述**: 
    ```python
    if not page_archive_path.exists():
        page_archive_path.write_text(html, encoding="utf-8")
    ```
    这里有 TOCTOU（Time-of-check-to-time-of-use）竞争条件：如果两个进程同时针对同一 field 运行脚本，两者都可能通过 `exists()` 检查后同时写入。虽然最终文件内容相同（同一天的 HTML），后果不严重，但设计上应使用原子写入。
    
    更重要的幂等性问题是：**output markdown 的存在性检查**（L1236）仅在非 `--date` 模式下生效。当使用 `--date` 参数时，即使 markdown 已存在，也会重新运行 Phase2（消耗 LLM Token）并覆盖已有 markdown 文件。这可能是 intentional（用于修复），但缺乏文档说明。
  - **修复指令**:
    1. HTML 写入使用原子 rename 模式（同 ISSUE 005）。
    2. 在 `--date` 模式下，如果 output 已存在且未指定 `--no-cache`，打印提示并询问是否跳过，或至少在日志中明确记录 "overwriting existing output"。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:run_normal_mode`：新增 `_atomic_write_text(path, text)`，HTML 页面归档现在通过临时文件写入后 `os.replace` 原子替换，避免直接 `write_text` 中断造成半写入文件；`--date` 模式下如果目标 markdown 已存在且未指定 `--no-cache`，会同时向日志和 stderr 记录 `--date mode overwriting existing output`，明确本次会重跑 Phase2 并覆盖输出。补充测试覆盖原子文本写入和 `--date` 覆盖提示。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestPhase2Helpers tests/test_arxiv_daily.py::TestMainCli -q` 通过，15 passed。

  ---

  #### [ISSUE ID: 008] `match_llm_results` 质量检查删除结果后无恢复路径
  - **定位**: `arxiv_daily.py:match_llm_results` (L471-499)
  - **问题描述**: Pass 3 质量检查（summary 长度 < 50 字符或缺少 tags）会将论文从 `results` 中**删除**。被删除的论文会计入 `unmatched_count`，最终导致该 batch 被标记为失败并进入重试。但重试时会重新调用 LLM 处理**整个 batch**（包括那些已经通过质量检查的论文），浪费了已成功论文的 Token。
    
    更严重的是：如果某篇论文的摘要确实很短（例如数学公式密集型论文，中文概括可能天然简短），连续两次重试都会失败，该论文最终不会出现在 markdown 中——这是**静默数据丢失**。
  - **修复指令**:
    1. 重试时仅重新处理质量检查失败的论文子集，而非整个 batch。
    2. 对 `MIN_SUMMARY_LEN = 50` 添加配置项或降低阈值（中文50字符约等于25个汉字，对于短摘要论文可能过于严格）。
    3. 在最终输出中，对质量检查失败的论文仍然生成条目，但标记 summary 为 "[摘要生成失败]" 而非完全省略。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py`：将 `MIN_SUMMARY_LEN` 提升为模块常量并下调为 30；首轮 batch 中已通过质量检查的论文立即保留在 `results_by_id`，重试阶段只重新处理未成功进入 `results` 的论文子集（包含质量失败和 ID 未匹配），避免重复消耗已成功论文的 LLM token；若重试后仍未恢复，则写入 `SUMMARY_FAILURE_PLACEHOLDER = "[摘要生成失败]"` 和 `SUMMARY_FAILURE_TAG = "未分类"`，保证最终 Markdown 不静默丢论文。为避免把子集响应误写成完整 batch 缓存，子集重试成功时不写 `batch_*.json`，只有整批重试成功才沿用原缓存写入。补充测试覆盖摘要阈值、子集重试、最终占位输出。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMatchLlmResults tests/test_arxiv_daily.py::TestRunPhase2 -q` 通过，17 passed；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestRunPhase2ErrorModes -q` 通过，5 passed。

  ---

  #### [ISSUE ID: 009] Prompt 组装中论文分隔符与内容冲突风险
  - **定位**: `arxiv_daily.py:build_prompt` (L261)
  - **问题描述**: 
    ```python
    {"---".join(papers_text)}
    ```
    使用 `---` 作为论文之间的分隔符。如果某篇论文的 abstract 中包含 `---`（例如 Markdown 格式的摘要或数学公式分隔线），LLM 可能会错误地将其识别为论文边界，导致解析混乱。虽然概率较低，但考虑到每天处理数十到上百篇论文，长期运行必然会触发。
  - **修复指令**:
    使用更不常见的分隔符，例如 `\n=====PAPER_SEPARATOR=====\n`，或为每篇论文添加明确的序号标记（`[Paper 1/16]`, `[Paper 2/16]`），这样即使没有分隔符 LLM 也能正确识别边界。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:build_prompt`：新增 `PAPER_SEPARATOR = "\n=====PAPER_SEPARATOR=====\n"`，移除 prompt 中用于边界的裸 `---`，并为每篇论文添加 `[Paper i/n]` 显式序号标记；abstract 内部若包含 `---` 会作为正文保留，不再与论文边界混淆。补充 `TestBuildPrompt` 覆盖显式序号和非 Markdown 分隔符。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestBuildPrompt tests/test_arxiv_daily.py::TestCallLlm::test_normal_response -q` 通过，8 passed。

  ---

  #### [ISSUE ID: 010] `patch_markdown` 正则替换不支持多次 patch 同一文件
  - **定位**: `arxiv_daily.py:patch_markdown` (L962-1009)
  - **问题描述**: `patch_markdown` 对每篇论文执行正则搜索替换后，更新 `content` 字符串。由于替换后字符串长度可能变化，后续论文的 `re.search` 可能在已被前一轮替换修改过的区域产生错误匹配。当前实现实际上是安全的（每次 search 都在最新 content 上执行），但效率为 O(n*m)（n 篇论文 * m 文件长度）。对于大文件和大量 patch 论文，性能会显著下降。
  - **修复指令**:
    这是低优先级问题。如果 patch 论文数通常 < 10，当前实现可接受。记录为已知限制即可。

### [Round 2] — 补充遗漏项（来源：上一次 review 存档 TODO.md）
- **Reviewer**:
  - **状态判决**: `[STATUS: REFACTOR_NEEDED]`
  - **背景**: Round 1 仅覆盖了历史 review 的 2/13 项。以下为未覆盖项的补充，按优先级分组。

  ---

  #### [ISSUE ID: 011] LLM response 缺少统一 schema 校验（原 TODO#4，High）
  - **定位**: `arxiv_daily.py:call_llm` (L365-396)
  - **问题描述**: `json.loads(content)` 或 `json_repair_loads(content)` 成功后，返回值未经任何结构校验。一个形如 `{"foo": "bar"}` 或 `{"papers": "not a list"}` 的响应会被当作成功返回给 `run_phase2`。错误直到 `match_llm_results` 中 `result.get("papers", [])` 返回空列表时才暴露为"全部 unmatched"——此时已经浪费了一轮重试机会且日志诊断信息不足。
    
    `json_repair_loads` 路径仅检查 `isinstance(result, dict)` (L378)，更为宽松。
  - **修复指令**:
    在 `call_llm` 返回前（L396 之前）插入统一校验：
    ```python
    def _validate_llm_schema(result: dict) -> None:
        """Raise ValueError if LLM result lacks required structure."""
        if not isinstance(result.get("papers"), list):
            raise ValueError(f"LLM response missing 'papers' list, got keys: {list(result.keys())}")
        if "new_tags" in result and not isinstance(result["new_tags"], list):
            raise ValueError(f"'new_tags' must be list, got {type(result['new_tags'])}")
    ```
    - **结构性失败**（无 `papers` 键、`papers` 非 list）→ 抛 `ValueError`，触发重试
    - **部分质量问题**（个别 paper 缺 id/summary/tags）→ 不在此层处理，交给 `match_llm_results` 的现有质量检查

  Executor: [RESOLVED] 已修复。重构了 `arxiv_daily.py` 的 `call_llm` 返回路径，新增 `_validate_llm_schema` 统一校验函数，并同时接入 `json.loads` 与 `json_repair_loads` 成功路径；补充了 schema 校验单元测试和 fake LLM schema 异常模式测试。

  ---

  #### [ISSUE ID: 012] `current_tags` 缓存恢复顺序导致 tag 状态不一致（原 TODO#8，Medium）
  - **定位**: `arxiv_daily.py:run_phase2` (L599-608)
  - **问题描述**: 缓存预加载阶段遍历所有 `batch_*.json`，逐个用 `cached["current_tags"]` 覆盖 `current_tags`。如果 batch 1 缓存缺失（损坏/删除）但 batch 2 缓存存在：
    1. 预加载跳过 batch 1，加载 batch 2 时 `current_tags` 被更新为 batch 2 之后的状态
    2. 主循环处理 batch 1（cache miss）时，`build_prompt(batch, current_tags)` 使用了包含 batch 2 新增 tags 的列表
    3. LLM 看到了完整顺序执行中本不该出现的"未来 tags"，可能影响 tag 分配决策
  - **修复指令**:
    - 预加载阶段**不修改** `current_tags`，仅将 cache 内容读入 `cached_batches` dict
    - 主循环中按顺序命中 cache 时，再从该 cache entry 恢复 `current_tags`
    - 可选增强：每个 batch cache 额外存储输入时的 `input_tags` snapshot，加载时校验一致性，不一致则该 batch 缓存失效

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:run_phase2` 的缓存预加载逻辑：预加载阶段现在只读取 `batch_*.json` 到 `cached_batches`，不再用缓存中的 `current_tags` 覆盖当前标签状态；标签恢复仍保留在主循环的 cache hit 分支中，因此按 batch 顺序推进。未采用可选的 `input_tags` 快照校验，因为这会扩大缓存格式变更范围，属于额外一致性增强，不是本 Issue 的必要修复。补充回归测试 `test_cache_preload_does_not_leak_future_tags`，覆盖 batch 1 cache miss、batch 2 cache hit 时，batch 1 prompt 不会看到 batch 2 的未来标签。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestRunPhase2 -q` 通过，6 passed。

  ---

  #### [ISSUE ID: 013] `compute_metadata_hash` 仅含 paper ID，缓存可能复用过时 summary（原 TODO#12，Medium）
  - **定位**: `arxiv_daily.py:compute_metadata_hash` (L532-539)
  - **问题描述**: Hash 仅基于排序后的 paper ID。但 LLM prompt 实际依赖 `id`、`title`、`abstract`、`comments`。如果 arXiv 页面中同一 paper ID 的 abstract 被作者更新（常见于 replacement submissions），缓存 hash 不变，会复用旧 summary，直接输出过时内容。
  - **修复指令**:
    ```python
    def compute_metadata_hash(data: dict) -> str:
        hash_input = []
        for section in data["sections"]:
            for paper in section["papers"]:
                hash_input.append({
                    "id": paper["id"],
                    "title": paper.get("title", ""),
                    "abstract": paper.get("abstract", ""),
                    "comments": paper.get("comments", ""),
                })
        hash_input.sort(key=lambda x: x["id"])
        return hashlib.sha256(json.dumps(hash_input, sort_keys=True).encode()).hexdigest()
    ```
    注意：**不应**将 `current_tags` 纳入全局 hash——tags 是跨 batch 累积的，任何新增 tag 都会导致全部缓存失效。Tags 一致性应在 per-batch 级别校验（配合 ISSUE 012 方案）。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:compute_metadata_hash`，缓存 hash 现在基于 prompt 相关的 `id`、`title`、`abstract`、`comments` 生成，并按 `id` 排序以保持输入顺序无关；未将 `current_tags` 纳入全局 hash，避免 tag 累积导致全量缓存无谓失效。补充 `TestComputeMetadataHash.test_prompt_metadata_changes_hash`，覆盖 title/abstract/comments 变化会使缓存失效，同时更新顺序无关测试覆盖完整元数据。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestComputeMetadataHash -q` 通过，5 passed。

  ---

  #### [ISSUE ID: 014] HTML 解析脆弱，patch mode 缺少完整性检查（原 TODO#5，Medium）
  - **定位**: `arxiv_daily.py:parse_html` (L185-229), `main` patch mode (L1054-1128)
  - **问题描述**: 
    1. HTML 解析完全依赖正则匹配特定 class name 和 tag 结构。arXiv 页面的任何 HTML 重构都会导致静默解析失败（返回空 sections 或缺字段 paper）。
    2. Normal mode（L1175-1188）对解析结果有三层验证（sections 非空、papers 非零、abstracts 非零），但 **patch mode**（L1068-1076）仅检查 `patch_papers` 是否为空，不验证找到的 paper 是否具有完整字段（尤其是 abstract）。如果 HTML 格式变化导致 abstract 提取失败，patch mode 会将无 abstract 的 paper 送入 LLM，产生无意义 summary。
  - **修复指令**:
    1. patch mode 中对 `patch_papers` 增加与 normal mode 相同的完整性检查：
       ```python
       papers_without_abstract = [p for p in patch_papers if not p.get("abstract")]
       if papers_without_abstract:
           logger.warning(f"[Patch] {len(papers_without_abstract)} papers missing abstract: "
                         f"{[p['id'] for p in papers_without_abstract]}")
       ```
    2. 中期建议用 `html.parser.HTMLParser` 或 BeautifulSoup 替代正则，增强对 HTML 结构变动的鲁棒性。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:main` 的 patch mode：在目标论文解析完成后，立即统计并记录缺少 `abstract` 的 patch paper ID，同时向 stderr 打印 warning，避免用户只在后续 LLM/patch 失败时才间接发现 HTML 解析不完整。未在本轮替换正则 HTML parser，因为这是中期结构性改造，超出本 Issue 的最小行为修复范围。补充 `TestMainCli.test_patch_warns_for_missing_abstract`，模拟 patch mode 解析到目标论文但缺少 abstract 的场景，并断言 stderr 与日志均包含诊断。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMainCli -q` 通过，6 passed。

  ---

  #### [ISSUE ID: 015] `run_phase2` 职责过多，应拆分为独立函数（原 TODO#6，Medium）
  - **定位**: `arxiv_daily.py:run_phase2` (L542-749, ~200 lines)
  - **问题描述**: 单个函数承担了 tag 加载、batch 构建、cache 校验与预加载、LLM 调用循环、结果匹配、失败重试、tag 保存等全部职责。除了 ISSUE 004 指出的 DRY 问题外，函数的认知复杂度过高，难以单元测试各个子流程。
  - **修复指令**:
    拆分为以下子函数（建议在 ISSUE 004 和 ISSUE 012 修复后执行）：
    - `load_tags(tags_path) -> list[str]`
    - `collect_batches(data, batch_size) -> list[list[dict]]`
    - `load_cached_batches(field_cache, hash_file, current_hash, no_cache) -> dict[int, dict]`
    - `process_batch(batch, idx, total, current_tags, ...) -> BatchResult`
    - `save_tags(tags_path, current_tags)`

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:run_phase2` 的职责边界：新增 `load_tags`、`collect_batches`、`load_cached_batches`、`save_tags`，将标签加载/保存、batch 构建、metadata hash 校验和 batch cache 预加载从主流程中拆出；Issue 004 已抽出的 `_process_batch` 继续承担单批 LLM 调用与匹配处理。`run_phase2` 现在主要负责流程编排、cache hit 恢复、失败队列与 retry 协调。补充 `TestPhase2Helpers` 覆盖 legacy tag 格式归一、缺文件目录创建、仅收集有 abstract 的论文、stale cache 清理和 UTF-8 tag 保存。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestPhase2Helpers tests/test_arxiv_daily.py::TestRunPhase2 tests/test_arxiv_daily.py::TestRunPhase2ErrorModes -q` 通过，18 passed；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q` 通过，123 passed, 1 xfailed。

  ---

  #### [ISSUE ID: 016] Python 版本兼容性未声明（原 TODO#15，Medium）
  - **定位**: 多处（`str | None`, `dict | None`, `tuple[str | None, str]` 等）
  - **问题描述**: 使用了 PEP 604 的 `X | Y` 类型注解语法，仅在 Python ≥ 3.10 可用。当前环境为 Python 3.12，但 repo 中无 `pyproject.toml`、`setup.cfg` 或任何地方声明最低 Python 版本要求。
  - **修复指令**:
    在 `SKILL.md` 或脚本头部 docstring 中明确声明 `Requires: Python >= 3.10`。如需兼容 3.9，改用 `from __future__ import annotations` 或 `Optional[str]` 形式。

  Executor: [RESOLVED] 同意评审意见。仓库根部无 `pyproject.toml`、`setup.cfg` 或根级 `SKILL.md` 可声明运行版本；当前代码已大量使用 PEP 604 和内建泛型注解，强行改写兼容 Python 3.9 会扩大无谓改动面。已在 `arxiv-update/scripts/arxiv_daily.py` 头部 docstring 明确追加 `Requires: Python >= 3.10`，并补充 `TestModuleMetadata.test_python_version_requirement_is_declared` 固定该声明，避免后续误删。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestModuleMetadata -q` 通过，1 passed。

  ---

  #### [ISSUE ID: 017] `RETRY_DELAYS` 与 `MAX_RETRIES` 耦合，修改一方可能越界（原 TODO#2，Low）
  - **定位**: `arxiv_daily.py` (L36-37, L407)
  - **问题描述**: `RETRY_DELAYS = [2, 4, 8]` 长度为 3，`MAX_RETRIES = 3`。当前 `time.sleep(RETRY_DELAYS[attempt])` 仅在 `attempt < MAX_RETRIES - 1` 时执行（即 attempt 0 和 1），安全。但如果未来将 `MAX_RETRIES` 改为 4 而忘记扩展 `RETRY_DELAYS`，`attempt=2` 时会触发 `IndexError`。
  - **修复指令**:
    ```python
    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
    time.sleep(delay)
    ```
    或封装为 `def retry_delay(attempt): return RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]`。

  Executor: [RESOLVED] 同意评审意见。当前 `arxiv-update/scripts/arxiv_daily.py` 已在 Issue 001 修复中引入 `_retry_delay(attempt)`，并且 HTTP 429 fallback、JSON parse retry、网络/IO retry 的 sleep 路径均调用该 helper，不再直接访问 `RETRY_DELAYS[attempt]`。本轮补充 `TestCallLlmErrorModes.test_retry_delay_clamps_to_last_configured_delay`，明确验证 attempt 超过 `RETRY_DELAYS` 长度时复用最后一个延迟值，防止未来调整 `MAX_RETRIES` 时回退。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestCallLlmErrorModes -q` 通过，11 passed。

  ---

  #### [ISSUE ID: 018] 硬编码时区 UTC+8 散落多处（原 TODO#9，Low）
  - **定位**: `arxiv_daily.py` (L742, L866)
  - **问题描述**: `timezone(timedelta(hours=8))` 在 tag 保存和 markdown front matter 中各出现一次，语义相同但未抽取常量。修改时区时需搜索所有实例。
  - **修复指令**:
    ```python
    # 模块顶部
    OUTPUT_TZ = timezone(timedelta(hours=8))
    ```
    替换所有 `timezone(timedelta(hours=8))` 为 `OUTPUT_TZ`。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py`：新增模块级 `OUTPUT_TZ = timezone(timedelta(hours=8))`，并将 `save_tags` 的 `updated_at` 和 `generate_markdown` 的 front matter 时间生成统一改为 `datetime.now(OUTPUT_TZ)`。补充测试断言 tags JSON 使用 `+08:00` ISO offset、Markdown front matter 使用 `+0800` 输出格式，避免后续重新引入散落时区。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestPhase2Helpers::test_save_tags_writes_utf8_tags tests/test_arxiv_daily.py::TestGenerateMarkdown::test_front_matter -q` 通过，2 passed。

  ---

  #### [ISSUE ID: 019] `extract_paper` 缺少关键字段时无警告（原 TODO#11，Low）
  - **定位**: `arxiv_daily.py:extract_paper` (L132-182)
  - **问题描述**: 函数仅保证 `id` 存在即返回 paper dict。`title`、`authors`、`abstract` 均可能缺失。下游通过 `.get()` 做了防御，但如果大量 paper 缺 abstract（表明 HTML 结构已变），不会有任何主动报警——只能通过 normal mode 中 `papers_with_abstract == 0` 的总量检查发现。逐条 paper 级别的诊断信息丢失。
  - **修复指令**:
    在 `parse_html` 中统计缺 title/abstract 的 paper 数量，打印 warning：
    ```python
    missing_abstract = [p["id"] for p in papers if not p.get("abstract")]
    if missing_abstract:
        logger.warning(f"[Phase1] {len(missing_abstract)} papers missing abstract: {missing_abstract[:5]}...")
    ```

  Executor: [DISPUTE] 修正评审意见。直接在 `parse_html` 内调用 logger 会让解析函数产生日志副作用，并扩大调用方耦合；我采用更清晰的替代方案：`parse_html` 负责统计并返回 `diagnostics.missing_title_ids` 与 `diagnostics.missing_abstract_ids`，新增 `log_parse_diagnostics(data, logger)` 在 normal mode 的 Phase1 校验阶段统一输出 warning。已修改 `arxiv-update/scripts/arxiv_daily.py:parse_html`、`log_parse_diagnostics` 和 `run_normal_mode`，对部分缺 title/abstract 的页面记录逐条诊断，同时保留既有“全部无 abstract 则失败”的保护。补充 `TestParseHtml` 覆盖缺字段统计与 `[Phase1]` warning 输出。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestParseHtml -q` 通过，5 passed；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMainCli -q` 通过，7 passed。

  ---

  #### [ISSUE ID: 020] `shutil.rmtree(field_cache)` 静默删除无日志（原 TODO#13，Low）
  - **定位**: `arxiv_daily.py:main` (L1290-1291)
  - **问题描述**: Phase3 成功后 `shutil.rmtree(field_cache)` 直接删除整个缓存目录，无任何日志记录。如果用户需要事后排查问题，无法得知缓存是否/何时被删除。
  - **修复指令**:
    ```python
    logger.info(f"[Cache] Removing completed cache directory: {field_cache}")
    shutil.rmtree(field_cache)
    ```

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py:run_normal_mode` 的成功清理路径，在 `shutil.rmtree(field_cache)` 前记录 `logger.info(f"[Cache] Removing completed cache directory: {field_cache}")`，保留原有“仅 Phase3 全成功后清理 field cache”的行为。补充 `TestMainCli.test_normal_run` 断言日志文件包含被删除的 cache 目录，便于事后排查。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMainCli::test_normal_run -q` 通过，1 passed。

  ---

  #### [ISSUE ID: 021] `main()` 函数过长，patch/normal mode 应拆分（原 TODO#14，Low）
  - **定位**: `arxiv_daily.py:main` (L1016-1312, ~296 lines)
  - **问题描述**: `main()` 同时处理 patch mode 和 normal mode，逻辑交织。函数过长影响可读性和测试覆盖。
  - **修复指令**:
    拆为 `run_patch_mode(args, logger, cache_base)` 和 `run_normal_mode(args, logger, cache_base)`。建议在行为正确性修复全部完成后再执行此重构。

  Executor: [RESOLVED] 同意评审意见。已修改 `arxiv-update/scripts/arxiv_daily.py`：新增 `run_patch_mode(args, logger, cache_base)` 和 `run_normal_mode(args, logger, cache_base)`，分别承接原 patch mode 与 normal mode 流程；`main()` 现在只负责 argparse、环境变量校验、logger 初始化和 mode 分派。保留原有 sys.exit、stdout JSON、stderr 提示和日志行为；未顺手处理 Issue 020 的 cache 删除日志。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMainCli tests/test_arxiv_daily.py::TestGenerateMarkdown tests/test_arxiv_daily.py::TestPatchMarkdown -q` 通过，23 passed；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q` 通过，123 passed, 1 xfailed。

  ---

  #### 综合优先级排序（合并 Round 1 + Round 2 全部 21 个 issue）

  | 优先级 | Issue IDs | 主题 |
  |--------|-----------|------|
  | **P0 行为正确性** | 011, 001, 002, 012, 013 | Schema 校验、错误类型区分、流断检测、Tags 顺序、Hash 覆盖 |
  | **P1 数据完整性** | 008, 014 | 质量检查恢复路径、Patch mode 完整性 |
  | **P2 DRY/可维护性** | 004, 015, 021 | 重试逻辑去重、run_phase2 拆分、main 拆分 |
  | **P3 鲁棒性** | 005, 006, 007, 009, 017 | 原子写入、fd 泄漏、TOCTOU、分隔符冲突、越界防御 |
  | **P4 规范/低风险** | 003, 016, 018, 019, 020, 010 | 爬虫礼仪、版本声明、时区常量、缺字段警告、日志、patch 性能 |

## 4. 最终决策 (Final Decision)
（等待 Executor 回复后在后续轮次中给出）
