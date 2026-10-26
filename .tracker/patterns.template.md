# glean · pattern 台账（模板）

新环境初始化：把本文件复制为同目录下的 `patterns.md`，再按规则追加条目。
`patterns.md` 被 .gitignore 忽略，因为它记录本机会话 ID 与个人表达习惯，不入版本控制。

只存 pattern-key、计数、会话 ID 与用户反馈。**不存对话原文。**
与 self-improving-agent 的 `.learnings/` 严格隔离，互不读写（D4）。

## 受控 pattern-key 词表（D9）

禁止自由命名。每条候选缺陷归入下列 key 之一；确需新增时必须在报告中显式说明。

| key | 含义 |
|---|---|
| `missing.file_path` | 未给出文件/路径，AI 需追问定位 |
| `missing.context` | 未给出背景（项目、环境、前文） |
| `missing.output_format` | 未指定期望的输出形式或篇幅 |
| `missing.scope` | 未限定范围，AI 做过头或做不足 |
| `missing.acceptance` | 未给出验收标准，结果需反复调整 |
| `ambiguous.reference` | 指代不明（"那个""它""上面"） |
| `ambiguous.multi_intent` | 一句话多个意图，AI 只完成其一 |
| `preference.tone` | 对语气/详略的偏好反复被重申 |
| `preference.workflow` | 对流程顺序的偏好反复被重申 |
| `constraint.env` | 环境约束反复重述（路径、包管理器、运行时） |
| `constraint.convention` | 项目约定反复重述（命名、结构、提交规范） |

## 计数与去重规则（D9）

1. 每次扫描先读本文件的 `Counted-Sessions`。
2. 同一 `(pattern-key, session_id)` 已计入 → 只更新 `Last-Seen`，**不增加** `Recurrence-Count`。
3. 同一会话内同一 key 出现多次 → 计 1 次。
4. `Status: dismissed` 的 pattern 30 天内不再建议。
5. `Feedback` 累计 `wrong >= 2` → 自动转为 `dismissed`，并在下次报告中说明。

## 门槛（D2）

- L2：`Recurrence-Count >= 3` 且出现在 >= 2 个不同会话
- L3：`Recurrence-Count >= 3`
- L1：单次即可，不落盘

---

## 条目模板

```markdown
## [PTN-YYYYMMDD-XXX] <pattern-key>
**First-Seen**: YYYY-MM-DD
**Last-Seen**: YYYY-MM-DD
**Recurrence-Count**: 0
**Counted-Sessions**: session-id-1, session-id-2
**Workspaces**: ~/path (2)
**Status**: pending | confirmed | written | dismissed
**Proposed-Target**: USER.md | MEMORY.md | SKILL.md | AGENTS.md
**Feedback**: useful=0, wrong=0
```

---

（暂无条目。）
