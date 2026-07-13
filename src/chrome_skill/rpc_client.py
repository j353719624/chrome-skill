"""
RPC Client - Lightweight HTTP client for CLI -> Daemon communication.

Used by CLI processes to send skill execution requests to the persistent
daemon service, instead of starting their own WebSocket server.
"""

import asyncio
import json
import logging
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_RPC_HOST = "127.0.0.1"
DEFAULT_RPC_PORT = 8766
DEFAULT_TIMEOUT = 300  # 5 minutes max for a skill execution

# 服务身份签名：与 daemon 侧 daemon_server.SERVICE_NAME 保持一致。
# 在 check_health / _verify_daemon_identity 中用于区分"本守护进程"与其它端口占用者。
SERVICE_NAME = "chrome-skill-daemon"


class RPCError(Exception):
    """Raised when the RPC call fails."""
    pass


class DaemonNotRunningError(RPCError):
    """Raised when the daemon is not running."""
    pass


# 本地回环连接的建立应在毫秒级完成。若超过此值仍未建立，基本可判定为
# 端口未监听 / 处于 TIME_WAIT 幽灵状态 / 被防火墙静默丢包，继续等待无意义。
# 使用独立常量而非与 read timeout 耦合，语义清晰、所有调用方一致。
DEFAULT_CONNECT_TIMEOUT = 1.5

async def _http_request(host: str, port: int, method: str, path: str,
                        body: Optional[dict] = None, timeout: float = DEFAULT_TIMEOUT,
                        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                        auth_token: Optional[str] = None) -> dict:
    """Send an HTTP request using asyncio streams (no external dependencies).

    Returns the parsed JSON response body.

    Args:
        timeout: 读阶段超时（readline / readexactly / read 的单次等待上限）。
        connect_timeout: TCP 连接建立超时。默认 1.5s —— 对于 127.0.0.1 本地回环，
            正常建连应在 1ms 内完成；若 1.5s 都无法建连，继续重传只会无谓拖延。
        auth_token: 认证 Token，若提供则在请求头中携带 Authorization: Bearer <token>。
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=connect_timeout
        )
    except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
        raise DaemonNotRunningError(
            f"Cannot connect to daemon at {host}:{port}. "
            f"Is the daemon running? Start it with: chrome-skill serve --daemon\n"
            f"  Error: {e}"
        )

    try:
        # Build request
        body_bytes = b""
        if body is not None:
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        request_lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {host}:{port}",
            f"Content-Type: application/json",
            f"Content-Length: {len(body_bytes)}",
            f"Connection: close",
        ]
        if auth_token:
            request_lines.append(f"Authorization: Bearer {auth_token}")
        request_lines.extend(["", ""])
        request = "\r\n".join(request_lines).encode("utf-8") + body_bytes
        writer.write(request)
        await writer.drain()

        # Read response headers line by line
        raw_headers = b""
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            raw_headers += line
            if line == b"\r\n" or line == b"\n" or line == b"":
                break

        header_str = raw_headers.decode("utf-8", errors="replace")
        header_lines = header_str.strip().split("\r\n")
        if not header_lines:
            raise RPCError("Invalid HTTP response from daemon: empty headers")

        # Parse status line
        status_line = header_lines[0]
        parts = status_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) >= 2 else 0

        # Parse headers to find Content-Length
        content_length = -1
        for hl in header_lines[1:]:
            if ":" in hl:
                key, val = hl.split(":", 1)
                if key.strip().lower() == "content-length":
                    try:
                        content_length = int(val.strip())
                    except ValueError:
                        pass

        # Read body based on Content-Length or read until connection closes
        MAX_BODY = 100 * 1024 * 1024  # 100 MB max
        if content_length >= 0:
            if content_length > MAX_BODY:
                raise RPCError(f"Response too large: {content_length} bytes")
            body_bytes_resp = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=timeout
            )
        else:
            # No Content-Length: read until EOF (Connection: close)
            chunks = []
            total = 0
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BODY:
                    raise RPCError(f"Response too large: >{MAX_BODY} bytes")
            body_bytes_resp = b"".join(chunks)

        body_part = body_bytes_resp.decode("utf-8", errors="replace")

        # Parse JSON body
        try:
            result = json.loads(body_part)
        except json.JSONDecodeError:
            raise RPCError(f"Invalid JSON response from daemon: {body_part[:200]}")

        if status_code >= 400:
            error_msg = result.get("error", f"HTTP {status_code}")
            raise RPCError(f"Daemon returned error: {error_msg}")

        return result

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def check_health(host: str = DEFAULT_RPC_HOST,
                       port: int = DEFAULT_RPC_PORT,
                       timeout: float = 2.0) -> bool:
    """Check if the daemon is healthy AND is (plausibly) our service.

    校验规则（向后兼容老版本 daemon）：
      - 必要条件：响应是合法 JSON dict，且 ``status == "ok"``；
      - 若响应中包含 ``service`` 字段：必须等于 :data:`SERVICE_NAME`，
        否则判定为其它占用同端口的无关服务，返回 ``False``；
      - 若响应中**不包含** ``service`` 字段：视为老版本 daemon（其 ``/health``
        仅返回 ``{"status": "ok"}``），按 ``status`` 判定，返回 ``True``。

    任何网络 / 解析 / 超时异常均会降级为返回 ``False``，不向上抛出。

    默认超时 2 秒：本地回环的 health 检查不应等待过久；若超过 2 秒仍无响应，
    几乎可以确定对端不是我们的 daemon（或根本不存在）。
    """
    try:
        result = await _http_request(host, port, "GET", "/health", timeout=timeout)
    except Exception:
        return False
    if not isinstance(result, dict):
        return False
    if result.get("status") != "ok":
        return False
    # 向后兼容：老 daemon 不返回 service 字段，仅凭 status==ok 接受。
    service = result.get("service")
    if service is None:
        return True
    return service == SERVICE_NAME


async def _verify_daemon_identity(host: str = DEFAULT_RPC_HOST,
                                  port: int = DEFAULT_RPC_PORT,
                                  timeout: float = 1.5) -> bool:
    """向指定端口发起一次短超时的 ``GET /health``，判断对方是否为本服务。

    与 :func:`check_health` 的区别在于：
      - 使用更短的默认超时（1.5s），用于端口探测场景，避免阻塞 CLI；
      - 显式指定 ``host``/``port``，常用于检测非默认端口上的进程身份；
      - 所有异常（连接失败、超时、JSON 解析失败等）一律返回 ``False``。

    身份判定规则与 :func:`check_health` 保持一致，向后兼容老版本 daemon：
      - 必须 ``status == "ok"``；
      - 若响应包含 ``service`` 字段：必须等于 :data:`SERVICE_NAME`；
      - 若响应不包含 ``service`` 字段：视为老版本 daemon，接受为本服务。
    """
    try:
        result = await _http_request(host, port, "GET", "/health", timeout=timeout)
    except Exception:
        return False
    if not isinstance(result, dict):
        return False
    if result.get("status") != "ok":
        return False
    service = result.get("service")
    if service is None:
        return True
    return service == SERVICE_NAME


async def get_status(host: str = DEFAULT_RPC_HOST,
                     port: int = DEFAULT_RPC_PORT) -> dict:
    """Get daemon status."""
    return await _http_request(host, port, "GET", "/status", timeout=5)


async def execute_skill(skill_name: str, args: Dict[str, Any],
                        host: str = DEFAULT_RPC_HOST,
                        port: int = DEFAULT_RPC_PORT,
                        timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Execute a skill via the daemon's RPC endpoint.

    Args:
        skill_name: Name of the skill to execute.
        args: Skill arguments dict.
        host: Daemon RPC host.
        port: Daemon RPC port.
        timeout: Max wait time for the skill execution.

    Returns:
        Dict with 'success' and 'result' (or 'error') keys.
    """
    body = {
        "skill_name": skill_name,
        "args": args,
    }
    # 从状态文件读取 auth token
    auth_token = _get_auth_token_from_state()
    return await _http_request(host, port, "POST", "/execute", body=body,
                               timeout=timeout, auth_token=auth_token)


def _get_auth_token_from_state() -> Optional[str]:
    """从 daemon 状态文件中读取 auth token。

    返回 token 字符串，若状态文件不存在或无 token 字段则返回 None。
    """
    try:
        from .daemon_server import read_state_file
        state = read_state_file()
        if state and "auth_token" in state:
            return state["auth_token"]
    except Exception:
        pass
    return None


def get_rpc_port_from_state() -> int:
    """Read the RPC port from the daemon state file, falling back to default."""
    try:
        from .daemon_server import read_state_file, DEFAULT_RPC_PORT
        state = read_state_file()
        if state and "rpc_port" in state:
            return state["rpc_port"]
    except Exception:
        pass
    return DEFAULT_RPC_PORT
