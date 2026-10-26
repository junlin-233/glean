#!/usr/bin/env python3
"""glean :: scan_sessions.py

从本机 WorkBuddy 会话记录中提取返工信号，供模型层做归因与三层路由。

设计约束（glean-skill-design.md v1.1）：
  - 只做可复现的机械计数与摘录，语义判断全部交给模型层。
  - 默认排除当前进行中会话（D8）。
  - 输出不写入任何台账；脱敏在摘录阶段完成。

用法：
  python scan_sessions.py --last 3 --pretty --out scan.json
  python scan_sessions.py --last 5 --no-all --sim-threshold 0.55
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects")

# 注入上下文标签：不属于用户真实表达，必须剥掉，否则相似度会被系统性抬高
INJECTED_TAGS = (
    "system-reminder", "identity_context", "product_identity", "tone_and_style",
    "project_context", "connector-status", "craft_mode", "working_modes",
    "agent_loop", "result_presentation", "sharing_files", "final_answer_instructions",
    "tool_use", "search_routing", "memory_and_skills_reminder", "user_info",
    "additional-data", "current_time", "wecom_connector_off", "agent_mail",
    "binary_context", "memory_system", "instructions_for_visualizer",
)

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{6,}"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{8,}", re.I),
    re.compile(r"(?i)(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S+"),
]

STRONG_CORRECTIONS = ["不对", "不是这个意思", "我的意思是", "搞错了", "你错了", "理解错了",
                      "重来", "重新搞", "重新做", "重新写", "重做"]
CORRECTION_EXCLUDE = re.compile(r"是不是|不是吗|重新(运行|生成|安装|开始|打开|启动|构建|编译|下载|扫描)")

QUESTION_TAIL = re.compile(r"[?？]\s*$|[吗呢吧]\s*[?？!！。．.~～]?\s*$")

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "create_file", "apply_patch"}
PATH_KEYS = ("file_path", "path", "target_file", "notebook_path", "filename")

MIN_TURN_LEN = 12          # 过短的回复（"可以"/"好的"）不参与重述判定
PREVIEW_USER = 200
PREVIEW_ASSISTANT = 300
MIN_SIMILARITY = 0.6


def mask_secrets(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def home_collapse(text: str) -> str:
    """Windows 下记录里的 cwd 可能是 c:\\Users\\...（小写盘符），需大小写不敏感替换。"""
    home = os.path.expanduser("~")
    if not home:
        return text
    variants = {home, home.replace("\\", "/")}
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            text = re.sub(re.escape(variant), "~", text, flags=re.I)
    return text


def to_iso(timestamp):
    """记录里的 timestamp 是毫秒 epoch，契约要求 ISO8601。"""
    if timestamp is None:
        return None
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return str(timestamp)
    if value > 1e12:
        value /= 1000.0
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def clean(text: str) -> str:
    text = mask_secrets(text or "")
    text = home_collapse(text)
    return text


def block_text(content) -> str:
    """message.content 可能是 str，也可能是 [{'type': 'input_text', 'text': ...}]。"""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "input_text", "output_text", "content"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
    return "\n".join(parts)


def strip_injected(text: str) -> str:
    """剥离宿主注入的上下文，只保留用户真实输入。"""
    if not text:
        return ""
    query = re.search(r"<user_query>(.*?)</user_query>", text, re.S)
    if query:
        return query.group(1).strip()
    for tag in INJECTED_TAGS:
        text = re.sub(r"<%s\b[^>]*>.*?</%s>" % (tag, tag), " ", text, flags=re.S)
        text = re.sub(r"<%s\b[^>]*/>" % tag, " ", text)
    text = re.sub(r"<[^>]{1,60}>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set:
    """英文按单词、中文按字符 bigram，避免依赖分词库。"""
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    cjk = re.findall(r"[一-鿿]", lowered)
    grams = {"".join(cjk[i:i + 2]) for i in range(len(cjk) - 1)}
    return words | grams


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return max(jac, ratio)


def parse_args_blob(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return {}


def normalize_path(value: str) -> str:
    """仅用作计数键；展示用 display_path，避免把路径显示成全小写。"""
    return str(value).replace("\\", "/").lower()


def display_path(value: str) -> str:
    return home_collapse(str(value).replace("\\", "/"))


def is_genuine_user(record: dict, raw_text: str) -> bool:
    """真实用户输入带 startsNewUserRequest；工具通知/压缩摘要带 isMeta 或特定文本。

    真实数据里 role=user 的记录有相当比例是后台命令输出与任务通知，
    不过滤会把它们当成用户提问，直接摧毁重述检测与轮次统计。
    """
    provider = record.get("providerData") or {}
    if provider.get("isMeta"):
        return False
    if provider.get("startsNewUserRequest") is True:
        return True
    if provider.get("startsNewUserRequest") is False:
        return False
    # 旧格式没有 providerData 标记时的文本兜底
    if "<task-notification>" in raw_text:
        return False
    if raw_text.lstrip().startswith("Summary of the conversation"):
        return False
    return True


def scan_file(path: str, max_chars: int, sim_threshold: float) -> dict:
    messages = []        # (role, cleaned_text, raw_user_text)
    ai_title = None
    cwd = ""
    started_at = None
    calls = []           # (name, args_dict, call_id)
    results = {}         # call_id -> status
    parse_errors = 0
    meta_filtered = 0

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                parse_errors += 1
                continue
            rtype = record.get("type")
            if rtype == "message":
                role = record.get("role")
                if role not in ("user", "assistant"):
                    continue
                if started_at is None:
                    started_at = record.get("timestamp")
                if not cwd:
                    cwd = record.get("cwd") or ""
                raw = block_text(record.get("content"))
                if role == "user" and not is_genuine_user(record, raw):
                    meta_filtered += 1
                    continue
                text = strip_injected(raw) if role == "user" else raw.strip()
                messages.append((role, text))
            elif rtype == "ai-title":
                ai_title = record.get("aiTitle") or ai_title
                if started_at is None:
                    started_at = record.get("timestamp")
            elif rtype == "function_call":
                calls.append((record.get("name") or "", parse_args_blob(record.get("arguments")),
                              record.get("callId") or record.get("id") or ""))
            elif rtype == "function_call_result":
                cid = record.get("callId") or ""
                results[cid] = str(record.get("status") or "unknown")
            # reasoning / file-history-snapshot / session-meta 直接跳过

    users = [text for role, text in messages if role == "user"]
    assistants = [text for role, text in messages if role == "assistant"]

    # --- 信号 1：AI 提问轮次（机械，不等于澄清，由模型层判读） ---
    ai_question_turns = sum(1 for text in assistants if text and QUESTION_TAIL.search(text[-160:]))

    # --- 信号 2：重述候选对（机械初筛，模型层裁决） ---
    restatement_pairs = []
    for i in range(1, len(users)):
        prev, cur = users[i - 1], users[i]
        if len(prev) < MIN_TURN_LEN or len(cur) < MIN_TURN_LEN:
            continue
        sim = similarity(prev, cur)
        if sim >= sim_threshold:
            restatement_pairs.append({"turns": [i - 1, i], "similarity": round(sim, 3)})

    # --- 信号 3：纠正命中（强信号词 + 排除模式） ---
    correction_hits = []
    for i, text in enumerate(users):
        if not text or CORRECTION_EXCLUDE.search(text):
            continue
        for word in STRONG_CORRECTIONS:
            if word in text:
                correction_hits.append({"turn": i, "keyword": word})
                break

    # --- 信号 4：工具失败后的同名同参重试 ---
    tool_retry_events = 0
    for i in range(1, len(calls)):
        prev_name, prev_args, prev_cid = calls[i - 1]
        name, args, _ = calls[i]
        if name != prev_name or args != prev_args:
            continue
        status = results.get(prev_cid, "")
        if status and status != "completed":
            tool_retry_events += 1

    # --- 背景指标：文件重复编辑（不直接计为返工） ---
    edit_counter = Counter()
    edit_display = {}
    for name, args, _ in calls:
        if name.lower() not in EDIT_TOOLS:
            continue
        for key in PATH_KEYS:
            if args.get(key):
                norm = normalize_path(args[key])
                edit_counter[norm] += 1
                edit_display.setdefault(norm, display_path(args[key]))
                break
    file_reedit_count = sum(count - 1 for count in edit_counter.values() if count > 1)
    files_reedited = [{"file": edit_display[f], "edits": c}
                      for f, c in edit_counter.most_common(5) if c > 1]

    # --- 状态：未竟（仅提示） ---
    last_role = messages[-1][0] if messages else None
    turns = len(users)
    unfinished = bool(last_role == "user" or (not ai_title and turns < 3))

    # --- 摘录（受 max_chars 约束） ---
    user_turns, assistant_summaries = [], []
    budget = max_chars
    for i, text in enumerate(users):
        preview = clean(text)[:PREVIEW_USER]
        if budget - len(preview) < 0:
            break
        budget -= len(preview)
        user_turns.append({"index": i, "text_preview": preview})
    for text in assistants:
        preview = clean(re.sub(r"\s+", " ", text))[:PREVIEW_ASSISTANT]
        if budget - len(preview) < 0:
            break
        budget -= len(preview)
        assistant_summaries.append(preview)

    return {
        "session_id": os.path.splitext(os.path.basename(path))[0],
        "ai_title": clean(ai_title) if ai_title else None,
        "cwd": home_collapse(cwd),
        "started_at": to_iso(started_at),
        "turns": turns,
        "signals": {
            "ai_question_turns": ai_question_turns,
            "restatement_candidates": len(restatement_pairs),
            "correction_hits": len(correction_hits),
            "tool_retry_events": tool_retry_events,
            "file_reedit_count": file_reedit_count,
            "unfinished": unfinished,
        },
        "restatement_pairs": restatement_pairs,
        "correction_details": correction_hits,
        "files_reedited": files_reedited,
        "user_turns": user_turns,
        "assistant_summaries": assistant_summaries,
        "meta_messages_filtered": meta_filtered,
        "parse_errors": parse_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="glean :: 提取最近 N 次会话的返工信号")
    parser.add_argument("--last", type=int, default=3)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--all", dest="all", action="store_true", default=True)
    parser.add_argument("--no-all", dest="all", action="store_false")
    parser.add_argument("--since", default=None)
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--current-session", default=None)
    parser.add_argument("--include-current", action="store_true")
    parser.add_argument("--recent-exclude-minutes", type=float, default=5.0)
    parser.add_argument("--sim-threshold", type=float, default=MIN_SIMILARITY)
    parser.add_argument("--out", default=None)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    now = time.time()
    candidates = []
    for entry in os.listdir(args.root):
        directory = os.path.join(args.root, entry)
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(directory, name)
            try:
                candidates.append((os.path.getmtime(full), full))
            except OSError:
                continue
    candidates.sort(reverse=True)

    sessions, skipped = [], []
    since_ts = None
    if args.since:
        since_ts = datetime.fromisoformat(args.since).timestamp()
    local_cwd = os.getcwd().replace("\\", "/").lower()

    for mtime, path in candidates:
        if len(sessions) >= args.last:
            break
        session_id = os.path.splitext(os.path.basename(path))[0]
        if since_ts and mtime < since_ts:
            continue
        if not args.include_current:
            if args.current_session and session_id == args.current_session:
                skipped.append({"session_id": session_id, "reason": "current-session"})
                continue
            if (now - mtime) < args.recent_exclude_minutes * 60:
                skipped.append({"session_id": session_id, "reason": "recently-active"})
                continue
        try:
            data = scan_file(path, args.max_chars, args.sim_threshold)
        except OSError as exc:
            skipped.append({"session_id": session_id, "reason": str(exc)})
            continue
        if not args.all and local_cwd not in data["cwd"].replace("\\", "/").lower():
            skipped.append({"session_id": session_id, "reason": "other-workspace"})
            continue
        if data["turns"] == 0:
            skipped.append({"session_id": session_id, "reason": "no-user-turns"})
            continue
        sessions.append(data)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"last": args.last, "all": args.all, "since": args.since,
                   "sim_threshold": args.sim_threshold, "include_current": args.include_current},
        "sessions": sessions,
        "skipped": skipped,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text)

    total_errors = sum(s["parse_errors"] for s in sessions)
    print("scanned=%d skipped=%d parse_errors=%d" % (len(sessions), len(skipped), total_errors),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
