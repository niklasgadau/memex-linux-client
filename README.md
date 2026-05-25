# memex-linux-client

Linux client for Memex — syncs shell history, clipboard and Claude Code conversations (sessions, turns, recaps, plans, memory, prompt history) to the Memex server.

## Installation

```bash
# Add GPG key
curl -fsSL https://niklasgadau.github.io/memex-linux-client/KEY.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/memex.gpg

# Add APT repository
echo "deb [signed-by=/usr/share/keyrings/memex.gpg] https://niklasgadau.github.io/memex-linux-client stable main" \
  | sudo tee /etc/apt/sources.list.d/memex.list

# Install
sudo apt update && sudo apt install memex-client
```

## Setup

```bash
memex-client setup
```

The setup wizard will:
- Ask for your Memex server URL and API token
- Auto-detect available sources (fish, bash, GPaste, Claude Code)
- Test server connectivity
- Optionally install a systemd timer for background sync

## Usage

```bash
memex-client sync            # Sync all enabled sources
memex-client sync fish       # Sync only fish history
memex-client status          # Show config, sync state, server status
memex-client install         # Install systemd timer (every 5 min)
memex-client uninstall       # Remove systemd timer
```

## Exporters

- **Fish History** — parses `~/.local/share/fish/fish_history` with native timestamps
- **Bash History** — parses `~/.bash_history` (supports `HISTTIMEFORMAT="%s "`)
- **GPaste Clipboard** — parses `~/.local/share/gpaste/history.xml` (text entries)
- **Claude Code Sessions** — crawls `~/.claude/` and ships six document types per tick:
  - **sessions** — one aggregate per session UUID (title, cwd, git branch, message counts, tool list, first/last prompt, recap count)
  - **turns** — one per user prompt, with the merged assistant text response (no `tool_use`/`thinking` blocks, capped at 8 KiB per side)
  - **recaps** — every `system.subtype=="away_summary"` entry (verbatim)
  - **plans** — every `~/.claude/plans/*.md`, re-sent only when its SHA256 changes
  - **memory** — every `~/.claude/projects/*/memory/*.md`, same change-detection
  - **inputs** — every line of the global `~/.claude/history.jsonl` (including `pastedContents`)

  JSONL transcripts and the global history are read append-only via stored byte offsets; plans and memory files use content hashes. See [memex-server/docs/claude-sessions.md](https://github.com/niklasgadau/memex-server/blob/main/docs/claude-sessions.md) for the wire format and index schema.

## Configuration

```toml
# ~/.config/memex/config.toml
api_url = "http://localhost:8080"
api_token = "your-token"
client = "desktop-niklas"

[sources]
fish = true
bash = false
claude_sessions = true

[sources.gpaste]
enabled = true
data_dir = "~/.local/share/gpaste"

[notifications]
enabled = true
```

The state file `~/.local/share/memex/last_sync.json` tracks per-source progress so each tick only sends deltas:

```json
{
  "claude_sessions": {
    "transcripts":   { "<session_uuid>": {"byte_offset": 839318, "mtime": 1779706642} },
    "plans":         { "<plan-slug>": "<sha256-of-body>" },
    "memory":        { "<project-slug>/<filename>.md": "<sha256-of-body>" },
    "input_history": { "byte_offset": 140775 },
    "last_sync_time": "2026-05-25T20:14:32+00:00"
  }
}
```

Client name fallback: `MEMEX_CLIENT` env var > config file > system hostname.

A persistent client ID (`~/.local/share/memex/client_id`) is used for server-side deduplication, ensuring re-setup or reinstall never causes duplicate uploads.

## Related Repos

- [memex-server](https://github.com/niklasgadau/memex-server) — Backend API + Meilisearch
- [memex-chrome-extension](https://github.com/niklasgadau/memex-chrome-extension) — Browser history sync
