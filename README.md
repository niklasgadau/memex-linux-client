# memex-linux-client

Linux client for Memex — syncs shell history and clipboard to the Memex server.

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
- Auto-detect available sources (fish, bash, GPaste)
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

## Configuration

```toml
# ~/.config/memex/config.toml
api_url = "http://localhost:8080"
api_token = "your-token"
client = "desktop-niklas"

[sources]
fish = true
bash = false

[sources.gpaste]
enabled = true
data_dir = "~/.local/share/gpaste"

[notifications]
enabled = true
```

Client name fallback: `MEMEX_CLIENT` env var > config file > system hostname.

A persistent client ID (`~/.local/share/memex/client_id`) is used for server-side deduplication, ensuring re-setup or reinstall never causes duplicate uploads.

## Related Repos

- [memex-server](https://github.com/niklasgadau/memex-server) — Backend API + Meilisearch
- [memex-chrome-extension](https://github.com/niklasgadau/memex-chrome-extension) — Browser history sync
