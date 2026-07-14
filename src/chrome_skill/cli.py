"""
chrome-skill CLI entry point.

Drives a headless Chrome automation daemon (`chrome_skill.slim_daemon`) via
Playwright. `serve` starts the daemon; every other command sends one RPC call
to `http://127.0.0.1:<rpc_port>/rpc` and prints the JSON response.

Surface mirrors qqbrowser-skill so existing agent scripts that target
qqbrowser-skill work with chrome-skill unchanged. Run `chrome-skill --help`
for the full list.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


# ── daemon RPC plumbing ───────────────────────────────────────────────────────

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


def _rpc(cmd, args=None):
    info = _read_state()
    if not info:
        print("Daemon not running", file=sys.stderr)
        sys.exit(1)
    rpc_port = info.get("rpc_port")
    auth_token = info.get("auth_token", "")
    if not rpc_port:
        print("Daemon state file is missing rpc_port", file=sys.stderr)
        sys.exit(1)
    payload = json.dumps({"cmd": cmd, "args": args or []}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{rpc_port}/rpc", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if auth_token:
        req.add_header("Authorization", f"Bearer {auth_token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            sys.stdout.write(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ── lifecycle ────────────────────────────────────────────────────────────────

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


# ── main ──────────────────────────────────────────────────────────────────────

# Keep this list in sync with slim_daemon.COMMANDS so --help reflects reality.
_BROWSER_COMMANDS = [
    ("browser_go_to_url",           "Navigate to URL in the current tab."),
    ("browser_go_back",             "Go back to the previous page."),
    ("browser_wait",                "Wait for N seconds (default 3)."),
    ("browser_click_element",       "Click on an element by selector."),
    ("browser_input_text",          "Type text into an input element."),
    ("browser_snapshot",            "Get current page as JSON."),
    ("browser_screenshot",          "Take a screenshot to a file path."),
    ("browser_scroll_down",         "Scroll the page down."),
    ("browser_scroll_to_text",      "Scroll the page until text is visible."),
    ("browser_get_info",            "Get current URL and title."),
    ("browser_markdownify",         "Return page body innerText as markdown."),
    ("browser_eval_content_js",     "Evaluate a JS expression; pass the code as the first positional arg."),
]


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="chrome-skill",
        description="Local Chrome browser automation skill.",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="start daemon (foreground)")
    sub.add_parser("status", help="check daemon health")
    sub.add_parser("stop", help="stop daemon")

    for name, help_text in _BROWSER_COMMANDS:
        sub.add_parser(name, help=help_text)

    # parse_known_args: anything after the subcommand is forwarded verbatim
    # to the daemon RPC. We intentionally do NOT declare per-command arg
    # schemas in the CLI; the slim_daemon's COMMANDS table is the source of
    # truth for which positional args each command takes.
    args, rest = parser.parse_known_args(argv)

    if args.cmd is None:
        parser.print_help()
        sys.exit(2)
    if args.cmd == "serve":
        raise SystemExit(subprocess.call([sys.executable, "-m",
                                          "chrome_skill.slim_daemon", "serve"]))
    if args.cmd == "status":
        cmd_status(args)
        return
    if args.cmd == "stop":
        _rpc("stop", [])
        return

    # Any browser_* subcommand: forward `rest` as positional RPC args.
    _rpc(args.cmd, rest)


if __name__ == "__main__":
    main()
