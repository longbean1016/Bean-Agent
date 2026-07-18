---
name: summarize
description: Summarize or extract text and transcripts from URLs, podcasts, videos, and local files; use for requests such as “summarize this article” or “transcribe this video”.
when_to_use: 用户要求总结 URL、文章、本地文件、播客、YouTube 或视频转写时使用。
metadata:
  beanagent:
    always: false
    requires:
      bins: ["summarize"]
---

# Summarize

使用 `summarize` CLI 总结 URL、本地文件和视频链接。不要在未读取来源内容时凭标题或 URL 猜测摘要。

## 快速使用

```bash
summarize "https://example.com"
summarize "path/to/file.pdf"
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto
```

用户明确指定模型时再增加 `--model <provider/model>`，不要擅自覆盖用户环境中的默认模型配置。

## 转写与摘要

提取视频转写：

```bash
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto --extract-only
```

如果转写过长，先给出紧凑摘要，再询问需要展开的时间段或主题；用户明确要求完整原文时说明长度限制并分段处理。

## 常用参数

- `--length short|medium|long|xl|xxl|<chars>`
- `--max-output-tokens <count>`
- `--extract-only`
- `--json`
- `--firecrawl auto|off|always`
- `--youtube auto`

## 输出要求

- 区分来源事实与自己的归纳。
- 保留关键数字、日期、限制条件和结论依据。
- 无法获取、转写或解析来源时明确报告失败原因，不编造内容。
- 不在回复中输出 API key、环境变量值或 CLI 配置中的敏感字段。
