"""
日志远程上报模块。

将日志通过 HTTPS 上报到 Galileo Telemetry 平台（galileotelemetry.tencent.com），
不涉及本地日志文件的写入或修改。在需要上报日志的地方主动调用 log() 即可。

内部实现：使用单一后台守护线程 + 有界队列机制，支持批量合并上报。
调用 log() 时日志记录被放入队列，后台线程定期批量取出并合并为一次 HTTPS 请求发送，
减少线程创建开销和网络连接数。发送不阻塞主流程。
"""

import json
import logging
import os
import platform
import queue
import ssl
import threading
import time
import uuid
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "log", "init_log", "flush_logs",
    "report_skill_start", "report_skill_end_ok", "report_skill_end_err",
    "report_log_message", "RemoteLogHandler", "remote_logger",
]

# ---------------------------------------------------------------------------
# 上报目标地址
# ---------------------------------------------------------------------------
_LOG_URL = "https://galileotelemetry.tencent.com/v1/logs"

# ---------------------------------------------------------------------------
# 模块级动态属性（通过 init_log() 初始化，或保持默认值）
# ---------------------------------------------------------------------------
_log_guid: str = "unknown"
_log_qimei36: str = "unknown"
_skill_version: str = "unknown"
_initialized: bool = False

# 系统平台标识：win / linux / macos / unknown
_PLATFORM_MAP = {"Windows": "win", "Linux": "linux", "Darwin": "macos"}
_platform: str = _PLATFORM_MAP.get(platform.system(), "unknown")

# ---------------------------------------------------------------------------
# 队列与后台发送线程相关配置
# ---------------------------------------------------------------------------
_QUEUE_MAX_SIZE = 1000       # 队列最大容量
_BATCH_MAX_SIZE = 20         # 单次批量发送的最大日志条数
_BATCH_WAIT_SECONDS = 2.0    # 队列为空时等待收集日志的时间窗口（秒）

# 内部日志队列（存放 log_record 字典）
_log_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX_SIZE)

# 用于 flush_logs() 等待队列排空的事件
_flush_event = threading.Event()
_flush_event.set()  # 初始状态：队列为空，已就绪

# 缓存的 SSL 上下文，模块生命周期内只创建一次
_ssl_context: Optional[ssl.SSLContext] = None

# 后台发送线程及其保护锁
_sender_thread: Optional[threading.Thread] = None
_sender_thread_lock = threading.Lock()


# ============================= 辅助函数 =====================================


def _generate_trace_id() -> str:
    """生成 32 位唯一十六进制字符串，用作 trace_id。

    基于 UUID v4（密码学安全随机），去掉连字符后为 32 位。
    """
    return uuid.uuid4().hex


def _get_current_time_unix_nano() -> int:
    """获取当前时间的纳秒级 Unix 时间戳。"""
    return int(time.time() * 1_000_000_000)


def _build_log_record(
    body: str,
    event: str = "",
    command: str = "",
    duration_ms: Optional[float] = None,
    source: str = "",
    call_mode: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """构建单条符合 Galileo 日志格式的 log_record 字典。

    Args:
        body: 日志正文内容。
        event: 事件标识，用于区分不同的日志事件。
        command: 命令名称，用于标识当前执行的 skill 命令。
        duration_ms: 执行耗时（毫秒），仅在命令结束时传入。
        source: 调用来源标识（如 "cli"、"daemon"），用于区分上报来源。
        call_mode: 当前运行模式（如 "qbot_claw"、"normal"），用于区分调用模式。
        reason: 失败原因分类标识（如 "client_connection_timeout"、"browser_response_timeout"），
                仅在 skill_end_err 事件中使用，用于快速定位异常类型。

    Returns:
        单条 log_record 字典，可放入 payload 的 log_records 数组中。
    """
    log_record: dict[str, Any] = {
        "trace_id": _generate_trace_id(),
        "body": {
            "string_value": body,
        },
        "time_unix_nano": _get_current_time_unix_nano(),
        "severity_number": "SEVERITY_NUMBER_INFO",
        "severity_text": "INFO",
        "flags": 1,
        "attributes": [
            {
                "key": "guid",
                "value": {"string_value": _log_guid},
            },
            {
                "key": "qimei36",
                "value": {"string_value": _log_qimei36},
            },
            {
                "key": "skill_version",
                "value": {"string_value": _skill_version},
            },
            {
                "key": "platform",
                "value": {"string_value": _platform},
            },
            {
                "key": "event_name",
                "value": {"string_value": event},
            },
            {
                "key": "command",
                "value": {"string_value": command},
            },
            {
                "key": "source",
                "value": {"string_value": source},
            },
            {
                "key": "call_mode",
                "value": {"string_value": call_mode},
            },
        ],
    }

    # 当传入 duration_ms 时，在 attributes 中追加耗时属性
    if duration_ms is not None:
        log_record["attributes"].append(
            {
                "key": "duration_ms",
                "value": {"string_value": str(duration_ms)},
            }
        )

    # 当传入 reason 时，在 attributes 中追加失败原因分类
    if reason:
        log_record["attributes"].append(
            {
                "key": "reason",
                "value": {"string_value": reason},
            }
        )

    return log_record


def _build_batch_payload(log_records: list[dict[str, Any]]) -> dict[str, Any]:
    """将多条 log_record 包装为完整的 Galileo payload。

    Args:
        log_records: log_record 字典列表。

    Returns:
        可直接序列化为 JSON 的完整 payload 字典。
    """
    payload: dict[str, Any] = {
        "resource_logs": [
            {
                "instrumentation_library_logs": [
                    {
                        "instrumentation_library": {
                            "name": "chrome-skill-log",
                        },
                        "log_records": log_records,
                    },
                ],
                "resource": {
                    "attributes": [
                        {"key": "telemetry.sdk.language", "value": {"string_value": "go"}},
                        {"key": "telemetry.sdk.name", "value": {"string_value": "galileo"}},
                        {"key": "telemetry.sdk.version", "value": {"string_value": "0.0.1"}},
                        {"key": "target", "value": {"string_value": "RPC.ChromeSkill"}},
                        {"key": "namespace", "value": {"string_value": "Production"}},
                        {"key": "env", "value": {"string_value": "formal"}},
                        {"key": "server", "value": {"string_value": "ChromeSkill"}},
                    ],
                },
            },
        ],
    }
    return payload


def _build_payload(
    body: str,
    event: str = "",
    command: str = "",
    duration_ms: Optional[float] = None,
    source: str = "",
    call_mode: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """构建符合 Galileo 日志格式的完整 JSON payload（向后兼容包装）。

    内部调用 _build_log_record + _build_batch_payload 实现。

    Args:
        body: 日志正文内容。
        event: 事件标识，用于区分不同的日志事件。
        command: 命令名称，用于标识当前执行的 skill 命令。
        duration_ms: 执行耗时（毫秒），仅在命令结束时传入。
        source: 调用来源标识（如 "cli"、"daemon"），用于区分上报来源。
        call_mode: 当前运行模式（如 "qbot_claw"、"normal"），用于区分调用模式。
        reason: 失败原因分类标识（如 "client_connection_timeout"、"browser_response_timeout"），
                仅在 skill_end_err 事件中使用，用于快速定位异常类型。

    Returns:
        可直接序列化为 JSON 的字典。
    """
    record = _build_log_record(
        body, event=event, command=command, duration_ms=duration_ms,
        source=source, call_mode=call_mode, reason=reason,
    )
    return _build_batch_payload([record])


def _get_ssl_context() -> ssl.SSLContext:
    """获取缓存的 SSL 上下文，首次调用时创建并缓存。"""
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context()
    return _ssl_context


def _send_batch(payload: dict[str, Any]) -> None:
    """通过 HTTPS POST 将批量 payload 发送到 Galileo Telemetry 平台。

    复用模块级缓存的 SSL 上下文，减少重复创建开销。
    任何异常（网络错误、非 2xx 响应等）均静默忽略，不影响主流程。

    Args:
        payload: 已构建好的日志 payload 字典（可包含多条 log_record）。
    """
    # 统计本批次日志条数，用于日志标识
    _count = 0
    try:
        _count = len(payload["resource_logs"][0]["instrumentation_library_logs"][0]["log_records"])
    except Exception:
        pass

    logger.info("[report_log] 开始批量发送: count=%d", _count)
    try:
        data = json.dumps(payload).encode("utf-8")
        logger.debug("[report_log] payload size=%d bytes", len(data))
        req = urllib.request.Request(
            _LOG_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        context = _get_ssl_context()
        resp = urllib.request.urlopen(req, context=context, timeout=10)
        resp.read()  # 消费响应体
        logger.info(
            "[report_log] 批量上报成功: count=%d, status=%s",
            _count, resp.status,
        )
    except Exception:
        logger.info("[report_log] 批量上报失败: count=%d", _count, exc_info=True)


def _sender_loop() -> None:
    """后台发送线程的主循环。

    持续从 _log_queue 中取出日志记录，批量合并后通过 _send_batch 发送。
    - 使用 timeout 等待实现时间窗口收集（避免每条日志立即触发请求）
    - 每次最多取 _BATCH_MAX_SIZE 条日志合并发送
    - 任何异常均捕获并记录本地日志，不退出线程
    """
    while True:
        try:
            # 阻塞等待第一条日志，超时后继续循环（检查是否需要通知 flush）
            try:
                first_record = _log_queue.get(timeout=_BATCH_WAIT_SECONDS)
            except queue.Empty:
                # 队列为空，通知 flush_logs() 可以返回
                _flush_event.set()
                continue

            # 取到第一条后，尝试批量取出更多（非阻塞）
            batch: list[dict[str, Any]] = [first_record]
            while len(batch) < _BATCH_MAX_SIZE:
                try:
                    record = _log_queue.get_nowait()
                    batch.append(record)
                except queue.Empty:
                    break

            # 构建批量 payload 并发送
            payload = _build_batch_payload(batch)
            _send_batch(payload)

            # 标记所有已取出的任务为完成
            for _ in batch:
                _log_queue.task_done()

            # 如果队列已空，通知 flush_logs()
            if _log_queue.empty():
                _flush_event.set()

        except Exception:
            # 捕获所有异常，确保线程不退出
            logger.debug("[report_log] 后台发送线程异常", exc_info=True)


def _ensure_sender_thread() -> None:
    """确保后台发送线程正在运行，若未启动或已退出则（重新）启动。

    使用 _sender_thread_lock 保护，避免并发创建多个线程。
    """
    global _sender_thread
    # 快速检查（无锁），避免每次入队都获取锁
    if _sender_thread is not None and _sender_thread.is_alive():
        return
    with _sender_thread_lock:
        # 双重检查
        if _sender_thread is not None and _sender_thread.is_alive():
            return
        _sender_thread = threading.Thread(target=_sender_loop, daemon=True, name="report-log-sender")
        _sender_thread.start()
        logger.debug("[report_log] 后台发送线程已启动: %s", _sender_thread.name)


def _enqueue_record(record: dict[str, Any]) -> None:
    """将单条 log_record 放入发送队列（非阻塞）。

    若队列已满，丢弃该条日志并记录本地警告。
    同时确保后台发送线程正在运行。

    Args:
        record: 已构建好的 log_record 字典。
    """
    _ensure_sender_thread()
    _flush_event.clear()  # 有新日志入队，标记队列非空
    try:
        _log_queue.put_nowait(record)
    except queue.Full:
        logger.warning("[report_log] 日志队列已满（容量 %d），丢弃当前日志", _QUEUE_MAX_SIZE)


def _read_id_from_file(config_dir: Path) -> None:
    """从本地配置文件读取 guid 和 qimei36（macOS / Linux）。"""
    global _log_guid, _log_qimei36

    guid_file = config_dir / "guid.txt"
    try:
        guid_content = guid_file.read_text(encoding="utf-8").strip()
        if guid_content:
            _log_guid = guid_content
    except Exception:
        logger.debug("读取 guid 文件失败: %s", guid_file, exc_info=True)

    qimei36_file = config_dir / "qimei36.txt"
    try:
        qimei36_content = qimei36_file.read_text(encoding="utf-8").strip()
        if qimei36_content:
            _log_qimei36 = qimei36_content
    except Exception:
        logger.debug("读取 qimei36 文件失败: %s", qimei36_file, exc_info=True)


def _read_id_from_registry() -> None:
    """从 Windows 注册表读取 guid 和 qimei36。

    查询注册表键：
    HKEY_CURRENT_USER\\Software\\Tencent\\Chrome\\FavSync
    - ClientGuid  -> guid
    - ClientQ36   -> qimei36
    """
    global _log_guid, _log_qimei36

    try:
        import winreg

        subkey = r"Software\Tencent\Chrome\FavSync"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "ClientGuid")
                if value:
                    _log_guid = str(value).strip()
            except OSError:
                logger.debug("从注册表读取 ClientGuid 失败", exc_info=True)

            try:
                value, _ = winreg.QueryValueEx(key, "ClientQ36")
                if value:
                    _log_qimei36 = str(value).strip()
            except OSError:
                logger.debug("从注册表读取 ClientQ36 失败", exc_info=True)
    except Exception:
        logger.debug("打开注册表键失败", exc_info=True)


# ============================= 公开接口 =====================================


def init_log() -> None:
    """初始化日志上报参数。

    - 从本地配置文件读取 ``guid`` 和 ``qimei36`` 设备标识：
      - macOS: ``~/Library/Application Support/Chrome3/Default/``
      - Linux: ``~/.config/chrome/Default/``
      - Windows: 注册表 ``HKCU\\Software\\Tencent\\Chrome\\Default``
    - 读取当前 Python 包 ``chrome-skill`` 的版本号作为 skill_version

    应在程序入口处尽早调用一次。若未调用，将使用各参数的默认值进行上报。
    """
    global _log_guid, _log_qimei36, _skill_version, _initialized

    # 根据操作系统确定读取方式
    system = platform.system()
    if system == "Darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "Chrome3" / "Default"
        _read_id_from_file(config_dir)
    elif system == "Linux":
        config_dir = Path.home() / ".config" / "chrome" / "Default"
        _read_id_from_file(config_dir)
    elif system == "Windows":
        _read_id_from_registry()

    # 读取当前包版本号作为 skill_version
    try:
        from importlib.metadata import version as pkg_version
        _skill_version = pkg_version("chrome-skill")
    except Exception:
        logger.debug("读取 chrome-skill 包版本失败", exc_info=True)
        _skill_version = "unknown"

    _initialized = True
    logger.info(
        "[report_log] init_log 完成: guid=%s, qimei36=%s, skill_version=%s, platform=%s",
        _log_guid, _log_qimei36, _skill_version, _platform,
    )


def log(
    body: str,
    event: str = "",
    command: str = "",
    duration_ms: Optional[float] = None,
    source: str = "",
    call_mode: str = "",
    reason: str = "",
) -> None:
    """上报一条日志到 Galileo Telemetry 平台。

    将日志记录放入内部队列，由后台线程批量合并发送，不阻塞主流程。
    任何异常均记录到本地日志，不影响主流程。

    Args:
        body: 日志正文内容。
        event: 事件标识，用于区分不同的日志事件。
        command: 命令名称，用于标识当前执行的 skill 命令。
        duration_ms: 执行耗时（毫秒），仅在命令结束时传入。
        source: 调用来源标识（如 "cli"、"daemon"），用于区分上报来源。
        call_mode: 当前运行模式（如 "qbot_claw"、"normal"），用于区分调用模式。
        reason: 失败原因分类标识，仅在 skill_end_err 事件中使用。
    """
    # 懒初始化：如果 init_log() 尚未被调用，自动初始化一次
    if not _initialized:
        init_log()

    # 构建可读的摘要日志，直接展示关键信息
    _summary = f"event={event}, command={command}"
    if duration_ms is not None:
        _summary += f", duration_ms={duration_ms:.1f}"
    # 尝试从 body 中提取关键字段用于日志展示
    try:
        _body_dict = json.loads(body)
        _parts = []
        if "args" in _body_dict:
            _parts.append(f"args={_body_dict['args']}")
        if "success" in _body_dict:
            _parts.append(f"success={_body_dict['success']}")
        if "error" in _body_dict:
            _err_preview = str(_body_dict['error'])[:100]
            _parts.append(f"error={_err_preview}")
        if "result" in _body_dict:
            _res_preview = str(_body_dict['result'])[:100]
            _parts.append(f"result={_res_preview}")
        if _parts:
            _summary += ", " + ", ".join(_parts)
    except Exception:
        pass
    if source:
        _summary += f", source={source}"
    logger.info("[report_log] 准备上报: %s", _summary)
    try:
        record = _build_log_record(
            body, event=event, command=command, duration_ms=duration_ms,
            source=source, call_mode=call_mode, reason=reason,
        )
        _enqueue_record(record)
    except Exception:
        # 确保不影响主流程
        logger.debug("日志上报构建/入队失败", exc_info=True)


# ---------------------------------------------------------------------------
# 统计上报相关常量与便捷方法
# ---------------------------------------------------------------------------
_REPORT_MAX_RESULT_LEN = 3000

_SENSITIVE_PARAMS = {"url", "text", "actionValue"}


def _sanitize_args(args: Any) -> Any:
    """对上报参数进行处理。"""
    if not isinstance(args, dict):
        return args
    sanitized = {}
    for key, value in args.items():
        if key in _SENSITIVE_PARAMS and isinstance(value, str):
            if key == "url":
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(value)
                    sanitized[key] = f"{parsed.scheme}://{parsed.netloc}/***"
                except Exception:
                    sanitized[key] = "<url, parse_failed>"
            else:
                sanitized[key] = f"<text, len={len(value)}>"
        else:
            sanitized[key] = value
    return sanitized


def _sanitize_result(result: str) -> str:
    """对执行结果进行处理。"""
    if not result:
        return "<empty>"
    return f"<result, len={len(result)}>"


def _truncate_str(text: str, max_len: int = _REPORT_MAX_RESULT_LEN) -> str:
    """截断过长的字符串，超出部分用截断标记替代。

    Args:
        text: 待截断的字符串。
        max_len: 最大保留字符数，默认 500。

    Returns:
        截断后的字符串。若未超出则原样返回。
    """
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"...(truncated, total {len(text)} chars)"


def report_skill_start(
    skill_name: str,
    args: Any,
    source: str = "",
    call_mode: str = "",
) -> None:
    """上报命令开始事件。

    Args:
        skill_name: 技能名称。
        args: 技能参数。
        source: 调用来源标识（如 "cli"、"daemon"）。
        call_mode: 当前运行模式（如 "qbot_claw"、"normal"）。
    """
    try:
        detail = json.dumps({
            "skill_name": skill_name,
            "args": _sanitize_args(args),
        }, ensure_ascii=False)
        _tag = f"{source.upper()}_START" if source else "START"
        body = f"[{_tag}] {skill_name} | {detail}"
        log(body, event="skill_start", command=skill_name, source=source, call_mode=call_mode)
    except Exception:
        logger.debug("统计上报异常（skill_start）", exc_info=True)


def report_skill_end_ok(
    skill_name: str,
    args: Any,
    start_time: float,
    result: str,
    source: str = "",
    call_mode: str = "",
) -> None:
    """上报命令成功结束事件。

    Args:
        skill_name: 技能名称。
        args: 技能参数。
        start_time: 命令开始时间（time.perf_counter() 返回值）。
        result: 命令执行结果文本。
        source: 调用来源标识（如 "cli"、"daemon"）。
        call_mode: 当前运行模式（如 "qbot_claw"、"normal"）。
    """
    try:
        duration_ms = (time.perf_counter() - start_time) * 1000
        detail = json.dumps({
            "skill_name": skill_name,
            "args": _sanitize_args(args),
            "duration_ms": round(duration_ms, 2),
            "result": _sanitize_result(result),
        }, ensure_ascii=False)
        _tag = f"{source.upper()}_SUCCESS" if source else "SUCCESS"
        body = f"[{_tag}] {skill_name} | cost {duration_ms:.0f}ms | {detail}"
        log(body, event="skill_end_ok", command=skill_name, duration_ms=duration_ms, source=source, call_mode=call_mode)
    except Exception:
        logger.debug("统计上报异常（skill_end_ok）", exc_info=True)


def report_skill_end_err(
    skill_name: str,
    args: Any,
    start_time: float,
    error: str,
    source: str = "",
    call_mode: str = "",
    reason: str = "",
) -> None:
    """上报命令失败结束事件。

    Args:
        skill_name: 技能名称。
        args: 技能参数。
        start_time: 命令开始时间（time.perf_counter() 返回值）。
        error: 错误信息。
        source: 调用来源标识（如 "cli"、"daemon"）。
        call_mode: 当前运行模式（如 "qbot_claw"、"normal"）。
        reason: 失败原因分类标识（如 "client_connection_timeout"、"browser_response_timeout"、
                "daemon_not_running"、"rpc_error" 等），用于快速定位异常类型。
    """
    try:
        duration_ms = (time.perf_counter() - start_time) * 1000
        detail_dict = {
            "skill_name": skill_name,
            "args": _sanitize_args(args),
            "duration_ms": round(duration_ms, 2),
            "error": _truncate_str(error),
        }
        if reason:
            detail_dict["reason"] = reason
        detail = json.dumps(detail_dict, ensure_ascii=False)
        _tag = f"{source.upper()}_FAILED" if source else "FAILED"
        body = f"[{_tag}] {skill_name} | cost {duration_ms:.0f}ms | reason={reason} | {detail}" if reason else f"[{_tag}] {skill_name} | cost {duration_ms:.0f}ms | {detail}"
        log(body, event="skill_end_err", command=skill_name, duration_ms=duration_ms, source=source, call_mode=call_mode, reason=reason)
    except Exception:
        logger.debug("统计上报异常（skill_end_err）", exc_info=True)


def flush_logs(timeout: float = 15.0) -> None:
    """等待队列中所有待发送日志发送完毕。

    在程序退出前调用，确保所有异步发送的日志都已完成。
    若超时则直接返回，不无限阻塞调用方。

    Args:
        timeout: 最大等待时间（秒），默认 15 秒。
    """
    if _log_queue.empty():
        return
    _flush_event.clear()
    _flush_event.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# 纯日志远程上报（与事件上报区分，event 为空字符串）
# ---------------------------------------------------------------------------


def report_log_message(
    message: str,
    log_level: str = "INFO",
    module: str = "",
    source: str = "",
    call_mode: str = "",
    event: str = "",
) -> None:
    """上报一条纯日志到 Galileo Telemetry 平台。

    与事件上报（skill_start/end）区分：默认 event 字段为空字符串，
    额外携带 log_level 和 module 属性。如需在 Galileo 上按事件名检索，
    可通过 ``event`` 参数显式指定 event_name。

    Args:
        message: 日志消息正文。
        log_level: 日志级别（如 "INFO"、"WARNING"、"ERROR"）。
        module: 日志来源模块名（如 "daemon_server"、"websocket_manager"）。
        source: 调用来源标识（如 "cli"、"daemon"）。
        call_mode: 当前运行模式（如 "qbot_claw"、"normal"）。
        event: 事件名，用于在 Galileo 上按 event_name 维度过滤统计；
               默认空字符串，表示纯日志上报。
    """
    try:
        # 懒初始化
        if not _initialized:
            init_log()

        record = _build_log_record(
            body=message,
            event=event,
            source=source,
            call_mode=call_mode,
        )

        # 在 attributes 中追加 log_level 和 module 字段
        record["attributes"].append(
            {"key": "log_level", "value": {"string_value": log_level}}
        )
        record["attributes"].append(
            {"key": "module", "value": {"string_value": module}}
        )

        _enqueue_record(record)
    except Exception:
        # 静默忽略，不影响主流程
        pass


# ---------------------------------------------------------------------------
# RemoteLogHandler：自定义 logging.Handler，用于远程上报
# ---------------------------------------------------------------------------


class RemoteLogHandler(logging.Handler):
    """自定义 logging Handler，将日志记录远程上报到 Galileo Telemetry 平台。

    通过 emit() 方法从 LogRecord 中提取模块名、日志级别、格式化消息，
    调用 report_log_message() 完成远程上报。内部异常静默处理，不影响日志系统。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            report_log_message(
                message=message,
                log_level=record.levelname,
                module=record.name,
            )
        except Exception:
            # 静默处理，不影响日志系统正常运行
            pass


# ---------------------------------------------------------------------------
# remote_logger：模块级 logger 实例，导入即可使用
# ---------------------------------------------------------------------------

remote_logger = logging.getLogger("chrome_skill.remote")
# propagate=True（默认值），日志消息会传播到 root logger 的 handler（文件日志、stderr），
# 确保本地日志输出不受影响
remote_logger.propagate = True

try:
    # 避免重复添加 handler
    if not any(isinstance(h, RemoteLogHandler) for h in remote_logger.handlers):
        remote_logger.addHandler(RemoteLogHandler())
except Exception:
    # 初始化失败时退化为普通 logger（仅本地输出，不远程上报）
    logger.warning("[report_log] RemoteLogHandler 初始化失败，remote_logger 将仅输出本地日志", exc_info=True)
