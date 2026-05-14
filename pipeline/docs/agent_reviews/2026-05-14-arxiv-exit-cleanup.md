---
title: "arxiv_daily.py sys.exit 散落重构"
date: "2026-05-14"
status: "PLANNING_COMPLETE"
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

#### [TASK ID: 002] [状态: OPEN]
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

#### [TASK ID: 003] [状态: OPEN]
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

#### [TASK ID: 004] [状态: OPEN]
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

#### [TASK ID: 005] [状态: OPEN]
- **定位**: `arxiv-update/scripts/arxiv_daily.py` 全文
- **目标描述**: 验证无残留 `sys.exit` 调用，确认行为等价
- **前置依赖**: TASK 004
- **执行指令与限制**:
  1. 执行 `grep -n "sys.exit" arxiv_daily.py`，期望仅剩 `if __name__` 行的 `sys.exit(main())`
  2. 如果 `sys` 模块不再有其他用途（`sys.stderr` 仍在用），保留 import
  3. 运行 `python3 -c "import arxiv_daily"` 确认无语法错误（注意：不要实际执行 main，仅 import 检查）
  4. 确认 stderr 输出格式与重构前一致（人类需确认此点）

## 4. 最终决策 (Final Decision)

（由 Reviewer 在最终判决 PASS 时填写）
