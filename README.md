# chrome-skill

Local Chrome browser automation skill — drives system Chrome via a Playwright
RPC daemon and exposes page snapshots for AI agents.

A thin Python wrapper that starts a background daemon hosting two services:

- **WebSocket Server** (port 8765) — Chrome DevTools Protocol bridge.
- **HTTP RPC Server** (port 8766) — CLI / agent requests land here.

## Install

```bash
pip install chrome-skill
playwright install chromium   # one-time, downloads Chromium
```

Requires Windows + Python 3.9+.

## Usage

```bash
chrome-skill serve            # start daemon (foreground)
chrome-skill status           # health check
chrome-skill stop             # stop daemon
chrome-skill browser_snapshot # current page as JSON (url + title + elements)
```

The daemon writes its PID, RPC port, and auth token to
`%LOCALAPPDATA%/chrome-skill/server.json`.

## License

MIT — see `LICENSE`.