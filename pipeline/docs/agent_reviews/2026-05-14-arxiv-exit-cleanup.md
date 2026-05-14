---
title: "arxiv_daily.py sys.exit 散落重构"
date: "2026-05-14"
status: "PASS"
---

## 1. 元数据 (Metadata)

- **原始需求**: 消除 `arxiv_daily.py` 中散落的 `sys.exit()` 调用，统一为异常驱动的单出口模式
- **技术翻译**: 将 18 处分散的 `sys.exit()` 替换为自定义异常体系，函数通过 raise/return 表达成败，仅在 `main()` 入口处统一转换为进程退出码。目标：提升可测试性、可复用性，消除隐式控制流跳转。
- **涉及文件**: `arxiv-update/scripts/arxiv_daily.py`

## 2. 设计概要 (Design Overview)

### 数据流向 (Data Flow)

```
main()
  ├── validate_env()          → raise ConfigError
  ├── run_patch_mode(...)     → raise PipelineError / return PatchResult
  └── run_normal_mode(...)    → raise PipelineError / return NormalResult
                                    ↓
main() catches exceptions → print error → return exit_code
sys.exit(main())  ← 唯一出口
```

### 核心接口定义 (API Contracts)

```python
class ArxivDailyError(Exception):
    """Base exception for arxiv_daily pipeline."""
    exit_code: int = 1

class ConfigError(ArxivDailyError):
    """Missing env vars or invalid CLI args."""
    exit_code: int = 2

class FetchError(ArxivDailyError):
    """Network / HTML fetch failure."""
    exit_code: int = 2

class ParseError(ArxivDailyError):
    """HTML parsing yielded no usable data."""
    exit_code: int = 2

class PipelineError(ArxivDailyError):
    """LLM batch failures or markdown generation blocked."""
    exit_code: int = 1

@dataclass
class RunResult:
    summary: dict          # JSON-serializable summary for stdout
    exit_code: int = 0     # 0=success, 1=partial failure

def main() -> int:
    """Single exit point. Returns process exit code."""
```

### 风险预警 (Risk Assessment)

> **本项目无 AGENTS.md 规范文件，以下设计基于通用最佳实践。**

| 风险 | 应对 |
|------|------|
| 重构后遗漏某个 exit 路径导致静默继续执行 | TASK 002 完成后执行 `grep -n "sys.exit" arxiv_daily.py` 确认为零 |
| 异常信息格式变化影响上游 SKILL.md 中的 JSON 解析 | 保持 stdout 输出 JSON 不变，stderr 错误信息格式不变 |
| `run_normal_mode` 成功路径末尾原有 `sys.exit(0)` 后的代码不会执行 — 移除后需确认无遗漏逻辑 | 当前 `sys.exit(0)` 已是函数末行，安全移除 |

## 3. 交互历史 (The Thread)

### [Round 1]

*Architect 寄语：以下任务已就绪，请 Executor 严格按照顺序和规范执行。*

#### [TASK ID: 001] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` 顶部（`import` 区域之后、常量定义之前）
- **目标描述**: 定义自定义异常层级和 `RunResult` 数据类
- **前置依赖**: 无
- **执行指令与限制**:
  1. 在 `BATCH_SIZE = 16` 行之前插入异常类定义：
     - `ArxivDailyError(Exception)` — 基类，含 `exit_code: int = 1` 类属性
     - `ConfigError(ArxivDailyError)` — `exit_code = 2`
     - `FetchError(ArxivDailyError)` — `exit_code = 2`
     - `ParseError(ArxivDailyError)` — `exit_code = 2`
     - `PipelineError(ArxivDailyError)` — `exit_code = 1`
  2. 新增 `RunResult` dataclass（字段：`summary: dict`, `exit_code: int = 0`）
  3. 不要修改任何现有代码，仅新增定义

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已在 `arxiv-update/scripts/arxiv_daily.py` 的 `BATCH_SIZE` 常量之前新增 `ArxivDailyError`、`ConfigError`、`FetchError`、`ParseError`、`PipelineError` 异常层级，以及 `RunResult(summary: dict, exit_code: int = 0)` dataclass；未修改任何现有逻辑。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过。

#### [TASK ID: 002] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `run_patch_mode` 函数
- **目标描述**: 将 `run_patch_mode` 中的 4 处 `sys.exit()` 替换为 raise 或 return
- **前置依赖**: TASK 001
- **执行指令与限制**:
  1. 函数签名改为 `-> RunResult`
  2. 将 `sys.exit(2)` 替换为 `raise ConfigError("...")` 或 `raise ParseError("...")`（根据语义）
  3. 将 `sys.exit(1)` 替换为 `raise PipelineError("...")`
  4. 末尾 `sys.exit(0 if total_failed == 0 else 1)` 改为 `return RunResult(summary, exit_code=0 if total_failed == 0 else 1)`
  5. 移除函数内所有 `print(..., file=sys.stderr)` 中的 `f"Log: {log_path}"` 行（日志路径由 main 统一输出）
  6. **必须保留** stderr 的用户可见错误/警告信息（只是不再自行 exit）

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已修改 `arxiv-update/scripts/arxiv_daily.py:run_patch_mode`：函数签名改为 `-> RunResult`；函数内全部 `sys.exit()` 均替换为 `ConfigError`、`ParseError`、`PipelineError` 或最终 `RunResult(summary, exit_code=...)` 返回；保留现有 stderr 用户可见错误/警告信息。实现中实际发现 6 处 `sys.exit()`，多于任务描述的 4 处，已按任务目标语义全部清除。未移除 stdout JSON 输出，因为统一 stdout 输出迁移属于 TASK 004 范围。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`python3 - <<'PY' ... PY` 直接验证 patch mode 缺少 `--date` 时抛出 `ConfigError` 且保留 stderr、成功路径返回 `RunResult` 通过。

#### [TASK ID: 003] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `run_normal_mode` 函数
- **目标描述**: 将 `run_normal_mode` 中的 ~12 处 `sys.exit()` 替换为 raise 或 return
- **前置依赖**: TASK 001
- **执行指令与限制**:
  1. 函数签名改为 `-> RunResult`
  2. 逐一替换：
     - 环境/输入校验失败 → `raise ConfigError(...)`
     - fetch 失败 → `raise FetchError(...)`
     - parse 失败（无 section / 无 paper / 无 abstract / 日期不匹配 / 格式不可识别）→ `raise ParseError(...)`
     - LLM batch 失败 → `raise PipelineError(...)`
     - 成功路径（含 skipped）→ `return RunResult(summary)`
  3. 对于 "skipped" 路径（output 已存在），保持 `exit_code=0`
  4. **必须保留** stderr 的用户可见状态信息（进度、警告）
  5. 移除函数内对 `log_path` 的引用（改由 main 统一处理）

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已修改 `arxiv-update/scripts/arxiv_daily.py:run_normal_mode`：函数签名改为 `-> RunResult`；保存 HTML 缺失、解析失败、日期不匹配、格式不可识别等路径改为 `ParseError`；抓取失败改为 `FetchError`；日期参数格式错误改为 `ConfigError`；LLM batch 失败改为 `PipelineError`；skipped 和成功路径返回 `RunResult(summary)`。已移除该函数内 `log_path` 引用和 `Log:` stderr 行，同时保留现有用户可见进度、警告和错误 stderr。未移除 stdout JSON 输出，因为统一 stdout 输出迁移属于 TASK 004 范围。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py` 通过；`python3 - <<'PY' ... PY` 直接验证缺失保存 HTML 抛 `ParseError`、fetch 失败抛 `FetchError`、skipped 路径返回 `RunResult(exit_code=0)` 且不输出 `Log:` 通过。

#### [TASK ID: 004] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` — `main` 函数
- **目标描述**: 重写 `main()` 为统一异常捕获 + 单出口模式
- **前置依赖**: TASK 002, TASK 003
- **执行指令与限制**:
  1. `main()` 返回 `int`（exit code）
  2. 结构：
     ```python
     def main() -> int:
         args = parse_args(...)
         cache_base = ...
         log_path = cache_base / "arxiv_daily.log"
         try:
             validate_env(...)          # 新提取的校验函数，raise ConfigError
             llm_config = LlmConfig(...)
             logger = setup_logging(cache_base)
             if args.patch:
                 result = run_patch_mode(args, logger, cache_base, llm_config)
             else:
                 result = run_normal_mode(args, logger, cache_base, llm_config)
             print(json.dumps(result.summary, ensure_ascii=False))
             return result.exit_code
         except ArxivDailyError as e:
             print(f"Error: {e}", file=sys.stderr)
             print(f"Log: {log_path}", file=sys.stderr)
             return e.exit_code
     ```
  3. 文件末尾改为：
     ```python
     if __name__ == "__main__":
         sys.exit(main())
     ```
  4. 将原 `main()` 中环境变量校验逻辑提取为 `validate_env() -> LlmConfig`，校验失败 raise `ConfigError`
  5. **注意**：`run_patch_mode` 和 `run_normal_mode` 内部已有 `print(json.dumps(summary))` 的 stdout 输出——重构后应移除函数内的 stdout JSON 输出，改由 `main()` 统一 `print(json.dumps(result.summary))`

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已修改 `arxiv-update/scripts/arxiv_daily.py`：新增 `validate_env() -> LlmConfig` 并将缺失环境变量转换为 `ConfigError`；重写 `main() -> int`，由入口统一构造 `LlmConfig`、初始化 logger、调用 patch/normal mode、打印 `RunResult.summary` 并返回 `RunResult.exit_code`；`except ArxivDailyError` 统一输出 `Error:` 与 `Log:` 并返回异常 exit code；文件末尾改为 `sys.exit(main())`。已移除 `run_patch_mode` 和 `run_normal_mode` 内部 stdout JSON 输出，并将 fatal error stderr 收敛到 `main()`，避免同一错误重复打印；保留进度和 warning stderr。同步更新 `arxiv-update/scripts/tests/test_arxiv_daily.py::TestMainCli` 断言以匹配 `main() -> int` 接口。验证：`python3 -m py_compile arxiv-update/scripts/arxiv_daily.py arxiv-update/scripts/tests/test_arxiv_daily.py` 通过；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMainCli -q` 通过，9 passed；`cd arxiv-update/scripts && ./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q` 通过，136 passed, 1 xfailed。

#### [TASK ID: 005] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` 全文
- **目标描述**: 验证无残留 `sys.exit` 调用，确认行为等价
- **前置依赖**: TASK 004
- **执行指令与限制**:
  1. 执行 `grep -n "sys.exit" arxiv_daily.py`，期望仅剩 `if __name__` 行的 `sys.exit(main())`
  2. 如果 `sys` 模块不再有其他用途（`sys.stderr` 仍在用），保留 import
  3. 运行 `python3 -c "import arxiv_daily"` 确认无语法错误（注意：不要实际执行 main，仅 import 检查）
  4. 确认 stderr 输出格式与重构前一致（人类需确认此点）

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。本项为验证任务，未修改源码。`grep -n "sys.exit" arxiv_daily.py` 仅返回 `1560:    sys.exit(main())`，确认全文只剩入口处单出口调用；`sys` 仍用于 `sys.stderr` 和入口 `sys.exit(main())`，保留 import；`python3 -c "import arxiv_daily"` 成功，未执行 main。stderr 输出格式已通过 `tests/test_arxiv_daily.py::TestMainCli` 覆盖主要成功、错误、warning 路径；任务要求中的“人类需确认”部分无法由 Executor 自动完成，已保留为人工最终验收事项。验证：`grep -n "sys.exit" arxiv_daily.py` 通过；`python3 -c "import arxiv_daily"` 通过；`python3 -m py_compile arxiv_daily.py tests/test_arxiv_daily.py` 通过；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMainCli -q` 通过，9 passed；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q` 通过，136 passed, 1 xfailed。

### [Round 2]
- **Reviewer**:
  - **状态判决**: `[STATUS: PASS]`
  - **审查摘要**:
    重构目标完全达成：18 处散落的 `sys.exit()` 已被替换为自定义异常体系，`main()` 作为唯一出口处统一捕获异常并返回进程退出码。代码结构清晰，异常层级语义明确，测试覆盖充分（136 passed, 1 xfailed）。
  - **[ISSUE ID: 001] (Advisory, non-blocking)**:
    - **定位**: `arxiv-update/scripts/arxiv_daily.py:run_normal_mode` L1381
    - **问题描述**: `raise FetchError(f"Failed to fetch arxiv page: {e}")` 丢失原始异常的 traceback。应使用 `raise ... from e` 语法保留异常链，便于 debug 时在日志中追踪原始堆栈。
    - **修复指令**: 改为 `raise FetchError(f"Failed to fetch arxiv page: {e}") from e`
    - **前置依赖**: 无
    - **严重程度**: 低。当前 `main()` 中 `except ArxivDailyError` 只打印 message 不打印 traceback，因此实际用户可见行为不受影响。但作为可维护性最佳实践，建议后续补上。此 Issue 不阻塞合并。
  - **回应 Executor**: 所有 5 个 TASK 的实现符合设计预期，测试断言已正确从 `pytest.raises(SystemExit)` 迁移为直接断言 `main()` 返回值，接口清晰。
  - **Issue 优先级与依赖总表**:
    | Issue | 优先级 | 阻塞合并 | 前置依赖 |
    |-------|--------|----------|----------|
    | 001   | Low    | 否       | 无       |

  - **综合评价**:
    - 异常层级设计合理，`exit_code` 作为类属性而非实例属性，简洁且不易出错。
    - `RunResult` dataclass 提供了清晰的返回值契约。
    - `validate_env()` 的提取使环境校验逻辑可独立测试。
    - stderr 输出格式在 `main()` 中统一为 `Error: ...` + `Log: ...`，一致性良好。
    - 未引入不必要的抽象或过度工程。

> **本项目无 AGENTS.md 规范文件，以通用最佳实践为准。** Issue 001 基于 Python 异常链最佳实践（PEP 3134），非项目强制约定。

#### [TASK ID: 006] [状态: RESOLVED]
- **定位**: `arxiv-update/scripts/arxiv_daily.py:run_normal_mode` L1379-1381
- **目标描述**: 为 `raise FetchError(...)` 补充异常链（`from e`），保留原始异常的 traceback
- **前置依赖**: 无
- **执行指令与限制**:
  1. 将 L1381 的 `raise FetchError(f"Failed to fetch arxiv page: {e}")` 改为 `raise FetchError(f"Failed to fetch arxiv page: {e}") from e`
  2. 检查全文件是否有其他 `except ... as e:` + `raise XxxError(...)` 的模式遗漏了 `from e`，如有一并补上
  3. 运行 `python3 -m py_compile arxiv_daily.py` 确认无语法错误
  4. 运行测试确认无回归

Executor: [RESOLVED] 同意并执行。本项目无 AGENTS.md 规范文件，以通用最佳实践为准。已修改 `arxiv-update/scripts/arxiv_daily.py:run_normal_mode`，将 fetch 失败路径改为 `raise FetchError(f"Failed to fetch arxiv page: {e}") from e`，保留原始异常 traceback。已检查全文 `except ... as e:` 模式：仅该处存在捕获后包装为自定义 `ArxivDailyError` 且需要补充异常链；LLM 重试循环不会包装为自定义异常，`main()` 仅捕获不重抛。验证：`python3 -m py_compile arxiv_daily.py` 通过；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py::TestMainCli -q` 通过，9 passed；直接断言 `FetchError.__cause__` 保留原始异常通过；`./.venv/bin/python -m pytest tests/test_arxiv_daily.py -q` 通过，136 passed, 1 xfailed。

## 4. 最终决策 (Final Decision)

- **决策记录**: 代码审查通过，可合并。重构完整实现了异常驱动单出口模式，消除了全部散落的 `sys.exit()` 调用，测试全部通过且覆盖了主要成功/失败/跳过路径。Advisory Issue 001（`raise ... from e`）建议后续 PR 补上，不阻塞本次合并。
