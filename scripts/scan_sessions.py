#!/usr/bin/env python3
"""glean :: scan_sessions.py

从会话转录中提取返工信号，供模型层做归因与三层路由。

数据源无关：通过 adapter 支持多种宿主格式，也支持直接喂文件 / stdin。

  workbuddy    ~/.workbuddy/projects/**/*.jsonl
  claude-code  ~/.claude/projects/**/*.jsonl
  text         .md / .txt 转录（"User:" / "Assistant:" 分段），或 stdin

设计约束：
  - 只做可复现的机械计数与摘录，语义判断全部交给模型层。
  - 默认排除当前进行中会话。
  - 输出不写入任何台账；脱敏在摘录阶段完成。

用法：
  python scan_sessions.py --last 3 --pretty --out scan.json
  python scan_sessions.py --source claude-code --last 5
  python scan_sessions.py --input chat.md
  cat chat.txt | python scan_sessions.py --input -
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

DEFAULT_ROOTS = {
    "workbuddy": os.path.join(os.path.expanduser("~"), ".workbuddy", "projects"),
    "claude-code": os.path.join(os.path.expanduser("~"), ".claude", "projects"),
}
SUPPORTED_SOURCES = ("workbuddy", "claude-code", "text")

# 宿主注入的上下文，不属于用户真实表达，必须剥掉，否则相似度会被系统性抬高
INJECTED_TAGS = (
    "system-reminder", "identity_context", "product_identity", "tone_and_style",
    "project_context", "connector-status", "craft_mode", "working_modes",
    "agent_loop", "result_presentation", "sharing_files", "final_answer_instructions",
    "tool_use", "search_routing", "memory_and_skills_reminder", "user_info",
    "additional-data", "current_time", "wecom_connector_off", "agent_mail",
    "binary_context", "memory_system", "instructions_for_visualizer",
    "task-notification", "ide_opened_file", "ide_selection", "command-name",
    "command-message", "command-args", "local-command-stdout", "local-command-stderr",
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

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "create_file", "apply_patch",
              "str_replace_editor", "str_replace_based_edit_tool"}

PATH_KEYS = ("file_path", "path", "target_file", "notebook_path", "filename")
TEXT_BLOCK_TYPES = ("text", "input_text", "output_text")

MIN_TURN_LEN = 12
MAX_USER_CHARS = 3000     # 超长用户轮次常是历史拼接，截断后仍需在报告里标注
PREVIEW_USER = 200
PREVIEW_ASSISTANT = 300
MIN_SIMILARITY = 0.6
PROBE_LINES = 200


# ---------------------------------------------------------------- 通用工具

def mask_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def home_collapse(text: str) -> str:
    """Windows 记录的 cwd 可能是小写盘符，需大小写不敏感替换。"""
    home = os.path.expanduser("~")
    if not home:
        return text
    for variant in sorted({home, home.replace("\\", "/")}, key=len, reverse=True):
        if variant:
            text = re.sub(re.escape(variant), "~", text, flags=re.I)
    return text


def clean(text: str) -> str:
    return home_collapse(mask_secrets(text or ""))


def to_iso(timestamp):
    """兼容毫秒 epoch 与已有 ISO 字符串。"""
    if timestamp is None:
        return None
    if isinstance(timestamp, str):
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).isoformat(timespec="seconds")
        except ValueError:
            return timestamp
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return str(timestamp)
    if value > 1e12:
        value /= 1000.0
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def block_text(content, text_types=TEXT_BLOCK_TYPES) -> str:
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") not in text_types:
                    continue
                for key in ("text", "content"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
    return "\n".join(parts)


def strip_injected(text: str) -> str:
    if not text:
        return ""
    query = re.search(r"<user_query>(.*?)</user_query>", text, re.S)
    if query:
        return query.group(1).strip()
    original = text
    for tag in INJECTED_TAGS:
        text = re.sub(r"<%s\b[^>]*>.*?</%s>" % (tag, tag), " ", text, flags=re.S)
        text = re.sub(r"<%s\b[^>]*/>" % tag, " ", text)
    if text == original:
        # 未命中注入标签就原样返回，避免误删用户输入里的尖括号
        return text.strip()
    text = re.sub(r"<[^>]{1,60}>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set:
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
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def normalize_path(value: str) -> str:
    return str(value).replace("\\", "/").lower()


def display_path(value: str) -> str:
    return home_collapse(str(value).replace("\\", "/"))


# ---------------------------------------------------------------- adapter

class RawSession:
    """adapter 的统一输出。events 按时间顺序存放三类事件。"""

    def __init__(self, source, path):
        self.source = source
        self.path = path
        self.session_id = os.path.splitext(os.path.basename(path))[0]
        self.title = None
        self.cwd = ""
        self.started_at = None
        self.messages = []      # [(role, text)]
        self.calls = []         # [(name, args_dict, call_id)]
        self.results = {}       # call_id -> failed(bool)
        self.filtered = 0
        self.truncated = 0      # 被 MAX_USER_CHARS 截断的轮次数


def parse_workbuddy(path):
    session = RawSession("workbuddy", path)
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            rtype = record.get("type")
            if rtype == "message":
                role = record.get("role")
                if role not in ("user", "assistant"):
                    continue
                raw = block_text(record.get("content"))
                if role == "user" and not is_workbuddy_user(record, raw):
                    session.filtered += 1
                    continue
                session.started_at = session.started_at or record.get("timestamp")
                session.cwd = session.cwd or (record.get("cwd") or "")
                text = strip_injected(raw) if role == "user" else raw.strip()
                session.messages.append((role, text))
            elif rtype == "ai-title":
                session.title = session.title or record.get("aiTitle")
            elif rtype == "function_call":
                session.calls.append((record.get("name") or "",
                                      parse_args_blob(record.get("arguments")),
                                      record.get("callId") or record.get("id") or ""))
            elif rtype == "function_call_result":
                cid = record.get("callId") or ""
                status = str(record.get("status") or "")
                session.results[cid] = bool(status and status != "completed")
    return session


def is_workbuddy_user(record, raw_text):
    provider = record.get("providerData") or {}
    if provider.get("isMeta"):
        return False
    if provider.get("startsNewUserRequest") is True:
        return True
    if provider.get("startsNewUserRequest") is False:
        return False
    if "<task-notification>" in raw_text:
        return False
    if raw_text.lstrip().startswith("Summary of the conversation"):
        return False
    return True


def parse_claude_code(path):
    session = RawSession("claude-code", path)
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            rtype = record.get("type")
            if rtype == "ai-title":
                session.title = session.title or record.get("aiTitle")
                continue
            if rtype not in ("user", "assistant"):
                continue
            if record.get("isSidechain") or record.get("isMeta"):
                session.filtered += 1
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            role = message.get("role") or rtype
            session.started_at = session.started_at or record.get("timestamp")
            session.cwd = session.cwd or (record.get("cwd") or "")

            if role == "assistant":
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        session.calls.append((block.get("name") or "",
                                              block.get("input") if isinstance(block.get("input"), dict) else {},
                                              block.get("id") or ""))
                text = block_text(content, ("text",)).strip()
                if text:
                    session.messages.append(("assistant", text))
            else:
                blocks = content if isinstance(content, list) else []
                results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
                for block in results:
                    session.results[block.get("tool_use_id") or ""] = bool(block.get("is_error"))
                # 逐 block 清洗：IDE 注入常独占一个 text block，混在一起会污染用户输入
                texts = []
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        cleaned = strip_injected(block.get("text") or "")
                        if cleaned:
                            texts.append(cleaned)
                text = "\n".join(texts).strip()
                if not text:
                    session.filtered += 1
                    continue
                if len(text) > MAX_USER_CHARS:
                    text = text[:MAX_USER_CHARS]
                    session.truncated += 1
                session.messages.append(("user", text))
    return session


ROLE_MARKER = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*\s*)?(user|human|assistant|ai|我|用户|助手|提问|回答)"
    r"\s*(?:\*\*)?\s*(?:[:：]\s*(.*))?$",
    re.I,
)
ROLE_MAP = {"user": "user", "human": "user", "我": "user", "用户": "user", "提问": "user",
            "assistant": "assistant", "ai": "assistant", "助手": "assistant", "回答": "assistant"}


def parse_text(path):
    """通用转录：按 'User:' / 'Assistant:' / '我：' / '助手：' 分段。"""
    session = RawSession("text", path)
    if path == "-":
        lines = sys.stdin.read().splitlines()
    else:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    current, buffer = None, []
    for line in lines:
        match = ROLE_MARKER.match(line)
        if match:
            if current and buffer:
                session.messages.append((current, "\n".join(buffer).strip()))
            current = ROLE_MAP.get(match.group(1).lower())
            buffer = [re.sub(r"^\*\*\s*", "", match.group(2) or "")]
        elif current:
            buffer.append(line)
    if current and buffer:
        session.messages.append((current, "\n".join(buffer).strip()))
    return session


PARSERS = {"workbuddy": parse_workbuddy, "claude-code": parse_claude_code, "text": parse_text}


def detect_source(path):
    """按内容特征探测，不依赖目录名。"""
    if path == "-":
        return "text"
    if path.lower().endswith((".md", ".markdown", ".txt")):
        return "text"
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= PROBE_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    return "text"
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "message" and "providerData" in record:
                    return "workbuddy"
                if record.get("type") in ("user", "assistant") and isinstance(record.get("message"), dict):
                    return "claude-code"
    except OSError:
        return None
    return None


# ---------------------------------------------------------------- 信号计算

def looks_mixed(text: str) -> bool:
    """部分宿主会把历史 assistant 输出拼进 user 消息。不粗暴截断，只标记，交模型判断。"""
    return len(text) > 800 and (text.count("\n#") > 0 or text.count("**") >= 4)


def analyze(session: RawSession, max_chars: int, sim_threshold: float) -> dict:
    users = [text for role, text in session.messages if role == "user"]
    assistants = [text for role, text in session.messages if role == "assistant"]
    suspect_mixed = sum(1 for text in users if looks_mixed(text))

    ai_question_turns = sum(1 for text in assistants if text and QUESTION_TAIL.search(text[-160:]))

    restatement_pairs = []
    for i in range(1, len(users)):
        prev, cur = users[i - 1], users[i]
        if len(prev) < MIN_TURN_LEN or len(cur) < MIN_TURN_LEN:
            continue
        sim = similarity(prev, cur)
        if sim >= sim_threshold:
            restatement_pairs.append({"turns": [i - 1, i], "similarity": round(sim, 3)})

    correction_hits = []
    for i, text in enumerate(users):
        if not text or CORRECTION_EXCLUDE.search(text):
            continue
        for word in STRONG_CORRECTIONS:
            if word in text:
                correction_hits.append({"turn": i, "keyword": word})
                break

    tool_retry_events = 0
    for i in range(1, len(session.calls)):
        prev_name, prev_args, prev_cid = session.calls[i - 1]
        name, args, _ = session.calls[i]
        if name != prev_name or args != prev_args:
            continue
        if session.results.get(prev_cid):
            tool_retry_events += 1

    edit_counter, edit_display = Counter(), {}
    for name, args, _ in session.calls:
        if name.lower() not in EDIT_TOOLS:
            continue
        for key in PATH_KEYS:
            if args.get(key):
                norm = normalize_path(args[key])
                edit_counter[norm] += 1
                edit_display.setdefault(norm, display_path(args[key]))
                break

    last_role = session.messages[-1][0] if session.messages else None
    turns = len(users)

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
        "session_id": session.session_id,
        "source": session.source,
        "ai_title": clean(session.title) if session.title else None,
        "cwd": home_collapse(session.cwd),
        "started_at": to_iso(session.started_at),
        "turns": turns,
        "signals": {
            "ai_question_turns": ai_question_turns,
            "restatement_candidates": len(restatement_pairs),
            "correction_hits": len(correction_hits),
            "tool_retry_events": tool_retry_events,
            "file_reedit_count": sum(c - 1 for c in edit_counter.values() if c > 1),
            "unfinished": bool(last_role == "user" or (not session.title and turns < 3)),
        },
        "restatement_pairs": restatement_pairs,
        "correction_details": correction_hits,
        "files_reedited": [{"file": edit_display[f], "edits": c}
                           for f, c in edit_counter.most_common(5) if c > 1],
        "user_turns": user_turns,
        "assistant_summaries": assistant_summaries,
        "meta_messages_filtered": session.filtered,
        "long_user_turns": session.truncated,
        "suspect_mixed_turns": suspect_mixed,
    }


# ---------------------------------------------------------------- CLI

def collect_candidates(pairs):
    """pairs: [(source, root)]，返回 [(mtime, path, source)]。"""
    candidates = []
    for source, root in pairs:
        if not root or not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            directory = os.path.join(root, entry)
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                if not name.endswith(".jsonl"):
                    continue
                full = os.path.join(directory, name)
                try:
                    candidates.append((os.path.getmtime(full), full, source))
                except OSError:
                    continue
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="glean :: 从会话转录提取返工信号（数据源无关）")
    parser.add_argument("--last", type=int, default=3)
    parser.add_argument("--source", choices=("auto",) + SUPPORTED_SOURCES, default="auto")
    parser.add_argument("--root", action="append", default=None,
                        help="自定义数据根目录，可重复；与 --source 配合")
    parser.add_argument("--input", action="append", default=None,
                        help="直接指定转录文件（.jsonl/.md/.txt），'-' 读 stdin")
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

    if args.input:
        candidates = [(float("inf"), item,
                       args.source if args.source != "auto" else detect_source(item))
                      for item in args.input]
    else:
        if args.source == "text":
            parser.error("--source text 需要配合 --input 使用")
        if args.root:
            source_for_root = args.source if args.source != "auto" else "workbuddy"
            pairs = [(source_for_root, root) for root in args.root]
        else:
            pairs = [(source, root) for source, root in DEFAULT_ROOTS.items()
                     if args.source in ("auto", source)]
        candidates = collect_candidates(pairs)
        candidates.sort(reverse=True)

    sessions, skipped = [], []
    since_ts = None
    if args.since:
        since_ts = datetime.fromisoformat(args.since).timestamp()
    local_cwd = os.getcwd().replace("\\", "/").lower()

    for mtime, path, source in candidates:
        if len(sessions) >= args.last:
            break
        if not path:
            continue
        if source is None:
            skipped.append({"session_id": os.path.basename(path), "reason": "unknown-format"})
            continue
        if source == "text" and path == "-":
            session_id = "stdin"
        else:
            session_id = os.path.splitext(os.path.basename(path))[0]

        if since_ts and path != "-" and mtime < since_ts:
            continue
        if not args.include_current and not args.input:
            if args.current_session and session_id == args.current_session:
                skipped.append({"session_id": session_id, "reason": "current-session"})
                continue
            if path != "-" and (time.time() - mtime) < args.recent_exclude_minutes * 60:
                skipped.append({"session_id": session_id, "reason": "recently-active"})
                continue
        try:
            raw = PARSERS[source](path)
        except OSError as exc:
            skipped.append({"session_id": session_id, "reason": str(exc)})
            continue
        data = analyze(raw, args.max_chars, args.sim_threshold)
        if not args.all and data["cwd"] and local_cwd not in data["cwd"].replace("\\", "/").lower():
            skipped.append({"session_id": session_id, "reason": "other-workspace"})
            continue
        if data["turns"] == 0:
            skipped.append({"session_id": session_id, "reason": "no-user-turns"})
            continue
        sessions.append(data)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"last": args.last, "source": args.source, "all": args.all,
                   "since": args.since, "sim_threshold": args.sim_threshold,
                   "include_current": args.include_current},
        "sessions": sessions,
        "skipped": skipped,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text)

    print("scanned=%d skipped=%d sources=%s" % (
        len(sessions), len(skipped), sorted({s["source"] for s in sessions})), file=sys.stderr)
    return 0


def time_now() -> float:
    import time
    return time.time()


if __name__ == "__main__":
    raise SystemExit(main())
