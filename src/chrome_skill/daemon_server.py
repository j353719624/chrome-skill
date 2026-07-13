"""
Daemon Server - Persistent background service for Chrome Browser Skill.

Hosts two servers:
  1. WebSocket Server (port 8765) — browser extension connects here (long-lived).
  2. HTTP RPC Server (port 8766) — CLI processes connect here to send skill requests.

Usage:
    chrome-skill serve                     # foreground mode
    chrome-skill serve --daemon            # background (daemon) mode
    chrome-skill serve --ws-port 8765 --rpc-port 8766
"""

import asyncio
import errno
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
from http import HTTPStatus
from typing import Optional, Tuple

from .skill_registry import get_executor, SKILL_MAP
from . import vnc_util
from .vnc_proxy import get_vnc_proxy_url
from .constants import DEFAULT_LOG_DIR, DEFAULT_DATA_DIR
from .report_log import init_log, remote_logger, _sanitize_args


def _get_version() -> str:
    """获取当前包版本号。优先从 importlib.metadata 读取，失败则返回 unknown。"""
    try:
        from importlib.metadata import version
        return version("chrome-skill")
    except Exception:
        return "unknown"

logger = logging.getLogger(__name__)

# Default ports
DEFAULT_WS_PORT = 8765
DEFAULT_RPC_PORT = 8766
DEFAULT_RPC_HOST = "127.0.0.1"

# RPC 端口回退：当默认端口被占用时依次尝试的固定备用端口。
# 若备用端口也全部被占用，最终会回退到 0（由操作系统分配的随机可用端口）。
FALLBACK_RPC_PORTS = [60124, 60125]

# 服务身份签名：/health 和 /status 端点返回该字段，
# 供 CLI / 端口探测逻辑区分"本守护进程"与"其它占用同端口的无关进程"。
SERVICE_NAME = "chrome-skill-daemon"

# HTTP 请求体最大大小限制（1MB），防止恶意大请求体导致内存耗尽（DoS）
MAX_REQUEST_BODY_SIZE = 1 * 1024 * 1024  # 1 MB

# PID / state file location
_STATE_DIR: Optional[str] = None

# 认证 Token：daemon 启动时生成，CLI 通过状态文件读取后在请求中携带。
# 用于防止恶意网页通过本地端口调用 /execute 接口（CSRF 攻击）。
_daemon_auth_token: Optional[str] = None


def _build_rpc_port_candidates(user_port: int, user_explicit: bool) -> list:
    """构造 RPC 端口候选列表。

    - 当用户通过 ``--rpc-port`` 显式指定了一个非默认端口时，完全尊重用户意图，
      仅在用户指定端口失败时回退到随机端口（``0``），不再注入默认端口与备用端口。
    - 当用户未显式指定（或显式指定的就是默认值 ``DEFAULT_RPC_PORT``）时，
      使用完整候选列表：``[DEFAULT_RPC_PORT, *FALLBACK_RPC_PORTS, 0]``。
    - 列表按保序去重，避免重复尝试同一端口。

    Args:
        user_port: 用户通过 CLI 传入的 ``--rpc-port`` 参数值（或上层函数的 ``rpc_port``）。
        user_explicit: 是否为用户显式指定（``True``）还是使用了默认值（``False``）。

    Returns:
        一个保序去重后的端口候选列表，末尾始终含 ``0`` 作为随机端口兜底。
    """
    if user_explicit and user_port != DEFAULT_RPC_PORT:
        # 用户显式指定了非默认端口：仅使用该端口 + 随机端口兜底
        raw = [user_port, 0]
    else:
        # 未显式指定，或显式指定的恰好就是默认端口：使用完整候选列表
        raw = [DEFAULT_RPC_PORT, *FALLBACK_RPC_PORTS, 0]

    # 保序去重
    seen = set()
    result = []
    for p in raw:
        if p in seen:
            continue
        seen.add(p)
        result.append(p)
    return result


def _get_state_dir() -> str:
    """Return the directory used for PID/state files.

    使用统一的数据目录 DEFAULT_DATA_DIR：
    - Windows:     %LOCALAPPDATA%/chrome-skill
    - Linux/macOS: ~/.chrome-skill
    """
    global _STATE_DIR
    if _STATE_DIR is not None:
        return _STATE_DIR

    _STATE_DIR = DEFAULT_DATA_DIR
    os.makedirs(_STATE_DIR, exist_ok=True)
    return _STATE_DIR


def _state_file_path() -> str:
    return os.path.join(_get_state_dir(), "server.json")


def write_state_file(pid: int, ws_port: int, rpc_port: int, from_qbotclaw: bool = False,
                     auth_token: Optional[str] = None):
    """Write the daemon state (PID + ports + mode + auth token) to a JSON file."""
    state = {"pid": pid, "ws_port": ws_port, "rpc_port": rpc_port, "from_qbotclaw": from_qbotclaw}
    if auth_token:
        state["auth_token"] = auth_token
    path = _state_file_path()
    try:
        # 状态文件包含 auth_token，设置严格的文件权限（仅当前用户可读写）
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        logger.info(f"State file written: {path} -> pid={pid}, ws_port={ws_port}, rpc_port={rpc_port}")
    except Exception as e:
        logger.error(f"Failed to write state file {path}: {e}")


def read_state_file() -> Optional[dict]:
    """Read the daemon state file. Returns None if not found or invalid."""
    path = _state_file_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read state file {path}: {e}")
        return None


def remove_state_file():
    """Remove the daemon state file."""
    path = _state_file_path()
    try:
        os.remove(path)
    except OSError as e:
        logger.warning(f"Failed to remove state file {path}: {e}")


def is_daemon_running() -> bool:
    """Check if the daemon is currently running.

    检测顺序：
    1. 优先通过状态文件 + PID 验活 + 服务签名校验
    2. 状态文件不存在 / 校验失败时，回退到进程扫描（通过命令行特征匹配）
    3. 进程扫描也找不到时，回退到端口探测：依次检查默认 RPC 端口与所有备用端口，
       每个端口必须通过 ``_is_our_daemon_on_port`` 的身份校验才算数
    """
    # 路径1：状态文件 + PID 验活 + 服务签名校验
    state = read_state_file()
    if state is not None:
        pid = state.get("pid")
        state_rpc_port = state.get("rpc_port")
        pid_alive = False
        if pid is not None:
            pid_alive = _is_pid_alive_windows(pid) if sys.platform == "win32" else _is_pid_alive_unix(pid)

        if pid_alive and isinstance(state_rpc_port, int) and state_rpc_port > 0:
            # 进一步通过服务签名校验，避免"陈旧状态文件 + 新占用者"场景
            if _is_our_daemon_on_port(state_rpc_port):
                return True
            # 校验失败：状态文件中记录的端口上跑的不是本服务，视为陈旧
            logger.info(
                f"状态文件记录的 RPC 端口 {state_rpc_port} 身份校验失败，清理陈旧状态文件"
            )
            remove_state_file()
        elif not pid_alive:
            # PID 已死，状态文件也没意义了
            logger.info(f"状态文件记录的 PID {pid} 已不存在，清理陈旧状态文件")
            remove_state_file()

    # 路径2：进程扫描兜底
    found_pid, _ = _find_daemon_process()
    if found_pid is not None:
        return True

    # 路径3：端口探测兜底（依次探测默认端口与备用端口；每个端口必须通过身份校验）
    for p in [DEFAULT_RPC_PORT, *FALLBACK_RPC_PORTS]:
        if _is_our_daemon_on_port(p):
            return True

    return False


def get_daemon_info() -> Optional[dict]:
    """获取 daemon 进程信息，综合状态文件和进程扫描。

    返回 dict 包含 pid, ws_port, rpc_port，或 None（未运行）。
    对所有"可能"的 daemon 端点都会做服务签名校验，避免把陌生进程识别为本服务。
    """
    # 优先从状态文件获取
    state = read_state_file()
    if state is not None:
        pid = state.get("pid")
        state_rpc_port = state.get("rpc_port")
        pid_alive = False
        if pid is not None:
            pid_alive = _is_pid_alive_windows(pid) if sys.platform == "win32" else _is_pid_alive_unix(pid)

        if pid_alive and isinstance(state_rpc_port, int) and state_rpc_port > 0:
            # 通过服务签名校验确认状态文件与实际进程一致
            if _is_our_daemon_on_port(state_rpc_port):
                return state
            # 校验失败 → 视为陈旧
            logger.info(
                f"状态文件记录的 RPC 端口 {state_rpc_port} 身份校验失败，清理陈旧状态文件"
            )
            remove_state_file()
        elif not pid_alive:
            logger.info(f"状态文件记录的 PID {pid} 已不存在，清理陈旧状态文件")
            remove_state_file()

    # 回退：进程扫描
    found_pid, cmdline = _find_daemon_process()
    if found_pid is not None:
        info = {"pid": found_pid, "ws_port": DEFAULT_WS_PORT, "rpc_port": DEFAULT_RPC_PORT}
        # 尝试从命令行参数中解析端口
        info.update(_parse_ports_from_cmdline(cmdline))
        return info

    # 回退：端口探测（依次探测默认端口与备用端口，必须通过身份校验）
    for p in [DEFAULT_RPC_PORT, *FALLBACK_RPC_PORTS]:
        if _is_our_daemon_on_port(p):
            return {"pid": "?", "ws_port": DEFAULT_WS_PORT, "rpc_port": p}

    return None


def _is_pid_alive_unix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def _is_pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 进程扫描：通过命令行特征查找 daemon 进程
# ---------------------------------------------------------------------------

_DAEMON_CMDLINE_MARKER = "chrome_skill.daemon_server"


def _find_daemon_process() -> Tuple[Optional[int], str]:
    """扫描进程列表，查找正在运行的 daemon 进程。

    通过匹配命令行中的 'chrome_skill.daemon_server' 特征来识别。
    返回 (pid, cmdline_str) 或 (None, "")。
    """
    try:
        if sys.platform == "win32":
            return _find_daemon_process_windows()
        else:
            return _find_daemon_process_unix()
    except Exception as e:
        logger.debug(f"进程扫描失败: {e}")
        return None, ""


def _find_daemon_process_unix() -> Tuple[Optional[int], str]:
    """Unix/macOS：使用 ps 命令扫描进程。"""
    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "pid,command"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None, ""

        my_pid = os.getpid()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if _DAEMON_CMDLINE_MARKER not in line:
                continue
            # 排除 grep 自身和当前进程
            if "grep" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid == my_pid:
                continue
            return pid, parts[1]
    except Exception as e:
        logger.debug(f"Unix 进程扫描失败: {e}")
    return None, ""


def _find_daemon_process_windows() -> Tuple[Optional[int], str]:
    """Windows：使用 WMIC 命令扫描进程。"""
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             f"commandline like '%{_DAEMON_CMDLINE_MARKER}%'",
             "get", "processid,commandline", "/format:csv"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        if result.returncode != 0:
            return None, ""

        my_pid = os.getpid()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or _DAEMON_CMDLINE_MARKER not in line:
                continue
            # CSV 格式: Node,CommandLine,ProcessId
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[-1].strip())
            except ValueError:
                continue
            if pid == my_pid:
                continue
            cmdline = ",".join(parts[1:-1])  # CommandLine 可能包含逗号
            return pid, cmdline
    except Exception as e:
        logger.debug(f"Windows 进程扫描失败: {e}")
    return None, ""


def _parse_ports_from_cmdline(cmdline: str) -> dict:
    """从命令行字符串中解析 --ws-port 和 --rpc-port 参数。"""
    info = {}
    parts = cmdline.split()
    for i, part in enumerate(parts):
        if part == "--ws-port" and i + 1 < len(parts):
            try:
                info["ws_port"] = int(parts[i + 1])
            except ValueError:
                pass
        elif part == "--rpc-port" and i + 1 < len(parts):
            try:
                info["rpc_port"] = int(parts[i + 1])
            except ValueError:
                pass
    return info


def _is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    """检测指定端口是否有服务在监听。"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            return True
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


def _is_our_daemon_on_port(port: int, host: str = "127.0.0.1",
                          identity_timeout: float = 1.5) -> bool:
    """判断指定端口上的服务是否为本守护进程。

    两道校验：
      1. TCP 连接能否建立（:func:`_is_port_listening`）；
      2. ``GET /health`` 返回合法 JSON 且 ``service`` 字段匹配 :data:`SERVICE_NAME`。

    任一失败即返回 ``False``；任何异常（超时 / 连接重置 / JSON 解析失败）
    都会被降级为 ``False``，避免阻塞 CLI 正常流程。
    """
    if not _is_port_listening(port, host=host, timeout=2.0):
        return False

    # 延迟导入以避免循环依赖
    from . import rpc_client

    try:
        return asyncio.run(
            rpc_client._verify_daemon_identity(host=host, port=port, timeout=identity_timeout)
        )
    except RuntimeError:
        # 已有 event loop 在运行（罕见：从 async 上下文同步调用）
        # 此时无法用 asyncio.run，降级为"未识别"，让调用方按正常流程处理
        logger.debug("_is_our_daemon_on_port: 已有 event loop 运行，无法执行身份校验，降级为 False")
        return False
    except Exception as e:
        logger.debug(f"_is_our_daemon_on_port({port}): 身份校验异常: {e!r}")
        return False


# ---------------------------------------------------------------------------
# HTTP RPC Server (for CLI -> Daemon communication)
# ---------------------------------------------------------------------------

class SimpleHTTPRPCServer:
    """A minimal async HTTP server for receiving skill execution requests from CLI.

    Uses only Python stdlib (asyncio streams) to avoid extra dependencies.
    Supports:
      POST /execute  — execute a skill
      GET  /health   — health check
      GET  /status   — daemon status (connected clients, etc.)
    """

    def __init__(self, host: str, port: int, executor_func):
        self._host = host
        # \u7528\u6237\u4f20\u5165\u7684\u7aef\u53e3\uff08\u53ef\u80fd\u4e3a 0\uff0c\u8868\u793a\u7531\u5185\u6838\u5206\u914d\u968f\u673a\u7aef\u53e3\uff09
        self._requested_port = port
        # \u5b9e\u9645\u7ed1\u5b9a\u6210\u529f\u540e\u7684\u7aef\u53e3\uff08\u4ec5\u5728 start() \u6210\u529f\u540e\u624d\u6709\u503c\uff09
        self._port = port
        self._executor_func = executor_func
        self._server: Optional[asyncio.AbstractServer] = None

    @property
    def port(self) -> int:
        """\u8fd4\u56de RPC \u670d\u52a1\u5b9e\u9645\u76d1\u542c\u7684\u7aef\u53e3\u3002

        - \u5728 :meth:`start` \u6210\u529f\u8fd4\u56de\u524d\u4e0e\u8c03\u7528\u8005\u4f20\u5165\u7684\u7aef\u53e3\u76f8\u540c\uff1b
        - ``start()`` \u6210\u529f\u540e\uff0c\u5982\u679c\u8c03\u7528\u8005\u4f20\u5165 ``0``\uff0c\u8be5\u5c5e\u6027\u4f1a\u53cd\u6620\u64cd\u4f5c\u7cfb\u7edf\u5206\u914d\u7684\u968f\u673a\u7aef\u53e3\u3002
        """
        return self._port

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._requested_port
        )
        # \u5c24\u5176\u5f53\u8c03\u7528\u8005\u4f20\u5165 port=0 \u65f6\uff0c\u5fc5\u987b\u901a\u8fc7 getsockname() \u89e3\u6790\u51fa\u5185\u6838\u5b9e\u9645\u5206\u914d\u7684\u7aef\u53e3\u3002
        try:
            sockets = self._server.sockets or ()
            if sockets:
                self._port = sockets[0].getsockname()[1]
            else:
                self._port = self._requested_port
        except Exception:
            # \u89e3\u6790\u5931\u8d25\u65f6\u56de\u9000\u5230\u8bf7\u6c42\u7684\u7aef\u53e3\uff08\u6781\u5c11\u53d1\u751f\uff09
            self._port = self._requested_port
        logger.info(f"HTTP RPC Server listening on http://{self._host}:{self._port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("HTTP RPC Server stopped")

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a single HTTP connection."""
        try:
            # Read the request line and headers
            request_line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_str.split(" ")
            if len(parts) < 2:
                await self._send_response(writer, 400, {"error": "Bad request"})
                return

            method = parts[0].upper()
            path = parts[1]

            # Read headers
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    break
                if ":" in line_str:
                    key, _, value = line_str.partition(":")
                    headers[key.strip().lower()] = value.strip()

            # Read body if Content-Length is present
            body = b""
            content_length = int(headers.get("content-length", 0))
            if content_length > MAX_REQUEST_BODY_SIZE:
                logger.warning(f"Rejected request: Content-Length {content_length} exceeds limit {MAX_REQUEST_BODY_SIZE}")
                await self._send_response(writer, 413, {"error": f"Request body too large (max {MAX_REQUEST_BODY_SIZE} bytes)"})
                return
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=300)

            # Route
            if method == "GET" and path == "/health":
                await self._handle_health(writer)
            elif method == "GET" and path == "/status":
                await self._handle_status(writer, headers)
            elif method == "POST" and path == "/execute":
                await self._handle_execute(writer, body, headers)
            else:
                await self._send_response(writer, 404, {"error": "Not found"})

        except asyncio.TimeoutError:
            logger.warning("HTTP RPC: connection timed out")
            try:
                await self._send_response(writer, 408, {"error": "Request timeout"})
            except Exception:
                pass
        except Exception as e:
            logger.error(f"HTTP RPC: error handling connection: {e}")
            try:
                await self._send_response(writer, 500, {"error": str(e)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_health(self, writer: asyncio.StreamWriter):
        """Health check endpoint.

        返回体中包含 ``service`` 字段用于身份识别，帮助 CLI 区分
        "本守护进程" 与 "其它恰好占用相同端口的无关进程"。
        """
        await self._send_response(writer, 200, {
            "status": "ok",
            "service": SERVICE_NAME,
        })

    async def _handle_status(self, writer: asyncio.StreamWriter, headers: dict = None):
        """Status endpoint — returns daemon info.

        需要 Authorization: Bearer <token> 认证，防止信息泄露。
        """
        # Token 认证校验
        if _daemon_auth_token is None:
            await self._send_response(writer, 503, {"error": "Service not ready: auth token not initialized"})
            return
        auth_header = (headers or {}).get("authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("Rejected /status request: missing or invalid Authorization header")
            await self._send_response(writer, 401, {"error": "Missing or invalid Authorization header"})
            return
        token = auth_header[len("Bearer "):]
        if not secrets.compare_digest(token, _daemon_auth_token):
            logger.warning("Rejected /status request: invalid auth token")
            await self._send_response(writer, 401, {"error": "Invalid auth token"})
            return

        executor = self._executor_func()
        ws_mgr = executor.ws_manager
        status = {
            "status": "running",
            "service": SERVICE_NAME,
            "ws_server_started": ws_mgr.is_server_started(),
            "connected_clients": len(ws_mgr._connected_clients),
        }
        await self._send_response(writer, 200, status)

    async def _handle_execute(self, writer: asyncio.StreamWriter, body: bytes, headers: dict = None):
        """Execute a skill request from CLI.

        需要 Authorization: Bearer <token> 认证，token 来自 server.json 状态文件。
        """
        # Token 认证校验（fail-close：Token 未初始化时也拒绝请求）
        if _daemon_auth_token is None:
            logger.error("Auth token not initialized, rejecting /execute request (fail-close)")
            await self._send_response(writer, 503, {"error": "Service not ready: auth token not initialized"})
            return
        auth_header = (headers or {}).get("authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("Rejected /execute request: missing or invalid Authorization header")
            await self._send_response(writer, 401, {"error": "Missing or invalid Authorization header"})
            return
        token = auth_header[len("Bearer "):]
        if not secrets.compare_digest(token, _daemon_auth_token):
            logger.warning("Rejected /execute request: invalid auth token")
            await self._send_response(writer, 401, {"error": "Invalid auth token"})
            return

        try:
            request = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            await self._send_response(writer, 400, {"error": f"Invalid JSON: {e}"})
            return

        skill_name = request.get("skill_name")
        args = request.get("args", {})

        if not skill_name:
            await self._send_response(writer, 400, {"error": "Missing 'skill_name'"})
            return

        if skill_name not in SKILL_MAP:
            await self._send_response(writer, 400, {"error": f"Unknown skill: {skill_name}"})
            return

        remote_logger.info("RPC 收到 skill 执行请求: skill=[%s], args=%s", skill_name, _sanitize_args(args))

        try:
            # Set browser log dir (固定使用默认日志目录，不允许外部指定)
            vnc_util.set_browser_log_dir(DEFAULT_LOG_DIR)

            executor = self._executor_func()
            result = await executor.execute(skill_name, **args)
            await self._send_response(writer, 200, {
                "success": True,
                "result": result.to_dict(),
            })
        except Exception as e:
            remote_logger.error("RPC 请求处理失败: skill=%s, error=%s", skill_name, e)
            await self._send_response(writer, 500, {
                "success": False,
                "error": str(e),
            })

    async def _send_response(self, writer: asyncio.StreamWriter, status_code: int, body: dict):
        """Send an HTTP response with JSON body."""
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status_phrase = HTTPStatus(status_code).phrase
        response = (
            f"HTTP/1.1 {status_code} {status_phrase}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8") + body_bytes
        try:
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Daemon entry point
# ---------------------------------------------------------------------------

async def run_daemon(ws_port: int = DEFAULT_WS_PORT,
                     rpc_port: int = DEFAULT_RPC_PORT,
                     log_dir: str = DEFAULT_LOG_DIR,
                     from_qbotclaw: bool = False,
                     rpc_port_explicit: bool = False):
    """Start the persistent daemon service (foreground, blocking).

    This coroutine starts both the WebSocket server and the HTTP RPC server,
    then blocks forever until interrupted.

    Args:
        ws_port: WebSocket 服务端口。
        rpc_port: RPC 服务首选端口。当 ``rpc_port_explicit=False`` 时会启用端口回退机制
            （尝试 ``DEFAULT_RPC_PORT`` -> ``FALLBACK_RPC_PORTS`` -> 随机端口）。
        log_dir: 日志目录。
        from_qbotclaw: 是否由 qbotclaw 启动。
        rpc_port_explicit: 用户是否显式通过 ``--rpc-port`` 指定了该端口。
            ``True`` 表示仅尝试该端口 + 随机端口兜底，尊重用户意图。
    """
    # 确保 daemon 运行期间日志写入文件
    _ensure_daemon_file_logging(log_dir)

    # 初始化日志上报参数（guid、qimei36、skill_version）
    init_log()

    executor = get_executor()
    # 将 qbotclaw 模式传递给 executor，跳过浏览器进程检查和启动
    if from_qbotclaw:
        executor.set_from_qbotclaw(True)
    rpc_server = None

    # 1. Start WebSocket server for browser extension
    ws_mgr = executor.ws_manager
    try:
        await _start_ws_server_on_port(ws_mgr, ws_port)
    except Exception as e:
        remote_logger.error(f"WebSocket 服务器启动失败，daemon 无法运行: {e}")
        # 启动失败，不写状态文件，直接清理退出
        await executor.cleanup()
        return

    # 2. Start HTTP RPC server for CLI（支持端口回退：默认 → 备用 → 随机）
    rpc_candidates = _build_rpc_port_candidates(rpc_port, rpc_port_explicit)
    rpc_server, actual_rpc_port = await _start_rpc_server_with_fallback(
        rpc_candidates, executor.cleanup
    )
    if rpc_server is None:
        # 全部候选端口启动失败，executor.cleanup 已在辅助函数中调用
        return

    # 3. 生成认证 Token 并写入状态文件
    global _daemon_auth_token
    _daemon_auth_token = secrets.token_urlsafe(32)
    write_state_file(pid=os.getpid(), ws_port=ws_port, rpc_port=actual_rpc_port,
                     from_qbotclaw=from_qbotclaw, auth_token=_daemon_auth_token)

    # 是否发生了端口回退（即实际端口与首选端口不同），以及是否最终落在随机端口
    preferred_port = rpc_candidates[0]
    fell_back = actual_rpc_port != preferred_port
    is_random_port = 0 in rpc_candidates and actual_rpc_port not in rpc_candidates[:-1]

    mode_label = "qbotclaw" if from_qbotclaw else "normal"
    logger.info("=" * 60)
    logger.info(f"Chrome Browser Skill Daemon is running")
    logger.info(f"  Version          : {_get_version()}")
    logger.info(f"  Mode             : {mode_label}")
    logger.info(f"  WebSocket Server : ws://127.0.0.1:{ws_port}  (browser extension)")
    logger.info(f"  HTTP RPC Server  : http://127.0.0.1:{actual_rpc_port}  (CLI requests)")
    logger.info(f"  PID              : {os.getpid()}")
    logger.info(f"  State file       : {_state_file_path()}")
    logger.info("=" * 60)

    if fell_back:
        random_suffix = " (random port)" if is_random_port else ""
        fallback_msg = (
            f"⚠️  Preferred RPC port {preferred_port} was occupied, "
            f"fell back to port {actual_rpc_port}{random_suffix}"
        )
        logger.warning(fallback_msg)
        remote_logger.warning(
            "RPC 端口发生回退: preferred=%s, actual=%s, random=%s",
            preferred_port, actual_rpc_port, is_random_port,
        )

    # 远程上报：Daemon 启动完成（使用实际端口）
    remote_logger.info(
        "Daemon 启动完成: ws_port=%s, rpc_port=%s, mode=%s, pid=%s",
        ws_port, actual_rpc_port, mode_label, os.getpid(),
    )

    # 4. Block forever
    stop_event = asyncio.Event()

    def _signal_handler(sig_num: int):
        sig_name = signal.Signals(sig_num).name
        remote_logger.info("Daemon 收到关闭信号: %s (signal=%s)", sig_name, sig_num)
        # 立即设置 shutting_down 标志，让 send_message 中的等待循环快速退出
        # 这比等到 cleanup() 被调用时再设置更早，避免 send_message 阻塞 cleanup
        ws_mgr._shutting_down = True
        stop_event.set()

    loop = asyncio.get_running_loop()

    if sys.platform != "win32":
        # Unix: use loop.add_signal_handler
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler, sig)
    else:
        # Windows: signal handlers in asyncio are limited;
        # KeyboardInterrupt (Ctrl+C) will be caught by the caller.
        pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        remote_logger.info("Daemon 开始关闭")
        # 先关闭 RPC 服务器，拒绝新的请求
        if rpc_server:
            await rpc_server.stop()
        # 再清理 WebSocket 服务器和所有客户端连接
        # executor.cleanup() 内部会调用 ws_manager.cleanup()，
        # 关闭 WS server 并断开所有客户端，从而使 send_message 循环立即退出
        await executor.cleanup()
        remove_state_file()
        remote_logger.info("Daemon 关闭完成")


async def _start_ws_server_on_port(ws_mgr, port: int):
    """Start the WebSocket server on the specified port.

    Delegates to WebSocketManager.start_server(port) which handles
    duplicate-start protection, IP allow-list, and all internal state.
    """
    await ws_mgr.start_server(port=port)


def _is_port_in_use_error(exc: BaseException) -> bool:
    """判断异常是否属于"端口被占用类"错误。

    符合条件会触发端口回退机制：
    - ``PermissionError``（Windows/Unix 上权限不足绑定到该端口）
    - ``OSError`` 且 ``errno`` 为 ``EADDRINUSE`` / ``EACCES`` / ``EADDRNOTAVAIL``
    """
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        err_code = getattr(exc, "errno", None)
        if err_code in (errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL):
            return True
        # Windows 下 WSAEACCES(10013) / WSAEADDRINUSE(10048)
        if err_code in (10013, 10048):
            return True
    return False


async def _start_rpc_server_with_fallback(candidates: list, executor_cleanup):
    """按候选端口列表依次尝试启动 RPC 服务器。

    Args:
        candidates: 由 :func:`_build_rpc_port_candidates` 返回的端口候选列表，
            末尾通常含 ``0`` 作为随机端口兜底。
        executor_cleanup: 所有候选端口均失败时，用于清理 executor 的异步回调
            （保持与现有失败路径一致）。

    Returns:
        成功时返回 ``(SimpleHTTPRPCServer, actual_port)`` 二元组；
        全部失败时返回 ``(None, None)``。在全部失败分支已调用 ``executor_cleanup()``。
    """
    logger.info(f"RPC 端口候选列表: {candidates}")
    remote_logger.info("RPC 端口候选列表: %s", candidates)

    last_exc: Optional[BaseException] = None
    for idx, port in enumerate(candidates):
        rpc_server = SimpleHTTPRPCServer(DEFAULT_RPC_HOST, port, get_executor)
        try:
            await rpc_server.start()
            actual_port = rpc_server.port
            return rpc_server, actual_port
        except BaseException as e:
            last_exc = e
            if _is_port_in_use_error(e):
                port_label = "random (0)" if port == 0 else str(port)
                logger.warning(
                    f"RPC 端口 {port_label} 被占用或不可用，尝试下一个候选端口: {e!r}"
                )
                remote_logger.warning(
                    "RPC 端口 %s 被占用或不可用，尝试下一个候选端口: %r", port_label, e
                )
                continue
            # 非端口占用类异常：立即终止并上抛，避免无意义重试
            remote_logger.error(
                f"HTTP RPC 服务器启动时遇到非端口占用异常 (port={port})，中止回退流程: {e}"
            )
            await executor_cleanup()
            raise

    # 所有候选端口都失败
    remote_logger.error(
        f"HTTP RPC 服务器启动失败，所有候选端口均不可用: candidates={candidates}, last_error={last_exc!r}"
    )
    await executor_cleanup()
    return None, None


def _check_already_running(ws_port: int, rpc_port: int) -> bool:
    """Check if a daemon is already running. If so, print info and return True."""
    info = get_daemon_info()
    if info is None:
        # 没有 daemon 在运行，清理可能残留的状态文件
        state = read_state_file()
        if state is not None:
            logger.info("Removing stale state file (process is dead)")
            remove_state_file()
        return False

    pid = info.get("pid", "?")
    existing_ws = info.get("ws_port", "?")
    existing_rpc = info.get("rpc_port", "?")
    print(f"⚠️  Daemon is already running (PID: {pid})")
    print(f"   WebSocket Server : ws://127.0.0.1:{existing_ws}")
    print(f"   HTTP RPC Server  : http://127.0.0.1:{existing_rpc}")
    print(f"   To stop it first: chrome-skill stop")
    return True


def _auto_stop_existing_daemon() -> bool:
    """qbotclaw 模式下自动停止已有的守护进程。

    返回 True 表示可以继续启动（无进程在运行或已成功停止），
    返回 False 表示停止失败，不应继续启动。

    安全性：在调用 ``stop_daemon()`` 前，会额外通过服务签名校验确认目标
    确实是本服务，避免误杀端口/命令行恰好匹配的无关进程。
    """
    info = get_daemon_info()
    if info is None:
        # 没有 daemon 在运行，清理可能残留的状态文件
        state = read_state_file()
        if state is not None:
            logger.info("Removing stale state file (process is dead)")
            remove_state_file()
        return True

    pid = info.get("pid", "?")
    existing_mode = "模式=qbotclaw" if info.get("from_qbotclaw") else "模式=normal"

    # 安全校验：如果能定位到 RPC 端口，则通过服务签名再次确认是本服务。
    # （状态文件 / 端口探测路径在 get_daemon_info 中已校验过，这里主要针对"进程扫描兜底"路径。）
    existing_rpc_port = info.get("rpc_port")
    if isinstance(existing_rpc_port, int) and existing_rpc_port > 0:
        if not _is_our_daemon_on_port(existing_rpc_port):
            remote_logger.warning(
                "[qbotclaw mode] 命令行特征匹配到进程 (PID=%s)，但 RPC 端口 %s 上不是本服务，"
                "拒绝发送 SIGTERM，仅清理状态文件",
                pid, existing_rpc_port,
            )
            print(
                f"⚠️  Detected process (PID: {pid}) matching our cmdline, "
                f"but the RPC port {existing_rpc_port} is not serving our daemon. "
                f"Refusing to send SIGTERM; cleaning stale state only."
            )
            state = read_state_file()
            if state is not None:
                remove_state_file()
            return True

    remote_logger.info(f"[qbotclaw mode] Existing daemon detected (PID: {pid}, {existing_mode}), auto-stopping...")
    print(f"⚠️  Existing daemon detected (PID: {pid}, {existing_mode}), auto-stopping...")

    success = stop_daemon()
    if not success:
        # stop_daemon 返回 False 表示停止失败
        remote_logger.error("[qbotclaw mode] Failed to stop existing daemon, aborting startup")
        print("❌ Failed to stop existing daemon. Aborting startup.", file=sys.stderr)
        return False

    # 等待一小段时间确保旧进程完全退出
    import time
    time.sleep(1)

    # 再次检查确认已停止
    if is_daemon_running():
        remote_logger.error("[qbotclaw mode] Daemon still running after stop attempt, aborting startup")
        print("❌ Daemon still running after stop attempt. Aborting startup.", file=sys.stderr)
        return False

    remote_logger.info("[qbotclaw mode] Existing daemon stopped successfully")
    return True


def start_daemon_foreground(ws_port: int = DEFAULT_WS_PORT,
                            rpc_port: int = DEFAULT_RPC_PORT,
                            log_dir: str = DEFAULT_LOG_DIR,
                            from_qbotclaw: bool = False,
                            rpc_port_explicit: bool = False):
    """Start the daemon in the foreground (blocking). Called from CLI.

    ``rpc_port_explicit`` 用于区分用户显式指定端口 vs. 使用默认端口，
    以决定是否启用端口回退机制。默认 ``False`` 保持向前兼容。
    """
    if from_qbotclaw:
        # qbotclaw 模式：自动停止已有守护进程
        if not _auto_stop_existing_daemon():
            return
    else:
        # 普通模式：检测到已有进程时提示并退出
        if _check_already_running(ws_port, rpc_port):
            return
    asyncio.run(run_daemon(
        ws_port=ws_port,
        rpc_port=rpc_port,
        log_dir=log_dir,
        from_qbotclaw=from_qbotclaw,
        rpc_port_explicit=rpc_port_explicit,
    ))


def start_daemon_background(ws_port: int = DEFAULT_WS_PORT,
                            rpc_port: int = DEFAULT_RPC_PORT,
                            log_dir: str = DEFAULT_LOG_DIR,
                            from_qbotclaw: bool = False,
                            rpc_port_explicit: bool = False):
    """Start the daemon as a detached background process.

    Works cross-platform:
      - Windows: uses CREATE_NO_WINDOW + DETACHED_PROCESS
      - Unix: uses double-fork or nohup

    ``rpc_port_explicit`` 用于区分用户显式指定端口 vs. 使用默认端口，
    以决定是否启用端口回退机制。默认 ``False`` 保持向前兼容；当为 ``True`` 时，
    会在启动子进程的命令行中传递 ``--rpc-port`` 参数。
    """
    import subprocess

    if from_qbotclaw:
        # qbotclaw 模式：自动停止已有守护进程
        if not _auto_stop_existing_daemon():
            return
    else:
        # 普通模式：检测到已有进程时提示并退出
        if _check_already_running(ws_port, rpc_port):
            return

    # Build the command to run the daemon using module execution (-m).
    # This ensures relative imports work correctly.
    # We use a simple entry point that calls the daemon's main function.
    # 仅当用户显式指定了 --rpc-port 时才把它传给子进程；否则让子进程使用
    # default=None 的行为进入端口回退候选列表逻辑。
    cmd = [
        sys.executable,
        "-m", "chrome_skill.daemon_server",
        "--ws-port", str(ws_port),
        "--log-dir", log_dir,
    ]
    if rpc_port_explicit:
        cmd.extend(["--rpc-port", str(rpc_port)])
    if from_qbotclaw:
        cmd.append("--from-qbotclaw")

    # 将后台进程的 stdout/stderr 重定向到日志文件，而非 /dev/null，
    # 避免文件 handler 配置失败时所有日志全部丢失。
    daemon_stdout = subprocess.DEVNULL
    daemon_stderr = subprocess.DEVNULL
    _daemon_log_fh = None
    try:
        os.makedirs(log_dir, exist_ok=True)
        _daemon_log_path = os.path.join(log_dir, "daemon_stdout.log")
        _daemon_log_fh = open(_daemon_log_path, "a", encoding="utf-8")
        daemon_stdout = _daemon_log_fh
        daemon_stderr = _daemon_log_fh
    except Exception as e:
        remote_logger.warning(f"无法创建 daemon stdout 日志文件: {e}，回退到 /dev/null")

    if sys.platform == "win32":
        # Windows: detached process
        # 注意：DETACHED_PROCESS 不会继承当前工作目录，需要显式指定 cwd
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        # Windows 上通过 __file__ 推导 chrome_skill 包所在的父目录（如 Lib/site-packages），
        # 确保子进程能正确找到 chrome_skill 模块，无论是 --prefix 还是 --target 安装。
        _pkg_dir = os.path.dirname(os.path.abspath(__file__))        # .../chrome_skill/
        _pkg_parent_dir = os.path.dirname(_pkg_dir)                  # .../site-packages/
        _existing_pythonpath = os.environ.get("PYTHONPATH", "")
        _daemon_pythonpath = os.pathsep.join(
            filter(None, [_pkg_parent_dir, _existing_pythonpath])
        )
        logger.info(f"Windows PYTHONPATH for daemon: {_daemon_pythonpath}")
        proc = subprocess.Popen(
            cmd,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True,
            stdout=daemon_stdout,
            stderr=daemon_stderr,
            cwd=os.getcwd(),  # 确保 daemon 在正确的工作目录中启动
            env={**os.environ, "PYTHONPATH": _daemon_pythonpath},
        )
    else:
        # Unix: use nohup-like detachment
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=daemon_stdout,
            stderr=daemon_stderr,
            close_fds=True,
            env={**os.environ, "PYTHONPATH": os.getcwd()},
        )

    # 启动后关闭父进程持有的日志文件句柄（子进程已继承 fd）
    if _daemon_log_fh is not None:
        _daemon_log_fh.close()

    # 等待子进程写出 state 文件，拿到 daemon 实际 bind 的 rpc 端口再打印。
    # 直接打印传入的 rpc_port 会产生误导：当 8766 被占用时子进程会回退到 60124/60125/随机端口，
    # 但父进程无从知晓，若按入参打印会输出一个并未实际 bind 的端口号。
    import time as _time
    _actual_rpc_port: Optional[int] = None
    _actual_ws_port: int = ws_port
    _state_deadline = _time.perf_counter() + 5.0  # 最多等 5s；保守值，正常 <200ms 即可完成
    while _time.perf_counter() < _state_deadline:
        _state = read_state_file()
        # 要求 state 文件的 pid 与本次启动的子进程 pid 一致，避免读到旧残留
        if _state is not None and _state.get("pid") == proc.pid and "rpc_port" in _state:
            _actual_rpc_port = int(_state["rpc_port"])
            _actual_ws_port = int(_state.get("ws_port", ws_port))
            break
        _time.sleep(0.05)

    logger.info(f"Daemon started in background (PID: {proc.pid})")
    print(f"✅ Daemon started in background (PID: {proc.pid})")
    print(f"   WebSocket Server : ws://127.0.0.1:{_actual_ws_port}")
    if _actual_rpc_port is not None:
        print(f"   HTTP RPC Server  : http://127.0.0.1:{_actual_rpc_port}")
        if _actual_rpc_port != rpc_port:
            print(f"   ⚠️  RPC 端口 {rpc_port} 被占用，已回退到 {_actual_rpc_port}")
    else:
        # state 文件仍未生成（极少数情况：子进程启动失败或磁盘异常）。
        # 不再伪称已知端口，明确告知用户需自行查询状态文件。
        print(f"   HTTP RPC Server  : http://127.0.0.1:{rpc_port} (未确认，实际端口以状态文件为准)")
        logger.warning(
            f"Daemon 子进程 {proc.pid} 在 5s 内未写出 state 文件，"
            f"实际 RPC 端口未知；将回退打印入参端口 {rpc_port}。"
        )


def stop_daemon():
    """Stop a running daemon.

    优先从状态文件获取 PID，如果状态文件不存在则通过进程扫描查找。
    """
    # 综合获取 daemon 信息（状态文件 + 进程扫描 + 端口探测）
    info = get_daemon_info()
    if info is None:
        print("No daemon is running.")
        return False

    pid = info.get("pid")
    if pid is None or pid == "?":
        # 只通过端口探测到了服务，但无法获取 PID
        existing_rpc = info.get("rpc_port", DEFAULT_RPC_PORT)
        print(f"⚠️  Detected a service on RPC port {existing_rpc}, but cannot determine PID.")
        print(f"   Please manually check port {existing_rpc} and stop the process.")
        remove_state_file()
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                kernel32.TerminateProcess(handle, 0)
                kernel32.CloseHandle(handle)
                print(f"✅ Daemon (PID: {pid}) stopped.")
            else:
                print(f"Could not open process {pid}.")
        except Exception as e:
            print(f"Failed to stop daemon: {e}")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"✅ Daemon (PID: {pid}) stopped.")
        except ProcessLookupError:
            print(f"Daemon (PID: {pid}) is not running.")
        except PermissionError:
            print(f"Permission denied to stop daemon (PID: {pid}).")

    remove_state_file()
    return True


# ---------------------------------------------------------------------------
# Module entry point (for background process spawning)
# ---------------------------------------------------------------------------

def _ensure_daemon_file_logging(log_dir: str):
    """确保 daemon 运行期间日志写入文件。

    使用显式 handler 配置，避免 logging.basicConfig() 在 root logger
    已有 handler 时静默不生效的问题。
    """
    root = logging.getLogger()
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # 检查是否已有 daemon.log 的文件 handler，避免重复添加
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and h.baseFilename.endswith("daemon.log"):
            return

    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(
            os.path.join(log_dir, "daemon.log"), encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
        logger.info(f"Daemon file logging configured: {os.path.join(log_dir, 'daemon.log')}")
    except Exception as e:
        # 非致命错误：输出到 stderr 并继续，确保错误不被静默吞掉
        print(f"⚠️  Could not create daemon log file in {log_dir}: {e}", file=sys.stderr)
        logger.warning(f"Failed to create daemon file handler at {log_dir}: {e}")


def _module_main():
    """Entry point when invoked as `python -m chrome_skill.daemon_server`."""
    import argparse

    # 在 Windows 上强制将 stdout/stderr 设置为 UTF-8 编码，避免中文输出乱码
    if sys.platform == "win32":
        import io
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Chrome Browser Skill Daemon Server")
    parser.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT)
    # 注意：--rpc-port 的 default 故意设为 None，用来区分"用户显式指定"与"使用默认值"，
    # 以便决定是否启用端口回退机制（候选列表 vs. 仅使用用户指定端口）。
    # 实际的缺省值仍为 DEFAULT_RPC_PORT（在下方填充）。
    parser.add_argument("--rpc-port", type=int, default=None)
    parser.add_argument("--log-dir", type=str, default=DEFAULT_LOG_DIR)
    parser.add_argument("--from-qbotclaw", action="store_true", default=False,
                        help="Mark this daemon as launched by qbotclaw")
    args = parser.parse_args()

    # 根据 argparse 的解析结果判定用户是否显式指定 --rpc-port
    rpc_port_explicit = args.rpc_port is not None
    rpc_port = args.rpc_port if rpc_port_explicit else DEFAULT_RPC_PORT

    # 使用显式 handler 配置日志，避免 basicConfig 在 root logger 已有 handler 时不生效
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # 如果 root logger 还没有 handler，添加一个 stderr handler 作为兜底
    if not root.handlers:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(fmt)
        root.addHandler(stderr_handler)

    logger.info(f"chrome-skill daemon version: {_get_version()}")

    # 添加文件日志 handler
    _ensure_daemon_file_logging(args.log_dir)

    start_daemon_foreground(
        ws_port=args.ws_port,
        rpc_port=rpc_port,
        log_dir=args.log_dir,
        from_qbotclaw=args.from_qbotclaw,
        rpc_port_explicit=rpc_port_explicit,
    )

if __name__ == "__main__":
    _module_main()
