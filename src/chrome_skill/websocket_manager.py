import asyncio
import argparse
import json
import socket
import uuid
import websockets
import time
from typing import Dict, Any, Optional, Set
import logging

from .report_log import remote_logger
logger = logging.getLogger(__name__)

# Default browser function timeout in seconds
DEFAULT_BROWSER_FUNCTION_TIMEOUT = 180.0

# send_message 总超时上限（秒），防止重试导致耗时雪崩
# 包含等待客户端连接 + 发送 + 等待响应 + 重试的总时间
SEND_MESSAGE_TOTAL_TIMEOUT = 300.0

# WebSocket 心跳检测间隔（秒），用于及时发现死连接
WS_PING_INTERVAL = 30
# WebSocket 心跳超时（秒），超过此时间未收到 pong 则认为连接已断开
WS_PING_TIMEOUT = 10

# 日志上报时消息内容的最大截取字符数
MAX_LOG_MESSAGE_LENGTH = 3000


# ---------------------------------------------------------------------------
# 自定义异常：用于区分 send_message 中的不同错误场景
# ---------------------------------------------------------------------------

class WebSocketError(Exception):
    """WebSocket 通信相关的基础异常。"""
    pass


class ClientConnectionTimeoutError(WebSocketError):
    """等待浏览器扩展客户端连接超时。"""
    pass


class NoActiveClientError(WebSocketError):
    """没有可用的活跃客户端连接。"""
    pass


class BrowserResponseTimeoutError(WebSocketError):
    """等待浏览器响应超时。"""
    pass


class ServerShuttingDownError(WebSocketError):
    """服务器正在关闭，无法处理请求。"""
    pass

# 允许连接的IP地址列表
ALLOWED_IPS = [
    "127.0.0.1",  # localhost
    "::1",  # localhost IPv6
]

# 允许的 WebSocket Origin 白名单
# 允许终端 CLI（无 Origin 头）和 x5 use 插件连接
ALLOWED_ORIGINS = [
    # None 表示允许没有 Origin 头的连接（终端 CLI 直接连接）
    None,
    # x5 use 浏览器插件
    "chrome-extension://aaplhnhcdcgkjbijfkjdbfmiagjojdjf",
]

class WebSocketManager:
    def __init__(self):
        self._connected_clients: Set[websockets.WebSocketServerProtocol] = set()
        self._client_connections: Dict[str, Dict[str, Any]] = {}
        self._server_started = False
        self._server = None
        self._response_futures: Dict[str, asyncio.Future] = {}
        self._port: int = 8765  # 默认端口，可通过 start_server(port=...) 覆盖
        self._shutting_down = False  # 标记是否正在关闭，用于快速退出等待循环
        self._call_platform: str = ""  # 当前运行平台（win/linux/mac）
        self._call_mode: str = "normal"  # 当前运行模式（qbot_claw/normal）
        # 当前活跃的 AI 会话 ID。由 SkillExecutor 在 start_session 成功后写入、
        # end_session 后清空。send_message 在发送 action 协议（业务命令）时，
        # 会基于此值自动注入顶层 sessionId 和 commandId（含 sessionId 前缀），
        # 让插件端能据此路由到对应 controller / 走 SGM enforce 保护。
        self._current_session_id: Optional[str] = None
        self._command_seq: int = 0  # 同一进程内自增，保证 commandId 唯一

    def set_call_context(self, call_platform: str, call_mode: str):
        """设置 callPlatform 和 callMode，后续所有 send_message 发送的消息都会自动携带。"""
        self._call_platform = call_platform
        self._call_mode = call_mode
        logger.info(f"WebSocketManager: call context set - callPlatform={call_platform}, callMode={call_mode}")

    def set_session_id(self, session_id: Optional[str]):
        """设置/清空当前 AI 会话 ID。

        - 在 start_session 成功 ack 后调用，传入 sessionId；
        - 在 end_session 成功 ack 后调用，传入 None；
        - 后续所有 send_message（action 协议，即业务命令）会自动注入顶层
          sessionId，并生成形如 "<sessionId>:<seq>" 的 commandId。
        """
        self._current_session_id = session_id
        if session_id:
            logger.info(f"WebSocketManager: session context set - sessionId={session_id}")
        else:
            logger.info("WebSocketManager: session context cleared")

    def get_session_id(self) -> Optional[str]:
        return self._current_session_id

    def is_server_started(self):
        return self._server_started

    async def start_server(self, port: int = 8765):
        """启动WebSocket服务器
        
        Args:
            port: 监听端口，默认 8765。可通过 daemon 模式传入自定义端口。
        """
        # 防止重复启动：如果服务已在运行，直接返回
        if self._server_started:
            remote_logger.info(f"WebSocket 服务器已在端口 {self._port} 上运行，跳过重复启动")
            return

        try:
            # 获取本机IP地址
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('8.8.8.8', 80))
                local_ip = s.getsockname()[0]
            except Exception:
                local_ip = '127.0.0.1'
            finally:
                s.close()
            
            # 确保本地IP在允许列表中
            if local_ip not in ALLOWED_IPS:
                ALLOWED_IPS.append(local_ip)
                logger.info(f"添加本地IP到允许列表: {local_ip}")
            
            # 启动服务器，只监听本地地址
            # origins 参数：限制允许的 WebSocket 连接来源
            # 仅允许终端 CLI 发起的连接（无 Origin 头），拒绝网页和浏览器插件
            ws_origins = list(ALLOWED_ORIGINS)  # 允许 CLI（无 Origin）和 x5 use 插件
            self._server = await websockets.serve(
                self._handle_client,
                "127.0.0.1",  # 只监听本地地址
                port,
                ping_interval=WS_PING_INTERVAL,  # 心跳检测间隔，及时发现死连接
                ping_timeout=WS_PING_TIMEOUT,     # 心跳超时，超时则断开连接
                max_size=10*1024*1024, # 10 MB
                origins=ws_origins,  # Origin 校验：阻止恶意网页的 WebSocket 连接
                process_request=self._check_origin,  # 自定义 Origin 校验逻辑
            )
            self._port = port
            self._server_started = True
            remote_logger.info("WebSocket 服务器启动成功: port=%s", port)
            
        except Exception as e:
            remote_logger.error(f"启动WebSocket服务器失败 (端口 {port}): {e}")
            raise

    # 允许连接的 Chrome 插件 Origin 白名单
    ALLOWED_EXTENSION_ORIGINS = {
        "chrome-extension://aaplhnhcdcgkjbijfkjdbfmiagjojdjf",  # x5 use 插件
    }

    async def _check_origin(self, connection, request):
        """自定义 WebSocket 连接的 Origin 校验（process_request 回调）。

        允许以下来源的连接：
        - 终端 CLI（无 Origin 头）
        - x5 use 浏览器插件（chrome-extension://aaplhnhcdcgkjbijfkjdbfmiagjojdjf）

        拒绝其他所有携带 Origin 头的连接，包括：
        - 网页（http://、https://）
        - 其他浏览器插件

        websockets 13+ process_request 签名：(connection, request) -> Response | None
        - connection: ServerConnection 对象
        - request: Request 对象，通过 request.headers 获取头信息
        - 返回 None 表示允许连接，返回 Response 对象表示拒绝
        """
        from http import HTTPStatus
        from websockets.http11 import Response
        from websockets.datastructures import Headers

        origin = request.headers.get("Origin")
        if origin is None:
            # 无 Origin 头：仅允许来自本地 IP 的连接（终端 CLI 直接连接不会携带 Origin）
            # 阻止通过设置 Origin: null 的方式绕过校验
            client_ip = connection.remote_address[0] if connection.remote_address else None
            if client_ip not in ALLOWED_IPS:
                remote_logger.warning(f"拒绝无 Origin 头的非本地 WebSocket 连接 (IP: {client_ip})")
                return Response(
                    HTTPStatus.FORBIDDEN,
                    "Forbidden",
                    Headers(),
                    b"Forbidden: non-local connection without Origin not allowed\n",
                )
            return None
        if origin in self.ALLOWED_EXTENSION_ORIGINS:
            # x5 use 插件：允许连接
            return None
        # 其他所有来源均拒绝
        remote_logger.warning(f"拒绝来自非授权来源的 WebSocket 连接 (Origin: {origin})")
        return Response(
            HTTPStatus.FORBIDDEN,
            "Forbidden",
            Headers(),
            b"Forbidden: only CLI and authorized extensions allowed\n",
        )

    async def _handle_client(self, client_ws: websockets.WebSocketServerProtocol):
        """处理WebSocket客户端连接

        Only one browser extension client is expected at a time.  When a new
        connection arrives we gracefully replace the previous one (if any),
        rather than blindly closing everything.
        """
        client_ip = client_ws.remote_address[0]

        # IP 白名单校验：仅允许 ALLOWED_IPS 中的地址连接
        if client_ip not in ALLOWED_IPS:
            remote_logger.warning(f"拒绝来自非允许 IP 的 WebSocket 连接: {client_ip}")
            await client_ws.close(4003, "Forbidden: IP not allowed")
            return

        # Replace the previous connection (only one browser client allowed)
        if self._connected_clients:
            old_clients = list(self._connected_clients)
            self._connected_clients.clear()
            self._client_connections.clear()
            for old_client in old_clients:
                try:
                    await old_client.close()
                    remote_logger.info(f"已替换旧客户端连接 (IP: {old_client.remote_address[0]})")
                except Exception:
                    pass  # old connection may already be gone

        # Register the new client
        client_id = str(uuid.uuid4())
        self._client_connections[client_id] = {
            'ip': client_ip,
            'connected_at': asyncio.get_event_loop().time(),
            'ws': client_ws
        }
        self._connected_clients.add(client_ws)

        remote_logger.info(f"新客户端连接 (ID: {client_id}, IP: {client_ip}), 当前连接数: {len(self._connected_clients)}")
        remote_logger.info("浏览器扩展客户端连接成功: client_id=%s, ip=%s", client_id, client_ip)
        
        try:
            # 发送初始化消息，要求客户端确认连接
            init_message = {
                "type": "init",
                "client_id": client_id,
                "message": "请发送 connection_confirm 消息以确认连接"
            }
            remote_logger.info(f"准备发送初始化消息给客户端 {client_id}: {init_message}")
            await client_ws.send(json.dumps(init_message))
            remote_logger.info(f"已发送初始化消息给客户端 {client_id}")
            
            # 处理接收到的消息
            async for message in client_ws:
                try:
                    msg_str = str(message)
                    remote_logger.info(f"收到客户端 {client_id} 的消息(长度={len(msg_str)})")
                    data = json.loads(message)
                    
                    # 处理连接确认消息
                    if data.get("type") == "connection_confirm":
                        remote_logger.info(f"客户端 {client_id} 已确认连接")
                    
                    # 处理动作响应消息
                    elif "type" in data:
                        action = data.get("type")
                        if action in self._response_futures:
                            future = self._response_futures[action]
                            if not future.done():
                                # 直接返回字符串格式的结果
                                if action == "get_state":
                                    future.set_result(data)
                                elif action == "get_screenshot":
                                    future.set_result(data)                                    
                                elif action == "get_clickable_elements":
                                    future.set_result(data)
                                else:
                                    future.set_result(data)
                    
                    # 处理其他类型的响应消息
                    else:
                        # 获取消息中的第一个键作为动作类型
                        action = data['actionName']
                        if action in self._response_futures:
                            future = self._response_futures[action]
                            if not future.done():
                                # 返回 actionResult 及额外状态字段（success/reason/errorDetail）
                                # 兼容老版本扩展：仅在远端实际返回了对应字段时才放入 payload，
                                # 避免缺失字段以 None 值存在导致下游 'key in dict' 判断误命中
                                result_payload = {}
                                if 'actionResult' in data:
                                    result_payload['actionResult'] = data['actionResult']
                                if 'success' in data:
                                    result_payload['success'] = data['success']
                                if 'reason' in data:
                                    result_payload['reason'] = data['reason']
                                if 'errorDetail' in data:
                                    result_payload['errorDetail'] = data['errorDetail']
                                future.set_result(result_payload)
                                
                except json.JSONDecodeError:
                    remote_logger.info(f"处理客户端 {client_id} 消息时出错 JSONDecodeError")
                except Exception as e:
                    remote_logger.error(f"处理客户端 {client_id} 消息时出错: {e}")

            # async for 循环正常结束，说明浏览器端主动发起了正常的 WebSocket 关闭握手
            # （例如用户关闭浏览器、关闭标签页、扩展被禁用等）
            remote_logger.info(f"客户端 {client_id} 正常断开连接")
            self._handle_connection_closed(client_ws, client_id)
                    
        except websockets.exceptions.ConnectionClosed as e:
            remote_logger.info(f"客户端 {client_id} 异常断开连接 - 状态码: {e.code}, 原因: {e.reason}")
            self._handle_connection_closed(client_ws, client_id)
        except Exception as e:
            remote_logger.error(f"处理客户端 {client_id} 消息时出错: {e}")
            self._handle_connection_closed(client_ws, client_id)

    def _handle_connection_closed(self, client_ws: websockets.WebSocketServerProtocol, client_id: str):
        """处理连接关闭"""
        remote_logger.info(f"客户端 {client_id} 断开连接")
        self._connected_clients.discard(client_ws)
        if client_id in self._client_connections:
            del self._client_connections[client_id]
        remote_logger.info(f"当前连接数: {len(self._connected_clients)}")

    def _purge_dead_clients(self):
        """Remove clients whose underlying connection is already closed."""
        dead: set = set()
        for client in self._connected_clients:
            # websockets >= 14.x removed the `.closed` attribute;
            # use `.state` (an enum) instead.
            try:
                is_closed = client.state.name != "OPEN"
            except AttributeError:
                # Fallback for older versions that still have `.closed`
                is_closed = getattr(client, "closed", False)
            if is_closed:
                dead.add(client)
        for client in dead:
            self._connected_clients.discard(client)
            for cid, conn in list(self._client_connections.items()):
                if conn['ws'] == client:
                    del self._client_connections[cid]
                    remote_logger.info(f"已清理断开的客户端 {cid}")
                    break

    async def send_message(self, message: Dict[str, Any], retry_times=1, _deadline: Optional[float] = None, response_action: Optional[str] = None) -> str:
        """Send a message to the connected browser client and wait for a response.

        The method keeps the existing connection alive across calls.  It will
        only clean up connections that are *already dead*, never proactively
        close a healthy connection.

        Args:
            message: 要发送的消息字典。
            retry_times: 当前重试次数（内部递归使用）。
            _deadline: 总超时截止时间戳（内部递归使用），首次调用时自动设置。
            response_action: 显式指定要监听的响应 action key。当请求 type 与响应 type
                不同名时（如 start_session → start_session_ack）必须传入。
                未传时保持原有行为：从 message 中按 type / actionName 推导。
        """
        # 首次调用时设置总超时截止时间，后续重试共享同一个 deadline
        if _deadline is None:
            _deadline = time.monotonic() + SEND_MESSAGE_TOTAL_TIMEOUT

        # 检查是否已超过总超时上限
        remaining = _deadline - time.monotonic()
        if remaining <= 0:
            remote_logger.error("send_message 总超时: 已超过 %ss 上限", SEND_MESSAGE_TOTAL_TIMEOUT)
            raise BrowserResponseTimeoutError(
                f"send_message 总超时（已超过 {SEND_MESSAGE_TOTAL_TIMEOUT}s 上限）: {message}"
            )

        # Purge stale connections first
        self._purge_dead_clients()

        # Wait for a client to connect, but respect shutdown signal and timeout
        _wait_timeout = 60  # 最多等待60秒
        _wait_elapsed = 0
        while not self._connected_clients:
            if self._shutting_down:
                raise ServerShuttingDownError("Server is shutting down")
            if _wait_elapsed >= _wait_timeout:
                remote_logger.error("等待客户端连接超时: timeout=%ss", _wait_timeout)
                raise ClientConnectionTimeoutError(
                    f"Waiting for client connection timed out after {_wait_timeout}s"
                )
            remote_logger.info(f"等待客户端连接... ({_wait_elapsed}/{_wait_timeout}s)")
            await asyncio.sleep(1)
            _wait_elapsed += 1
            
        # 创建消息副本并自动注入 callPlatform 和 callMode，避免修改原始字典
        message = dict(message)
        if self._call_platform:
            message["callPlatform"] = self._call_platform
        message["callMode"] = self._call_mode
        logger.info(f"注入调用上下文: callPlatform={self._call_platform}, callMode={self._call_mode}")

        # 自动注入 sessionId / commandId（仅对 action 协议的业务命令）。
        # 判定规则：消息含 `actionName` 字段，并且不含顶层 `type`（type 协议
        # 由调用方自行控制 sessionId，例如 start_session/end_session）。
        # - sessionId：取当前 AI 会话；若调用方已显式传入则尊重调用方的值；
        # - commandId：若调用方未提供，则用 "<sessionId>:<seq>" 自动生成，
        #   保证插件端 socket 层读取到的 commandId 含 sessionId 前缀，便于按
        #   sessionId 路由到对应的 controller / 走 SGM enforce 分支。
        if "actionName" in message and "type" not in message:
            if self._current_session_id and not message.get("sessionId"):
                message["sessionId"] = self._current_session_id
            existing_command_id = message.get("commandId")
            if not existing_command_id:
                self._command_seq += 1
                sid_prefix = self._current_session_id or "no-session"
                message["commandId"] = f"{sid_prefix}:{self._command_seq}"
            logger.info(
                f"注入会话上下文: sessionId={message.get('sessionId')}, commandId={message.get('commandId')}"
            )

        # Determine action key from the message (优先使用调用方显式指定的 response_action)
        if response_action is not None:
            action = response_action
        elif "type" in message:
            action = message["type"]
        else:
            action = message.get("actionName")

        future = asyncio.Future()
        self._response_futures[action] = future
        
        try:
            _action_name = message.get("actionName") or message.get("type", "unknown")
            remote_logger.info(f"发送消息: action={_action_name}")
            tasks = []
            closed_clients: set = set()
            
            for client in self._connected_clients:
                try:
                    # Find the client_id for logging
                    client_id = None
                    for cid, conn in self._client_connections.items():
                        if conn['ws'] == client:
                            client_id = cid
                            break
                    
                    if client_id:
                        remote_logger.info(f"向客户端 {client_id} 发送消息")
                        tasks.append(client.send(json.dumps(message)))
                    else:
                        remote_logger.warning("找不到客户端ID，跳过发送消息")
                        closed_clients.add(client)
                except websockets.exceptions.ConnectionClosed:
                    remote_logger.warning("客户端连接已关闭，跳过发送消息")
                    closed_clients.add(client)
                except Exception as e:
                    remote_logger.error(f"检查客户端连接状态时出错: {e}")
                    closed_clients.add(client)
            
            # Clean up only the connections that are actually dead
            for client in closed_clients:
                self._connected_clients.discard(client)
                for cid, conn in list(self._client_connections.items()):
                    if conn['ws'] == client:
                        del self._client_connections[cid]
                        break
            
            if tasks:
                await asyncio.gather(*tasks)
                remote_logger.info(f"消息已发送给 {len(tasks)} 个客户端")
            else:
                remote_logger.warning("没有可用的客户端连接")
                raise NoActiveClientError("No active client connections")
            
            # Wait for the response
            # 使用 remaining 和 DEFAULT_BROWSER_FUNCTION_TIMEOUT 中较小的值，确保不超过总超时
            _response_timeout = min(DEFAULT_BROWSER_FUNCTION_TIMEOUT, remaining)
            try:
                response = await asyncio.wait_for(future, timeout=_response_timeout)
                resp_str = str(response)
                remote_logger.info(f"收到响应(长度={len(resp_str)})")
                return response
            except asyncio.TimeoutError:
                remote_logger.error("等待浏览器响应超时: timeout=%ss, action=%s, remaining=%ss", _response_timeout, action, remaining)
                raise BrowserResponseTimeoutError(
                    f"等待浏览器响应超时（{_response_timeout:.1f}s）: {message}"
                )
            finally:
                self._response_futures.pop(action, None)
                    
        except Exception as e:
            # Clean up only dead connections, do NOT close healthy ones
            self._purge_dead_clients()

            # 检查总超时是否已到期，避免无意义的重试
            if retry_times <= 3 and (time.monotonic() < _deadline):
                remote_logger.error(f"发送消息时出错 ({e}), retry {retry_times}/3, 剩余时间={_deadline - time.monotonic():.1f}s")
                # Brief pause before retry to give the connection a moment
                await asyncio.sleep(0.5)
                return await self.send_message(message, retry_times + 1, _deadline=_deadline, response_action=response_action)
            raise WebSocketError(f"发送消息失败（已重试{retry_times - 1}次）: {str(e)}") from e

    async def cleanup(self):
        """清理资源"""
        remote_logger.info(f"开始清理连接 - 当前连接数: {len(self._connected_clients)}")
        
        # 设置关闭标志，让 send_message 中的等待循环立即退出
        self._shutting_down = True
        
        # 取消所有等待中的 response futures，避免阻塞
        for action, future in list(self._response_futures.items()):
            if not future.done():
                future.cancel()
        self._response_futures.clear()
        
        # 关闭所有客户端连接
        for client_ws in list(self._connected_clients):
            try:
                await client_ws.close()
            except Exception as e:
                remote_logger.error(f"关闭客户端连接时出错: {e}")
        
        self._connected_clients.clear()
        self._client_connections.clear()
        remote_logger.info("所有客户端连接已关闭")
        
        # 关闭 WebSocket 服务器，释放端口
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        
        self._server_started = False
        remote_logger.info("WebSocket 服务器已关闭，端口已释放")


async def test_client_connection():
    """测试客户端连接逻辑"""
    try:
        # 连接到WebSocket服务器
        async with websockets.connect("ws://127.0.0.1:8765") as websocket:
            remote_logger.info("客户端已连接到服务器")
            
            # 接收初始化消息
            init_msg = await websocket.recv()
            remote_logger.info(f"收到服务器消息: {init_msg}")
            
            # 解析并确认连接
            init_data = json.loads(init_msg)
            if init_data.get("type") == "init":
                confirm_msg = {
                    "type": "connection_confirm",
                    "client_id": init_data["client_id"],
                    "message": "连接已确认"
                }
                await websocket.send(json.dumps(confirm_msg))
                remote_logger.info("已发送连接确认消息")
            
            # 测试发送一条消息
            test_msg = {
                "actionName": "test",
                "actionResult": "this is test message!",
            }
            await websocket.send(json.dumps(test_msg))
            remote_logger.info("已发送测试消息")
            
    except Exception as e:
        remote_logger.error(f"客户端测试失败: {e}")
        raise

async def main():
    """主入口函数"""
    parser = argparse.ArgumentParser(description='WebSocket服务器管理')
    parser.add_argument('--test', action='store_true', help='运行客户端测试')
    args = parser.parse_args()
    
    manager = WebSocketManager()
    
    try:
        # 启动服务器
        await manager.start_server()
        
        if args.test:
            # 运行客户端测试
            await test_client_connection()
        else:
            # 持续运行服务器
            remote_logger.info("服务器运行中，按Ctrl+C退出...")
            while True:
                await asyncio.sleep(1)
                
    except KeyboardInterrupt:
        remote_logger.info("收到中断信号，准备关闭...")
    except Exception as e:
        remote_logger.error(f"运行出错: {e}")
    finally:
        # 清理资源
        await manager.cleanup()
        remote_logger.info("服务器已关闭")

if __name__ == "__main__":
    asyncio.run(main())
