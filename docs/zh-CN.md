# PawRecall — 中文说明

**你与 AI coding agent 的每一次对话，都可以被任何一个 agent 随时检索。**

零依赖核心 · MCP + CLI + skill 三通道 · 语义层与事实层可选、缺省自动降级为纯关键词检索。[English](../README.md) · [日本語](ja.md)

## 解决什么问题

你用多个 AI 编程工具。几个月前其中某个帮你解决过手头这个问题——但你想不起来是**哪个工具**、在**哪个目录**下聊的。

每个 agent 都把会话记录存在本地磁盘上，只是各自按"启动目录"归档。历史没丢，只是散落。pawrecall 把这些记录统一索引进一个 SQLite 库，再通过一个本地 MCP server 交还给所有 agent——与启动路径无关。

## 特性

- **零依赖核心**：纯 Python 标准库（SQLite + 几个文件），没有 npm / Rust 二进制 / 守护进程。语义层、事实层全是可选插件——什么都不配，行为就是即时的精确关键词检索
- **混合检索（auto 模式）**：精确子串优先（中日文无分词器也能完美匹配），弱信号时语义向量经 RRF 融合补强。强信号查询 0 网络调用，~100-150ms
- **蒸馏事实层（v1.1）**：可选的 chat 模型把历史会话提炼成决定/偏好/约束级原子事实，检索时排在原文之前——"上次怎么定的"直接给答案，而不是让你读原文。新决定推翻旧决定时自动降权（被推翻项默认隐藏，可审计）
- **注入预算**：检索输出按字符预算封顶（`PAWRECALL_MAX_CHARS`，默认 6000），不会撑爆 agent 的上下文窗口
- **原文保真**：存的是对话原文而非 LLM 压缩摘要，任何会话都能 `paw_read` 完整回放
- **确定性触发**：可选的 UserPromptSubmit 钩子在消息提到"之前/上次/we discussed"时自动注入检索提醒，不依赖模型自觉
- **本地隐私**：一切留在 `~/.pawrecall/`，MCP 走 stdio 不走网络，数据库 `chmod 600`；可选层只调用你自己在 `embed.json` / `facts.json` 里配置的端点

## 安装

```bash
git clone https://github.com/chaucerj/pawrecall.git
cd pawrecall && ./install.sh          # 加 --with-scheduler 启用每30分钟自动索引（macOS）
```

要求：Python 3.9+。仅此而已。

## 使用

**在任意 agent 里**（重启会话后）：

> "我之前好像和某个 AI 讨论过简历项目排序，帮我找一下"

agent 会调用 `paw_search`：事实层命中（★事实）排最前，然后是原文命中（标注工具 / 项目 / 时间）→ 需要完整上下文再 `paw_read`。

**终端里：**

```bash
python3 index.py search "知识库"                    # 混合检索（auto）
python3 index.py search "MCP" --source codex       # 只搜某工具的记录
python3 index.py sessions --project jiuge-mall     # 会话分类目录（按项目隔离浏览）
python3 index.py facts status                      # 事实层状态
python3 index.py index                             # 增量索引（秒级）
```

搜索默认可用 `--project` / `project` 参数限定目录，避免跨项目全量扫描；agent 的工具描述与 skill 已内置"作用域优先"规则。

## 事实层（v1.1）

```bash
cp facts.json.example ~/.pawrecall/facts.json
# url 指向任意 OpenAI 兼容 chat 端点；本地推荐 ollama pull qwen3:4b（提取不需要大模型）
```

每次索引后，最近的会话会被异步提炼成原子事实（decision / preference / constraint / fact），带完整溯源（工具 · 项目 · 会话 · 时间）。检索时 `paw_search` 把匹配事实排最前；`paw_facts` 浏览事实层，`include_superseded=true` 审计某个决定的演变过程。什么都不配时整层自动禁用、行为不变。

**矛盾处理默认 HITL（人审）**：模型判定新旧事实矛盾时只有提议权——挂起待确认，裁决前两条都保留可见并在检索结果中带 `⚠ 待确认矛盾` 标注；由人（或 agent 问过用户之后）用 `paw_facts_resolve` 裁决：new_wins（新的取代旧的）/ old_wins（否决新的）/ both（并非矛盾，都保留），每次裁决记录审计人。`conflict_mode: auto` 回到直接信任模型的旧行为；`auto_resolve_days: 30` 可让超时未裁决的冲突自动收敛。

## 评测

两层度量，都跑在副本库上（`PAWRECALL_DB`），绝不碰生产索引：

- **个人回归**：`eval.py`（自标注用例）+ `scripts/recall_test.py`（改述/跨语言召回）
- **公开基准**：`bench/bench_locomo.py` 把 [LoCoMo](https://huggingface.co/datasets/snap-research/locomo)
  长对话数据集灌进真实管线，报告证据召回率@k / MRR（无需 LLM judge，可复现），可选 `--judge` 判答

首组公开数字（bge-m3 向量、LoCoMo cat1-2 共 602 题、evidence recall@5）：**30.9% / MRR 0.165**（纯语义检索基线）。

## 与同类项目的区别（2026-10 时点）

- **Hy-Memory 2.0（腾讯混元）**：用 30B 专用模型做记忆蒸馏，是"蒸馏式个人记忆"的强对标；pawrecall 走"原文保真 + 可选逐层加蒸馏"路线，零配置可用性是差异点，两者不冲突
- **claude-mem**：压缩摘要式记忆，丢原文细节、有模型成本；pawrecall 是原文索引，两者互补可共存
- **memex**：覆盖 agent 多，但无 MCP server、BM-25 对中文失效、需装 Rust 二进制

中文用户做"原文保真 + 零依赖"路线的目前仍是唯一选择——这是本项目的核心差异点。

## Roadmap

- [x] 数据源：Claude Code / Codex / OpenCode / Cursor / ZCode（全文）；Qoder（会话标题 + Agent 记忆 + 续写摘要，其正文为客户端加密无法解析）；Trae（加密私有库，暂无法索引，但 Trae 的 agent 可通过 CLI 兜底检索其他工具的历史）
- [x] 混合检索：精确 + 语义向量 RRF 融合（embed.json / Ollama / fastembed 三选一）
- [x] 蒸馏事实层 + 冲突语义（facts.json，v1.1）
- [ ] 事实层开放基准：LoCoMo +judge 判答口径出对比数字
- [ ] 更多数据源：Gemini CLI、Windsurf 等（欢迎 PR，新增一个源 ≈ 30-60 行解析函数）
- [ ] Homebrew / pipx 分发

## 联系与支持

- 🐛 Bug 与功能建议：[提 GitHub Issue](https://github.com/chaucerj/pawrecall/issues)（优先，公开讨论让所有人受益）
- 📬 私密咨询 / 商务合作：**767092677@qq.com**
- ⭐ 如果 PawRecall 帮你省了时间，Star / PR 就是对作者最好的支持。赞助入口见仓库侧栏 "Sponsor this project"。
