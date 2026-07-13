# -*- coding: utf-8 -*-
"""
chrome-skill daemon - asyncio 重写，完全参照 x5use 架构
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ─── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_WS_PORT = 9865
DEFAULT_RPC_PORT = 9866
DEFAULT_RPC_HOST = "127.0.0.1"
FALLBACK_RPC_PORTS = [60124, 60125]
SERVICE_NAME = "chrome-skill-daemon"
MAX_REQUEST_BODY_SIZE = 1 * 1024 * 1024
_STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~/.local/share")), "chrome-skill")
DAEMON_MARKER = "chrome-skill-daemon"

def _get_state_dir():
    os.makedirs(_STATE_DIR, exist_ok=True)
    return _STATE_DIR

def _state_file_path():
    return os.path.join(_get_state_dir(), "server.json")

# ─── 状态文件 ────────────────────────────────────────────────────────────────

_daemon_auth_token: Optional[str] = None

def write_state_file(pid: int, ws_port: int, rpc_port: int):
    global _daemon_auth_token
    _daemon_auth_token = secrets.token_hex(16)
    state = {
        "pid": pid,
        "ws_port": ws_port,
        "rpc_port": rpc_port,
        "service": SERVICE_NAME,
        "auth_token": _daemon_auth_token,
    }
    path = _state_file_path()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"Failed to write state file: {e}")

def read_state_file() -> Optional[dict]:
    path = _state_file_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def remove_state_file():
    try:
        os.remove(_state_file_path())
    except OSError:
        pass

def is_daemon_running() -> bool:
    state = read_state_file()
    if state:
        pid = state.get("pid")
        if pid and _is_pid_alive_windows(pid):
            if _is_our_daemon_on_port(state.get("rpc_port", 0)):
                return True
        remove_state_file()

    found, _ = _find_daemon_process()
    return found is not None

def get_daemon_info() -> Optional[dict]:
    state = read_state_file()
    if state:
        pid = state.get("pid")
        if pid and _is_pid_alive_windows(pid):
            if _is_our_daemon_on_port(state.get("rpc_port", 0)):
                return state
        remove_state_file()

    found_pid, cmdline = _find_daemon_process()
    if found_pid:
        info = {"pid": found_pid, "ws_port": DEFAULT_WS_PORT, "rpc_port": DEFAULT_RPC_PORT}
        info.update(_parse_ports_from_cmdline(cmdline))
        return info
    return None

def _is_pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False

def _find_daemon_process() -> Tuple[Optional[int], str]:
    try:
        result = subprocess.run(
            ["wmic", "process", "where", f"commandline like '%{DAEMON_MARKER}%'",
             "get", "processid,commandline", "/format:csv"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000
        )
        if result.returncode != 0:
            return None, ""
        my_pid = os.getpid()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or DAEMON_MARKER not in line:
                continue
            parts = line.rsplit(",", 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[-1].strip())
            except ValueError:
                continue
            if pid == my_pid:
                continue
            cmdline = ",".join(parts[1:-1])
            return pid, cmdline
    except Exception as e:
        logger.debug(f"Process scan failed: {e}")
    return None, ""

def _parse_ports_from_cmdline(cmdline: str) -> dict:
    info = {}
    for i, part in enumerate(cmdline.split()):
        if part == "--ws-port" and i + 1 < len(cmdline.split()):
            try:
                info["ws_port"] = int(cmdline.split()[i + 1])
            except ValueError:
                pass
        elif part == "--rpc-port" and i + 1 < len(cmdline.split()):
            try:
                info["rpc_port"] = int(cmdline.split()[i + 1])
            except ValueError:
                pass
    return info

def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    if port <= 0:
        return False
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((host, port))
            return True
    except Exception:
        return False

def _is_our_daemon_on_port(port: int, host: str = "127.0.0.1") -> bool:
    if not _is_port_listening(port, host):
        return False
    try:
        import urllib.request
        req = urllib.request.Request(f"http://{host}:{port}/health")
        resp = urllib.request.urlopen(req, timeout=2.0)
        data = json.loads(resp.read().decode())
        if data.get("status") == "ok":
            svc = data.get("service", "")
            return svc == SERVICE_NAME or svc == ""
        return False
    except Exception:
        return False

# ─── 浏览器控制 ──────────────────────────────────────────────────────────────

browser_instance = None
page_instance = None
pw_instance = None
_browser_lock = threading.Lock()

def _ensure_browser():
    global browser_instance, page_instance, pw_instance
    with _browser_lock:
        if browser_instance is None:
            from playwright.sync_api import sync_playwright
            pw_instance = sync_playwright().start()
            browser_instance = pw_instance.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page_instance = browser_instance.new_page()
    return browser_instance, page_instance

def cmd_go_to_url(url):
    _, p = _ensure_browser()
    p.goto(url, wait_until="networkidle", timeout=30000)
    return {"success": True, "url": p.url, "title": p.title()}

def cmd_snapshot():
    _, p = _ensure_browser()
    content = p.content()
    title = p.title()
    url = p.url
    elements = []
    try:
        for elem in p.query_selector_all("a, button, input, select, textarea"):
            tag = elem.evaluate("el => el.tagName")
            text = (elem.inner_text() or "").strip()[:80]
            attrs = {}
            for attr in ["id", "class", "name", "type", "placeholder", "href"]:
                try:
                    v = elem.get_attribute(attr)
                    if v:
                        attrs[attr] = v[:100]
                except Exception:
                    pass
            elements.append({"tag": tag, "text": text, "attrs": attrs})
    except Exception:
        pass
    return {"success": True, "url": url, "title": title, "elements": elements[:300]}

def cmd_screenshot(path="screenshot.png", full_page=False):
    _, p = _ensure_browser()
    p.screenshot(path=path, full_page=full_page)
    return {"success": True, "path": path}

def cmd_click_element(selector):
    _, p = _ensure_browser()
    p.click(selector, timeout=10000)
    return {"success": True}

def cmd_input_text(selector, text):
    _, p = _ensure_browser()
    p.fill(selector, text)
    return {"success": True}

def cmd_wait(seconds=3):
    time.sleep(seconds)
    return {"success": True}

def cmd_scroll_down():
    _, p = _ensure_browser()
    p.evaluate("window.scrollBy(0, document.body.clientHeight)")
    return {"success": True}

def cmd_scroll_to_text(text):
    _, p = _ensure_browser()
    found = p.evaluate(f"""
        () => {{
            const el = Array.from(document.querySelectorAll('*')).find(e => e.innerText && e.innerText.includes({repr(text)})));
            if (el) {{ el.scrollIntoView(); return true; }}
            return false;
        }}
    """)
    return {"success": found}

def cmd_go_back():
    _, p = _ensure_browser()
    p.go_back()
    return {"success": True}

def cmd_get_info():
    _, p = _ensure_browser()
    return {"url": p.url, "title": p.title()}

def cmd_markdownify():
    _, p = _ensure_browser()
    text = p.inner_text("body")
    return {"success": True, "text": text[:5000]}

def cmd_stop():
    global browser_instance, page_instance, pw_instance
    with _browser_lock:
        if page_instance:
            try:
                page_instance.close()
            except Exception:
                pass
            page_instance = None
        if browser_instance:
            try:
                browser_instance.close()
            except Exception:
                pass
            browser_instance = None
        if pw_instance:
            try:
                pw_instance.stop()
            except Exception:
                pass
            pw_instance = None
    return {"success": True}

def cmd_status():
    with _browser_lock:
        alive = browser_instance is not None
    return {"alive": alive, "ws_port": DEFAULT_WS_PORT, "http_port": DEFAULT_RPC_PORT, "browser": "chrome"}

COMMANDS = {
    "browser_go_to_url": cmd_go_to_url,
    "browser_snapshot": cmd_snapshot,
    "browser_screenshot": cmd_screenshot,
    "browser_click_element": cmd_click_element,
    "browser_input_text": cmd_input_text,
    "browser_wait": lambda sec=3: cmd_wait(sec),
    "browser_scroll_down": lambda: cmd_scroll_down(),
    "browser_scroll_to_text": lambda text: cmd_scroll_to_text(text),
    "browser_go_back": lambda: cmd_go_back(),
    "browser_get_info": lambda: cmd_get_info(),
    "browser_markdownify": lambda: cmd_markdownify(),
    "stop": cmd_stop,
    "status": cmd_status,
}

# ─── HTTP RPC Server (asyncio) ────────────────────────────────────────────────

class AsyncHTTPRPCServer:
    def __init__(self, host, port, executor_func):
        self._host = host
        self._requested_port = port
        self._port = port
        self._executor_func = executor_func
        self._server: Optional[asyncio.AbstractServer] = None

    @property
    def port(self):
        return self._port

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._requested_port
        )
        try:
            sockets = self._server.sockets or ()
            if sockets:
                self._port = sockets[0].getsockname()[1]
            else:
                self._port = self._requested_port
        except Exception:
            self._port = self._requested_port
        logger.info(f"HTTP RPC listening on http://{self._host}:{self._port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("HTTP RPC stopped")

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not request_line:
                writer.close()
                return
            request_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_str.split(" ")
            if len(parts) < 2:
                writer.close()
                return
            method = parts[0].upper()
            path = parts[1]

            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    break
                if ":" in line_str:
                    k, v = line_str.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = int(headers.get("content-length", 0))
            if content_length > MAX_REQUEST_BODY_SIZE:
                await self._send_response(writer, 413, {"error": "Request too large"})
                return

            body_bytes = b""
            if content_length > 0:
                body_bytes = await asyncio.wait_for(reader.readexactly(content_length), timeout=30)

            if path == "/health":
                await self._send_response(writer, 200, {
                    "status": "ok",
                    "service": SERVICE_NAME,
                    "pid": os.getpid(),
                })
            elif path == "/status":
                await self._send_response(writer, 200, cmd_status())
            elif method == "POST" and path == "/rpc":
                try:
                    body = json.loads(body_bytes.decode("utf-8", errors="replace"))
                    cmd = body.get("cmd", "")
                    args = body.get("args", [])
                    fn = COMMANDS.get(cmd)
                    if fn:
                        # Playwright sync API must run outside asyncio loop
                        result = await asyncio.to_thread(fn, *args) if args else await asyncio.to_thread(fn)
                    else:
                        result = {"error": f"Unknown command: {cmd}"}
                    await self._send_response(writer, 200, result)
                except Exception as e:
                    await self._send_response(writer, 500, {"error": str(e)})
            else:
                await self._send_response(writer, 404, {"error": "Not found"})
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_response(self, writer, status_code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        lines = [
            f"HTTP/1.1 {status_code} OK",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "", "",
        ]
        writer.write(b"\r\n".join(l.encode() for l in lines) + body)
        await writer.drain()

# ─── WebSocket Server ────────────────────────────────────────────────────────

async def run_ws_server(port):
    import websockets
    async def handler(ws, path):
        async for msg in ws:
            try:
                data = json.loads(msg)
                cmd = data.get("cmd", "")
                args = data.get("args", [])
                fn = COMMANDS.get(cmd)
                result = await asyncio.to_thread(fn, *args) if args else await asyncio.to_thread(fn)
                await ws.send(json.dumps(result))
            except Exception as e:
                await ws.send(json.dumps({"error": str(e)}))

    async with websockets.serve(handler, "127.0.0.1", port):
        await asyncio.Future()

# ─── 主入口 ─────────────────────────────────────────────────────────────────

async def async_main(host, ws_port, rpc_port):
    http_server = AsyncHTTPRPCServer(host, rpc_port, None)
    await http_server.start()
    actual_rpc_port = http_server.port

    pid = os.getpid()
    write_state_file(pid, ws_port, actual_rpc_port)
    logger.info(f"Daemon started: PID={pid}, HTTP={actual_rpc_port}, WS={ws_port}")

    # 启动 WS server
    ws_task = asyncio.create_task(run_ws_server(ws_port))

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        cmd_stop()
        remove_state_file()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await asyncio.Future()
    finally:
        ws_task.cancel()
        await http_server.stop()

def main():
    global DEFAULT_RPC_PORT, DEFAULT_WS_PORT

    parser = argparse.ArgumentParser(prog="chrome-skill")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT)
    p.add_argument("--rpc-port", type=int, default=0)  # 0 = 由系统分配

    p = sub.add_parser("status")
    p = sub.add_parser("stop")

    args = parser.parse_args()

    if args.cmd == "serve":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        asyncio.run(async_main(args.host, args.ws_port, args.rpc_port))
    elif args.cmd == "status":
        info = get_daemon_info()
        if info:
            print(json.dumps(info, indent=2))
        else:
            print("Daemon not running")
    elif args.cmd == "stop":
        info = get_daemon_info()
        if info:
            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://127.0.0.1:{info.get('rpc_port', DEFAULT_RPC_PORT)}/rpc",
                    data=json.dumps({"cmd": "stop", "args": []}).encode(),
                    timeout=5
                )
            except Exception:
                pass
            print("Stop signal sent")
        else:
            print("Daemon not running")

if __name__ == "__main__":
    main()
