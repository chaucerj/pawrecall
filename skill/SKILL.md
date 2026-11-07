---
name: pawrecall
description: Search the local cross-agent conversation archive covering every past Claude Code, Codex, OpenCode, Cursor, Qoder and ZCode session on this machine, regardless of which directory each agent was started in. Hybrid retrieval = exact keyword matching fused with semantic vector search (RRF), plus a distilled fact layer (decisions / preferences / constraints) ranked above raw transcripts. Use when the user refers to previous conversations or prior decisions — e.g. 之前/上次/上回/记得/聊过/讨论过/以前, "we discussed this before", "I remember talking about X", "what did we decide last time" — or when a task needs context or conclusions from earlier sessions with any AI tool.
---

# search-ai-history

检索本机所有 AI agent（Claude Code / Codex / OpenCode / Cursor / Qoder / ZCode）的历史对话。索引按会话启动路径无关的方式统一存储，任何目录下都能查。

## 数据源

- 统一索引：`~/.pawrecall/history.db`（SQLite，4 万+ 条消息）
- 由 LaunchAgent 每 30 分钟自动增量更新（含增量向量嵌入，见下）
- 只含对话文字（用户消息 + agent 回复），不含工具输出的日志

## 检索模式（paw_search 的 mode 参数）

- `auto`（默认）：**事实层前置、exact 优先、语义按需**。强信号（命中≥5 条或最热会话≥2 条）→ 本地精确+会话聚合，0 网络调用（~100-150ms）；弱信号（0 命中/孤立命中）→ 语义路介入做 RRF 融合（~160ms，查询向量有双层缓存，二次查询更快）。两路都做会话聚合：命中条数加权、每会话出最优消息（附 session_hits），精确头部会话强制锚定。**优先用它**
- `exact`：逐字子串匹配，纯时间排序（原版行为）。找报错原文、确切措辞时用它
- `semantic`：纯语义。换说法、跨语言改述时用它（需要向量已建立）
- `facts`：只查蒸馏事实层（决定/偏好/约束），配好 chat 模型后可用

结果首行 `[...]` 标注了本次实际用的模式。`★事实` 开头的条目是事实层命中——蒸馏过的决定/偏好/约束，回答"上次怎么定的"优先信它，需要完整上下文再按 session 去 `paw_read`。输出整体受字符预算封顶（默认 6000 字符，`PAWRECALL_MAX_CHARS` 可调），不会撑爆上下文窗口。

## 首选方式：MCP 工具（pawrecall server）

1. `paw_search(query, source?, project?, mode?, limit?)` — 混合检索，事实层前置，返回来源 / 项目路径 / 时间 / 摘录
2. `paw_facts(query?, project?, include_superseded?, limit?)` — 浏览/检索事实层；`include_superseded=true` 可审计被推翻的旧决定
3. `paw_read(session_id 或 file_path, limit?)` — 按时间顺序读回单次会话的完整内容

## 兜底方式：CLI（MCP 工具不可用时，用 bash 执行）

```bash
python3 ~/.pawrecall/index.py search "关键词" [--mode auto|exact|semantic|facts] [--source claude|codex|opencode|cursor|qoder|zcode] [--limit 20]
python3 ~/.pawrecall/index.py facts status    # 事实层状态
python3 ~/.pawrecall/index.py facts run       # 手动补一轮事实提取（--sessions N 限量）
python3 ~/.pawrecall/index.py embed --status  # 语义层状态
python3 ~/.pawrecall/index.py embed           # 增量嵌入（配好 provider 后手动补全用）
python3 ~/.pawrecall/eval.py                  # 检索回归评测
```

## 语义层与事实层（可选，零依赖原则不破）

**语义层**未配置任何 embedding 来源时自动禁用，行为与纯精确检索完全一致。启用方式（三选一，详见 `embed.json.example`）：

1. **OpenAI 兼容 API**：`cp embed.json.example embed.json` 后填 url/key/model（火山 Ark / OpenAI / SiliconFlow / LM Studio 均可）
2. **Ollama 本地**：`ollama pull bge-m3` 后无需配置，自动探测 localhost:11434
3. **fastembed 离线**：venv 里 `pip install fastembed`，自动探测，完全离线

**事实层**需要 chat 模型做提取（详见 `facts.json.example`）：`cp facts.json.example facts.json` 填 url/model（本地推荐 `ollama pull qwen3:4b`，提取不需要大模型）；或直接配置。没配置时整层自动禁用、检索行为不变。配好后每 30 分钟的增量索引会自动提取几个最新会话的事实；`facts status` 看进度。被新决定推翻的旧事实自动降权（默认不出现，审计时传 `include_superseded`）。

**矛盾处理是 HITL（人审）**：模型判定新旧事实矛盾时只有提议权——默认挂起待确认（`conflict_mode: hitl`），裁决前新旧事实都保留可见，命中时带 `⚠ 与事实 #N 待确认矛盾` 标注；`paw_facts(pending_conflicts=true)` 看队列，`paw_facts_resolve(conflict_id, decision)` 裁决（new_wins=新的取代旧的 / old_wins=否决新的 / both=并非矛盾，都保留）。可选 `auto_resolve_days` 兜底超时未裁决的冲突，`conflict_mode: auto` 回到"直接信任模型"旧行为。

## 工作流

0. **作用域优先（数据隔离）**：默认传 `project=<当前目录名或项目路径子串>` 只搜当前项目的历史，避免扫描其他项目；仅当用户明确要求"跨项目/全局"时才省略 project。浏览某项目聊过什么用 `paw_sessions` / `paw_facts`，需要精确回放单次会话用 `paw_read`
1. 从用户需求中提炼 1-3 个**会真实出现在对话原文里**的关键词（中英文均可）。原文关键词用 `exact`；描述性/改述性问题用 `auto` 或 `semantic`；"上次怎么定的/为什么这么改"优先 `auto`（事实层命中会排最前）
2. `paw_search` 检索，向用户呈现结果时注明出处（哪个工具、哪个项目路径、什么时间）；`★事实` 条目注明"提取自某会话"
3. 需要完整上下文时用 `paw_read` 深入该次会话
4. 0 结果时的处理顺序：
   - 换同义词 / 更具体的词重试（精确 0 结果时换 `auto`/`semantic` 模式重试效果最好）
   - 怀疑索引过期（用户刚聊完不久）：先运行 `python3 ~/.pawrecall/index.py`（增量，秒级）再检索

## 纪律

- 回答"之前聊过 / 上次怎么定的"类问题前**必须先检索**，禁止编造或凭印象回答
- 引用历史结论时必须给出出处（工具 · 项目路径 · 时间）；`★事实` 条目引用时说明"据事实层提取"
- **带 ⚠ 待确认矛盾的事实**：如果它与用户当前问题相关，先向用户呈现新旧两条并询问哪条有效，得到答复后调用 `paw_facts_resolve` 裁决；**绝不替用户默默裁决**，与问题无关的矛盾不要主动打断用户
