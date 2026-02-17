#!/usr/bin/env python3
"""
Enhanced Claude Code Insights Generator

Reimplements the /insights command with raised limits:
- No session cap (processes ALL sessions, not just 200)
- Higher token limits for summarization (2048 vs 500)
- Higher facet extraction limits (8192 vs 4096)
- Higher report generation limits (16384 vs 8192)
- Full transcript context (no 500/300 char truncation)
- More facets sent to report generation (200 vs 50)
- Uses existing OAuth tokens from Claude Code
- Reuses cached facets from ~/.claude/usage-data/facets/
"""

import argparse
import fcntl
import json
import os
import sys
import time
import glob
import re
import hashlib
import html
import tempfile
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

# ---------------------------------------------------------------------------
# Configuration — these are the knobs we're raising
# ---------------------------------------------------------------------------

# Model to use for all API calls
MODEL = "claude-opus-4-6"             # Opus 4.6 — highest quality (use --model to override)

# Token limits (originals in comments)
SUMMARIZE_MAX_TOKENS = 2048       # was 500
FACET_MAX_TOKENS = 8192           # was 4096
REPORT_SECTION_MAX_TOKENS = 16384 # was 8192

# Data limits (originals in comments)
MAX_SESSIONS_TO_PROCESS = 9999    # was 200
MAX_FACETS_FOR_REPORT = 200       # was 50
MAX_FRICTION_DETAILS = 50         # was 20
MAX_USER_INSTRUCTIONS = 30        # was 15
MAX_SESSION_SUMMARIES = 100       # was 50
MAX_TOP_ITEMS = 15                # was 8

# Transcript limits
USER_MSG_TRUNCATE = 2000          # was 500
ASSISTANT_MSG_TRUNCATE = 1000     # was 300
LONG_SESSION_THRESHOLD = 60000    # was 30000
CHUNK_SIZE = 50000                # was 25000

# Parallelism
FACET_BATCH_SIZE = 5              # parallel facet extractions per batch
FACET_BATCH_DELAY = 1.0           # seconds between batches (rate limit protection)
REPORT_PARALLELISM = 7

# Paths
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
CREDENTIALS_FILE = CLAUDE_DIR / ".credentials.json"
USAGE_DATA_DIR = CLAUDE_DIR / "usage-data"
FACETS_DIR = USAGE_DATA_DIR / "facets"
SESSION_META_DIR = USAGE_DATA_DIR / "session-meta"
OUTPUT_DIR = Path(os.environ.get("INSIGHTS_OUTPUT_DIR", USAGE_DATA_DIR / "enhanced"))

# OAuth
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers"

# Language mapping
LANG_MAP = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".py": "Python", ".rb": "Ruby", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".md": "Markdown", ".json": "JSON",
    ".yaml": "YAML", ".yml": "YAML", ".sh": "Shell", ".css": "CSS",
    ".html": "HTML", ".zig": "Zig", ".c": "C", ".cpp": "C++",
    ".h": "C/C++ Header", ".sql": "SQL", ".toml": "TOML",
}

# Display names for categories
DISPLAY_NAMES = {
    "debug_investigate": "Debug/Investigate", "implement_feature": "Implement Feature",
    "fix_bug": "Fix Bug", "write_script_tool": "Write Script/Tool",
    "refactor_code": "Refactor Code", "configure_system": "Configure System",
    "create_pr_commit": "Create PR/Commit", "analyze_data": "Analyze Data",
    "understand_codebase": "Understand Codebase", "write_tests": "Write Tests",
    "write_docs": "Write Docs", "deploy_infra": "Deploy/Infra",
    "warmup_minimal": "Cache Warmup", "fast_accurate_search": "Fast/Accurate Search",
    "correct_code_edits": "Correct Code Edits", "good_explanations": "Good Explanations",
    "proactive_help": "Proactive Help", "multi_file_changes": "Multi-file Changes",
    "good_debugging": "Good Debugging", "misunderstood_request": "Misunderstood Request",
    "wrong_approach": "Wrong Approach", "buggy_code": "Buggy Code",
    "user_rejected_action": "User Rejected Action", "claude_got_blocked": "Claude Got Blocked",
    "user_stopped_early": "User Stopped Early", "wrong_file_or_location": "Wrong File/Location",
    "excessive_changes": "Excessive Changes", "slow_or_verbose": "Slow/Verbose",
    "tool_failed": "Tool Failed", "frustrated": "Frustrated",
    "dissatisfied": "Dissatisfied", "likely_satisfied": "Likely Satisfied",
    "satisfied": "Satisfied", "happy": "Happy", "unsure": "Unsure",
    "neutral": "Neutral", "delighted": "Delighted",
    "single_task": "Single Task", "multi_task": "Multi Task",
    "iterative_refinement": "Iterative Refinement", "exploration": "Exploration",
    "quick_question": "Quick Question", "fully_achieved": "Fully Achieved",
    "mostly_achieved": "Mostly Achieved", "partially_achieved": "Partially Achieved",
    "not_achieved": "Not Achieved", "unclear_from_transcript": "Unclear",
    "unhelpful": "Unhelpful", "slightly_helpful": "Slightly Helpful",
    "moderately_helpful": "Moderately Helpful", "very_helpful": "Very Helpful",
    "essential": "Essential",
}


# ---------------------------------------------------------------------------
# OAuth Token Management
# ---------------------------------------------------------------------------

class OAuthManager:
    def __init__(self):
        self._token = None
        self._load()

    def _load(self):
        with open(CREDENTIALS_FILE) as f:
            creds = json.load(f)
        self._creds = creds.get("claudeAiOauth", {})
        self._token = self._creds.get("accessToken")
        self._refresh_token = self._creds.get("refreshToken")
        self._expires_at = self._creds.get("expiresAt", 0)

    def _is_expired(self):
        # expiresAt is in milliseconds
        return time.time() * 1000 > self._expires_at - 60000  # 1 min buffer

    def refresh(self):
        import httpx
        resp = httpx.post(TOKEN_URL, json={
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPES,
        }, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._expires_at = int(time.time() * 1000) + data["expires_in"] * 1000
        # Save back atomically with file locking
        with open(CREDENTIALS_FILE, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                all_creds = json.load(f)
                all_creds["claudeAiOauth"]["accessToken"] = self._token
                all_creds["claudeAiOauth"]["refreshToken"] = self._refresh_token
                all_creds["claudeAiOauth"]["expiresAt"] = self._expires_at
                # Write to temp file then rename for atomicity
                fd, tmp = tempfile.mkstemp(dir=CREDENTIALS_FILE.parent, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as tf:
                        json.dump(all_creds, tf)
                    os.chmod(tmp, 0o600)
                    os.rename(tmp, CREDENTIALS_FILE)
                except Exception:
                    os.unlink(tmp)
                    raise
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        print(f"  [auth] Token refreshed, expires in {data['expires_in']//60} min")

    @property
    def token(self):
        if self._is_expired():
            print("  [auth] Token expired, refreshing...")
            self.refresh()
        return self._token


OAUTH_BETA = "oauth-2025-04-20"

_json_decoder = json.JSONDecoder()


def extract_json_object(text):
    """Extract the first valid JSON object from text using incremental parsing.

    Handles nested braces correctly unlike regex approaches.
    """
    idx = text.find("{")
    if idx == -1:
        return None
    try:
        obj, _ = _json_decoder.raw_decode(text, idx)
        return obj
    except json.JSONDecodeError:
        return None


def create_client(oauth: OAuthManager) -> anthropic.Anthropic:
    """Create Anthropic client using OAuth token."""
    return anthropic.Anthropic(
        auth_token=oauth.token,
        max_retries=2,
        timeout=600.0,
    )


def api_create(client, **kwargs):
    """Call the messages API with OAuth beta flag."""
    return client.beta.messages.create(betas=[OAUTH_BETA], **kwargs)


# ---------------------------------------------------------------------------
# Session Discovery & Loading
# ---------------------------------------------------------------------------

UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
AGENT_RE = re.compile(r'^agent-[0-9a-f]+$', re.I)


def discover_sessions():
    """Find all JSONL session files across all projects, including subagent sessions."""
    sessions = []
    if not PROJECTS_DIR.exists():
        return sessions

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        try:
            for jsonl_file in project_dir.rglob("*.jsonl"):
                session_id = jsonl_file.stem
                is_subagent = "/subagents/" in str(jsonl_file)

                # Accept UUID-named files (main sessions) and agent-* files (subagents)
                if not UUID_RE.match(session_id) and not AGENT_RE.match(session_id):
                    continue

                try:
                    stat = jsonl_file.stat()
                except OSError:
                    continue
                sessions.append({
                    "session_id": session_id,
                    "path": str(jsonl_file),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "project": project_dir.name,
                    "is_subagent": is_subagent,
                })
        except PermissionError:
            continue

    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def load_session_messages(path):
    """Load and parse a JSONL session file into message list."""
    messages = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") in ("user", "assistant", "system"):
                        messages.append(obj)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"  [warn] Failed to load {path}: {e}")
    return messages


def is_insights_session(messages):
    """Check if this session was generated by /insights itself."""
    for msg in messages[:5]:
        if msg.get("type") == "user" and msg.get("message"):
            content = msg["message"].get("content", "")
            if isinstance(content, str):
                if "RESPOND WITH ONLY A VALID JSON OBJECT" in content or "record_facets" in content:
                    return True
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if "RESPOND WITH ONLY A VALID JSON OBJECT" in text or "record_facets" in text:
                            return True
    return False


# ---------------------------------------------------------------------------
# Per-Session Metric Extraction (equivalent to Awz + LFA)
# ---------------------------------------------------------------------------

def extract_session_metrics(messages, session_id, project_path):
    """Extract structured metrics from a session's messages."""
    tool_counts = {}
    languages = {}
    git_commits = 0
    git_pushes = 0
    input_tokens = 0
    output_tokens = 0
    interruptions = 0
    response_times = []
    tool_errors = 0
    tool_error_categories = {}
    uses_task = False
    uses_mcp = False
    uses_web_search = False
    uses_web_fetch = False
    lines_added = 0
    lines_removed = 0
    files_modified = set()
    message_hours = []
    user_message_timestamps = []
    user_msg_count = 0
    assistant_msg_count = 0
    last_assistant_ts = None
    first_prompt = ""
    summary = ""
    start_time = None
    end_time = None

    for msg in messages:
        ts = msg.get("timestamp")

        if msg["type"] == "assistant" and msg.get("message"):
            assistant_msg_count += 1
            if ts:
                last_assistant_ts = ts
            usage = msg["message"].get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)

            content = msg["message"].get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        name = item.get("name", "")
                        tool_counts[name] = tool_counts.get(name, 0) + 1
                        if name == "Task":
                            uses_task = True
                        if name.startswith("mcp__"):
                            uses_mcp = True
                        if name == "WebSearch":
                            uses_web_search = True
                        if name == "WebFetch":
                            uses_web_fetch = True

                        inp = item.get("input", {})
                        if isinstance(inp, dict):
                            fp = inp.get("file_path", "")
                            if fp:
                                ext = os.path.splitext(fp)[1].lower()
                                lang = LANG_MAP.get(ext)
                                if lang:
                                    languages[lang] = languages.get(lang, 0) + 1
                                if name in ("Edit", "Write"):
                                    files_modified.add(fp)
                            if name == "Write":
                                content_str = inp.get("content", "")
                                if content_str:
                                    lines_added += content_str.count("\n") + 1
                            if name == "Edit":
                                old = inp.get("old_string", "")
                                new = inp.get("new_string", "")
                                old_lines = old.count("\n") + 1 if old else 0
                                new_lines = new.count("\n") + 1 if new else 0
                                lines_added += max(0, new_lines - old_lines)
                                lines_removed += max(0, old_lines - new_lines)
                            cmd = inp.get("command", "")
                            if "git commit" in cmd:
                                git_commits += 1
                            if "git push" in cmd:
                                git_pushes += 1

        if msg["type"] == "user" and msg.get("message"):
            content = msg["message"].get("content", "")
            has_text = False
            text_content = ""

            if isinstance(content, str) and content.strip():
                has_text = True
                text_content = content
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text" and item.get("text", "").strip():
                            has_text = True
                            text_content = item["text"]
                            break
                        if item.get("type") == "tool_result" and item.get("is_error"):
                            tool_errors += 1
                            err_content = item.get("content", "")
                            if isinstance(err_content, str):
                                err_lower = err_content.lower()
                                if "exit code" in err_lower:
                                    cat = "Command Failed"
                                elif "rejected" in err_lower or "doesn't want" in err_lower:
                                    cat = "User Rejected"
                                elif "string to replace not found" in err_lower or "no changes" in err_lower:
                                    cat = "Edit Failed"
                                elif "modified since read" in err_lower:
                                    cat = "File Changed"
                                elif "exceeds maximum" in err_lower or "too large" in err_lower:
                                    cat = "File Too Large"
                                elif "file not found" in err_lower or "does not exist" in err_lower:
                                    cat = "File Not Found"
                                else:
                                    cat = "Other"
                                tool_error_categories[cat] = tool_error_categories.get(cat, 0) + 1

            if has_text:
                user_msg_count += 1
                if not first_prompt:
                    first_prompt = text_content[:200]
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        message_hours.append(dt.hour)
                        user_message_timestamps.append(ts)
                        if start_time is None:
                            start_time = dt
                        end_time = dt
                    except Exception:
                        pass
                    if last_assistant_ts:
                        try:
                            at = datetime.fromisoformat(last_assistant_ts.replace("Z", "+00:00"))
                            ut = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            delta = (ut - at).total_seconds()
                            if 2 < delta < 3600:
                                response_times.append(delta)
                        except Exception:
                            pass

            # Check for interruptions
            if isinstance(content, str) and "[Request interrupted by user" in content:
                interruptions += 1
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        if "[Request interrupted by user" in item.get("text", ""):
                            interruptions += 1
                            break

    duration_minutes = 0
    start_iso = ""
    if start_time and end_time:
        duration_minutes = max(1, int((end_time - start_time).total_seconds() / 60))
        start_iso = start_time.isoformat()

    return {
        "session_id": session_id,
        "project_path": project_path,
        "start_time": start_iso,
        "duration_minutes": duration_minutes,
        "user_message_count": user_msg_count,
        "assistant_message_count": assistant_msg_count,
        "tool_counts": tool_counts,
        "languages": languages,
        "git_commits": git_commits,
        "git_pushes": git_pushes,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "first_prompt": first_prompt,
        "summary": summary,
        "user_interruptions": interruptions,
        "user_response_times": response_times,
        "tool_errors": tool_errors,
        "tool_error_categories": tool_error_categories,
        "uses_task_agent": uses_task,
        "uses_mcp": uses_mcp,
        "uses_web_search": uses_web_search,
        "uses_web_fetch": uses_web_fetch,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "files_modified": len(files_modified),
        "message_hours": message_hours,
        "user_message_timestamps": user_message_timestamps,
    }


# ---------------------------------------------------------------------------
# Transcript Serialization (equivalent to Kwz)
# ---------------------------------------------------------------------------

def serialize_transcript(messages):
    """Serialize session messages into a text transcript for Claude."""
    lines = []
    for msg in messages:
        if msg["type"] == "user" and msg.get("message"):
            content = msg["message"].get("content", "")
            if isinstance(content, str):
                lines.append(f"[User]: {content[:USER_MSG_TRUNCATE]}")
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        lines.append(f"[User]: {item['text'][:USER_MSG_TRUNCATE]}")
        elif msg["type"] == "assistant" and msg.get("message"):
            content = msg["message"].get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            lines.append(f"[Assistant]: {item['text'][:ASSISTANT_MSG_TRUNCATE]}")
                        elif item.get("type") == "tool_use":
                            lines.append(f"[Tool: {item.get('name', '?')}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Facet Extraction (equivalent to Jwz)
# ---------------------------------------------------------------------------

FACET_SYSTEM_PROMPT = """Analyze this Claude Code session and extract structured facets.

CRITICAL GUIDELINES:

1. **goal_categories**: Count ONLY what the USER explicitly asked for.
   - DO NOT count Claude's autonomous codebase exploration
   - DO NOT count work Claude decided to do on its own
   - ONLY count when user says "can you...", "please...", "I need...", "let's..."

2. **user_satisfaction_counts**: Base ONLY on explicit user signals.
   - "Yay!", "great!", "perfect!" → happy
   - "thanks", "looks good", "that works" → satisfied
   - "ok, now let's..." (continuing without complaint) → likely_satisfied
   - "that's not right", "try again" → dissatisfied
   - "this is broken", "I give up" → frustrated

3. **friction_counts**: Be specific about what went wrong.
   - misunderstood_request: Claude interpreted incorrectly
   - wrong_approach: Right goal, wrong solution method
   - buggy_code: Code didn't work correctly
   - user_rejected_action: User said no/stop to a tool call
   - excessive_changes: Over-engineered or changed too much

4. If very short or just warmup, use warmup_minimal for goal_category

SESSION:
"""

FACET_SCHEMA = """\n\nRESPOND WITH ONLY A VALID JSON OBJECT matching this schema:
{
  "underlying_goal": "What the user fundamentally wanted to achieve",
  "goal_categories": {"category_name": count, ...},
  "outcome": "fully_achieved|mostly_achieved|partially_achieved|not_achieved|unclear_from_transcript",
  "user_satisfaction_counts": {"level": count, ...},
  "claude_helpfulness": "unhelpful|slightly_helpful|moderately_helpful|very_helpful|essential",
  "session_type": "single_task|multi_task|iterative_refinement|exploration|quick_question",
  "friction_counts": {"friction_type": count, ...},
  "friction_detail": "One sentence describing friction or empty",
  "primary_success": "none|fast_accurate_search|correct_code_edits|good_explanations|proactive_help|multi_file_changes|good_debugging",
  "brief_summary": "One sentence: what user wanted and whether they got it"
}"""


def load_cached_facet(session_id):
    """Try to load a cached facet from disk."""
    path = FACETS_DIR / f"{session_id}.json"
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_facet(facet):
    """Save a facet to the cache directory."""
    FACETS_DIR.mkdir(parents=True, exist_ok=True)
    path = FACETS_DIR / f"{facet['session_id']}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(facet, f, indent=2)


def summarize_long_transcript(client, transcript):
    """Summarize a long transcript in chunks."""
    if len(transcript) <= LONG_SESSION_THRESHOLD:
        return transcript

    chunks = []
    for i in range(0, len(transcript), CHUNK_SIZE):
        chunks.append(transcript[i:i + CHUNK_SIZE])

    prompt = """Summarize this portion of a Claude Code session transcript. Focus on:
1. What the user asked for
2. What Claude did (tools used, files modified)
3. Any friction or issues
4. The outcome

Keep it detailed - capture specific file names, error messages, and user feedback.
Preserve technical specifics that would help analyze the session quality.

TRANSCRIPT CHUNK:
"""
    summaries = []
    for chunk in chunks:
        try:
            resp = api_create(client,
                model=MODEL,
                max_tokens=SUMMARIZE_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt + chunk}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            summaries.append(text or chunk[:SUMMARIZE_MAX_TOKENS * 4])
        except Exception as e:
            print(f"    [warn] Summarization failed: {e}")
            summaries.append(chunk[:SUMMARIZE_MAX_TOKENS * 4])

    return "\n\n---\n\n".join(summaries)


def extract_facets(client, messages, session_id, metrics):
    """Extract structured facets from a session using Claude."""
    transcript = serialize_transcript(messages)

    # Add session header
    header = (
        f"Session: {session_id[:8]}\n"
        f"Date: {metrics['start_time']}\n"
        f"Project: {metrics['project_path']}\n"
        f"Duration: {metrics['duration_minutes']} min\n\n"
    )
    transcript = header + transcript

    # Summarize if too long
    transcript = summarize_long_transcript(client, transcript)

    prompt = FACET_SYSTEM_PROMPT + transcript + FACET_SCHEMA

    try:
        resp = api_create(client,
            model=MODEL,
            max_tokens=FACET_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        facet = extract_json_object(text)
        if not facet:
            return None
        facet["session_id"] = session_id
        return facet
    except Exception as e:
        print(f"    [error] Facet extraction failed for {session_id[:8]}: {e}")
        return None


# ---------------------------------------------------------------------------
# Data Aggregation (equivalent to jwz)
# ---------------------------------------------------------------------------

def aggregate_data(session_metrics, facets_map):
    """Aggregate all session metrics and facets into summary stats."""
    data = {
        "total_sessions": len(session_metrics),
        "sessions_with_facets": len(facets_map),
        "date_range": {"start": "", "end": ""},
        "total_messages": 0,
        "total_duration_hours": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "tool_counts": {},
        "languages": {},
        "git_commits": 0,
        "git_pushes": 0,
        "projects": {},
        "goal_categories": {},
        "outcomes": {},
        "satisfaction": {},
        "helpfulness": {},
        "session_types": {},
        "friction": {},
        "success": {},
        "session_summaries": [],
        "total_interruptions": 0,
        "total_tool_errors": 0,
        "tool_error_categories": {},
        "user_response_times": [],
        "median_response_time": 0,
        "avg_response_time": 0,
        "sessions_using_task_agent": 0,
        "sessions_using_mcp": 0,
        "sessions_using_web_search": 0,
        "sessions_using_web_fetch": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0,
        "total_files_modified": 0,
        "days_active": 0,
        "messages_per_day": 0,
        "message_hours": [],
    }

    dates = []
    all_response_times = []
    all_hours = []

    for sm in session_metrics:
        dates.append(sm["start_time"])
        data["total_messages"] += sm["user_message_count"]
        data["total_duration_hours"] += sm["duration_minutes"] / 60
        data["total_input_tokens"] += sm["input_tokens"]
        data["total_output_tokens"] += sm["output_tokens"]
        data["git_commits"] += sm["git_commits"]
        data["git_pushes"] += sm["git_pushes"]
        data["total_interruptions"] += sm["user_interruptions"]
        data["total_tool_errors"] += sm["tool_errors"]

        for k, v in sm["tool_error_categories"].items():
            data["tool_error_categories"][k] = data["tool_error_categories"].get(k, 0) + v

        all_response_times.extend(sm["user_response_times"])
        all_hours.extend(sm["message_hours"])

        if sm["uses_task_agent"]:
            data["sessions_using_task_agent"] += 1
        if sm["uses_mcp"]:
            data["sessions_using_mcp"] += 1
        if sm["uses_web_search"]:
            data["sessions_using_web_search"] += 1
        if sm["uses_web_fetch"]:
            data["sessions_using_web_fetch"] += 1

        data["total_lines_added"] += sm["lines_added"]
        data["total_lines_removed"] += sm["lines_removed"]
        data["total_files_modified"] += sm["files_modified"]

        for k, v in sm["tool_counts"].items():
            data["tool_counts"][k] = data["tool_counts"].get(k, 0) + v
        for k, v in sm["languages"].items():
            data["languages"][k] = data["languages"].get(k, 0) + v

        if sm["project_path"]:
            data["projects"][sm["project_path"]] = data["projects"].get(sm["project_path"], 0) + 1

        # Merge facets
        facet = facets_map.get(sm["session_id"])
        if facet:
            for k, v in facet.get("goal_categories", {}).items():
                if v and v > 0:
                    data["goal_categories"][k] = data["goal_categories"].get(k, 0) + v
            outcome = facet.get("outcome", "")
            if outcome:
                data["outcomes"][outcome] = data["outcomes"].get(outcome, 0) + 1
            for k, v in facet.get("user_satisfaction_counts", {}).items():
                if v and v > 0:
                    data["satisfaction"][k] = data["satisfaction"].get(k, 0) + v
            helpfulness = facet.get("claude_helpfulness", "")
            if helpfulness:
                data["helpfulness"][helpfulness] = data["helpfulness"].get(helpfulness, 0) + 1
            session_type = facet.get("session_type", "")
            if session_type:
                data["session_types"][session_type] = data["session_types"].get(session_type, 0) + 1
            for k, v in facet.get("friction_counts", {}).items():
                if v and v > 0:
                    data["friction"][k] = data["friction"].get(k, 0) + v
            primary_success = facet.get("primary_success", "none")
            if primary_success and primary_success != "none":
                data["success"][primary_success] = data["success"].get(primary_success, 0) + 1

        if len(data["session_summaries"]) < MAX_SESSION_SUMMARIES:
            data["session_summaries"].append({
                "id": sm["session_id"][:8],
                "date": sm["start_time"][:10] if sm["start_time"] else "",
                "summary": sm.get("summary") or sm["first_prompt"][:200],
                "goal": facet.get("underlying_goal") if facet else None,
            })

    dates.sort()
    if dates:
        data["date_range"]["start"] = dates[0][:10] if dates[0] else ""
        data["date_range"]["end"] = dates[-1][:10] if dates[-1] else ""

    data["user_response_times"] = all_response_times
    if all_response_times:
        sorted_rt = sorted(all_response_times)
        data["median_response_time"] = sorted_rt[len(sorted_rt) // 2]
        data["avg_response_time"] = sum(all_response_times) / len(all_response_times)

    unique_days = set(d[:10] for d in dates if d)
    data["days_active"] = len(unique_days)
    if data["days_active"] > 0:
        data["messages_per_day"] = round(data["total_messages"] / data["days_active"], 1)

    data["message_hours"] = all_hours
    return data


# ---------------------------------------------------------------------------
# Report Generation Prompts (equivalent to Dwz)
# ---------------------------------------------------------------------------

REPORT_PROMPTS = [
    {
        "name": "project_areas",
        "prompt": """Analyze this Claude Code usage data and identify project areas.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "areas": [
    {"name": "Area name", "session_count": N, "description": "2-3 sentences about what was worked on and how Claude Code was used."}
  ]
}

Include 4-6 areas. Skip internal CC operations.""",
    },
    {
        "name": "interaction_style",
        "prompt": """Analyze this Claude Code usage data and describe the user's interaction style.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "narrative": "3-4 paragraphs analyzing HOW the user interacts with Claude Code. Use second person 'you'. Describe patterns: iterate quickly vs detailed upfront specs? Interrupt often or let Claude run? Include specific examples. Use **bold** for key insights.",
  "key_pattern": "One sentence summary of most distinctive interaction style"
}""",
    },
    {
        "name": "what_works",
        "prompt": """Analyze this Claude Code usage data and identify what's working well for this user. Use second person ("you").

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "intro": "1 sentence of context",
  "impressive_workflows": [
    {"title": "Short title (3-6 words)", "description": "3-4 sentences describing the impressive workflow or approach. Use 'you' not 'the user'. Be specific about what made this effective."}
  ]
}

Include 5-7 impressive workflows. Be specific to this user's actual sessions.""",
    },
    {
        "name": "friction_analysis",
        "prompt": """Analyze this Claude Code usage data and identify friction points for this user. Use second person ("you").

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "intro": "1 sentence summarizing friction patterns",
  "categories": [
    {"category": "Concrete category name", "description": "2-3 sentences explaining this category and what could be done differently. Use 'you' not 'the user'.", "examples": ["Specific example with consequence", "Another example"]}
  ]
}

Include 4-6 friction categories with 2-3 examples each. Be specific to this user's actual sessions.""",
    },
    {
        "name": "suggestions",
        "prompt": """Analyze this Claude Code usage data and suggest improvements.

## CC FEATURES REFERENCE (pick from these for features_to_try):
1. **MCP Servers**: Connect Claude to external tools, databases, and APIs via Model Context Protocol.
   - How to use: Run `claude mcp add <server-name> -- <command>`
   - Good for: database queries, Slack integration, GitHub issue lookup, connecting to internal APIs

2. **Custom Skills**: Reusable prompts you define as markdown files that run with a single /command.
   - How to use: Create `.claude/skills/commit/SKILL.md` with instructions. Then type `/commit` to run it.
   - Good for: repetitive workflows - /commit, /review, /test, /deploy, /pr, or complex multi-step workflows

3. **Hooks**: Shell commands that auto-run at specific lifecycle events.
   - How to use: Add to `.claude/settings.json` under "hooks" key.
   - Good for: auto-formatting code, running type checks, enforcing conventions

4. **Headless Mode**: Run Claude non-interactively from scripts and CI/CD.
   - How to use: `claude -p "fix lint errors" --allowedTools "Edit,Read,Bash"`
   - Good for: CI/CD integration, batch code fixes, automated reviews

5. **Task Agents**: Claude spawns focused sub-agents for complex exploration or parallel work.
   - How to use: Claude auto-invokes when helpful, or ask "use an agent to explore X"
   - Good for: codebase exploration, understanding complex systems

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "claude_md_additions": [
    {"addition": "A specific line or block to add to CLAUDE.md", "why": "1 sentence explaining why", "prompt_scaffold": "Where to add this in CLAUDE.md"}
  ],
  "features_to_try": [
    {"feature": "Feature name", "one_liner": "What it does", "why_for_you": "Why this would help YOU", "example_code": "Actual command or config to copy"}
  ],
  "usage_patterns": [
    {"title": "Short title", "suggestion": "1-2 sentence summary", "detail": "3-4 sentences explaining how this applies", "copyable_prompt": "A specific prompt to copy and try"}
  ]
}

IMPORTANT: Include 5-8 items for claude_md_additions, 4-5 items for features_to_try, and 4-5 items for usage_patterns. Be thorough and specific to this user's actual sessions.""",
    },
    {
        "name": "on_the_horizon",
        "prompt": """Analyze this Claude Code usage data and identify future opportunities.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "intro": "1 sentence about evolving AI-assisted development",
  "opportunities": [
    {"title": "Short title (4-8 words)", "whats_possible": "2-3 ambitious sentences about autonomous workflows", "how_to_try": "1-2 sentences mentioning relevant tooling", "copyable_prompt": "Detailed prompt to try"}
  ]
}

Include 4-6 opportunities. Think BIG - autonomous workflows, parallel agents, iterating against tests. Be specific to this user's actual work.""",
    },
    {
        "name": "fun_ending",
        "prompt": """Analyze this Claude Code usage data and find a memorable moment.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "headline": "A memorable QUALITATIVE moment from the transcripts - not a statistic. Something human, funny, or surprising.",
  "detail": "Brief context about when/where this happened"
}

Find something genuinely interesting or amusing from the session summaries.""",
    },
]

AT_A_GLANCE_PROMPT = """You're writing an "At a Glance" summary for a Claude Code usage insights report for Claude Code users. The goal is to help them understand their usage and improve how they can use Claude better, especially as models improve.

Use this 4-part structure:

1. **What's working** - What is the user's unique style of interacting with Claude and what are some impactful things they've done? Include specific details but keep it high level.

2. **What's hindering you** - Split into (a) Claude's fault (misunderstandings, wrong approaches, bugs) and (b) user-side friction (not providing enough context, environment issues). Be honest but constructive.

3. **Quick wins to try** - Specific Claude Code features they could try, or workflow techniques.

4. **Ambitious workflows for better models** - As models improve, what workflows that seem hard now will become possible?

Keep each section to 3-4 sentences. Use a coaching tone.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "whats_working": "(refer to instructions above)",
  "whats_hindering": "(refer to instructions above)",
  "quick_wins": "(refer to instructions above)",
  "ambitious_workflows": "(refer to instructions above)"
}

SESSION DATA:
"""


def generate_report_section(client, prompt_def, data_payload):
    """Call Claude to generate one report section."""
    full_prompt = prompt_def["prompt"] + "\n\nDATA:\n" + data_payload
    try:
        resp = api_create(client,
            model=MODEL,
            max_tokens=REPORT_SECTION_MAX_TOKENS,
            messages=[{"role": "user", "content": full_prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        obj = extract_json_object(text)
        if obj:
            return {"name": prompt_def["name"], "result": obj}
    except Exception as e:
        print(f"    [error] Report section '{prompt_def['name']}' failed: {e}")
    return {"name": prompt_def["name"], "result": None}


def generate_insights(client, agg_data, facets_map):
    """Generate all report sections including At a Glance."""
    # Build the data payload for report prompts
    facet_summaries = []
    friction_details = []
    for facet in list(facets_map.values())[:MAX_FACETS_FOR_REPORT]:
        facet_summaries.append(
            f"- {facet.get('brief_summary', 'N/A')} ({facet.get('outcome', '?')}, {facet.get('claude_helpfulness', '?')})"
        )
        if facet.get("friction_detail"):
            friction_details.append(f"- {facet['friction_detail']}")

    data_payload = json.dumps({
        "sessions": agg_data["total_sessions"],
        "analyzed": agg_data["sessions_with_facets"],
        "date_range": agg_data["date_range"],
        "messages": agg_data["total_messages"],
        "hours": round(agg_data["total_duration_hours"]),
        "commits": agg_data["git_commits"],
        "top_tools": sorted(agg_data["tool_counts"].items(), key=lambda x: x[1], reverse=True)[:MAX_TOP_ITEMS],
        "top_goals": sorted(agg_data["goal_categories"].items(), key=lambda x: x[1], reverse=True)[:MAX_TOP_ITEMS],
        "outcomes": agg_data["outcomes"],
        "satisfaction": agg_data["satisfaction"],
        "friction": agg_data["friction"],
        "success": agg_data["success"],
        "languages": agg_data["languages"],
    }, indent=2)

    data_payload += "\n\nSESSION SUMMARIES:\n" + "\n".join(facet_summaries[:MAX_FACETS_FOR_REPORT])
    data_payload += "\n\nFRICTION DETAILS:\n" + "\n".join(friction_details[:MAX_FRICTION_DETAILS])

    # Generate report sections in parallel
    print(f"\n[4/5] Generating report sections ({len(REPORT_PROMPTS)} calls)...")
    results = {}
    with ThreadPoolExecutor(max_workers=REPORT_PARALLELISM) as pool:
        futures = {
            pool.submit(generate_report_section, client, p, data_payload): p["name"]
            for p in REPORT_PROMPTS
        }
        for future in as_completed(futures):
            name = futures[future]
            result = future.result()
            if result["result"]:
                results[name] = result["result"]
            print(f"  [{name}] done")

    # Generate At a Glance (depends on previous results)
    print("  [at_a_glance] generating...")
    glance_data = data_payload
    for key in ("project_areas", "what_works", "friction_analysis", "suggestions", "on_the_horizon"):
        section = results.get(key)
        if section:
            glance_data += f"\n\n## {key}:\n{json.dumps(section, indent=2)[:3000]}"

    try:
        resp = api_create(client,
            model=MODEL,
            max_tokens=REPORT_SECTION_MAX_TOKENS,
            messages=[{"role": "user", "content": AT_A_GLANCE_PROMPT + glance_data}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        obj = extract_json_object(text)
        if obj:
            results["at_a_glance"] = obj
    except Exception as e:
        print(f"    [error] At a glance failed: {e}")
    print("  [at_a_glance] done")

    return results


# ---------------------------------------------------------------------------
# HTML Report Generation
# ---------------------------------------------------------------------------

def html_escape(s):
    if not s:
        return ""
    return html.escape(str(s))


def md_bold_to_html(s):
    escaped = html_escape(s)
    return re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)


def render_bar_chart(data_dict, color, max_items=8, order=None):
    if order:
        items = [(k, data_dict.get(k, 0)) for k in order if data_dict.get(k, 0) > 0]
    else:
        items = sorted(data_dict.items(), key=lambda x: x[1], reverse=True)[:max_items]
    if not items:
        return '<p class="empty">No data</p>'
    max_val = max(v for _, v in items)
    rows = []
    for key, val in items:
        pct = (val / max_val) * 100 if max_val else 0
        label = DISPLAY_NAMES.get(key, key.replace("_", " ").title())
        rows.append(f'''<div class="bar-row">
            <div class="bar-label">{html_escape(label)}</div>
            <div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>
            <div class="bar-value">{val}</div>
        </div>''')
    return "\n".join(rows)


def render_response_time_chart(times):
    if not times:
        return '<p class="empty">No response time data</p>'
    buckets = {"2-10s": 0, "10-30s": 0, "30s-1m": 0, "1-2m": 0, "2-5m": 0, "5-15m": 0, ">15m": 0}
    for t in times:
        if t < 10: buckets["2-10s"] += 1
        elif t < 30: buckets["10-30s"] += 1
        elif t < 60: buckets["30s-1m"] += 1
        elif t < 120: buckets["1-2m"] += 1
        elif t < 300: buckets["2-5m"] += 1
        elif t < 900: buckets["5-15m"] += 1
        else: buckets[">15m"] += 1
    max_val = max(buckets.values()) or 1
    rows = []
    for label, count in buckets.items():
        pct = (count / max_val) * 100
        rows.append(f'''<div class="bar-row">
            <div class="bar-label">{label}</div>
            <div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:#6366f1"></div></div>
            <div class="bar-value">{count}</div>
        </div>''')
    return "\n".join(rows)


def render_time_of_day_chart(hours):
    if not hours:
        return '<p class="empty">No time data</p>'
    periods = [
        ("Morning (6-12)", range(6, 12)),
        ("Afternoon (12-18)", range(12, 18)),
        ("Evening (18-24)", range(18, 24)),
        ("Night (0-6)", range(0, 6)),
    ]
    counts = {}
    for h in hours:
        counts[h] = counts.get(h, 0) + 1
    period_counts = [(label, sum(counts.get(h, 0) for h in rng)) for label, rng in periods]
    max_val = max(c for _, c in period_counts) or 1
    rows = []
    for label, count in period_counts:
        pct = (count / max_val) * 100
        rows.append(f'''<div class="bar-row">
            <div class="bar-label">{label}</div>
            <div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:#8b5cf6"></div></div>
            <div class="bar-value">{count}</div>
        </div>''')
    return "\n".join(rows)


def render_narrative(text):
    if not text:
        return ""
    paragraphs = text.split("\n\n")
    result = []
    for p in paragraphs:
        escaped = html_escape(p)
        escaped = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)
        escaped = escaped.replace("\n", "<br>")
        escaped = re.sub(r'^- ', '&bull; ', escaped, flags=re.MULTILINE)
        result.append(f"<p>{escaped}</p>")
    return "\n".join(result)


def generate_html_report(agg_data, insights):
    """Generate the full HTML report."""
    glance = insights.get("at_a_glance", {})
    areas = insights.get("project_areas", {}).get("areas", [])
    style = insights.get("interaction_style", {})
    wins = insights.get("what_works", {})
    friction = insights.get("friction_analysis", {})
    suggestions = insights.get("suggestions", {})
    horizon = insights.get("on_the_horizon", {})
    fun = insights.get("fun_ending", {})

    stats_line = " &middot; ".join(filter(None, [
        f"{agg_data['total_messages']:,} messages",
        f"{agg_data['total_sessions']} sessions",
        f"{round(agg_data['total_duration_hours'])}h total",
        f"{agg_data['git_commits']} commits",
        f"{agg_data['days_active']} days active",
    ]))

    # Build sections
    glance_html = ""
    if glance:
        glance_parts = []
        if glance.get("whats_working"):
            glance_parts.append(f'<div class="glance-section"><strong>What&#39;s working:</strong> {md_bold_to_html(glance["whats_working"])}</div>')
        if glance.get("whats_hindering"):
            glance_parts.append(f'<div class="glance-section"><strong>What&#39;s hindering you:</strong> {md_bold_to_html(glance["whats_hindering"])}</div>')
        if glance.get("quick_wins"):
            glance_parts.append(f'<div class="glance-section"><strong>Quick wins to try:</strong> {md_bold_to_html(glance["quick_wins"])}</div>')
        if glance.get("ambitious_workflows"):
            glance_parts.append(f'<div class="glance-section"><strong>Ambitious workflows:</strong> {md_bold_to_html(glance["ambitious_workflows"])}</div>')
        glance_sections = "\n            ".join(glance_parts)
        glance_html = f'''<div class="at-a-glance">
        <div class="glance-title">At a Glance</div>
        <div class="glance-sections">
            {glance_sections}
        </div></div>'''

    areas_html = ""
    if areas:
        area_cards = "\n".join(
            f'''<div class="project-area">
                <div class="area-header"><span class="area-name">{html_escape(a.get("name",""))}</span>
                <span class="area-count">~{a.get("session_count",0)} sessions</span></div>
                <div class="area-desc">{html_escape(a.get("description",""))}</div>
            </div>''' for a in areas
        )
        areas_html = f'<h2 id="section-work">What You Work On</h2><div class="project-areas">{area_cards}</div>'

    style_html = ""
    if style.get("narrative"):
        style_html = f'''<h2 id="section-usage">How You Use Claude Code</h2>
        <div class="narrative">{render_narrative(style["narrative"])}
        {f'<div class="key-insight"><strong>Key pattern:</strong> {html_escape(style.get("key_pattern",""))}</div>' if style.get("key_pattern") else ""}
        </div>'''

    wins_html = ""
    if wins.get("impressive_workflows"):
        win_cards = "\n".join(
            f'''<div class="big-win">
                <div class="big-win-title">{html_escape(w.get("title",""))}</div>
                <div class="big-win-desc">{html_escape(w.get("description",""))}</div>
            </div>''' for w in wins["impressive_workflows"]
        )
        wins_html = f'''<h2 id="section-wins">Impressive Things You Did</h2>
        {f'<p class="section-intro">{html_escape(wins.get("intro",""))}</p>' if wins.get("intro") else ""}
        <div class="big-wins">{win_cards}</div>'''

    friction_html = ""
    if friction.get("categories"):
        fric_cards = "\n".join(
            f'''<div class="friction-category">
                <div class="friction-title">{html_escape(fc.get("category",""))}</div>
                <div class="friction-desc">{html_escape(fc.get("description",""))}</div>
                {f'<ul class="friction-examples">{"".join(f"<li>{html_escape(ex)}</li>" for ex in fc.get("examples",[]))}</ul>' if fc.get("examples") else ""}
            </div>''' for fc in friction["categories"]
        )
        friction_html = f'''<h2 id="section-friction">Where Things Go Wrong</h2>
        {f'<p class="section-intro">{html_escape(friction.get("intro",""))}</p>' if friction.get("intro") else ""}
        <div class="friction-categories">{fric_cards}</div>'''

    suggestions_html = ""
    if suggestions:
        parts = []
        if suggestions.get("claude_md_additions"):
            items = "\n".join(
                f'''<div class="claude-md-item">
                    <code class="cmd-code">{html_escape(s.get("addition",""))}</code>
                    <div class="cmd-why">{html_escape(s.get("why",""))}</div>
                </div>''' for s in suggestions["claude_md_additions"]
            )
            parts.append(f'''<h2 id="section-features">Suggested CLAUDE.md Additions</h2>
            <div class="claude-md-section">{items}</div>''')

        if suggestions.get("features_to_try"):
            feat_cards = "\n".join(
                f'''<div class="feature-card">
                    <div class="feature-title">{html_escape(ft.get("feature",""))}</div>
                    <div class="feature-oneliner">{html_escape(ft.get("one_liner",""))}</div>
                    <div class="feature-why"><strong>Why for you:</strong> {html_escape(ft.get("why_for_you",""))}</div>
                    {f'<div class="feature-examples"><code class="example-code">{html_escape(ft.get("example_code",""))}</code></div>' if ft.get("example_code") else ""}
                </div>''' for ft in suggestions["features_to_try"]
            )
            parts.append(f'''<h3>Features to Try</h3><div class="features-section">{feat_cards}</div>''')

        if suggestions.get("usage_patterns"):
            pat_cards = "\n".join(
                f'''<div class="pattern-card">
                    <div class="pattern-title">{html_escape(up.get("title",""))}</div>
                    <div class="pattern-summary">{html_escape(up.get("suggestion",""))}</div>
                    {f'<div class="pattern-detail">{html_escape(up.get("detail",""))}</div>' if up.get("detail") else ""}
                    {f'<div class="copyable-prompt-section"><div class="prompt-label">Try this prompt:</div><code class="copyable-prompt">{html_escape(up.get("copyable_prompt",""))}</code></div>' if up.get("copyable_prompt") else ""}
                </div>''' for up in suggestions["usage_patterns"]
            )
            parts.append(f'''<h2 id="section-patterns">New Ways to Use Claude Code</h2>
            <div class="patterns-section">{pat_cards}</div>''')

        suggestions_html = "\n".join(parts)

    horizon_html = ""
    if horizon.get("opportunities"):
        hor_cards = "\n".join(
            f'''<div class="horizon-card">
                <div class="horizon-title">{html_escape(op.get("title",""))}</div>
                <div class="horizon-possible">{html_escape(op.get("whats_possible",""))}</div>
                {f'<div class="horizon-tip"><strong>Getting started:</strong> {html_escape(op.get("how_to_try",""))}</div>' if op.get("how_to_try") else ""}
                {f'<div class="copyable-prompt-section"><code class="copyable-prompt">{html_escape(op.get("copyable_prompt",""))}</code></div>' if op.get("copyable_prompt") else ""}
            </div>''' for op in horizon["opportunities"]
        )
        horizon_html = f'''<h2 id="section-horizon">On the Horizon</h2>
        {f'<p class="section-intro">{html_escape(horizon.get("intro",""))}</p>' if horizon.get("intro") else ""}
        <div class="horizon-section">{hor_cards}</div>'''

    fun_html = ""
    if fun:
        fun_html = f'''<div class="fun-ending">
            <div class="fun-headline">{html_escape(fun.get("headline",""))}</div>
            <div class="fun-detail">{html_escape(fun.get("detail",""))}</div>
        </div>'''

    # Charts
    goals_chart = render_bar_chart(agg_data["goal_categories"], "#2563eb", MAX_TOP_ITEMS)
    tools_chart = render_bar_chart(agg_data["tool_counts"], "#10b981", MAX_TOP_ITEMS)
    langs_chart = render_bar_chart(agg_data["languages"], "#f59e0b", MAX_TOP_ITEMS)
    types_chart = render_bar_chart(agg_data["session_types"], "#8b5cf6")
    outcomes_chart = render_bar_chart(agg_data["outcomes"], "#10b981",
                                      order=["not_achieved", "partially_achieved", "mostly_achieved", "fully_achieved", "unclear_from_transcript"])
    satisfaction_chart = render_bar_chart(agg_data["satisfaction"], "#6366f1",
                                          order=["frustrated", "dissatisfied", "likely_satisfied", "satisfied", "happy", "delighted"])
    helpfulness_chart = render_bar_chart(agg_data["helpfulness"], "#14b8a6",
                                          order=["unhelpful", "slightly_helpful", "moderately_helpful", "very_helpful", "essential"])
    friction_chart = render_bar_chart(agg_data["friction"], "#ef4444")
    success_chart = render_bar_chart(agg_data["success"], "#22c55e")
    response_chart = render_response_time_chart(agg_data["user_response_times"])
    tod_chart = render_time_of_day_chart(agg_data["message_hours"])
    errors_chart = render_bar_chart(agg_data["tool_error_categories"], "#f97316")

    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Claude Code Enhanced Insights</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Inter', -apple-system, sans-serif; background: #f8fafc; color: #0f172a; line-height: 1.6; padding: 40px 20px; }}
.container {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 4px; }}
h2 {{ font-size: 20px; font-weight: 600; margin: 36px 0 16px; color: #1e293b; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}
h3 {{ font-size: 16px; font-weight: 600; margin: 24px 0 12px; color: #334155; }}
.subtitle {{ color: #64748b; font-size: 14px; margin-bottom: 24px; }}
.enhanced-badge {{ display: inline-block; background: #dbeafe; color: #1e40af; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; margin-left: 8px; }}
.at-a-glance {{ background: linear-gradient(135deg, #fef3c7, #fde68a); border-radius: 12px; padding: 24px; margin: 24px 0; }}
.glance-title {{ font-size: 18px; font-weight: 700; margin-bottom: 16px; color: #92400e; }}
.glance-section {{ margin-bottom: 12px; font-size: 14px; color: #78350f; line-height: 1.7; }}
.stats-row {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 20px 0; }}
.stat-box {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; flex: 1; min-width: 120px; text-align: center; }}
.stat-value {{ font-size: 24px; font-weight: 700; color: #1e293b; }}
.stat-label {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
.chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin: 20px 0; }}
.chart-box {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; }}
.chart-title {{ font-size: 14px; font-weight: 600; color: #334155; margin-bottom: 12px; }}
.bar-row {{ display: flex; align-items: center; margin-bottom: 6px; font-size: 13px; }}
.bar-label {{ width: 140px; flex-shrink: 0; color: #475569; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.bar-track {{ flex: 1; height: 18px; background: #f1f5f9; border-radius: 4px; overflow: hidden; margin: 0 8px; }}
.bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
.bar-value {{ width: 36px; text-align: right; color: #64748b; font-weight: 500; }}
.project-areas, .big-wins, .friction-categories, .features-section, .patterns-section, .horizon-section {{ display: flex; flex-direction: column; gap: 12px; }}
.project-area {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }}
.area-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
.area-name {{ font-weight: 600; color: #1e293b; }}
.area-count {{ font-size: 12px; color: #64748b; }}
.area-desc {{ font-size: 13px; color: #475569; }}
.big-win {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 16px; }}
.big-win-title {{ font-weight: 600; color: #166534; margin-bottom: 6px; }}
.big-win-desc {{ font-size: 13px; color: #15803d; }}
.friction-category {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 16px; }}
.friction-title {{ font-weight: 600; color: #991b1b; margin-bottom: 6px; }}
.friction-desc {{ font-size: 13px; color: #7f1d1d; margin-bottom: 8px; }}
.friction-examples {{ font-size: 12px; color: #991b1b; padding-left: 20px; }}
.friction-examples li {{ margin-bottom: 4px; }}
.claude-md-section {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 16px; }}
.claude-md-item {{ margin-bottom: 12px; }}
.cmd-code {{ display: block; background: #1e293b; color: #e2e8f0; padding: 10px 12px; border-radius: 6px; font-size: 12px; font-family: monospace; white-space: pre-wrap; margin-bottom: 4px; }}
.cmd-why {{ font-size: 12px; color: #1e40af; }}
.feature-card {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 16px; }}
.feature-title {{ font-weight: 600; color: #166534; }}
.feature-oneliner {{ font-size: 13px; color: #15803d; margin: 4px 0; }}
.feature-why {{ font-size: 13px; color: #166534; margin: 6px 0; }}
.feature-examples {{ margin-top: 8px; }}
.example-code {{ display: block; background: #1e293b; color: #e2e8f0; padding: 8px 12px; border-radius: 6px; font-size: 12px; font-family: monospace; }}
.pattern-card {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 16px; }}
.pattern-title {{ font-weight: 600; color: #1e40af; }}
.pattern-summary {{ font-size: 13px; color: #1e3a8a; margin: 4px 0; }}
.pattern-detail {{ font-size: 13px; color: #334155; margin: 8px 0; }}
.horizon-card {{ background: linear-gradient(135deg, #f5f3ff, #ede9fe); border: 1px solid #c4b5fd; border-radius: 8px; padding: 16px; }}
.horizon-title {{ font-weight: 600; color: #5b21b6; }}
.horizon-possible {{ font-size: 13px; color: #6d28d9; margin: 6px 0; }}
.horizon-tip {{ font-size: 13px; color: #7c3aed; margin: 6px 0; }}
.copyable-prompt-section {{ margin-top: 8px; }}
.prompt-label {{ font-size: 11px; color: #64748b; margin-bottom: 4px; }}
.copyable-prompt {{ display: block; background: #1e293b; color: #e2e8f0; padding: 8px 12px; border-radius: 6px; font-size: 12px; font-family: monospace; white-space: pre-wrap; }}
.narrative p {{ margin-bottom: 12px; font-size: 14px; color: #334155; }}
.key-insight {{ background: #fef3c7; border-radius: 6px; padding: 12px; margin-top: 12px; font-size: 14px; color: #92400e; }}
.section-intro {{ font-size: 14px; color: #64748b; margin-bottom: 16px; }}
.fun-ending {{ background: linear-gradient(135deg, #fdf2f8, #fce7f3); border: 1px solid #f9a8d4; border-radius: 12px; padding: 24px; margin: 36px 0; text-align: center; }}
.fun-headline {{ font-size: 18px; font-weight: 700; color: #9d174d; margin-bottom: 8px; }}
.fun-detail {{ font-size: 14px; color: #be185d; }}
.empty {{ color: #94a3b8; font-style: italic; font-size: 13px; }}
.footer {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 48px; padding-top: 24px; border-top: 1px solid #e2e8f0; }}
@media (max-width: 640px) {{ .chart-grid {{ grid-template-columns: 1fr; }} .stats-row {{ flex-direction: column; }} .bar-label {{ width: 100px; }} }}
</style>
</head>
<body>
<div class="container">

<h1>Claude Code Insights <span class="enhanced-badge">ENHANCED</span></h1>
<p class="subtitle">{stats_line}<br>{agg_data["date_range"]["start"]} to {agg_data["date_range"]["end"]}</p>

{glance_html}

<div class="stats-row">
    <div class="stat-box"><div class="stat-value">{agg_data["total_messages"]:,}</div><div class="stat-label">Messages</div></div>
    <div class="stat-box"><div class="stat-value">{agg_data["total_lines_added"]:,}</div><div class="stat-label">Lines Added</div></div>
    <div class="stat-box"><div class="stat-value">{agg_data["total_files_modified"]:,}</div><div class="stat-label">Files Modified</div></div>
    <div class="stat-box"><div class="stat-value">{agg_data["days_active"]}</div><div class="stat-label">Days Active</div></div>
    <div class="stat-box"><div class="stat-value">{agg_data["messages_per_day"]}</div><div class="stat-label">Msgs/Day</div></div>
</div>

{areas_html}
{style_html}

<div class="chart-grid">
    <div class="chart-box"><div class="chart-title">What You Wanted</div>{goals_chart}</div>
    <div class="chart-box"><div class="chart-title">Top Tools Used</div>{tools_chart}</div>
</div>
<div class="chart-grid">
    <div class="chart-box"><div class="chart-title">Languages</div>{langs_chart}</div>
    <div class="chart-box"><div class="chart-title">Session Types</div>{types_chart}</div>
</div>

{wins_html}

<div class="chart-grid">
    <div class="chart-box"><div class="chart-title">What Helped Most</div>{success_chart}</div>
    <div class="chart-box"><div class="chart-title">Outcomes</div>{outcomes_chart}</div>
</div>

{friction_html}

<div class="chart-grid">
    <div class="chart-box"><div class="chart-title">Primary Friction Types</div>{friction_chart}</div>
    <div class="chart-box"><div class="chart-title">Inferred Satisfaction</div>{satisfaction_chart}</div>
</div>
<div class="chart-grid">
    <div class="chart-box"><div class="chart-title">Claude Helpfulness</div>{helpfulness_chart}</div>
    <div class="chart-box"><div class="chart-title">Response Time Distribution</div>{response_chart}</div>
</div>
<div class="chart-grid">
    <div class="chart-box"><div class="chart-title">Time of Day</div>{tod_chart}</div>
    <div class="chart-box"><div class="chart-title">Tool Errors</div>{errors_chart}</div>
</div>

{suggestions_html}
{horizon_html}
{fun_html}

<div class="footer">
    Generated by Enhanced Insights &middot; {datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; Model: {MODEL}
</div>

</div>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def estimate_cost(n_uncached, n_long_sessions=0):
    """Rough cost estimate for Sonnet 4.5 at $3/M input, $15/M output."""
    # Facet extraction: ~4K input + ~1K output per session
    facet_input = n_uncached * 4000
    facet_output = n_uncached * 1000
    # Long session summarization: ~2K input + ~500 output per chunk
    summary_input = n_long_sessions * 2000
    summary_output = n_long_sessions * 500
    # Report generation: 8 calls, ~8K input + ~4K output each
    report_input = 8 * 8000
    report_output = 8 * 4000
    total_input = facet_input + summary_input + report_input
    total_output = facet_output + summary_output + report_output
    cost = (total_input / 1_000_000) * 3.0 + (total_output / 1_000_000) * 15.0
    return cost, total_input, total_output


def parse_args():
    parser = argparse.ArgumentParser(
        description="Enhanced Claude Code Insights Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s                           # Full run, all sessions
  %(prog)s --dry-run                 # Show what would be processed, with cost estimate
  %(prog)s --project "*claude*"      # Only projects matching glob
  %(prog)s --after 2026-02-01        # Only sessions after this date
  %(prog)s --model claude-opus-4-6   # Use Opus instead of Sonnet
""",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show session counts and cost estimate without making API calls")
    parser.add_argument("--project", type=str, default=None,
                        help="Filter projects by glob pattern (e.g. '*claude*', '*ghostden*')")
    parser.add_argument("--after", type=str, default=None,
                        help="Only include sessions modified after this date (YYYY-MM-DD)")
    parser.add_argument("--model", type=str, default=None,
                        help=f"Override model (default: {MODEL})")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.model:
        global MODEL
        MODEL = args.model

    print("=" * 60)
    print("Enhanced Claude Code Insights Generator")
    print("=" * 60)

    # Auth
    print("\n[0/5] Loading OAuth credentials...")
    oauth = OAuthManager()
    client = create_client(oauth)
    print(f"  Token valid, subscription: {oauth._creds.get('subscriptionType', '?')}")

    # Discover sessions
    print("\n[1/5] Discovering sessions...")
    sessions = discover_sessions()

    # Apply filters
    if args.project:
        import fnmatch
        before = len(sessions)
        sessions = [s for s in sessions if fnmatch.fnmatch(s["project"], args.project)]
        print(f"  Project filter '{args.project}': {before} -> {len(sessions)}")

    if args.after:
        try:
            cutoff = datetime.strptime(args.after, "%Y-%m-%d").timestamp()
            before = len(sessions)
            sessions = [s for s in sessions if s["mtime"] >= cutoff]
            print(f"  Date filter --after {args.after}: {before} -> {len(sessions)}")
        except ValueError:
            print(f"  [error] Invalid date format: {args.after} (expected YYYY-MM-DD)")
            sys.exit(1)

    n_subagent = sum(1 for s in sessions if s.get("is_subagent"))
    n_main = len(sessions) - n_subagent
    print(f"  Found {len(sessions)} session files ({n_main} main, {n_subagent} subagent) across {len(set(s['project'] for s in sessions))} projects")

    # Load and process sessions
    print("\n[2/5] Loading sessions and extracting metrics...")
    all_metrics = []
    facets_map = {}
    sessions_needing_facets = []

    for i, sess in enumerate(sessions):
        if i % 20 == 0 and i > 0:
            print(f"  Processed {i}/{len(sessions)} sessions...")

        messages = load_session_messages(sess["path"])
        if not messages:
            continue
        if is_insights_session(messages):
            continue

        metrics = extract_session_metrics(messages, sess["session_id"], sess["project"])

        # Filter trivial sessions
        if metrics["user_message_count"] < 2 or metrics["duration_minutes"] < 1:
            continue

        all_metrics.append(metrics)

        # Check for cached facets
        cached = load_cached_facet(sess["session_id"])
        if cached:
            facets_map[sess["session_id"]] = cached
        else:
            sessions_needing_facets.append((sess, messages, metrics))

    print(f"  {len(all_metrics)} non-trivial sessions")
    print(f"  {len(facets_map)} cached facets, {len(sessions_needing_facets)} need extraction")

    # Dry run — show cost estimate and exit
    if args.dry_run:
        n_uncached = min(len(sessions_needing_facets), MAX_SESSIONS_TO_PROCESS)
        cost, tok_in, tok_out = estimate_cost(n_uncached)
        print(f"\n{'=' * 60}")
        print(f"DRY RUN — no API calls will be made")
        print(f"{'=' * 60}")
        print(f"  Sessions to scan:     {len(sessions)}")
        print(f"  Non-trivial sessions: {len(all_metrics)}")
        print(f"  Cached facets:        {len(facets_map)}")
        print(f"  Uncached (need API):  {n_uncached}")
        print(f"  Report API calls:     8")
        print(f"  Model:                {MODEL}")
        print(f"  Est. input tokens:    {tok_in:,}")
        print(f"  Est. output tokens:   {tok_out:,}")
        print(f"  Est. cost:            ${cost:.2f}")
        if args.project:
            print(f"  Project filter:       {args.project}")
        if args.after:
            print(f"  Date filter:          after {args.after}")
        print(f"{'=' * 60}")
        return

    # Extract facets for uncached sessions (batched parallel with rate limiting)
    if sessions_needing_facets:
        to_process = sessions_needing_facets[:MAX_SESSIONS_TO_PROCESS]
        total = len(to_process)
        print(f"\n[3/5] Extracting facets for {total} sessions (model: {MODEL}, batch_size: {FACET_BATCH_SIZE})...")
        done = 0
        for batch_start in range(0, total, FACET_BATCH_SIZE):
            batch = to_process[batch_start:batch_start + FACET_BATCH_SIZE]
            if batch_start > 0:
                time.sleep(FACET_BATCH_DELAY)

            def _extract_one(item):
                sess, messages, metrics = item
                sid_full = sess["session_id"]
                facet = extract_facets(client, messages, sid_full, metrics)
                return sid_full, facet

            with ThreadPoolExecutor(max_workers=FACET_BATCH_SIZE) as pool:
                futures = {pool.submit(_extract_one, item): item for item in batch}
                for future in as_completed(futures):
                    done += 1
                    sid_full, facet = future.result()
                    sid = sid_full[:8]
                    if facet:
                        facets_map[sid_full] = facet
                        save_facet(facet)
                        print(f"  [{done}/{total}] {sid} ok ({facet.get('outcome', '?')})")
                    else:
                        print(f"  [{done}/{total}] {sid} skip")
    else:
        print("\n[3/5] All facets cached, skipping extraction.")

    # Filter out warmup-only sessions
    def is_warmup_only(session_id):
        f = facets_map.get(session_id)
        if not f:
            return False
        cats = f.get("goal_categories", {})
        active = [k for k, v in cats.items() if v and v > 0]
        return len(active) == 1 and active[0] == "warmup_minimal"

    filtered_metrics = [m for m in all_metrics if not is_warmup_only(m["session_id"])]
    filtered_facets = {k: v for k, v in facets_map.items() if not is_warmup_only(k)}

    print(f"\n  After filtering warmups: {len(filtered_metrics)} sessions, {len(filtered_facets)} facets")

    # Aggregate
    agg_data = aggregate_data(filtered_metrics, filtered_facets)
    agg_data["total_sessions_scanned"] = len(sessions)

    # Generate insights
    insights = generate_insights(client, agg_data, filtered_facets)

    # Generate HTML
    print("\n[5/5] Generating HTML report...")
    html_content = generate_html_report(agg_data, insights)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = Path.home() / f"claude-insights-{timestamp}.html"
    raw_path = Path.home() / f"claude-insights-{timestamp}.json"
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(html_content)
    print(f"  Report saved to: {report_path}")
    print(f"  Open with: xdg-open {report_path}")

    # Also save raw data
    fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({
            "aggregated": agg_data,
            "insights": insights,
            "config": {
                "model": MODEL,
                "summarize_max_tokens": SUMMARIZE_MAX_TOKENS,
                "facet_max_tokens": FACET_MAX_TOKENS,
                "report_max_tokens": REPORT_SECTION_MAX_TOKENS,
                "max_sessions": MAX_SESSIONS_TO_PROCESS,
                "max_facets_for_report": MAX_FACETS_FOR_REPORT,
                "user_msg_truncate": USER_MSG_TRUNCATE,
                "assistant_msg_truncate": ASSISTANT_MSG_TRUNCATE,
            },
        }, f, indent=2, default=str)
    print(f"  Raw data saved to: {raw_path}")

    print("\n" + "=" * 60)
    print("Done!")
    print(f"Sessions scanned: {len(sessions)}")
    print(f"Sessions analyzed: {len(filtered_metrics)}")
    print(f"Facets extracted: {len(filtered_facets)}")
    print(f"Report: file://{report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
