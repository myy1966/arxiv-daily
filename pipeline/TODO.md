# `arxiv_daily.py` 测试套件计划

## 背景

`arxiv-update/scripts/arxiv_daily.py`（1309 行）是 arxiv-update 技能的核心脚本，负责下载 arXiv 每日论文列表、调用 LLM 生成摘要、输出 Markdown 文件。目前该脚本没有任何测试覆盖。项目中已有 `tests/fake_llm_server.py` 作为模拟 LLM 服务器，但缺少正式的 pytest 测试套件。本计划旨在创建完整的测试覆盖。

## 文件结构

```
arxiv-update/scripts/
├── pytest.ini                          (新建)
└── tests/
    ├── fake_llm_server.py              (已有)
    ├── conftest.py                     (新建)
    └── test_arxiv_daily.py             (新建)
```

## 需要创建的文件

### 1. `arxiv-update/scripts/pytest.ini`

pytest 配置文件，设置测试路径和 Python 模块搜索路径。

```ini
[pytest]
testpaths = tests
pythonpath = .
```

### 2. `arxiv-update/scripts/tests/conftest.py`

共享 fixtures：

| Fixture | 说明 |
|---------|------|
| `sample_paper_chunk` | 单篇论文的最小 HTML 片段 |
| `sample_listing_html` | 完整的 arXiv 列表页面（内联字符串，含 2 篇 new + 1 篇 cross） |
| `sample_paper_dict` | `extract_paper` 返回的已解析论文字典 |
| `sample_data` | Phase 1 输出字典，包含 sections 和 papers |
| `sample_llm_result` | LLM 响应字典，含 papers、summaries、tags |
| `fake_llm_server` | 在随机端口启动 fake 服务器，返回 `(api_url, handler_class)`，测试结束后关闭 |
| `logger` | 写入 `tmp_path` 的测试日志器 |
| `tags_file` | 临时 `tags.json`，含初始标签列表 |

导入策略：通过 `sys.path.insert(0, ...)` 导入 `arxiv_daily` 和 `fake_llm_server`。

### 3. `arxiv-update/scripts/tests/test_arxiv_daily.py`

按功能分组的测试类：

| 测试类 | 测试目标 | 类型 | 关键用例 |
|--------|----------|------|----------|
| `TestCleanHtml` | `clean_html` | 单元测试 | 去除标签、规范化空白、反转义 HTML 实体、空输入 |
| `TestParseListingDate` | `parse_listing_date` | 单元测试 | "new" 格式、"catchup" 格式、无日期、空 HTML |
| `TestParseSections` | `parse_sections` | 单元测试 | 3 个 section、单个 section、无 section、计数提取 |
| `TestExtractCodeUrls` | `extract_code_urls` | 单元测试 | github/gitlab URL、去重、无 URL、排除非代码 URL |
| `TestExtractPaper` | `extract_paper` | 单元测试 | 完整论文、最小论文、无 ID 返回 None、评论/摘要中的代码链接 |
| `TestParseHtml` | `parse_html` | 单元测试 | 完整页面解析、空页面、正确的 section 分配 |
| `TestBuildPrompt` | `build_prompt` | 单元测试 | 消息结构、论文 ID 出现在内容中、空标签 |
| `TestMatchLlmResults` | `match_llm_results` | 单元测试 | 精确匹配、版本号 ID 归一化、质量检查（摘要过短、缺少标签）、错误 ID、重复 |
| `TestComputeMetadataHash` | `compute_metadata_hash` | 单元测试 | 确定性、顺序无关、十六进制格式 |
| `TestExtractPaperAbbrev` | `extract_paper_abbrev` | 单元测试 | 冒号/破折号/括号模式、无缩写、小写开头被拒绝 |
| `TestFetchPage` | `fetch_page` | 单元测试（mock） | URL 构造正确、成功响应、网络错误传播 |
| `TestCallLlm` | `call_llm` | 集成测试 | 正常响应、500 重试、重试全部失败、markdown_json 去除、debug 文件输出 |
| `TestRunPhase2` | `run_phase2` | 集成测试 | 正常运行、缓存命中、no_cache 标志、标签更新、失败批次 |
| `TestGenerateMarkdown` | `generate_markdown` | 集成测试 | 文件创建、front matter、section 标题、论文条目、录用/开源论文 |
| `TestPatchMarkdown` | `patch_markdown` | 集成测试 | 基本补丁、论文不存在、保留其他字段 |
| `TestMainCli` | `main` | 集成测试 | 缺少环境变量退出、正常运行（mock fetch_page） |

## 关键实现细节

1. **Fake 服务器 fixture**：在守护线程中用 `HTTPServer` + `FakeLLMHandler` 启动服务器，绑定随机可用端口。返回 `(base_url, handler_class)` 以便测试设置 `handler_class.error_mode` / `error_sequence`。

2. **测试速度**：所有集成测试中通过 monkey-patch 将 `arxiv_daily.RETRY_DELAYS` 设为 `[0.01, 0.01, 0.01]`，避免实际等待。

3. **fetch_page 模拟**：使用 `unittest.mock.patch("arxiv_daily.urllib.request.urlopen")` 模拟网络请求。

4. **main() 测试**：使用 `monkeypatch` 设置 `sys.argv` 和环境变量，mock `fetch_page`，直接调用 `main()`（非子进程），保持测试快速。

5. **Sample HTML**：在 conftest.py 中定义内联常量，匹配真实 arXiv 页面结构（与 `parse_listing_date`、`parse_sections`、`extract_paper` 中的正则兼容）。

## 涉及的关键文件

- `arxiv-update/scripts/arxiv_daily.py` — 被测源码
- `arxiv-update/scripts/tests/fake_llm_server.py` — 已有的模拟服务器，将被复用

## 验证方式

```bash
cd arxiv-update/scripts && python -m pytest tests/ -v
```

预期结果：所有测试通过，覆盖纯函数 + LLM 集成 + 文件 I/O + CLI 入口。
