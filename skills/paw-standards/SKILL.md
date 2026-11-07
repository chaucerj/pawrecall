---
name: paw-standards
description: Distill improvement-type user feedback (纠正、要求、偏好、流程约束) from past cross-agent sessions (pawrecall index) into a workspace-local design/development standards file. Use when the user says 沉淀规范 / 总结成规范 / 把之前的反馈沉淀一下 / 更新规范 / 回顾一下这个项目的要求, "consolidate past feedback into standards", "update the project standards", or when starting a new phase of an existing project and prior working agreements should be honored. Output goes ONLY into the specified conversation's workspace (an AGENTS.md managed block) — never into global config.
---

# paw-standards

把过去 AI 会话里的**改进型要求/建议**（纠正、偏好、流程约束）蒸馏成**设计开发规范**，写入**当前对话 workspace 内**的 AGENTS.md 受管区块。规范只在该 workspace 生效，不碰任何全局配置（~/.claude、~/.agents、~/.zcode 全局层一律不动）。

## 严格边界（先读这段）

- **落点只有两种**：默认 `<workspace>/AGENTS.md`；用户明确指定别的文件时才用指定文件，但**必须在 workspace 目录内**
- 只允许改动 `<!-- paw-standards:begin -->` 与 `<!-- paw-standards:end -->` 标记之间的内容；标记之外的已有内容（用户的 README、手写规范）一个字不动
- 规范必须是**用户在会话里表达过的**要求/建议，不发明用户没说过的规则
- 规范之间冲突或语义有歧义时，先问用户裁决，再落笔

## 工作流

1. **锚定作用域**：默认用当前对话的 workspace 路径；用户指名其他项目时用那个路径。
2. **召回候选**（确定性脚本，无模型调用）：
   ```bash
   python3 ~/.pawrecall/standards.py harvest <workspace路径> --since 90
   python3 ~/.pawrecall/standards.py harvest <workspace路径> --json   # 程序化消费
   python3 ~/.pawrecall/standards.py status                            # 看索引规模
   ```
   0 结果时：放宽 `--since`（去掉即全量）→ 用 `paw_search(project=<workspace名>, mode=auto)` 换关键词补捞。
3. **复核候选**：脚本是启发式召回（指令类/纠正类信号词），逐条判断是否真是改进型要求——剔除一次性任务指令、闲聊、对第三方的抱怨。判断依据不足时用 `paw_read` 回看该 session 的上下文再定。
4. **蒸馏成规范**：
   - 合并同类项（同一要求多次出现只留一条，注明"多次强调"）
   - 每条 = 一句可执行的规则 + 类别标签（编码实现 / 工作流验证 / 沟通协作 / 工具使用）+ 来源（工具 · session 前缀 · 日期）
   - 写成"做什么/不做什么"，不写空话（"注意质量" ❌ → "改完必须跑测试再交付" ✓）
5. **写入 AGENTS.md 受管区块**：
   - 无 AGENTS.md → 新建文件：一句话说明 + 受管区块
   - 已有受管区块 → 读旧区块条目做**去重合并**（旧条目未被新证据推翻就保留），整体重生成区块
   - 无受管区块但有 AGENTS.md → 在文件末尾追加区块，原有内容不动
   - 每次更新把区块头的更新日期刷新
6. **向用户汇报**：新增 / 保留 / 删除了哪些条目、各引自哪个 session；候选里被你剔除的信号是哪些、为什么。

## 受管区块格式

```markdown
<!-- paw-standards:begin | updated 2026-11-08 | managed by paw-standards skill -->
## 设计开发规范（沉淀自历史会话，勿在标记外编辑本区块）

### 编码实现
- 统一用 @Resource 注入而非 @Autowired —— src: zcode · sess_d8cdafb5 · 2026-11-07

### 工作流验证
- 提交前必须本地跑通测试 —— src: claude · sess_a1b65231 · 2026-10-12（多次强调）
<!-- paw-standards:end -->
```

## 纪律

- 这是"沉淀"不是"重写历史"：拿不准是不是规范的意见，宁可列入候选清单向用户确认，不要默默写进区块
- 区块条数保持克制（一般 ≤ 20 条）；被后续会话明确推翻的旧规范要移除并注明
- workspace 路径不存在或没有历史会话时如实说明，不硬凑规范
