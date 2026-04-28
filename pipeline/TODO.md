# Fake LLM API Server 实现计划

为 `arxiv-update/arxiv_daily.py` 提供一个可控的 fake LLM API，用于模拟各种错误场景以测试脚本的健壮性。

## 1. 整体架构

使用 Python 标准库 `http.server` 实现轻量级 HTTP 服务器，无需额外依赖。提供单个文件 `arxiv-update/tests/fake_llm_server.py`，可通过命令行参数或请求头控制错误模拟行为。

## 2. 核心功能（正常模式）

- 解析收到的 `messages`，从 prompt 中提取论文 ID 列表
- 自动生成符合 `arxiv_daily.py` 期望格式的 JSON 响应（包含 `papers` 和 `new_tags`）
- 以 SSE 流式格式返回，模拟真实的 chunk-by-chunk 输出
- 在最终 chunk 中返回伪造的 `usage` 统计

## 3. 可模拟的错误场景

通过 **请求头** `X-Fake-Error` 或 **启动参数** `--error-mode` 控制：

| 错误模式 | 说明 | 测试目标 |
|---|---|---|
| `none` | 正常响应 | 基线验证 |
| `http_500` | 返回 500 Internal Server Error | 重试机制（HTTP 错误） |
| `http_429` | 返回 429 Too Many Requests + Retry-After | 速率限制处理 |
| `timeout` | 接受连接但不响应（挂起） | 超时处理（300s） |
| `connection_reset` | 流式传输中途断开连接 | 流中断恢复 |
| `invalid_json` | 返回格式错误的 JSON 内容 | JSON 解析 + `json_repair` 回退 |
| `partial_stream` | SSE 流只发送部分 chunk 后停止 | 不完整响应处理 |
| `wrong_ids` | 返回的论文 ID 与请求不匹配（幻觉 ID） | `match_llm_results` 的 ID 匹配逻辑（`unmatched_batch_ids` 非空） |
| `duplicate_ids` | 返回的论文列表中包含重复 ID 条目 | `match_llm_results` 对重复 ID 的处理（`used_llm_indices` 去重逻辑） |
| `missing_fields` | 返回的论文缺少 `tags` 或 `summary` 过短 | 质量检查逻辑（Pass 3，`MIN_SUMMARY_LEN` 校验） |
| `no_period` | 返回的 summary 末尾没有句号（截断或缺少标点） | 摘要格式/完整性校验 |
| `empty_papers` | 返回 `{"papers": []}` | 空结果处理 |
| `markdown_json` | JSON 外层包裹 ` ``` ` 代码块 | markdown fence 剥离逻辑 |
| `slow_stream` | 每个 chunk 间延迟较长 | 长时间流式传输稳定性 |
| `versioned_ids` | 返回的论文 ID 带版本后缀（如 `2501.12345v2`） | `match_llm_results` Pass 2 归一化匹配逻辑 |
| `malformed_sse` | SSE 流中某行 `data:` 后的内容不是合法 JSON | 流式解析中单 chunk 的 `json.loads` 失败，触发重试 |

## 4. 控制机制

### 4.1 全局模式（启动参数）

- `--error-mode <mode>`：设置全局默认错误模式

### 4.2 请求级控制（请求头）

- `X-Fake-Error` 请求头：指定当前请求的错误模式，优先级高于全局模式

### 4.3 序列模式

- `--error-sequence <mode1,mode2,...>`：按请求顺序依次应用不同错误
- 示例：`--error-sequence 500,500,none` 模拟"前两次失败第三次成功"，直接测试重试逻辑

### 4.4 概率模式

- `--error-rate <float>`：以指定概率随机触发 `--error-mode` 指定的错误，其余请求正常响应

## 5. 日志

打印每个请求的方法、路径、选用的错误模式、响应状态码。

## 6. 使用方式

```bash
# 启动正常模式
python fake_llm_server.py --port 8080

# 全局错误模式
python fake_llm_server.py --port 8080 --error-mode http_500

# 序列模式：前2次返回500，第3次正常
python fake_llm_server.py --port 8080 --error-mode http_500 --error-sequence 500,500,none

# 概率模式：30%概率返回错误
python fake_llm_server.py --port 8080 --error-mode invalid_json --error-rate 0.3

# 配合 arxiv_daily.py 使用
export ARXIV_UPDATE_API=http://localhost:8080/v1
export ARXIV_UPDATE_KEY=fake-key
export ARXIV_UPDATE_MODEL=fake-model
python arxiv_daily.py cs.AI --output ./out --tags ./tags.json --date 2026-04-22
```

## 7. 文件结构

- `arxiv-update/tests/fake_llm_server.py` — 单文件，仅依赖 Python 标准库
