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
    with open(_state_file_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    try:
        os.chmod(_state_file_path(), 0o600)
    except Exception:
        pass


def read_state_file():
    if not os.path.exists(_state_file_path()):
        return None
    try:
        with open(_state_file_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def remove_state_file():
    try:
        if os.path.exists(_state_file_path()):
            os.remove(_state_file_path())
    except Exception:
        pass


def get_daemon_info():
    info = read_state_file()
    if not info:
        return None
    pid = info.get("pid")
    if pid and not _is_pid_alive(pid):
        remove_state_file()
        return None
    return info


def _is_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


def acquire_single_instance_lock():
    """Returns (lock_fd_or_handle, lock_path). Caller must keep handle alive."""
    import tempfile
    lock_dir = _get_state_dir()
    lock_path = os.path.join(lock_dir, "daemon.lock")
    if sys.platform == "win32":
        import msvcrt
        f = open(lock_path, "a+")
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f, lock_path
        except OSError:
            f.close()
            return None, lock_path
    else:
        import fcntl
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f, lock_path
        except OSError:
            f.close()
            return None, lock_path


# ─── 浏览器状态（playwright 持久上下文） ───────────────────────────────────

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
            # Persistent profile so cookies / localStorage / logins survive
            # daemon restarts. Without this, every `chrome-skill serve` start
            # loses login state and triggers login walls (e.g. xiaohongshu).
            user_data_dir = os.path.join(_STATE_DIR, "chrome-profile")
            os.makedirs(user_data_dir, exist_ok=True)
            browser_instance = pw_instance.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel="chrome",
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            # launch_persistent_context returns a BrowserContext directly;
            # `browser.new_page()` is the way to get a page on it.
            page_instance = browser_instance.new_page()
    return browser_instance, page_instance


def cmd_go_to_url(url):
    _, p = _ensure_browser()
    p.goto(url, wait_until="networkidle", timeout=30000)
    return {"success": True, "url": p.url, "title": p.title()}


def cmd_snapshot():
    _, p = _ensure_browser()
    return {"success": True, "url": p.url, "title": p.title(), "elements": []}


def cmd_screenshot(path):
    _, p = _ensure_browser()
    p.screenshot(path=path, full_page=False)
    return {"success": True, "path": path}


def cmd_click_element(selector):
    _, p = _ensure_browser()
    p.click(selector, timeout=10000)
    return {"success": True}


def cmd_input_text(selector, text):
    _, p = _ensure_browser()
    p.fill(selector, text)
    return {"success": True}


def cmd_wait(sec=3):
    time.sleep(float(sec))
    return {"success": True}


def cmd_scroll_down():
    _, p = _ensure_browser()
    p.mouse.wheel(0, 600)
    return {"success": True}


def cmd_scroll_to_text(text):
    _, p = _ensure_browser()
    p.locator(f"text={text}").first.scroll_into_view_if_needed()
    return {"success": True}


def cmd_go_back():
    _, p = _ensure_browser()
    p.go_back()
    return {"success": True, "url": p.url}


def cmd_get_info():
    _, p = _ensure_browser()
    return {"url": p.url, "title": p.title()}


def cmd_markdownify():
    _, p = _ensure_browser()
    return {"success": True, "url": p.url, "title": p.title(),
            "markdown": p.evaluate("document.body.innerText")}


def cmd_status():
    info = get_daemon_info()
    if info:
        return {"status": "ok", "service": SERVICE_NAME, "pid": info.get("pid")}
    return {"status": "down"}


def cmd_stop():
    global browser_instance, page_instance, pw_instance
    try:
        if pw_instance:
            pw_instance.stop()
    except Exception:
        pass
    browser_instance = None
    page_instance = None
    pw_instance = None
    remove_state_file()
    return {"success": True}


# ─── 命令表 ──────────────────────────────────────────────────────────────────

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
        self._port = port
        self._executor_func = executor_func
        self._server = None
        self.port = port

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            try:
                method, path, _ = request_line.decode("utf-8", errors="replace").split(" ", 2)
            except ValueError:
                writer.close()
                return
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                try:
                    k, v = line.decode("utf-8", errors="replace").split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                except ValueError:
                    pass
            content_length = int(headers.get("content-length", "0") or 0)
            body = b""
            if content_length:
                body = await reader.readexactly(content_length)

            if method == "GET" and path == "/health":
                await self._send_json(writer, 200, {"status": "ok", "service": SERVICE_NAME})
                return
            if method == "POST" and path == "/rpc":
                await self._dispatch_rpc(writer, body, headers)
                return
            await self._send_json(writer, 404, {"error": "Not found"})
        except Exception as e:
            logger.exception("HTTP handler error: %s", e)
            try:
                await self._send_json(writer, 500, {"error": str(e)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch_rpc(self, writer, body, headers):
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception as e:
            await self._send_json(writer, 400, {"error": f"Invalid JSON: {e}"})
            return
        cmd = req.get("cmd", "")
        args = req.get("args", [])
        token = headers.get("authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        if _daemon_auth_token and token != _daemon_auth_token:
            await self._send_json(writer, 401, {"error": "Invalid auth token"})
            return
        fn = COMMANDS.get(cmd)
        if fn is None:
            await self._send_json(writer, 400, {"error": f"Unknown command: {cmd}"})
            return
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: fn(*args))
        except Exception as e:
            logger.exception("RPC error: %s", e)
            await self._send_json(writer, 500, {"error": str(e)})
            return
        await self._send_json(writer, 200, result)

    async def _send_json(self, writer, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found", 500: "Internal Server Error"}.get(code, "OK")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(head + body)
        await writer.drain()


# ─── 启动入口 ─────────────────────────────────────────────────────────────────

def _do_serve(host, ws_port, rpc_port):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    http_server = AsyncHTTPRPCServer(host, rpc_port, None)
    actual_port = loop.run_until_complete(http_server.start())
    write_state_file(os.getpid(), ws_port, actual_port)
    logger.info("Daemon started: pid=%s rpc=%s ws=%s", os.getpid(), actual_port, ws_port)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(http_server.stop())
        loop.close()
        remove_state_file()


def main():
    parser = argparse.ArgumentParser(prog="chrome-skill")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("serve")
    p.add_argument("--host", default=DEFAULT_RPC_HOST)
    p.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT)
    p.add_argument("--rpc-port", type=int, default=DEFAULT_RPC_PORT)
    p = sub.add_parser("status")
    p = sub.add_parser("stop")

    args = parser.parse_args()

    if args.cmd == "serve":
        lock_f, _lock_path = acquire_single_instance_lock()
        if lock_f is None:
            info = get_daemon_info()
            if info:
                print(f"Daemon already running: pid={info.get('pid')} rpc_port={info.get('rpc_port')}", file=sys.stderr)
                sys.exit(2)
            else:
                print("Stale lock; another instance may be starting", file=sys.stderr)
                sys.exit(2)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        try:
            _do_serve(args.host, args.ws_port, args.rpc_port)
        finally:
            try:
                lock_f.close()
            except Exception:
                pass
    elif args.cmd == "status":
        info = get_daemon_info()
        if info:
            print(json.dumps({"status": "ok", "service": SERVICE_NAME, "pid": info.get("pid")}))
        else:
            print("Daemon not running")
    elif args.cmd == "stop":
        info = get_daemon_info()
        if info:
            try:
                import urllib.request
                rpc_port = info.get("rpc_port", DEFAULT_RPC_PORT)
                token = info.get("auth_token", "")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{rpc_port}/rpc",
                    data=json.dumps({"cmd": "stop", "args": []}).encode(),
                )
                req.add_header("Content-Type", "application/json")
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                urllib.request.urlopen(req, timeout=5)
                print("Stop signal sent")
            except Exception:
                pass
            remove_state_file()
        else:
            print("Daemon not running")


if __name__ == "__main__":
    main()