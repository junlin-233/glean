---
name: glean
description: >
  回溯最近 N 次会话，从返工信号中识别用户表达与协作模式，给出优化建议，
  并将重复且有复用价值的模式沉淀为 MEMORY/USER/SKILL/项目文档。
  Use when: (1) 用户说"复盘我最近几次对话""我哪里没说清楚""帮我看看怎么用你更高效";
  (2) 用户在本次会话中多次换说法重问同一意图;
  (3) 用户明确要求把某条经验固化为规则或偏好。
  Do NOT use when: 用户只是在当前任务中正常迭代需求（无跨会话信号）;
  或问题根因是 AI 能力/知识缺口（归 self-improving-agent）。
---

# glean · 拾穗

从已完结的对话里拾回被遗漏的经验。改进对象是**人**，不是 AI——这是与
`self-improving-agent` 的根本区别（后者记录 AI 自身的失败与纠正）。

## 触发与边界

手动触发。不要挂 UserPromptSubmit hook，不要在每次任务结束后自动跑。

适用：
- 用户明确要求复盘、回顾、分析自己最近的提问方式
- 用户要求把某条经验固化为偏好或规则

不适用：
- 当前任务内的正常需求迭代（没有跨会话信号）
- 根因是 AI 理解错、知识过时、工具失误 → 归 self-improving-agent
- 做单次 prompt 改写（不是本 skill 的目标）

## 工作流程

### 第 1 步：扫描（机械提取，不做判断）

**数据源无关。** 脚本通过 adapter 解析不同宿主的转录，也能直接读文件或 stdin：

| source | 位置 | 说明 |
|---|---|---|
| `workbuddy` | `~/.workbuddy/projects/**/*.jsonl` | 自动探测 |
| `claude-code` | `~/.claude/projects/**/*.jsonl` | 自动探测 |
| `text` | 任意 `.md` / `.txt` / stdin | 按 `User:` / `Assistant:` / `我：` 分段 |

```bash
# 默认：自动探测本机两种宿主，取最近 3 个会话
python ~/.workbuddy/skills/glean/scripts/scan_sessions.py --last 3 --pretty --out glean-scan.json

# 只扫某个平台
python ~/.workbuddy/skills/glean/scripts/scan_sessions.py --source claude-code --last 5

# 任意转录：ChatGPT 导出、网页复制、手抄的对话
python ~/.workbuddy/skills/glean/scripts/scan_sessions.py --input chat.md
cat chat.txt | python ~/.workbuddy/skills/glean/scripts/scan_sessions.py --input -
```

常用参数：
- `--last N` 最近 N 个会话，默认 3
- `--source auto|workbuddy|claude-code` 数据源，默认 auto（两种都扫）
- `--root PATH` 自定义数据根目录，可重复
- `--input PATH` 直接指定转录文件，`-` 读 stdin；**此时不做"当前活跃会话"排除**
- `--all` / `--no-all` 全局或仅当前工作目录，默认全局
- `--since YYYY-MM-DD` 只扫该日期之后
- `--current-session <id>` 显式排除当前会话；默认已按"最近 5 分钟内活跃"自动排除
- `--sim-threshold 0.6` 重述候选的相似度阈值

脚本只输出可复现的机械计数与摘录。它**不知道**哪些算表达问题——那是你的工作。
自动扫描模式下当前会话被排除属正常，不要为了提高样本量而加 `--include-current`。

自测：`python ~/.workbuddy/skills/glean/scripts/test_scan_sessions.py`

### 第 2 步：归因（前置必答题，不可跳过）

**返工 ≠ 用户表达问题。** 对每条候选缺陷先判定 `attribution`：

| 取值 | 含义 | 动作 |
|---|---|---|
| `user_expression` | 用户表达确实缺关键信息或指代不明 | 进入第 3 步路由 |
| `ai_capability` | AI 理解错、知识缺口、工具失误 | 不给用户建议 |
| `inherent_ambiguity` | 需求本身模糊或在正常演化 | 不给建议 |
| `unclear` | 证据不足 | 不给建议，宁缺毋滥 |

**只有 `user_expression` 才允许生成建议。** 若全部条目都属后三类，报告必须直说
"这几次对话的返工主要不是表达问题"，不要硬凑建议。

判读要点：
- `restatement_candidates` 只是候选。用户重发同一句话常常是刷新、断线重试或
  会话重开，不是表达失败 → `unclear`。
- `correction_hits` 只说明用户表达了否定，不说明是谁的错。要读上下文判断。
- `ai_question_turns` 只是提问轮次。AI 主动确认需求是好事，不算返工。
- `file_reedit_count` 是背景指标。分多次编辑同一文件是实现功能的常态；
  只有紧跟在 `correction_hits` 之后才可作为返工证据。
- `suspect_mixed_turns > 0` 表示有用户轮次混入了历史 assistant 输出（宿主会这样拼接）。
  读这些轮次时只看开头的问题部分，不要把人家的回答当成用户表达。
- `meta_messages_filtered` 是被剔除的工具通知/IDE 注入条数，用于核对过滤是否生效；
  数量异常高（远超真实轮次）说明该宿主的过滤规则需要更新。

### 第 3 步：三层路由

```
user_expression 的缺陷
└─ 是不是"系统本可以记住"的约束？
   ├─ 是 → 优先建议写入项目文档 / MEMORY.md（改系统，不改人）
   └─ 否 → 看复用性
      ├─ 一次性 → L1：当场说一句，不落盘
      ├─ 跨会话重复的偏好/用法 → L2：建议写 USER.md 或用户级 MEMORY.md
      └─ 确定性步骤或不可选约束 → L3：建议写 SKILL.md / AGENTS.md / CLAUDE.md
```

**"系统能记住"优先于"让用户改"。** 如果某个约束每次都要用户重述（运行环境、
包管理器、输出格式），正确做法是把约束写进文档，而不是训练用户每次写清楚。

### 第 4 步：计数与门槛

读 `~/.workbuddy/skills/glean/.tracker/patterns.md`。若该文件不存在（新环境、或从仓库克隆后），
先从同目录的 `patterns.template.md` 复制一份再写入；**不要直接往模板文件里记条目**。

台账规则：

- pattern-key 必须用台账里的受控词表，禁止自由命名
- 同一 `(pattern-key, session_id)` 已计入则不重复计数
- L2 门槛：`Recurrence-Count >= 3` 且跨 >= 2 个会话
- L3 门槛：`Recurrence-Count >= 3`
- 门槛靠多次扫描累积达成，单次扫描达不到是正常的，不要降低标准凑数

### 第 5 步：报告与写入

报告结构见下。**L2/L3 写入前必须显式确认**，逐条列出拟写入的文本与目标文件。
用户确认后才改文件；未确认的只更新台账计数。

```markdown
# glean 复盘 · 最近 N 次会话（起止日期）
数据来源：本机 ~/.workbuddy/projects/ 下最近 N 个会话文件（已排除当前会话）

## 信号统计
| 会话 | 标题 | cwd | AI提问 | 重述候选 | 纠正 | 工具重试 | 状态 |

## 归因总览
- 用户表达相关：n 条
- AI 能力相关：n 条（不给建议）
- 需求正常演化 / 证据不足：n 条（不给建议）

## 建议
### L1 当场反馈（不落盘）
### L2 建议写入用户偏好（待确认）
### L3 建议沉淀为规则（待确认）
```

落盘（D3）：报告追加写入当前项目 `.workbuddy/memory/YYYY-MM-DD.md`。
**落盘版只写建议、计数、归因，不写原文摘录**——该目录会被后续会话读取，
含原文等于把历史对话注入未来上下文。定位信息只保留 session_id 与轮次序号。
对话展示版可保留 <= 200 字符的原文引用。落盘后在结尾注明路径。

## 措辞纪律

- 所有建议以"下次可以少说一句"开头
- 禁止"你表达得不好""你错了""你没说清楚"类表述
- 只评价"这次协作有没有走弯路"，不评价用户聪不聪明
- 证据不足就说不确定，不要编造模式

## 脱敏

1. 引用用户原话 <= 200 字符/条，AI 摘要 <= 300 字符/条；落盘版不含原文
2. 写文件前检查凭据：`sk-`、`ghp_`、`gho_`、`xoxb-`、`-----BEGIN`、`Bearer `、密码字段
3. 路径中的用户主目录归一化为 `~`
4. 台账不存原文，只存 key、计数、会话 ID、反馈
5. 数据不出本机，不上传，不联网

## 反馈校准（D10）

每条建议允许用户回"有用 / 误判 / 以后别提"，写入台账 `Feedback`。
某 pattern 累计 `wrong >= 2` 自动转 `dismissed`，30 天内不再建议。
跑满 5 次复盘后，依据 useful/wrong 比例调整阈值，不要一直沿用初值。

## 明确不做

- 不做单次 prompt 改写
- 不做无确认的自动写入
- 不在归因不明时给建议
- 不与 self-improving-agent 互相读写台账（D4：各写各的，重复条目用户手动清理）
