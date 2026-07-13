"""
chrome-skill CLI entry point.

Mirrors the thin surface of qqbrowser-skill:
    chrome-skill serve
    chrome-skill status
    chrome-skill stop
    chrome-skill browser_snapshot

The `serve` command starts a standalone, headless Chrome automation daemon
(`chrome_skill.slim_daemon`) that drives Chrome via Playwright directly and
exposes an HTTP RPC endpoint on a dynamic port. The `browser_snapshot`
command talks to that endpoint.

For the heavyweight multi-tab daemon (Chrome extension bridge), call
`python -m chrome_skill.daemon_server` directly.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def _state_dir():
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~/.local/share")),
        "chrome-skill",
    )


def _state_file_path():
    return os.path.join(_state_dir(), "server.json")


def _read_state():
    sf = _state_file_path()
    if not os.path.exists(sf):
        return None
    try:
        return json.load(open(sf, encoding="utf-8"))
    except Exception:
        return None


def cmd_serve(_args):
    # Delegate to the standalone daemon module. Its own argparse expects
    # `serve` as the only positional subcommand; no extra args to forward
    # for the default surface.
    cmd = [sys.executable, "-m", "chrome_skill.slim_daemon", "serve"]
    raise SystemExit(subprocess.call(cmd))


def cmd_status(_args):
    info = _read_state()
    if not info:
        print("Daemon not running")
        return
    rpc_port = info.get("rpc_port", 9866)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{rpc_port}/health",
                                    timeout=3) as r:
            print(r.read().decode())
    except Exception:
        print("Daemon not running")


def cmd_stop(_args):
    info = _read_state()
    if not info:
        print("Daemon not running")
        return
    rpc_port = info.get("rpc_port", 9866)
    auth_token = info.get("auth_token", "")
    payload = json.dumps({"cmd": "stop", "args": []}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{rpc_port}/rpc", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if auth_token:
        req.add_header("Authorization", f"Bearer {auth_token}")
    try:
        urllib.request.urlopen(req, timeout=5)
        print("Stop signal sent")
    except Exception:
        print("Daemon not running")


def cmd_browser_snapshot(_args):
    info = _read_state()
    if not info:
        print("Daemon not running", file=sys.stderr)
        sys.exit(1)
    rpc_port = info.get("rpc_port")
    auth_token = info.get("auth_token", "")
    if not rpc_port:
        print("Daemon state file is missing rpc_port", file=sys.stderr)
        sys.exit(1)
    payload = json.dumps({"cmd": "browser_snapshot", "args": []}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{rpc_port}/rpc", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if auth_token:
        req.add_header("Authorization", f"Bearer {auth_token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="chrome-skill",
        description="Local Chrome browser automation skill.",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="start daemon (foreground)")
    sub.add_parser("status", help="check daemon health")
    sub.add_parser("stop", help="stop daemon")
    sub.add_parser("browser_snapshot", help="return current page as JSON")

    args = parser.parse_args(argv)

    handlers = {
        "serve": cmd_serve,
        "status": cmd_status,
        "stop": cmd_stop,
        "browser_snapshot": cmd_browser_snapshot,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        parser.print_help()
        sys.exit(2)
    handler(args)


if __name__ == "__main__":
    main()