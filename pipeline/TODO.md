# arxiv-update 重构计划

## 整体架构

单一脚本 `arxiv_daily.py` 完成全部工作，内部分三个阶段。LLM 仅在第 2 阶段介入，其余为确定性逻辑。

```bash
python3 arxiv_daily.py cs.AI \
  --output ~/arxiv-daily/_posts \
  --api-key $OPENAI_API_KEY
```

Agent 只需运行脚本、检查退出码、读输出文件向用户汇报。

## 文件结构

```
arxiv-update/
  SKILL.md              # skill 定义，描述触发条件和调用流程
  arxiv_daily.py        # 单一脚本，完成全部工作
  base_tags/
    cs.AI.json          # cs.AI 领域的 tag 列表，随运行自然增长
    cs.CL.json          # 首次运行新领域时自动创建
    ...
```

## 阶段 1：下载与解析

**输入**：arxiv 领域代码，如 `cs.AI`

**处理**：
1. 用 `urllib` 下载 `https://arxiv.org/list/{field}/new` 的 HTML
2. 正则解析出三个 section（New / Cross / Replacement）及每篇论文的字段
3. 从 abstract 和 comments 的 HTML 原文的 `href` 属性中提取 GitHub URL（arxiv 纯文本中会替换为 "this https URL"，需从原始 HTML 提取）

**内部数据结构**：
- metadata：论文结构化数据（id, title, authors, subjects, comments, github_urls），按 section 分组
- abstracts：按 16 篇一组切分为多个 batch

## 阶段 2：逐批调用 LLM 生成摘要 + tag

**核心思路**：串行处理每个 batch，每次调用 LLM 做三件事——翻译摘要、分配 tag、识别录用状态。tag 列表在批次间累积传递。

**处理流程**：

```
加载 base_tags/{field}.json → current_tags
（若文件不存在，创建空的初始结构）

for each batch (按序):

    构造 prompt:
    - 系统提示: 你是论文摘要翻译和分类助手
    - 输入: 16 篇论文的 title + abstract + comments（如有）
    - 当前 tag 列表: current_tags（含 name 和 keywords）
    - 要求:
      1. 为每篇论文生成 1-3 句中文摘要
      2. 为每篇论文从 current_tags 中选择 1-3 个最匹配的 tag（按 name 引用）
      3. 仅当现有 tag 确实无法覆盖时，才新增 tag（提供 name 和 keywords）
      4. 新增的 tag 需简洁、与已有 tag 粒度一致
      5. 如果论文的 comments 表明已被录用，提取 venue 缩写；
         未录用则 venue 为 null

    调用 LLM API，解析响应:
    - 每篇论文的中文摘要
    - 每篇论文的 tag 分配（tag name 列表）
    - 每篇论文的 venue（已录用则为缩写字符串，否则为 null）
    - 新增的 tag 列表（含 name 和 keywords）

    current_tags += new_tags  // 累积传递给下一个 batch

全部 batch 处理完成后，将 current_tags 回写 base_tags/{field}.json（更新 updated_at 时间戳）
```

**base_tags/{field}.json 格式**：

```json
{
  "updated_at": "2026-04-12T12:00:00+08:00",
  "tags": [
    {
      "name": "LLM/大语言模型",
      "keywords": ["large language model", "LLM", "GPT", "prompt"]
    },
    {
      "name": "多模态学习",
      "keywords": ["multimodal", "vision-language", "image-text"]
    }
  ]
}
```

tag 列表按领域独立维护，随每次运行自然增长。keywords 辅助 LLM 理解 tag 含义，提高分配准确性。首次运行某领域时自动创建空文件。

**LLM prompt 关键约束**：
- 必须优先复用已有 tag，只有确实不能表达时才新增
- 新增 tag 应与已有 tag 粒度一致，不要过细
- 输出严格 JSON 格式，便于脚本解析
- venue 提取规则：
  - 仅识别明确已录用的：accepted, published, to appear, camera-ready
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

2. 统计 tag 频次，按频次降序取 top-5 作为各 section 的主题概括
   - 附上每个 tag 下的代表性论文标题

3. 生成"整体趋势"一句话：
   - 纯脚本方式：用 tag 频次 top-3 拼模板句
   - 或轻量 LLM 调用：将 tag 频次表传入生成一句话（输入极小）

4. 从阶段 2 输出的 venue 字段汇总已录用论文（venue 非 null 的条目）

5. 从 metadata.github_urls 中识别开源论文

6. 按模板拼接 markdown:
   - YAML front matter (title, date, categories)
   - 顶部概括 (各 section 主题 + 整体趋势)
   - 已录用论文列表 [序号](#序号)(venue)
   - 开源论文列表 [序号](#序号)(github_url)
   - 各 section 论文条目 (锚点 + 标题 + arXiv + 作者 + 学科 + tag + 摘要)
```

**输出**：`{output_dir}/YYYY-MM-DD-{field}.md`

## SKILL.md 中 agent 的角色

Agent 不做数据处理，只负责调度：

```
1. 确认领域（如用户未指定则询问）
2. 运行 arxiv_daily.py → 检查退出码
3. 读取脚本输出的统计信息，向用户报告结果
```
