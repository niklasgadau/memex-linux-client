from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from memex_client.api import MemexAPI
from memex_client.config import get_client_id, resolve_client_name
from memex_client.exporters.base import BaseExporter
from memex_client.state import SyncState

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"
PLANS_DIR = CLAUDE_HOME / "plans"
HISTORY_FILE = CLAUDE_HOME / "history.jsonl"

# Per-turn caps so a single oversized tool dump can't blow up the payload.
PROMPT_MAX_BYTES = 8 * 1024
RESPONSE_MAX_BYTES = 8 * 1024


class ClaudeSessionsExporter(BaseExporter):
    """Crawl ~/.claude/ and ship conversation transcripts, plans, memory
    and the global prompt history to the Memex server.

    Deviates from the BaseExporter contract: a single sync() collects six
    document types from different sources, so collect_new_entries / _post /
    _save_state are no-ops and sync() is overridden directly.
    """

    name = "claude_sessions"

    def __init__(self, config: dict, state: SyncState, api: MemexAPI) -> None:
        super().__init__(config, state, api)
        self.client_name = resolve_client_name(config)
        self.client_id = get_client_id()

    def collect_new_entries(self) -> list[dict]:
        raise NotImplementedError("ClaudeSessionsExporter overrides sync() directly")

    def _post(self, entries):
        raise NotImplementedError

    def _save_state(self) -> None:
        raise NotImplementedError

    def sync(self) -> int:
        if not CLAUDE_HOME.exists():
            return 0

        prev = self.state.get(self.name)
        new_state = {
            "transcripts": dict(prev.get("transcripts", {})),
            "plans": dict(prev.get("plans", {})),
            "memory": dict(prev.get("memory", {})),
            "input_history": dict(prev.get("input_history", {})),
            "last_sync_time": prev.get("last_sync_time"),
        }

        sessions, turns, recaps = self._collect_jsonl(new_state)
        plans = self._collect_plans(new_state)
        memory = self._collect_memory(new_state)
        inputs = self._collect_input_history(new_state)

        batch = {
            "sessions": sessions,
            "turns": turns,
            "recaps": recaps,
            "plans": plans,
            "memory": memory,
            "inputs": inputs,
        }

        total = sum(len(v) for v in batch.values())
        if total == 0:
            return 0

        self.api.post_claude(batch)

        new_state["last_sync_time"] = datetime.now(timezone.utc).isoformat()
        self.state.update(self.name, new_state)
        return total

    # --- Discovery: transcripts -------------------------------------------------

    def _collect_jsonl(
        self, new_state: dict
    ) -> tuple[list[dict], list[dict], list[dict]]:
        sessions: list[dict] = []
        turns: list[dict] = []
        recaps: list[dict] = []

        transcripts_state = new_state["transcripts"]
        if not PROJECTS_DIR.exists():
            return sessions, turns, recaps

        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            for jsonl in sorted(project_dir.glob("*.jsonl")):
                session_uuid = jsonl.stem
                try:
                    file_size = jsonl.stat().st_size
                except OSError:
                    continue
                prev_offset = transcripts_state.get(session_uuid, {}).get(
                    "byte_offset", 0
                )
                # Truncation/rotation safety
                if file_size < prev_offset:
                    prev_offset = 0
                # Nothing changed since last sync → skip both aggregate rebuild
                # and per-line work
                if file_size == prev_offset and session_uuid in transcripts_state:
                    continue

                all_lines = list(self._iter_jsonl(jsonl))
                if not all_lines:
                    transcripts_state[session_uuid] = {
                        "byte_offset": file_size,
                        "mtime": int(jsonl.stat().st_mtime),
                    }
                    continue

                sessions.append(
                    self._build_session(session_uuid, all_lines, project_dir.name)
                )

                # Determine which lines are new (above prev_offset) by re-reading
                # only the new tail and matching uuids against the full list.
                with open(jsonl, "rb") as f:
                    f.seek(prev_offset)
                    new_blob = f.read()
                new_lines = self._parse_blob(new_blob)
                new_user_uuids = {
                    l.get("uuid") for l in new_lines if l.get("type") == "user"
                }
                new_recap_uuids = {
                    l.get("uuid")
                    for l in new_lines
                    if l.get("type") == "system"
                    and l.get("subtype") == "away_summary"
                }

                for line in all_lines:
                    t = line.get("type")
                    uid = line.get("uuid")
                    if t == "user" and uid in new_user_uuids:
                        turn = self._build_turn(
                            session_uuid, line, all_lines, project_dir.name
                        )
                        if turn:
                            turns.append(turn)
                    elif (
                        t == "system"
                        and line.get("subtype") == "away_summary"
                        and uid in new_recap_uuids
                    ):
                        recap = self._build_recap(session_uuid, line, project_dir.name)
                        if recap:
                            recaps.append(recap)

                transcripts_state[session_uuid] = {
                    "byte_offset": file_size,
                    "mtime": int(jsonl.stat().st_mtime),
                }

        return sessions, turns, recaps

    def _iter_jsonl(self, path: Path) -> Iterator[dict]:
        with open(path, "rb") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue

    def _parse_blob(self, blob: bytes) -> list[dict]:
        out = []
        for raw in blob.decode("utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    def _build_session(
        self, session_uuid: str, lines: list[dict], project_slug: str
    ) -> dict:
        timestamps = [
            ts for ts in (self._parse_ts(l.get("timestamp")) for l in lines) if ts
        ]
        start_time = min(timestamps) if timestamps else 0
        end_time = max(timestamps) if timestamps else 0

        user_msgs = [
            l for l in lines if l.get("type") == "user" and not l.get("isSidechain")
        ]
        asst_msgs = [l for l in lines if l.get("type") == "assistant"]

        first_prompt = ""
        last_prompt = ""
        if user_msgs:
            first_prompt = self._extract_user_text(user_msgs[0])[:1024]
            last_prompt = self._extract_user_text(user_msgs[-1])[:1024]

        title = None
        slug = None
        cwd = ""
        git_branch = None
        cc_version = None
        for l in lines:
            if not title and l.get("type") == "ai-title":
                title = l.get("title") or l.get("slug")
            if not slug and l.get("slug"):
                slug = l.get("slug")
            if not cwd and l.get("cwd"):
                cwd = l.get("cwd", "")
            if not git_branch and l.get("gitBranch"):
                git_branch = l.get("gitBranch")
            if not cc_version and l.get("version"):
                cc_version = l.get("version")

        tool_names: set[str] = set()
        for l in asst_msgs:
            for block in self._iter_blocks(l):
                if block.get("type") == "tool_use" and block.get("name"):
                    tool_names.add(block["name"])

        recap_count = sum(
            1
            for l in lines
            if l.get("type") == "system" and l.get("subtype") == "away_summary"
        )

        return {
            "session_uuid": session_uuid,
            "client": self.client_name,
            "client_id": self.client_id,
            "cwd": cwd,
            "project_slug": project_slug,
            "git_branch": git_branch,
            "cc_version": cc_version,
            "title": title,
            "slug": slug,
            "start_time": start_time,
            "end_time": end_time,
            "message_count": len(lines),
            "user_message_count": len(user_msgs),
            "assistant_message_count": len(asst_msgs),
            "tool_names": sorted(tool_names),
            "first_prompt": first_prompt,
            "last_prompt": last_prompt,
            "recap_count": recap_count,
        }

    def _build_turn(
        self,
        session_uuid: str,
        user_line: dict,
        all_lines: list[dict],
        project_slug: str,
    ) -> dict | None:
        user_uuid = user_line.get("uuid")
        if not user_uuid:
            return None
        idx = next(
            (i for i, l in enumerate(all_lines) if l.get("uuid") == user_uuid), None
        )
        if idx is None:
            return None

        response_chunks: list[str] = []
        tool_names: set[str] = set()
        for l in all_lines[idx + 1 :]:
            if l.get("type") == "user":
                break
            if l.get("type") != "assistant":
                continue
            for block in self._iter_blocks(l):
                if block.get("type") == "text" and block.get("text"):
                    response_chunks.append(block["text"])
                elif block.get("type") == "tool_use" and block.get("name"):
                    tool_names.add(block["name"])

        prompt = self._extract_user_text(user_line)
        prompt_trim = prompt.encode("utf-8")[:PROMPT_MAX_BYTES].decode(
            "utf-8", errors="replace"
        )
        response_full = "\n\n".join(response_chunks)
        response_trim = response_full.encode("utf-8")[:RESPONSE_MAX_BYTES].decode(
            "utf-8", errors="replace"
        )

        return {
            "session_uuid": session_uuid,
            "user_uuid": user_uuid,
            "parent_uuid": user_line.get("parentUuid"),
            "timestamp": self._parse_ts(user_line.get("timestamp")) or 0,
            "client": self.client_name,
            "client_id": self.client_id,
            "cwd": user_line.get("cwd", ""),
            "project_slug": project_slug,
            "git_branch": user_line.get("gitBranch"),
            "cc_version": user_line.get("version"),
            "slug": user_line.get("slug"),
            "prompt": prompt_trim,
            "response": response_trim,
            "tool_names": sorted(tool_names),
            "is_sidechain": bool(user_line.get("isSidechain")),
        }

    def _build_recap(
        self, session_uuid: str, line: dict, project_slug: str
    ) -> dict | None:
        uid = line.get("uuid")
        content = line.get("content") or ""
        if not uid or not content:
            return None
        return {
            "session_uuid": session_uuid,
            "recap_uuid": uid,
            "timestamp": self._parse_ts(line.get("timestamp")) or 0,
            "client": self.client_name,
            "client_id": self.client_id,
            "cwd": line.get("cwd", ""),
            "project_slug": project_slug,
            "slug": line.get("slug"),
            "content": content,
        }

    # --- Discovery: plans / memory / input history -----------------------------

    def _collect_plans(self, new_state: dict) -> list[dict]:
        out: list[dict] = []
        plans_state = new_state["plans"]
        if not PLANS_DIR.exists():
            return out
        for md in sorted(PLANS_DIR.glob("*.md")):
            try:
                body = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            h = hashlib.sha256(body.encode()).hexdigest()
            slug = md.stem
            if plans_state.get(slug) == h:
                continue
            out.append(
                {
                    "name": slug,
                    "body": body,
                    "timestamp": int(md.stat().st_mtime),
                    "client": self.client_name,
                    "client_id": self.client_id,
                }
            )
            plans_state[slug] = h
        return out

    def _collect_memory(self, new_state: dict) -> list[dict]:
        out: list[dict] = []
        mem_state = new_state["memory"]
        if not PROJECTS_DIR.exists():
            return out
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            memdir = project_dir / "memory"
            if not memdir.exists():
                continue
            for md in sorted(memdir.glob("*.md")):
                try:
                    body = md.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                h = hashlib.sha256(body.encode()).hexdigest()
                key = f"{project_dir.name}/{md.name}"
                if mem_state.get(key) == h:
                    continue
                out.append(
                    {
                        "project_slug": project_dir.name,
                        "name": md.stem,
                        "body": body,
                        "frontmatter": self._parse_frontmatter(body),
                        "timestamp": int(md.stat().st_mtime),
                        "client": self.client_name,
                        "client_id": self.client_id,
                    }
                )
                mem_state[key] = h
        return out

    def _collect_input_history(self, new_state: dict) -> list[dict]:
        out: list[dict] = []
        ih_state = new_state["input_history"]
        if not HISTORY_FILE.exists():
            return out
        file_size = HISTORY_FILE.stat().st_size
        prev_offset = ih_state.get("byte_offset", 0)
        if file_size < prev_offset:
            prev_offset = 0
        if file_size == prev_offset:
            return out
        with open(HISTORY_FILE, "rb") as f:
            f.seek(prev_offset)
            blob = f.read()
        for raw in blob.decode("utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = entry.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            ts_sec = int(ts) // 1000 if ts > 1e12 else int(ts)
            session_uuid = entry.get("sessionId") or ""
            if not session_uuid:
                continue
            pasted_text = ""
            pasted = entry.get("pastedContents")
            if isinstance(pasted, dict):
                pasted_text = "\n\n".join(
                    str(v.get("content", ""))
                    for v in pasted.values()
                    if isinstance(v, dict)
                )
            out.append(
                {
                    "session_uuid": session_uuid,
                    "timestamp": ts_sec,
                    "display": entry.get("display", ""),
                    "pasted_contents": pasted_text,
                    "project": entry.get("project"),
                    "client": self.client_name,
                    "client_id": self.client_id,
                }
            )
        ih_state["byte_offset"] = file_size
        return out

    # --- helpers ---------------------------------------------------------------

    def _extract_user_text(self, line: dict) -> str:
        msg = line.get("message", {})
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    tc = block.get("content", "")
                    if isinstance(tc, str):
                        parts.append(tc)
                    elif isinstance(tc, list):
                        for b in tc:
                            if isinstance(b, dict) and b.get("type") == "text":
                                parts.append(b.get("text", ""))
            return "\n".join(parts)
        return ""

    def _iter_blocks(self, line: dict) -> Iterator[dict]:
        msg = line.get("message", {})
        content = msg.get("content", []) if isinstance(msg, dict) else []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield block

    def _parse_ts(self, ts) -> int:
        if not ts:
            return 0
        if isinstance(ts, (int, float)):
            return int(ts) // 1000 if ts > 1e12 else int(ts)
        if isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return int(dt.timestamp())
            except ValueError:
                return 0
        return 0

    def _parse_frontmatter(self, body: str) -> dict | None:
        if not body.startswith("---"):
            return None
        try:
            end = body.index("\n---", 4)
        except ValueError:
            return None
        fm = {}
        for ln in body[4:end].splitlines():
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
        return fm
