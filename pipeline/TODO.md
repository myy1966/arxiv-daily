# arxiv-update 重构计划

## 整体架构

单一脚本 `arxiv_daily.py` 完成全部工作，内部分三个阶段。LLM 仅在第 2 阶段介入，其余为确定性逻辑。

```bash
python3 arxiv_daily.py cs.AI \
  --output ~/arxiv-daily/_posts \
  --tags ~/arxiv-daily/data/tags.json
```

LLM API key 从环境变量 `ARXIV_UPDATE_KEY` 读取，未设置时报错退出。API endpoint 从 `ARXIV_UPDATE_API` 读取（如 `https://api.openai.com/v1`），模型名从 `ARXIV_UPDATE_MODEL` 读取（如 `gpt-4o`），两者均为必需环境变量。

Agent 只需运行脚本、检查退出码、解析 stdout 输出的 JSON 摘要向用户汇报。

脚本 stdout 输出格式（供 Agent 解析）：

```json
{
  "listing_date": "2026-04-12",
  "field": "cs.AI",
  "output_file": "/path/to/2026-04-12-cs.AI.md",
  "sections": {"New submissions": 45, "Cross submissions": 12, "Replacement submissions": 8},
  "total": 65,
  "failed_batches": 0
}
```

## 文件结构

```
arxiv-update/                       # skill 项目目录（不写入运行时数据）
  SKILL.md                          # skill 定义，描述触发条件和调用流程
  arxiv_daily.py                    # 由 parse_arxiv.py 重命名而来（git mv 保留历史）

~/arxiv-daily/             # --output 的上级目录（Jekyll 站点）
  _posts/                           # --output 指向此处，存放生成的 markdown
  data/
    tags.json                       # 所有领域共用的 tag 列表，随运行自然增长

$HOME/.cache/arxiv-update/          # 运行时缓存与日志（脚本自动创建）
  cs.AI/
    metadata_hash                   # 当次运行的 metadata 指纹，用于判断缓存是否有效
    batch_001.json                  # 已完成 batch 的 LLM 结果（断点续传用）
    batch_002.json
    ...
  arxiv_daily.log                   # 运行日志（含错误详情），追加写入
```

## 阶段 1：下载与解析

**输入**：arxiv 领域代码，如 `cs.AI`

**处理**：
1. 用 `urllib` 下载 `https://arxiv.org/list/{field}/new` 的 HTML
2. 从页面中解析 listing date（如 "Showing new listings for Friday, 12 April 2026"），作为输出文件名和 front matter 的日期来源。必须使用页面中的日期而非脚本执行时间，因为 arxiv 的更新周期与本地时区可能不一致
3. 正则解析出三个 section（New / Cross / Replacement）及每篇论文的字段
4. 从 abstract 和 comments 的 HTML 原文的 `href` 属性中提取代码仓库 URL，包括 GitHub、GitLab 及自托管 Git 实例（匹配 `github.com`、`gitlab.com` 及常见路径模式如 `/username/repo`）。arxiv 纯文本中会将链接替换为 "this https URL"，需从原始 HTML 提取

**内部数据结构**：
- metadata：论文结构化数据（id, title, authors, subjects, comments, code_urls），按 section 分组
- abstracts：按 16 篇一组切分为多个 batch

## 阶段 2：逐批调用 LLM 生成摘要 + tag

**核心思路**：串行处理每个 batch，每次调用 LLM 做三件事——翻译摘要、分配 tag、识别录用状态。串行而非并行是为了避免占满 API 并发额度，将带宽留给可能同时在运行的更重要的任务。tag 列表在批次间累积传递。

**处理流程**：

```
tags_path = --tags 参数指定的路径（必需参数）
加载 tags_path → current_tags
（若文件不存在，创建初始结构 {"updated_at": ..., "tags": []}）

for each batch (按序):

    构造 prompt:
    - 系统提示: 你是论文摘要翻译和分类助手
    - 输入: 16 篇论文的 title + abstract + comments（如有）
    - 当前 tag 列表: current_tags（逗号分隔的字符串列表）
    - 要求:
      1. 为每篇论文生成 1-3 句中文摘要
      2. 为每篇论文从 current_tags 中选择 1-3 个最匹配的 tag
      3. 仅当现有 tag 确实无法覆盖时，才新增 tag（字符串即可）
      4. 新增的 tag 需简洁、与已有 tag 粒度一致
      5. 如果论文的 comments 表明已被录用，提取 venue 缩写；
         未录用则 venue 为 null

    调用 LLM API，解析响应:
    - 每篇论文的中文摘要
    - 每篇论文的 tag 分配（tag 字符串列表）
    - 每篇论文的 venue（已录用则为缩写字符串，否则为 null）
    - 新增的 tag 列表（字符串列表）

    LLM 响应的期望 JSON 结构:
    {
      "papers": [
        {
          "id": "2506.12345",
          "summary": "中文摘要...",
          "tags": ["LLM/大语言模型", "代码生成"],
          "venue": "ACL 2026"       // 或 null
        },
        ...
      ],
      "new_tags": ["新tag1", "新tag2"]  // 无新增则为空数组
    }

    将本 batch 结果即时写入 $HOME/.cache/arxiv-update/{field}/batch_NNN.json
    current_tags += new_tags  // 累积传递给下一个 batch

全部 batch 处理完成后，将 current_tags 回写 tags_path（更新 updated_at 时间戳）
```

**错误处理与重试**：
- LLM API 调用失败时，按 exponential backoff 重试，最多 3 次（间隔 2s → 4s → 8s）
- JSON 解析失败（LLM 返回格式不合规）视为调用失败，同样触发重试
- 3 次重试均失败的 batch，跳过并继续处理后续 batch，该 batch 内论文的摘要、tag、venue 在最终输出中留空
- 所有错误详情（含 LLM 原始响应）写入日志文件 `$HOME/.cache/arxiv-update/arxiv_daily.log`，追加写入，带时间戳
- stderr 仅输出简要进度信息（如 `[3/7] batch_003 done` 或 `[4/7] batch_004 FAILED, see log`），不输出完整错误堆栈
- 退出码规则：
  - 0：全部 batch 成功，markdown 已生成，stdout 输出 JSON 摘要
  - 1：部分 batch 失败但 markdown 已生成（含留空条目），stdout 输出 JSON 摘要（`failed_batches` > 0），stderr 提示日志路径
  - 2：致命错误（网络失败、环境变量缺失、全部 batch 失败等），无 markdown 输出，stdout 无 JSON，stderr 输出错误信息及日志路径

**断点续传**：
- 每个 batch 处理完成后，立即将结果写入 `$HOME/.cache/arxiv-update/{field}/batch_NNN.json`（含摘要、tag、venue、当前 current_tags 快照）
- 缓存有效性判断：对当次运行的论文 ID 有序列表计算 SHA-256，写入 `metadata_hash` 文件。重新运行时比对 hash，一致则复用已有 batch 缓存，从中恢复 current_tags 继续处理；不一致则清除旧缓存、全量重跑
- 阶段 3 生成 markdown 成功后，清理该领域的缓存目录
- `--no-cache` 参数可强制忽略缓存、全量重跑

**tags.json 格式**：

tag 文件路径由 `--tags` 参数显式指定。所有领域共用同一个文件，避免跨领域 tag 重复。

```json
{
  "updated_at": "2026-04-12T12:00:00+08:00",
  "tags": [
    "LLM/大语言模型",
    "多模态学习",
    "强化学习"
  ]
}
```

tag 随每次运行自然增长，采用简洁的描述性名称，推荐"中文/English"双语命名以兼顾可读性和语义清晰度。首次运行时自动创建空文件。传入 LLM prompt 时直接序列化为逗号分隔文本（如 `LLM/大语言模型, 多模态学习, 强化学习`），减少 token 开销。

**LLM prompt 关键约束**：
- 必须优先复用已有 tag，只有确实不能表达时才新增
- 新增 tag 应与已有 tag 粒度一致，不要过细
- 输出严格 JSON 格式，便于脚本解析
- venue 提取规则：
  - 仅识别明确已录用的：accepted, published, to appear, camera-ready, appearing at, presented at
  - 排除仅投稿的：submitted to, preprint, under review
  - 提取为简洁的 venue 缩写 + 年份，如 "ACL 2026"、"CVPR 2026 Findings"
  - Workshop 论文统一标注为 "{会议} {年份} Workshop"，例如：
    - "Accepted at ICLR 2026 the 2nd Workshop on World Models" → "ICLR 2026 Workshop"
    - "Accepted at CVPRW 2026" → "CVPR 2026 Workshop"
    - "Accepted at EMAS @ AAMAS 2026" → "AAMAS 2026 Workshop"

## 阶段 3：汇总生成 markdown

**纯确定性逻辑**，无 LLM 调用。

**处理**：

```
1. 建立论文全局编号（New 1..N1, Cross N1+1..N1+N2, Replace ...）

2. 每个 section 内统计 tag 频次，按频次降序取 top-5 作为该 section 的主题概括
   - 附上每个 tag 下的代表性论文标题

3. 生成"整体趋势"一句话：全局统计 tag 频次 top-3 拼模板句

4. 从阶段 2 输出的 venue 字段汇总已录用论文（venue 非 null 的条目）

5. 从 metadata.code_urls 中识别开源论文

6. 按模板拼接 markdown:
   - YAML front matter (title, date 取自 listing_date, categories)
   - 顶部概括 (各 section 主题 + 整体趋势)
   - 已录用论文列表 [序号](#序号)(venue)
   - 开源论文列表 [序号](#序号)(code_url)
   - 各 section 论文条目 (锚点 + 标题 + arXiv + 作者 + 学科 + tag + 摘要)
```

**输出**：`{output_dir}/YYYY-MM-DD-{field}.md`

## SKILL.md 中 agent 的角色

Agent 不做数据处理，只负责调度：

```
1. 确认领域（如用户未指定则询问）
2. 构造命令并运行:
   python3 <SKILL_DIR>/arxiv_daily.py <领域> \
     --output ~/arxiv-daily/_posts \
     --tags ~/arxiv-daily/data/tags.json
3. 根据退出码分支处理:
   - 退出码 0: 解析 stdout JSON，向用户汇报论文数量、各 section 分布、输出文件路径
   - 退出码 1: 解析 stdout JSON，汇报结果并提示有部分 batch 失败，告知日志路径
   - 退出码 2: 告知用户运行失败（领域不存在/当天无更新/环境变量缺失等），告知日志路径
```
