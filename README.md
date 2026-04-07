# memex-linux-client

Linux exporters for Memex — syncs shell history and clipboard to the Memex server.

## Exporters

- **Shell History** — fish (with timestamps) and bash (with `HISTTIMEFORMAT`)
- **GPaste Clipboard** — text entries and images (with OCR on the server)

## Configuration

```toml
# ~/.config/memex/config.toml
client = "desktop-niklas"
api_url = "http://localhost:8080"
```

Fallback chain for client name: `MEMEX_CLIENT` env var → config file → system hostname.

## Scheduling

Runs via systemd user timer (every 5 minutes).

## Related Repos

- [memex-server](https://github.com/niklasgadau/memex-server) — Backend API + Meilisearch
- [memex-chrome-extension](https://github.com/niklasgadau/memex-chrome-extension) — Browser history sync
