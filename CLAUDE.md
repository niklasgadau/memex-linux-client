# memex-linux-client

## Project

Python-based exporters that sync local shell history (fish + bash) and GPaste clipboard to the memex-server API.

## Architecture

- Exporters are standalone Python scripts, run via systemd user timer
- Config: `~/.config/memex/config.toml` (client name, API URL)
- State tracking: `~/.local/share/memex/last_sync.json` (last sync position per source)
- Client name fallback: MEMEX_CLIENT env → config.toml → hostname

## Conventions

- Each exporter is a single script in `exporters/`
- Target API: memex-server `/api/ingest/{shell,clipboard}`
- Fish timestamps are native (`when:` field); bash needs `HISTTIMEFORMAT="%s "`
- GPaste has no timestamps — use file mtime or null
- Batch POST to server, let Meilisearch handle dedup via document id
