#!/bin/sh
# Best-effort cleanup — timer may not be enabled
systemctl --user disable --now memex-sync.timer 2>/dev/null || true
