#!/usr/bin/env python3
"""chat2code — turn a claude.ai chat export into a Claude Code session you can resume.

Claude Code keeps every conversation as a JSONL file under ~/.claude/projects/<project-dir>/<session-id>.jsonl.
When you run `claude --resume <session-id>` from that project directory, Claude Code reads the file back and hands
the messages to the model as the conversation so far. This script writes such a file from your own chat history,
so a conversation that happened on claude.ai (or anywhere else, via a plain Markdown transcript) continues in
Claude Code with its full wording intact — nothing is summarised unless you supply the summary yourself.

Only the standard library is used. Tested with Claude Code 2.1.281 on Windows; the JSONL layout is an internal
format and may change between versions (see README).

Usage (see README for details):
    python chat2code.py --input conversations.json --list
    python chat2code.py --input conversations.json --conversation "My chat" --cwd /path/to/project
    python chat2code.py --input transcript.md --cwd /path/to/project --summary notes.md --all
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import uuid as uuidlib
from pathlib import Path

HEADER_TEMPLATE = (
    "This session continues a conversation that was imported from a chat export with chat2code "
    "(https://github.com/{repo}).\n\n"
    "Source: {source}\n"
    "Original conversation: {name}\n"
    "Messages in the original: {total}; messages included verbatim below: {included} "
    "(from {first_ts} to {last_ts}).\n"
    "{dropped_note}"
    "\n{summary_block}"
    "The messages after this header are the original messages, kept verbatim, in their original order. "
    "They are the user's own record of the conversation and were not generated here.\n"
)

ROLE_WORDS = {
    "user": "user", "human": "user", "你": "user", "我": "user",
    "assistant": "assistant", "claude": "assistant", "ai": "assistant",
}
MD_HEADER = re.compile(r"^(?:#{1,6}\s*|\*\*)?\s*(user|human|assistant|claude)\s*(?:\*\*)?\s*[:：]?\s*(?:\*\*)?\s*$", re.I)


def project_dir_name(cwd: str) -> str:
    """Claude Code maps a working directory to a folder name by replacing every character that is not
    A-Z, a-z or 0-9 with '-'.  C:\\Users\\me\\My project -> C--Users-me-My-project ; /home/me/x -> -home-me-x"""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def detect_claude_version(default: str) -> str:
    exe = shutil.which("claude")
    if not exe:
        return default
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        return m.group(1) if m else default
    except Exception:
        return default


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def parse_ts(value, fallback: dt.datetime) -> dt.datetime:
    if not value:
        return fallback
    try:
        s = str(value).replace("Z", "+00:00")
        t = dt.datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return fallback


# ----------------------------------------------------------------------------- readers

def read_claude_ai_export(path: Path) -> list[dict]:
    """conversations.json from claude.ai → Settings → Privacy → Export data. Returns a list of conversations,
    each {"name", "uuid", "messages": [{"role", "text", "ts"}]}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    convs = data if isinstance(data, list) else [data]
    out = []
    for c in convs:
        msgs = []
        for m in c.get("chat_messages") or []:
            role = "user" if str(m.get("sender", "")).lower() in ("human", "user") else "assistant"
            text = m.get("text") or ""
            if not text and isinstance(m.get("content"), list):
                text = "\n\n".join(b.get("text", "") for b in m["content"] if isinstance(b, dict) and b.get("type") == "text")
            text = (text or "").strip()
            if not text:
                continue
            msgs.append({"role": role, "text": text, "ts": m.get("created_at")})
        out.append({"name": c.get("name") or "(untitled)", "uuid": c.get("uuid") or "", "created_at": c.get("created_at"), "messages": msgs})
    return out


def read_markdown_transcript(path: Path) -> list[dict]:
    """A plain transcript: a header line naming the speaker, then the message text until the next header.
    Accepted headers (case-insensitive, optional #/** decoration, optional colon):  User:  Human:  Assistant:  Claude:
    An optional ISO timestamp may follow on the same line as '@ 2026-01-02T03:04:05Z'."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    msgs, cur = [], None
    for line in lines:
        head = line
        ts = None
        m_ts = re.search(r"@\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)\s*$", line)
        if m_ts:
            ts = m_ts.group(1).replace(" ", "T"); head = line[: m_ts.start()]
        m = MD_HEADER.match(head.strip())
        if m:
            if cur and cur["text"].strip():
                msgs.append(cur)
            cur = {"role": ROLE_WORDS[m.group(1).lower()], "text": "", "ts": ts}
            continue
        if cur is None:
            continue
        cur["text"] += line + "\n"
    if cur and cur["text"].strip():
        msgs.append(cur)
    for mm in msgs:
        mm["text"] = mm["text"].strip()
    return [{"name": path.stem, "uuid": "", "created_at": None, "messages": msgs}]


# ----------------------------------------------------------------------------- builder

def merge_alternating(msgs: list[dict]) -> list[dict]:
    """The model API needs strictly alternating user/assistant turns; consecutive messages from the same side are
    joined with a blank line (wording unchanged)."""
    out: list[dict] = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["text"] += "\n\n" + m["text"]
            out[-1]["ts_end"] = m.get("ts")
        else:
            out.append({"role": m["role"], "text": m["text"], "ts": m.get("ts"), "ts_end": m.get("ts")})
    return out


def select_tail(msgs: list[dict], max_chars: int | None) -> list[dict]:
    if max_chars is None:
        return msgs
    total, i = 0, len(msgs)
    while i > 0 and total + len(msgs[i - 1]["text"]) <= max_chars:
        total += len(msgs[i - 1]["text"]); i -= 1
    return msgs[i:]


def build_records(conv: dict, *, cwd: str, session_id: str, version: str, model: str, summary_text: str,
                  max_chars: int | None, source_label: str, repo: str, entrypoint: str) -> list[dict]:
    all_msgs = merge_alternating(conv["messages"])
    if not all_msgs:
        raise SystemExit("no messages found in the input")
    tail = select_tail(all_msgs, max_chars)
    # the header record plays the role of the user's first turn, so the verbatim part must start with the assistant;
    # a leading user message is quoted inside the header instead of being dropped.
    lead_user = ""
    if tail and tail[0]["role"] == "user":
        lead_user = tail[0]["text"]; tail = tail[1:]
    if not tail:
        raise SystemExit("too little text to import; raise --max-chars or use --all")
    now = dt.datetime.now(dt.timezone.utc)
    base = parse_ts(tail[0].get("ts"), now) - dt.timedelta(seconds=1)
    first_ts = iso(parse_ts(tail[0].get("ts"), now)); last_ts = iso(parse_ts(tail[-1].get("ts_end") or tail[-1].get("ts"), now))
    dropped = len(all_msgs) - len(tail) - (1 if lead_user else 0)
    dropped_note = (f"The {dropped} earliest messages were left out to fit the context window; they remain in the export file.\n" if dropped > 0 else "")
    summary_block = ""
    if summary_text.strip():
        summary_block += "Notes supplied by the user to go with the import:\n\n" + summary_text.strip() + "\n\n"
    if lead_user:
        summary_block += "The earliest included message, from the user, verbatim:\n\n" + lead_user + "\n\n"
    header = HEADER_TEMPLATE.format(repo=repo, source=source_label, name=conv["name"], total=len(conv["messages"]),
                                    included=len(tail) + (1 if lead_user else 0), first_ts=first_ts, last_ts=last_ts,
                                    dropped_note=dropped_note, summary_block=summary_block)
    common = {"isSidechain": False, "userType": "external", "entrypoint": entrypoint, "cwd": cwd,
              "sessionId": session_id, "version": version, "gitBranch": "HEAD"}
    records: list[dict] = []
    parent = None
    uid = str(uuidlib.uuid4())
    records.append({"parentUuid": parent, "type": "user", "message": {"role": "user", "content": header},
                    "isVisibleInTranscriptOnly": True, "isCompactSummary": True, "uuid": uid, "timestamp": iso(base), **common})
    parent = uid
    last_t = base
    for n, m in enumerate(tail, 1):
        t = parse_ts(m.get("ts"), last_t + dt.timedelta(seconds=1))
        if t <= last_t:
            t = last_t + dt.timedelta(milliseconds=1)
        last_t = t
        uid = str(uuidlib.uuid4())
        if m["role"] == "user":
            rec = {"parentUuid": parent, "type": "user", "message": {"role": "user", "content": m["text"]},
                   "uuid": uid, "timestamp": iso(t), **common}
        else:
            rec = {"parentUuid": parent, "type": "assistant",
                   "message": {"model": model, "id": f"msg_import_{n:06d}", "type": "message", "role": "assistant",
                               "content": [{"type": "text", "text": m["text"]}], "stop_reason": "end_turn", "stop_sequence": None,
                               "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
                   "requestId": f"req_import_{n:06d}", "uuid": uid, "timestamp": iso(t), **common}
        records.append(rec); parent = uid
    return records


# ----------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="conversations.json from the claude.ai data export, or a Markdown transcript")
    ap.add_argument("--list", action="store_true", help="list the conversations in the input and exit")
    ap.add_argument("--conversation", help="name or uuid (prefix ok) of the conversation to import; required when the file holds several")
    ap.add_argument("--cwd", default=os.getcwd(), help="project directory the session should belong to (default: current directory)")
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"), help="Claude Code config dir (default ~/.claude)")
    ap.add_argument("--summary", help="a text/markdown file with your own notes (e.g. a journal) to put in the header record; optional")
    ap.add_argument("--max-chars", type=int, default=240_000, help="keep only the most recent messages up to this many characters (default 240000, roughly 60-120k tokens)")
    ap.add_argument("--all", action="store_true", help="include every message (for 1M-context models; large histories compact immediately on 200k models)")
    ap.add_argument("--model", default="claude-opus-4-6", help="model id stamped on the imported assistant turns")
    ap.add_argument("--code-version", default=None, help="Claude Code version to stamp (default: from `claude --version`)")
    ap.add_argument("--entrypoint", default="cli", help="entrypoint field (cli or claude-desktop)")
    ap.add_argument("--session-id", default=None, help="use this session id instead of a fresh uuid4")
    ap.add_argument("--out", default=None, help="write the JSONL here instead of into the projects folder (dry run)")
    ap.add_argument("--repo", default="s1014105837-afk/claude-chat-to-claude-code-session", help="repo url slug written into the header")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"input not found: {src}")
    if src.suffix.lower() == ".json":
        convs = read_claude_ai_export(src); source_label = f"claude.ai data export ({src.name})"
    else:
        convs = read_markdown_transcript(src); source_label = f"Markdown transcript ({src.name})"

    if args.list or (len(convs) > 1 and not args.conversation):
        for c in convs:
            print(f"{c['uuid'][:8] or '--------'}  {len(c['messages']):6d} msgs  {c.get('created_at') or '':25s}  {c['name']}")
        if not args.list:
            print("\nseveral conversations found; pick one with --conversation <name or uuid prefix>", file=sys.stderr)
            return 2
        return 0
    conv = convs[0]
    if args.conversation:
        hits = [c for c in convs if c["name"] == args.conversation or (c["uuid"] and c["uuid"].startswith(args.conversation))]
        if len(hits) != 1:
            raise SystemExit(f"--conversation matched {len(hits)} conversations; use the uuid prefix")
        conv = hits[0]

    cwd = str(Path(args.cwd).resolve())
    session_id = args.session_id or str(uuidlib.uuid4())
    version = args.code_version or detect_claude_version("2.1.281")
    summary_text = Path(args.summary).read_text(encoding="utf-8", errors="replace") if args.summary else ""
    records = build_records(conv, cwd=cwd, session_id=session_id, version=version, model=args.model, summary_text=summary_text,
                            max_chars=None if args.all else args.max_chars, source_label=source_label, repo=args.repo, entrypoint=args.entrypoint)

    if args.out:
        out = Path(args.out)
    else:
        out = Path(args.claude_home) / "projects" / project_dir_name(cwd) / f"{session_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise SystemExit(f"refusing to overwrite existing file: {out}")
    with open(out, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    chars = sum(len(r["message"]["content"]) if isinstance(r["message"]["content"], str) else len(r["message"]["content"][0]["text"]) for r in records)
    print(f"wrote {out}")
    print(f"records: {len(records)} (1 header + {len(records) - 1} messages), {chars} characters")
    print(f"project dir: {project_dir_name(cwd)}" + ("  (dry run: written to --out instead)" if args.out else ""))
    print("\nresume it with:")
    print(f"  cd \"{cwd}\"")
    print(f"  claude --resume {session_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
