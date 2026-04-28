# TODO 执行者审查意见

基于对 `arxiv_daily.py` 源码的完整阅读，对 `TODO.md` 中各项修改建议的逐项审查。

## P0：修正缓存测试的断言强度 — 合理，无需修正

缓存目录结构已确认：`run_phase2` 将缓存写在 `cache_dir / data["field"]` 下，文件为 `metadata_hash`（纯文本，SHA-256）和 `batch_NNN.json`（`arxiv_daily.py:595-612, 659-667`）。

注意 `main()` 在全部成功后会 `shutil.rmtree(field_cache)`（`:1291`），但测试直接调用 `run_phase2`，不经过 `main()`，所以缓存目录在测试中始终存在，断言成立。

样例数据中 4 篇论文都有 abstract，`expected_ids` 应为 4 个 ID。fake server 默认 response 的 tags 非空、summary 长度超过 50 字符，质量检查不会失败，batch 会成功写入缓存。

TODO 中的修改建议可直接采纳。

## P0：让 tags 更新测试真正验证新增标签 — 方案 A 更合理，不推荐方案 B

源码中 `new_tags` 处理在 `run_phase2:640-643`：

```python
new_tags = result.get("new_tags", [])
for t in new_tags:
    if t not in current_tags:
        current_tags.append(t)
```

fake server 的 `generate_normal_response` 返回 `"new_tags": []`（`fake_llm_server.py:46`），所以当前测试永远不会触发 new_tags 逻辑。

**方案 B（mock `call_llm`）不推荐**：会绕过 fake server → streaming → JSON 解析 → `run_phase2` 的集成路径，变成纯单元测试，失去集成测试的意义。

**推荐方案 A 的改良版**：

- 修改 `generate_normal_response`，让默认 response 返回有意义的 `new_tags`，例如 `"new_tags": ["AI Evaluation"]`。
- paper 级别的 `tags` 字段不需要改。paper tags 可以包含不在当前标签列表中的标签——只有 `new_tags` 才触发标签列表更新，这两者是独立的。
- 测试中断言 tags_file 中出现了 `"AI Evaluation"`，且 `"LLM Reasoning"` 不重复。

补充说明：`conftest.py` 中 `sample_llm_result` fixture 的 `new_tags` 是 `["AI Evaluation", "Vision Transformer", "Attention Mechanism"]`，但该 fixture 目前没被任何测试引用。后续如需确定性结果的单元测试可考虑使用它。

## P1：改造 fake LLM server fixture，避免端口竞态和资源残留 — 合理，无需修正

源码确认 `conftest.py:284-289` 存在 TOCTOU 竞态。改为 `HTTPServer(("127.0.0.1", 0), ...)` 是标准做法。teardown 中确实缺少 `server.server_close()` 和 `thread.join()`（`:293`）。

`import socket`（`conftest.py:4`）修复后将无其他引用，应一并删除。

TODO 中的修改建议可直接采纳。

## P1：减少 FakeLLMHandler 的跨测试共享状态风险 — 方向合理，但建议降为 P2

当前 pytest 默认串行执行，fixture 在每次使用前已 reset 所有 class-level 状态，实际不会出现跨测试污染。动态子类方案的收益仅在 pytest-xdist 并行场景下体现，当前无此需求。

**如果实施，有一个必须同步修改的点**：`_resolve_error_mode`（`fake_llm_server.py:420-446`）中硬编码了 `FakeLLMHandler.counter_lock`、`FakeLLMHandler.error_sequence` 等引用。如果用动态子类但不改这些引用，子类仍读写父类状态，改了等于没改。必须改为 `cls = type(self)` 后通过 `cls.xxx` 访问。

建议降为 P2，与端口竞态修复分开做，避免一次改动范围过大。

## P1：给异常流模式补测试 — 合理，需根据源码细化预期行为

`call_llm`（`arxiv_daily.py:321-408`）的错误处理逻辑：

- 任何 `Exception`（含 `HTTPError`、`json.JSONDecodeError`）都会重试，共 `MAX_RETRIES=3` 次。
- 3 次都失败后 `raise last_error`。
- SSE streaming 中 `json.loads(data_str)`（`:347`）如果某行是非法 JSON **会直接抛异常**，不会跳过坏行。

基于此，各 error mode 的预期行为：

| error mode | `call_llm` 行为 | `run_phase2` 行为 |
|---|---|---|
| `http_429` | 得到 HTTPError(429)，触发重试 | 取决于重试是否最终成功 |
| `invalid_json` | 拼接内容为非法 JSON → `json.JSONDecodeError` → 重试 | 3次+retry 全失败 → batch 失败 |
| `partial_stream` | 拼接内容不完整 → JSON 解析失败 → 重试 | 同上 |
| `connection_reset` | 连接中断 → 异常 → 重试 | 同上 |
| `malformed_sse` | 坏行触发 `json.loads` 异常 → **整个请求失败** → 重试 | 同上 |
| `wrong_ids` | call 成功，`match_llm_results` 阶段全部 unmatched | failed > 0 |
| `empty_papers` | call 成功，match 阶段全部 unmatched | failed > 0 |
| `missing_fields` | call 成功，quality check（tags 为空或 summary 过短）失败 | failed > 0 |
| `versioned_ids` | call 成功，normalized match（`:452-469`）生效 | 应该成功，failed == 0 |

**重要修正**：TODO 中提到 `malformed_sse` 可能测试"跳过坏行后仍成功"，但当前实现不会跳过坏行——`json.loads` 直接抛异常导致整个请求失败。测试应断言 `call_llm` 抛异常或 batch 失败。

## P1：避免超长 sleep 模式污染自动测试 — 合理，无需修正

确认 `timeout` 模式 `time.sleep(600)`（`fake_llm_server.py:133`），`slow_stream` 每 chunk `time.sleep(2)`（`:289`）。给 `FakeLLMHandler` 增加 `timeout_sleep` 和 `stream_delay` class-level 属性，handler 中通过 `type(self).xxx` 读取，fixture 中设置为极小值。

TODO 中的修改建议可直接采纳。

## P2：加强 CLI 测试的行为断言 — 合理，细节已确认

`main()` 确认向 stdout 输出 JSON summary（`arxiv_daily.py:1306`），结构为：

```json
{
  "listing_date": "...",
  "field": "...",
  "output_file": "...",
  "sections": {"New submissions": N, "Cross submissions": N, ...},
  "total": N,
  "failed_batches": 0
}
```

`main()` 通过 `sys.exit()` 退出，测试中用 `pytest.raises(SystemExit)` 捕获。`capsys` 能在 `sys.exit` 前捕获到已 print 的 stdout 内容。

需新增的 CLI 测试及对应源码位置：

- `--date` 对应 HTML 不存在 → exit code 2（`:1143-1146`）
- 页面日期与 `--date` 不一致 → exit code 2（`:1208-1217`）
- `--patch` 缺少 `--date` → exit code 2（`:1055-1057`）

TODO 中的修改建议可直接采纳。

## P2：加强 Markdown patch 测试 — 合理，无需修正

`patch_markdown`（`arxiv_daily.py:953-1009`）的关键行为已确认：

1. **Venue 为 None 时 Venue 行被移除**：patch 用新 block 完整替换旧 block（`:1003-1004`），新 block 中 `if venue:`（`:998`）为 False 则不写 Venue 行。原有 Venue 行会消失。
2. **Code 行被保留**：从 existing 中提取 `Authors`、`Subjects`、`Code`（`:985-987`），重建时保留（`:999-1000`）。
3. **多篇论文 patch**：逐篇在最新 content 上做正则 search + 替换（`:1003-1004`），第一篇替换后 content 长度变化不影响第二篇的匹配（每次都是全文 search）。
4. **cross-listed/replaced 后缀匹配**：arXiv 行格式如 `- **arXiv**: [2604.08123](...) (cross-listed)`，正则中 `\[{pid}\].*\n`（`:970`）的 `.*` 会匹配后缀，能正确定位。

TODO 中的修改建议可直接采纳。

## P2：加强 HTML 解析的真实页面兼容性测试 — 合理，属长尾工作

确认正则对 class 顺序敏感。例如 `extract_paper` 中：

- `list-title mathjax`（`arxiv_daily.py:142`）
- `list-comments mathjax`（`:164`）

如果真实页面的 class 属性为 `mathjax list-title`（class 顺序不同），正则会匹配失败。建议增加 class 顺序变化的 fixture 用例。

但此项改动需要收集真实 arXiv HTML 样例，工作量较大，优先级 P2 合理。

## P2：去掉无用导入和过期测试计划内容 — 合理，无需修正

确认 `test_arxiv_daily.py` 中 `import logging`（:3）和 `import re`（:4）未被任何测试使用。

TODO 中的修改建议可直接采纳。

## 总结

| 项目 | TODO 建议 | 审查结论 | 调整 |
|---|---|---|---|
| P0 缓存断言 | 加强断言 | 合理 | 无 |
| P0 tags 更新 | 方案 A 或 B | **选方案 A**，不要 mock `call_llm` | 改 fake server 的 `generate_normal_response` |
| P1 端口竞态 | 改为 port 0 | 合理 | 删除 `import socket` |
| P1 handler 隔离 | 动态子类 | 合理但收益低 | **降为 P2**，必须同步改 `_resolve_error_mode` |
| P1 补异常流测试 | 覆盖各 error mode | 合理 | `malformed_sse` 预期应为**失败**而非成功 |
| P1 可配置延迟 | 加配置属性 | 合理 | 无 |
| P2 CLI 测试 | 加 stdout JSON 断言 | 合理 | 无 |
| P2 patch 测试 | 加 edge case | 合理 | 无 |
| P2 HTML 兼容性 | 加真实 HTML fixture | 合理，属长尾 | 无 |
| P2 清理导入 | 删未使用导入 | 合理 | 无 |

建议实施顺序：P0 两项 → P1 端口竞态 + 补异常流测试 + 可配置延迟 → P2 各项。
