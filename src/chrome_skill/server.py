"""
Chrome Browser Skill CLI entry point.

Each browser skill is exposed as a CLI subcommand with typed flags
auto-generated from the SkillParam definitions.

Usage:
    # Start the persistent daemon service (must be running first):
    chrome-skill serve                 # foreground mode
    chrome-skill serve --daemon        # background mode

    # Stop / check daemon status:
    chrome-skill stop
    chrome-skill status

    # List all skills:
    chrome-skill list

    # Execute a skill (sends request to the daemon via RPC):
    chrome-skill browser_go_to_url --url https://www.baidu.com
    chrome-skill browser_click_element --index 5
    chrome-skill browser_input_text --index 3 --text "hello world"
    chrome-skill browser_scroll_by --direction down --pixels 300
    chrome-skill browser_wait --seconds 5
    chrome-skill browser_snapshot

    # Show help for a specific skill:
    chrome-skill browser_find_and_act --help

    # Install Chrome Browser dependency:
    chrome-skill install
    chrome-skill install --dry-run
"""
import argparse
import asyncio
import json
import logging
import os
import sys


def _get_version() -> str:
    """获取当前包版本号。优先从 importlib.metadata 读取，失败则返回 unknown。"""
    try:
        from importlib.metadata import version
        return version("chrome-skill")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Logging setup: stdout with [LOG] prefix + optional file log
# ---------------------------------------------------------------------------

from .constants import DEFAULT_LOG_DIR, DEFAULT_DATA_DIR

_log_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# 用于追踪 _setup_logging 自己添加的 handler，避免 clear 时误删其他模块的 handler
_managed_handlers: list = []


class _StderrLogHandler(logging.StreamHandler):
    """A handler that writes log records to stderr with a [LOG] prefix.

    日志输出到 stderr 而非 stdout，确保 stdout 只包含 [RESULT] 结果，
    避免 MCP server 通过 subprocess 调用时日志混入工具执行结果。
    """

    def __init__(self):
        super().__init__(sys.stderr)

    def emit(self, record):
        try:
            msg = self.format(record)
            self.stream.write(f"[LOG] {msg}\n")
            self.stream.flush()
        except Exception:
            self.handleError(record)


def _setup_logging(log_dir: str = DEFAULT_LOG_DIR, enable_stderr: bool = False):
    """Configure root logger with file handler and optional stderr handler.

    Args:
        log_dir: 日志文件目录。
        enable_stderr: 是否启用 stderr 的 [LOG] 输出，默认关闭。
            关闭后日志仅写入文件（~/.chrome-skill/logs/skill.log），
            避免外部调用方合并 stdout+stderr 导致日志混入工具执行结果。
            如需调试可手动设为 True。

    只清除本函数之前添加的 handler，不影响其他模块（如 installer、upgrader）
    添加的 handler。
    """
    global _managed_handlers
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 只移除本函数之前添加的 handler，保留其他模块的 handler
    for h in _managed_handlers:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _managed_handlers.clear()

    # Handler 1: stderr with [LOG] prefix（默认关闭）
    # 开启后日志会同时输出到 stderr，便于终端调试。
    # 默认关闭，避免外部调用方合并 stdout+stderr 导致日志混入工具执行结果。
    if enable_stderr:
        stderr_handler = _StderrLogHandler()
        stderr_handler.setFormatter(_log_formatter)
        root.addHandler(stderr_handler)
        _managed_handlers.append(stderr_handler)

    # Handler 2: file log
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "skill.log")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(_log_formatter)
        root.addHandler(file_handler)
        _managed_handlers.append(file_handler)
    except Exception as e:
        root.warning(f"Failed to create file log handler at {log_dir}: {e}")


# Apply a basic stdout-only config immediately so early imports can log.
# The file handler is added later in main() once --log-dir is known.
_setup_logging()

from .skill_registry import get_executor, get_call_mode, SKILLS, SKILL_MAP, SkillDefinition, SkillParam
from . import vnc_util
from . import installer
from . import upgrader
from .report_log import flush_logs, report_skill_start, report_skill_end_ok, report_skill_end_err, remote_logger, _sanitize_args


def _output_error(error_msg: str):
    """将错误信息输出到 stdout，供模型识别。

    使用 [ERROR] 前缀与成功时的 [RESULT] 前缀区分，
    模型可通过前缀直观判断执行结果是成功还是失败。
    """
    print(f"[ERROR] {error_msg}")


# ---------------------------------------------------------------------------
# Type mapping: SkillParam.type -> Python type for argparse
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": None,  # handled specially as store_true / store_false
}


def _add_param_to_parser(parser: argparse.ArgumentParser, param: SkillParam):
    """Add a SkillParam as a CLI flag to an argparse parser."""
    flag = f"--{param.name}"
    py_type = _TYPE_MAP.get(param.type)

    if param.type == "boolean":
        # For boolean params, add --flag (store true) and --no-flag (store false)
        if param.required:
            # Required boolean: user must pass --name or --no-name
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument(flag, dest=param.name, action="store_true",
                               help=param.description)
            group.add_argument(f"--no-{param.name}", dest=param.name, action="store_false",
                               help=f"Negate: {param.description}")
        else:
            # Optional boolean: default is None (not passed)
            group = parser.add_mutually_exclusive_group(required=False)
            group.add_argument(flag, dest=param.name, action="store_true", default=None,
                               help=param.description)
            group.add_argument(f"--no-{param.name}", dest=param.name, action="store_false",
                               help=f"Negate: {param.description}")
    else:
        kwargs = {
            "type": py_type,
            "help": param.description,
        }
        if param.required:
            kwargs["required"] = True
        else:
            kwargs["default"] = param.default
            kwargs["required"] = False

        if param.enum:
            kwargs["choices"] = param.enum

        parser.add_argument(flag, **kwargs)


def _build_skill_args(skill: SkillDefinition, parsed: argparse.Namespace) -> dict:
    """Extract skill arguments from parsed CLI namespace."""
    args = {}
    for param in skill.params:
        value = getattr(parsed, param.name, None)
        if value is not None:
            args[param.name] = value
        elif param.required:
            # Should not happen if argparse is configured correctly
            args[param.name] = param.default
    return args


# ---------------------------------------------------------------------------
# List subcommand
# ---------------------------------------------------------------------------

def list_skills():
    """Print all available skills to stdout."""
    print("\n=== Available Browser Skills ===\n")
    for skill in SKILLS:
        params_str = ""
        if skill.params:
            parts = []
            for p in skill.params:
                req = "required" if p.required else f"optional, default={p.default}"
                enum_str = f", choices={p.enum}" if p.enum else ""
                parts.append(f"      --{p.name} ({p.type}, {req}{enum_str}): {p.description}")
            params_str = "\n" + "\n".join(parts)
        print(f"  {skill.name}: {skill.description}{params_str}\n")
    print(f"Total: {len(SKILLS)} skills\n")
    print("Usage: chrome-skill <skill_name> [--param value ...]")
    print("       chrome-skill <skill_name> --help")


# ---------------------------------------------------------------------------
# Auto-start daemon helper
# ---------------------------------------------------------------------------

def _auto_start_daemon_and_retry(
    skill: "SkillDefinition",
    skill_args: dict,
    args: argparse.Namespace,
    is_qbotclaw: bool,
):
    """当 daemon 未运行时，自动启动 daemon 并重试执行 skill。

    流程：
    1. 调用 daemon_server.start_daemon_background() 启动 daemon
    2. 先等待状态文件出现（daemon bind 成功后才会写），以避免在错误的默认端口上做无效 health 探测
    3. 以指数退避节奏轮询 rpc_client.check_health() 等待 daemon 就绪（总上限 ~15 秒）
    4. daemon 就绪后重新执行 skill
    5. 如果启动失败或超时，输出错误信息并退出
    """
    import time as _time
    from . import daemon_server
    from . import rpc_client

    remote_logger.info(f"Daemon 未运行，尝试自动启动 daemon 后重试... is_qbotclaw={is_qbotclaw}")

    try:
        daemon_server.start_daemon_background(
            log_dir=args.log_dir,
            from_qbotclaw=is_qbotclaw,
        )
    except Exception as auto_start_e:
        remote_logger.error(f"自动启动 daemon 失败: {auto_start_e}")
        _output_error(
            f"Daemon is not running. Auto-start failed: {auto_start_e}. "
            "Please run 'chrome-skill serve --daemon' manually."
        )
        flush_logs(timeout=5.0)
        sys.exit(1)

    # --- 阶段 A：等状态文件出现 ---
    # daemon 子进程在 bind 到实际 RPC 端口（可能是默认 8766，也可能回退到 60124/60125/随机端口）之后
    # 才会写状态文件。先等它出现，可以避免在错误端口上发起无效的 health 请求。
    _state_wait_deadline = _time.perf_counter() + 5.0  # 最多等 5 秒
    _state_ready = False
    while _time.perf_counter() < _state_wait_deadline:
        _state = daemon_server.read_state_file()
        if _state is not None and "rpc_port" in _state:
            _state_ready = True
            break
        _time.sleep(0.1)

    if not _state_ready:
        remote_logger.warning(
            "等待 daemon 状态文件超时（5s），将使用默认 RPC 端口继续轮询 health"
        )

    # --- 阶段 B：指数退避轮询 health ---
    # 节奏：0.1 → 0.2 → 0.4 → 0.8 → 1.0 → 1.0 → ... ，总上限约 15s
    _deadline = _time.perf_counter() + 15.0
    _delay = 0.1
    _daemon_ready = False
    while _time.perf_counter() < _deadline:
        try:
            # 每轮都重新读状态文件：回退/随机端口下实际端口可能变化
            _actual_port = rpc_client.get_rpc_port_from_state()
            if asyncio.run(rpc_client.check_health(port=_actual_port, timeout=2.0)):
                _daemon_ready = True
                break
        except Exception:
            pass
        # 根据剩余时间决定实际睡眠时长，避免越过 deadline
        _remaining = _deadline - _time.perf_counter()
        if _remaining <= 0:
            break
        _time.sleep(min(_delay, _remaining))
        _delay = min(_delay * 2, 1.0)

    if not _daemon_ready:
        remote_logger.error("Daemon 自动启动超时（15s），放弃重试")
        _output_error(
            "Daemon is not running. Attempted auto-start but timed out. "
            "Please run 'chrome-skill serve --daemon' manually."
        )
        flush_logs(timeout=5.0)
        sys.exit(1)

    # Daemon 就绪，重试执行 skill
    remote_logger.info("Daemon 自动启动成功，重试执行 skill '%s'", skill.name)
    _cli_start_time = _time.perf_counter()
    report_skill_start(skill.name, skill_args, source="cli", call_mode=get_call_mode(is_qbotclaw))
    try:
        asyncio.run(run_skill_via_rpc(skill.name, skill_args, args.log_dir))
        remote_logger.info(f"Skill '{skill.name}' executed successfully (after auto-start daemon)")
        report_skill_end_ok(
            skill.name, skill_args, _cli_start_time, "success",
            source="cli", call_mode=get_call_mode(is_qbotclaw),
        )
        flush_logs(timeout=5.0)
        sys.exit(0)
    except Exception as retry_e:
        remote_logger.error(f"自动启动 daemon 后重试仍然失败: {retry_e}")
        report_skill_end_err(
            skill.name, skill_args, _cli_start_time, str(retry_e),
            source="cli", call_mode=get_call_mode(is_qbotclaw),
            reason="retry_after_auto_start_failed",
        )
        _output_error(f"Auto-started daemon but skill execution still failed: {retry_e}")
        flush_logs(timeout=5.0)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Execution (via RPC to daemon)
# ---------------------------------------------------------------------------

async def run_skill_via_rpc(skill_name: str, args: dict, log_dir: str = DEFAULT_LOG_DIR):
    """Execute a skill by sending an RPC request to the running daemon.

    The daemon hosts the persistent WebSocket server and browser connection.
    CLI processes are lightweight clients that delegate execution to the daemon.
    """
    from . import rpc_client
    from .daemon_server import read_state_file, DEFAULT_RPC_PORT

    # Determine RPC port from state file
    rpc_port = DEFAULT_RPC_PORT
    state = read_state_file()
    if state and "rpc_port" in state:
        rpc_port = state["rpc_port"]

    result = await rpc_client.execute_skill(
        skill_name=skill_name,
        args=args,
        port=rpc_port,
    )

    if result.get("success"):
        result_json = json.dumps(result["result"], ensure_ascii=False, indent=2)
        print(f"[RESULT] {result_json}")
    else:
        error_msg = result.get("error", "Unknown error")
        _output_error(error_msg)
        sys.exit(1)


async def run_skill(skill_name: str, args: dict, log_dir: str = DEFAULT_LOG_DIR):
    """Execute a single skill directly (used by MCP server / in-process callers).

    NOTE: This function intentionally does NOT call executor.cleanup().
    The WebSocket server and connections are kept alive across calls so that
    long-lived callers (e.g. MCP server) can reuse the same connection.
    For CLI one-shot usage, cleanup is handled by the caller (main()).
    """
    vnc_util.set_browser_log_dir(log_dir)
    executor = get_executor()
    result = await executor.execute(skill_name, **args)
    result_json = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    # Mark result output so callers can distinguish it from log lines
    print(f"[RESULT] {result_json}")


# ---------------------------------------------------------------------------
# Main: build subcommand parser dynamically from skill definitions
# ---------------------------------------------------------------------------

def main():
    # 在 Windows 上强制将 stdout/stderr 设置为 UTF-8 编码，避免中文输出乱码
    if sys.platform == "win32":
        import io
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="chrome-skill",
        description="Chrome Browser Skill CLI — control QQ browser from the command line.",
        usage="chrome-skill <command> [options]",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=DEFAULT_LOG_DIR,
        help=f"Log directory for QQ browser & skill log files (default: {DEFAULT_LOG_DIR})"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command or skill name")

    # 'serve' subcommand — start the persistent daemon service
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the persistent daemon service (WebSocket + RPC servers)",
        description="Start the persistent daemon service that maintains the WebSocket "
                    "connection to the browser extension and accepts skill requests via RPC.",
    )
    serve_parser.add_argument(
        "--daemon", "-d",
        action="store_true",
        default=False,
        help="Run in background (daemon) mode",
    )
    serve_parser.add_argument(
        "--ws-port",
        type=int,
        default=8765,
        help="WebSocket server port for browser extension (default: 8765)",
    )
    serve_parser.add_argument(
        "--rpc-port",
        type=int,
        default=None,
        help="HTTP RPC server port for CLI requests (default: 8766, with fallback to 60124/60125/random)",
    )
    serve_parser.add_argument(
        "--from-qbotclaw",
        action="store_true",
        default=False,
        help="Mark this daemon as launched by qbotclaw (skips browser checks, auto-stops existing daemon)",
    )

    # 'stop' subcommand — stop the running daemon
    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop the running daemon service",
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force stop the daemon even in qbotclaw mode",
    )

    # 'status' subcommand — check daemon status
    subparsers.add_parser(
        "status",
        help="Check the daemon service status",
    )

    # 'list' subcommand
    subparsers.add_parser("list", help="List all available browser skills")

    # 'debug' subcommand — collect debug info and logs
    debug_parser = subparsers.add_parser(
        "debug",
        help="Collect debug information and log files into a tar.gz archive",
        description="Collect system info, daemon status, and all log files, "
                    "then package them into a .tar.gz archive for troubleshooting.",
    )
    debug_parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output file path for the debug archive (default: auto-generated in /tmp)",
    )

    # 'install' subcommand — install Chrome Browser dependency
    install_parser = subparsers.add_parser(
        "install",
        help="Install Chrome Browser and system dependencies",
        description="Download and install the Chrome Browser package for the current platform (Linux/macOS/Windows).",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be done without actually installing",
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force reinstall even if Chrome Browser is already installed",
    )

    # One subcommand per skill
    for skill in SKILLS:
        sub = subparsers.add_parser(
            skill.name,
            help=skill.description,
            description=skill.description,
        )
        for param in skill.params:
            _add_param_to_parser(sub, param)

    args = parser.parse_args()

    # Re-setup logging with the user-specified log directory (adds file handler)
    _setup_logging(args.log_dir)

    logger = logging.getLogger(__name__)
    # 远程上报：CLI 命令开始执行
    remote_logger.info("CLI 命令开始执行: command=[%s], version=[%s]", args.command, _get_version())
    logger.debug(f"Parsed arguments: {vars(args)}")

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "list":
        logger.info("Listing all available skills")
        list_skills()
        sys.exit(0)

    if args.command == "debug":
        logger.info("Running debug info collection")
        from .debug_collector import collect_debug_info
        output_path = collect_debug_info(DEFAULT_DATA_DIR, args.output)
        print(f"\n✅ Debug archive created: {output_path}")
        sys.exit(0)

    if args.command == "install":
        # qbotclaw 模式下，浏览器由外部管理，install 直接返回成功
        from . import daemon_server
        state = daemon_server.read_state_file()
        if state is not None and state.get("from_qbotclaw"):
            remote_logger.info("[qbotclaw mode] Skipping install, returning success directly")
            print("✅ Chrome Browser is already installed.")
            sys.exit(0)
        remote_logger.info(f"Running install (dry_run={args.dry_run}, force={args.force})")
        success = installer.install(dry_run=args.dry_run, log_dir=args.log_dir, force=args.force)
        remote_logger.info(f"Install finished, success={success}")
        sys.exit(0 if success else 1)

    if args.command == "stop":
        from . import daemon_server
        # qbotclaw 模式下，daemon 由外部管理，不允许手动停止（除非 --force）
        if not getattr(args, "force", False):
            state = daemon_server.read_state_file()
            if state is not None and state.get("from_qbotclaw"):
                remote_logger.info("[qbotclaw mode] Skipping stop, daemon is managed externally")
                print("✅ Daemon is running. You can use skill commands directly.")
                sys.exit(0)
        remote_logger.info("Stopping daemon")
        import time as _time
        _stop_start = _time.perf_counter()
        report_skill_start("stop", {"force": getattr(args, "force", False)}, source="cli", call_mode="normal")
        try:
            result = daemon_server.stop_daemon()
            report_skill_end_ok("stop", {"force": getattr(args, "force", False)}, _stop_start, "stopped" if result else "no_daemon", source="cli", call_mode="normal")
        except Exception as e:
            report_skill_end_err("stop", {"force": getattr(args, "force", False)}, _stop_start, str(e), source="cli", call_mode="normal", reason="stop_daemon_error")
        flush_logs(timeout=5.0)
        sys.exit(0)

    if args.command == "status":
        logger.info("Checking daemon status")
        _show_status()
        # Periodic update check — status is a natural time to notify the user
        upgrader.periodic_update_check()
        sys.exit(0)

    # qbotclaw 模式判断：serve 命令带 --from-qbotclaw 时跳过所有浏览器前置检查
    is_qbotclaw = args.command == "serve" and getattr(args, "from_qbotclaw", False)
    if not is_qbotclaw:
        # 当前命令未显式指定 qbotclaw 模式时，检查守护进程是否已以 qbotclaw 模式运行
        # 无论是 serve 还是 skill 命令，只要已有 daemon 在 qbotclaw 模式下运行，就继承该模式
        from . import daemon_server
        state = daemon_server.read_state_file()
        if state is not None and state.get("from_qbotclaw"):
            is_qbotclaw = True
            if args.command == "serve":
                remote_logger.info("[qbotclaw mode] Existing daemon is running in qbotclaw mode, inheriting qbotclaw mode for new serve command")
            else:
                remote_logger.info("[qbotclaw mode] Daemon is running in qbotclaw mode, skipping all browser pre-checks for skill command")
    if is_qbotclaw:
        remote_logger.info("[qbotclaw mode] Skipping all browser pre-checks (upgrade hook, install check, version check, browser process, periodic update, force upgrade)")

    if not is_qbotclaw:
        # Post-upgrade hook: 检测版本变更并自动执行更新后逻辑（重装浏览器等）。
        # 必须在浏览器安装检查和 serve 版本检查之前执行，
        # 否则升级后首次运行时浏览器版本检查会提前退出，导致 hook 永远无法执行。
        upgrader.run_post_upgrade_hook(args.log_dir)

        # 除 install、list、stop、status 外的所有命令，先检查浏览器是否已安装
        if not installer.is_installed():
            remote_logger.error("前置检查失败: Chrome Browser 未安装")
            _output_error("Chrome Browser is not installed. Please run 'chrome-skill install' first.")
            sys.exit(1)

        # serve 命令：检查浏览器是否需要更新
        if args.command == "serve":
            needs_upgrade, current_version, _ = installer.needs_upgrade()
            remote_logger.info(f"Chrome Browser needs upgrade: {needs_upgrade}")
            if needs_upgrade:
                remote_logger.error("前置检查失败: Chrome Browser 需要更新, current_version=%s", current_version)
                if current_version:
                    _output_error(f"Chrome Browser version {current_version} needs update. Please run 'chrome-skill install' first.")
                else:
                    _output_error("Chrome Browser is not installed. Please run 'chrome-skill install' first.")
                sys.exit(1)

            # 检查并关闭正在运行的浏览器进程
            installer.close_browser_process()

        # Periodic update check before executing browser-related commands (serve + skills).
        # This runs at most once every 10 minutes (controlled by state file),
        # so the network overhead is negligible for most invocations.
        upgrader.periodic_update_check()

        # Force upgrade check: block browser-related commands (serve + skills) if the
        # user's version is outdated beyond the threshold (FORCE_UPGRADE_THRESHOLD_DAYS).
        force_required, force_msg = upgrader.is_force_upgrade_required()
        if force_required:
            remote_logger.error("前置检查失败: 强制升级要求, %s", force_msg)
            _output_error(force_msg)
            sys.exit(1)

    if args.command == "serve":
        from . import daemon_server
        # 使用 is_qbotclaw 而非仅从 args 读取，以继承已有 daemon 的 qbotclaw 模式
        from_qbotclaw = is_qbotclaw

        # qbotclaw 模式下（非显式 --from-qbotclaw 启动），如果 daemon 已在运行，
        # 直接返回成功，避免模型误调 serve 导致重启已有 daemon
        if from_qbotclaw and not getattr(args, "from_qbotclaw", False):
            info = daemon_server.get_daemon_info()
            if info is not None:
                pid = info.get("pid", "?")
                ws_port = info.get("ws_port", "?")
                rpc_port = info.get("rpc_port", "?")
                remote_logger.info(f"[qbotclaw mode] Daemon already running (PID: {pid}), skipping serve")
                print(f"✅ Daemon is already running (PID: {pid}). You can use skill commands directly.")
                print(f"   WebSocket Server : ws://127.0.0.1:{ws_port}")
                print(f"   HTTP RPC Server  : http://127.0.0.1:{rpc_port}")
                sys.exit(0)

        import time as _time
        # 区分用户是否显式指定了 --rpc-port：未指定时 args.rpc_port 为 None，使用完整候选列表端口回退逻辑；
        # 显式指定时尊重用户设置，仅在用户端口占用时回退随机端口。
        user_rpc_port = args.rpc_port
        rpc_port_explicit = user_rpc_port is not None
        effective_rpc_port = user_rpc_port if rpc_port_explicit else 8766
        _serve_args = {"daemon": args.daemon, "ws_port": args.ws_port, "rpc_port": effective_rpc_port, "from_qbotclaw": from_qbotclaw}
        _serve_mode = "background" if args.daemon else "foreground"
        _serve_start = _time.perf_counter()
        report_skill_start("serve", _serve_args, source="cli", call_mode=get_call_mode(from_qbotclaw))
        try:
            if args.daemon:
                remote_logger.info(f"Starting daemon in background mode (ws_port={args.ws_port}, rpc_port={effective_rpc_port}, explicit={rpc_port_explicit}, from_qbotclaw={from_qbotclaw})")
                daemon_server.start_daemon_background(
                    ws_port=args.ws_port,
                    rpc_port=effective_rpc_port,
                    log_dir=args.log_dir,
                    from_qbotclaw=from_qbotclaw,
                    rpc_port_explicit=rpc_port_explicit,
                )
            else:
                remote_logger.info(f"Starting daemon in foreground mode (ws_port={args.ws_port}, rpc_port={effective_rpc_port}, explicit={rpc_port_explicit}, from_qbotclaw={from_qbotclaw})")
                daemon_server.start_daemon_foreground(
                    ws_port=args.ws_port,
                    rpc_port=effective_rpc_port,
                    log_dir=args.log_dir,
                    from_qbotclaw=from_qbotclaw,
                    rpc_port_explicit=rpc_port_explicit,
                )
            report_skill_end_ok("serve", _serve_args, _serve_start, f"daemon started in {_serve_mode} mode", source="cli", call_mode=get_call_mode(from_qbotclaw))
        except Exception as e:
            report_skill_end_err("serve", _serve_args, _serve_start, str(e), source="cli", call_mode=get_call_mode(from_qbotclaw), reason="daemon_start_error")
        flush_logs(timeout=5.0)
        sys.exit(0)

    # Execute skill via RPC to daemon
    skill = SKILL_MAP.get(args.command)
    if not skill:
        logger.error(f"Unknown skill: {args.command}")
        _output_error(f"Unknown skill: {args.command}")
        sys.exit(1)

    skill_args = _build_skill_args(skill, args)
    remote_logger.info(f"Executing skill '{skill.name}' via RPC with args: {_sanitize_args(skill_args)}")

    # --- 预探 daemon 健康度（避免在错误端口上 POST /execute 卡死 ~7.5s） ---
    # 背景：若默认 RPC 端口 8766 被"非本服务"的进程占用，直接 POST /execute 会陷入
    # HTTP 层的 readline 超时（当前默认 timeout=300s，实际被对端关闭后要等 ~7.5s）。
    # 改为先用短超时（2s）的 check_health 预探：check_health 会同时校验
    # service == SERVICE_NAME，能稳定地识别出"占用端口的不是本服务"。
    # 预探失败直接走自动启动分支，避开卡顿。
    from . import rpc_client as _rpc_client_probe
    try:
        _probe_port = _rpc_client_probe.get_rpc_port_from_state()
    except Exception:
        from .rpc_client import DEFAULT_RPC_PORT as _probe_port  # type: ignore
    _daemon_alive = False
    try:
        _daemon_alive = asyncio.run(_rpc_client_probe.check_health(port=_probe_port, timeout=2.0))
    except Exception:
        _daemon_alive = False

    if not _daemon_alive:
        remote_logger.info(
            "Pre-check 未探到本服务 daemon（port=%s），直接走自动启动分支", _probe_port,
        )
        _auto_start_daemon_and_retry(skill, skill_args, args, is_qbotclaw)
        # _auto_start_daemon_and_retry 内部会 sys.exit，理论上不会返回
        return

    # --- CLI 层面统计上报：命令开始 ---
    import time as _time
    _cli_start_time = _time.perf_counter()
    report_skill_start(skill.name, skill_args, source="cli", call_mode=get_call_mode(is_qbotclaw))

    try:
        asyncio.run(run_skill_via_rpc(skill.name, skill_args, args.log_dir))
        remote_logger.info(f"Skill '{skill.name}' executed successfully")

        # --- CLI 层面统计上报：命令成功结束 ---
        report_skill_end_ok(skill.name, skill_args, _cli_start_time, "success", source="cli", call_mode=get_call_mode(is_qbotclaw))

    except Exception as e:
        from .rpc_client import DaemonNotRunningError, RPCError

        if isinstance(e, DaemonNotRunningError):
            # daemon 未运行：不上报失败，直接自动启动 daemon 并重试（重试流程内部会独立上报）
            _auto_start_daemon_and_retry(skill, skill_args, args, is_qbotclaw)
        elif isinstance(e, RPCError):
            report_skill_end_err(skill.name, skill_args, _cli_start_time, str(e), source="cli", call_mode=get_call_mode(is_qbotclaw), reason="rpc_error")
            remote_logger.error(f"RPC call failed: {e}")
            _output_error(f"RPC call failed: {e}")
            flush_logs(timeout=5.0)
            sys.exit(1)
        else:
            report_skill_end_err(skill.name, skill_args, _cli_start_time, str(e), source="cli", call_mode=get_call_mode(is_qbotclaw), reason="unknown")
            remote_logger.exception(f"Unexpected error while executing skill '{skill.name}'")
            flush_logs(timeout=5.0)
            raise

    # 等待所有上报完成后再退出
    flush_logs(timeout=5.0)


def _show_status():
    """Show the current daemon status.

    综合使用状态文件、进程扫描和端口探测来判断 daemon 状态。
    """
    from . import daemon_server
    from . import rpc_client

    info = daemon_server.get_daemon_info()
    if info is None:
        # 所有检测手段都未发现 daemon
        # 清理可能残留的状态文件
        if daemon_server.read_state_file() is not None:
            daemon_server.remove_state_file()
        print("❌ Daemon is not running.")
        print("   Start it with: chrome-skill serve --daemon")
        return

    pid = info.get("pid", "?")
    ws_port = info.get("ws_port", "?")
    rpc_port = info.get("rpc_port", "?")

    # 如果状态文件不存在但进程/端口检测到了 daemon，给出提示
    state = daemon_server.read_state_file()
    source_hint = ""
    if state is None:
        source_hint = " (detected via process scan, state file missing)"

    print(f"✅ Daemon is running{source_hint}")
    print(f"   PID              : {pid}")
    print(f"   WebSocket Server : ws://127.0.0.1:{ws_port}")
    print(f"   HTTP RPC Server  : http://127.0.0.1:{rpc_port}")

    # 显示运行模式
    if state is not None and state.get("from_qbotclaw"):
        print(f"   Mode             : qbotclaw")
    else:
        print(f"   Mode             : normal")

    # Try to get detailed status via RPC
    try:
        status = asyncio.run(rpc_client.get_status(port=int(rpc_port)))
        clients = status.get("connected_clients", 0)
        print(f"   Connected clients: {clients}")
    except Exception:
        print("   (Could not retrieve detailed status via RPC)")


if __name__ == "__main__":
    main()
