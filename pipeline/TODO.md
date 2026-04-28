# `arxiv_daily.py` 测试改进 TODO

当前测试套件已经建立，下一步重点不是继续堆 happy path，而是提升测试的约束力、隔离性和 CI 稳定性。

## P0：修正缓存测试的断言强度

涉及文件：

- `tests/test_arxiv_daily.py`

问题：

- `TestRunPhase2.test_caching` 目前只断言第二次运行没有新增 LLM 请求。
- 如果缓存文件存在但内容为空、ID 不匹配、质量检查失败，测试仍可能通过。
- 这会掩盖 `run_phase2` 从缓存恢复结果时的真实行为回归。

修改建议：

- 第一次运行保存 `first_results, first_failed`。
- 第二次运行保存 `second_results, second_failed`。
- 断言 `first_failed == 0`、`second_failed == 0`。
- 断言 `second_results == first_results`。
- 断言结果覆盖样例数据中所有有 abstract 的论文 ID。
- 可额外断言缓存目录中生成了 `metadata_hash` 和对应 `batch_*.json`。

验收标准：

- 如果缓存 batch JSON 被写成空 papers、错误 ID 或缺少 tags，测试应失败。

## P0：让 tags 更新测试真正验证新增标签

涉及文件：

- `tests/test_arxiv_daily.py`
- `tests/fake_llm_server.py`

问题：

- `TestRunPhase2.test_tags_updated` 只检查 tags 文件包含 `"tags"` 和 `"updated_at"` 两个字段。
- 当前 fake server 正常响应会返回 `tags: ["LLM Reasoning", "AI Safety"]`，但 `new_tags` 为空。
- 初始 `tags_file` 没有 `"AI Safety"`，这意味着 fake response 和 prompt 协议不一致，而测试没有捕获。

修改建议：

- 优先保持集成路径，不要在该测试中 mock `call_llm`。
- 修改 `generate_normal_response`，让 fake server 的默认 response 返回有意义的 `new_tags`，例如 `["AI Evaluation"]`。
- paper 级别的 `tags` 可以保持当前值；`run_phase2` 只根据顶层 `new_tags` 更新 tags 文件。
- 测试应断言运行后 tags 文件中包含 `"AI Evaluation"`，并且已有标签如 `"LLM Reasoning"` 不会重复写入。
- 如后续需要纯单元测试，可单独使用 `sample_llm_result` fixture 或 mock `call_llm`，不要替代当前 fake server 集成测试。

验收标准：

- 如果 `run_phase2` 忽略 `new_tags`，测试应失败。
- 如果 fake server 返回了未声明在 `new_tags` 中的新标签，相关测试应能暴露协议不一致。

## P1：改造 fake LLM server fixture，避免端口竞态和资源残留

涉及文件：

- `tests/conftest.py`

问题：

- fixture 当前先手动创建 socket 找空闲端口，再关闭 socket，再用该端口创建 `HTTPServer`。
- 这是 TOCTOU 竞态：端口可能在两个操作之间被其他进程占用。
- teardown 只调用 `server.shutdown()`，没有 `server.server_close()` 和 `thread.join()`。
- 在并行测试、频繁重跑或 CI 环境中可能出现偶发失败或资源残留。

修改建议：

- 直接创建 `HTTPServer(("127.0.0.1", 0), FakeLLMHandler)`，让系统分配端口。
- 通过 `server.server_address[1]` 读取实际端口。
- teardown 中依次调用 `server.shutdown()`、`server.server_close()`、`thread.join(timeout=...)`。
- 修复后 `conftest.py` 不再需要 `import socket`，应一并删除。

验收标准：

- fixture 不再需要 `socket.socket(...).bind(("127.0.0.1", 0))` 手动探测端口。
- 多次连续运行测试不会残留监听线程或占用端口。

## P2：减少 FakeLLMHandler 的跨测试共享状态风险

涉及文件：

- `tests/conftest.py`
- `tests/fake_llm_server.py`

问题：

- `FakeLLMHandler` 使用 class-level 状态：`error_mode`、`error_sequence`、`error_rate`、`request_counter`。
- fixture 启动前会重置这些状态，但测试拿到的是 handler class，本质上仍是全局可变状态。
- 如果未来启用 pytest 并行、复用 server fixture 或增加后台请求，测试之间可能互相污染。

修改建议：

- 每个 fixture 实例动态创建一个 handler subclass，把状态绑定到该 subclass。
- 或者将 server config 放到 `handler.server` 上，由 handler 通过 `self.server.config` 读取。
- 保留测试内设置 error mode 的便利接口，但避免共享 `FakeLLMHandler` 本体状态。
- 如果采用动态子类方案，必须同步修改 `_resolve_error_mode` 中对 `FakeLLMHandler.xxx` 的硬编码引用；应使用 `cls = type(self)` 后通过 `cls.error_mode`、`cls.error_sequence`、`cls.request_counter`、`cls.counter_lock` 访问，否则子类仍会读写父类状态。

优先级说明：

- 当前 pytest 默认串行执行，fixture 每次启动前也会重置 class-level 状态，实际风险低于端口竞态。
- 该项不应和 P1 端口修复混在同一次改动中做，避免扩大变更范围。

验收标准：

- 两个 fake server 实例可以同时存在，并使用不同的 error mode 和 request counter。

## P1：给异常流模式补测试，避免 fake server 代码闲置失效

涉及文件：

- `tests/test_arxiv_daily.py`
- `tests/fake_llm_server.py`

当前 fake server 支持但测试未覆盖的模式包括：

- `http_429`
- `invalid_json`
- `partial_stream`
- `connection_reset`
- `wrong_ids`
- `duplicate_ids`
- `missing_fields`
- `empty_papers`
- `versioned_ids`
- `malformed_sse`

修改建议：

- 为 `call_llm` 增加 `invalid_json`、`malformed_sse`、`partial_stream` 的失败或重试测试。
- 明确当前实现不会跳过 malformed SSE data line；`json.loads(data_str)` 会直接抛异常，所以 `malformed_sse` 的预期应是请求失败并触发重试，而不是最终成功。
- 为 `run_phase2` 增加 `wrong_ids`、`missing_fields`、`empty_papers` 的失败批次测试。
- 为 `run_phase2` 增加 `versioned_ids` 的成功匹配测试。
- 为 `http_429` 增加重试测试，必要时检查 `Retry-After` 是否应被尊重；如果当前实现不打算尊重，也应明确测试现有退避策略。

验收标准：

- fake server 中注册的主要 error mode 至少被一个测试用例覆盖，或者明确标注为手动调试工具而非自动测试路径。

## P1：避免超长 sleep 模式污染自动测试

涉及文件：

- `tests/fake_llm_server.py`

问题：

- `timeout` 模式会 `time.sleep(600)`。
- `slow_stream` 每个 chunk 固定 `sleep(2)`。
- 当前自动测试没有覆盖这些模式；但未来一旦直接使用，会造成测试套件卡住数分钟。

修改建议：

- 给 fake server 增加可配置延迟，例如 `timeout_sleep`、`stream_delay`。
- 延迟配置应通过 handler class/subclass 读取；如果保留 class-level 状态，建议在 handler 中使用 `type(self).timeout_sleep` / `type(self).stream_delay`，便于未来做动态子类隔离。
- 在 fixture 中把这些延迟设为很小的值。
- 或将 `timeout` / `slow_stream` 标注为手动调试模式，避免自动测试误用。

验收标准：

- 新增 timeout/slow stream 测试时，单个测试耗时应在秒级以内。

## P2：加强 CLI 测试的行为断言

涉及文件：

- `tests/test_arxiv_daily.py`

问题：

- `TestMainCli.test_normal_run` 只断言退出码为 0，并且生成了一个 markdown 文件。
- 没有检查 stdout JSON 摘要、输出文件路径、sections、total、failed_batches。
- 没有覆盖 date mismatch、缺失 saved HTML、patch 模式等 CLI 分支。

修改建议：

- 使用 `capsys` 捕获 stdout/stderr。
- 解析 stdout 的 JSON summary。
- 断言 `listing_date`、`field`、`output_file`、`sections`、`total`、`failed_batches`。
- 增加 `--date` 对应 HTML 不存在时退出码 2 的测试。
- 增加页面日期与 `--date` 不一致时退出码 2 的测试。
- 增加 `--patch` 缺少 `--date` 时退出码 2 的测试。

验收标准：

- CLI 输出契约变化时，测试能直接失败，而不是只靠文件存在判断。

## P2：加强 Markdown patch 测试

涉及文件：

- `tests/test_arxiv_daily.py`

问题：

- `TestPatchMarkdown` 目前覆盖了基本 patch、保留字段和论文不存在。
- 未覆盖已有 Venue 被移除、Code 行保留、多篇论文 patch、cross-listed/replaced arXiv 行、summary 中包含特殊字符等情况。

修改建议：

- 增加已有 Venue 但新结果 `venue is None` 时应移除 Venue 的测试。
- 增加包含 `Code` 行时 patch 后仍保留 Code 的测试。
- 增加一次 patch 多篇论文的测试。
- 增加 arXiv 行带 `(cross-listed)` 或 `(replaced)` 后缀时仍能匹配的测试。

验收标准：

- patch 逻辑回归时，不会只因为最小 markdown 样例过于简单而漏测。

## P2：加强 HTML 解析的真实页面兼容性测试

涉及文件：

- `tests/conftest.py`
- `tests/test_arxiv_daily.py`

问题：

- 当前 sample HTML 是手写最小结构，和真实 arXiv 页面相似但不完全等价。
- `extract_paper` / `parse_sections` 主要依赖正则，容易被真实页面的 class 顺序、换行、额外属性影响。

修改建议：

- 增加一个更接近真实 arXiv HTML 的 fixture 文件或较完整内联样例。
- 覆盖 `list-title mathjax` class 顺序变化、额外 class、subject 中含多个代码、comments 缺失、authors 文本异常等情况。
- 对解析结果做完整结构断言，而不只断言几个字段包含关键字。

验收标准：

- 轻微 HTML 结构变化导致解析失败时，测试能暴露问题。

## P2：去掉无用导入和过期测试计划内容

涉及文件：

- `tests/test_arxiv_daily.py`

问题：

- `logging`、`re` 等导入当前没有被使用。
- 测试文件中存在一些偏弱断言和过时注释，例如“mock fetch_page”但实际 main 正常用 saved HTML。

修改建议：

- 清理未使用导入。
- 修正误导性注释。
- 将测试命名从“做了什么”调整为“保证什么行为”。

验收标准：

- 测试文件读起来只描述当前真实行为，不包含历史实现计划残留。

## 验证命令

优先使用项目内虚拟环境运行：

```bash
arxiv-update/scripts/.venv/bin/python -m pytest arxiv-update/scripts/tests
```

注意：

- 默认 sandbox 可能禁止创建本地 socket，导致 fake LLM server fixture 报 `PermissionError: [Errno 1] Operation not permitted`。
- 如果出现该错误，需要在允许本地 `127.0.0.1` socket 的环境中运行。
