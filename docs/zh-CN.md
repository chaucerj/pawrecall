# PawRecall — 中文说明

**你与 AI coding agent 的每一次对话，都可以被任何一个 agent 随时检索。**

单文件 · 零依赖 · MCP + CLI + skill 三通道。[English](../README.md) · [日本語](ja.md)

## 解决什么问题

你用多个 AI 编程工具。几个月前其中某个帮你解决过手头这个问题——但你想不起来是**哪个工具**、在**哪个目录**下聊的。

每个 agent 都把会话记录存在本地磁盘上，只是各自按"启动目录"归档。历史没丢，只是散落。pawrecall 把这些记录统一索引进一个 SQLite 库，再通过一个本地 MCP server 交还给所有 agent——与启动路径无关。

## 特性

- **单文件零依赖**：纯 Python 标准库，没有 npm / Rust 二进制 / embedding 模型 / 守护进程，十分钟读完全部代码
- **中文搜索完美**：无分词器的子串匹配。BM-25 / FTS 类方案对中日文（无空格分词）基本失效，pawrecall 不受影响
- **原文精确检索**：存的是对话原文而非 LLM 压缩摘要，零模型调用成本，细节不丢失
- **确定性触发**：可选的 UserPromptSubmit 钩子在消息提到“之前/上次/we discussed”时自动注入检索提醒，不依赖模型自觉
- **本地隐私**：一切留在 `~/.pawrecall/`，MCP 走 stdio 不走网络，数据库 `chmod 600`

## 安装

```bash
git clone https://github.com/chaucerj/pawrecall.git
cd pawrecall && ./install.sh          # 加 --with-scheduler 启用每30分钟自动索引（macOS）
```

要求：Python 3.9+。仅此而已。

## 使用

**在任意 agent 里**（重启会话后）：

> "我之前好像和某个 AI 讨论过简历项目排序，帮我找一下"

agent 会调用 `paw_search`（看到是哪个工具、哪个项目、什么时间聊的）→ 需要完整上下文再 `paw_read`。

**终端里：**

```bash
python3 pawrecall.py search "知识库"                # 全局搜索
python3 pawrecall.py search "MCP" --source codex   # 只搜某工具的记录
python3 pawrecall.py index                          # 增量索引（秒级）
python3 pawrecall.py sessions --project jiuge-mall  # 会话分类目录（按项目隔离浏览）
```

搜索默认可用 `--project` / `project` 参数限定目录，避免跨项目全量扫描；agent 的工具描述与 skill 已内置“作用域优先”规则。

## 评测

`scripts/eval.py` 在你自己的语料上量化整条管线：索引保真率（原文逐字回验，实测 100%）、
随机子串查询召回（含转义边界用例，实测约 90%）、可选的人工标注 precision@3。
开发期间该评测曾揪出两个真 bug（按插入序而非时间序排序；多词查询被空格破坏），均已修复。

## 与同类项目的区别（2026-02 时点）

- **claude-mem**：压缩摘要式记忆，丢原文细节、有模型成本；pawrecall 是原文索引，两者互补可共存
- **memex**：覆盖 agent 更多（10 个），但无 MCP server、BM-25 对中文失效、需装 Rust 二进制
- **claude-historian-mcp / crispy-recall**：单 agent 或需 Node/模型运行时

中文用户目前没有第二选择——这是本项目的核心差异点。

## Roadmap

- [x] 数据源：Claude Code / Codex / OpenCode / Cursor（全文）；Qoder（会话标题 + Agent 记忆 + 续写摘要，其正文为客户端加密无法解析）；Trae（加密私有库，暂无法索引，但 Trae 的 agent 可通过 CLI 兜底检索其他工具的历史）
- [ ] 更多数据源：Gemini CLI、Windsurf 等（欢迎 PR，新增一个源 ≈ 30-60 行解析函数）
- [ ] 可选的本地 embedding 语义模式（"记不清原话只记得意思"场景）
- [ ] Homebrew / pipx 分发

## 联系与支持

- 🐛 Bug 与功能建议：[提 GitHub Issue](https://github.com/chaucerj/pawrecall/issues)（优先，公开讨论让所有人受益）
- 📬 私密咨询 / 商务合作：**767092677@qq.com**
- ⭐ 如果 PawRecall 帮你省了时间，Star / PR 就是对作者最好的支持。赞助入口见仓库侧栏 "Sponsor this project"。
