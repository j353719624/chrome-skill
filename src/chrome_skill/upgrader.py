"""
chrome-skill auto-upgrade module.

Provides the following capabilities:
  - Query PyPI for the latest version
  - Periodic version check frequency control (every 10 minutes)
  - Stop browser processes and daemon service
  - Detect installation method (pip vs pipx) and generate upgrade command
  - Post-upgrade hook: auto-execute update logic on version change
"""

import json
import logging
import os
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

HOST_PRODUCTION = "https://pypi.org"
HOST_TEST = "https://test.pypi.org"

PYPI_HOST = HOST_PRODUCTION
IS_TEST = PYPI_HOST == HOST_TEST

PYPI_JSON_URL = f"{PYPI_HOST}/pypi/chrome-skill/json"

# Check interval: 30 minutes (seconds), 测试环境下为 10 秒
CHECK_INTERVAL_SECONDS = 10 if IS_TEST else 1800

# PyPI keyword 前缀，用于标记强制升级的最低版本边界。
# 格式: "force-upgrade-from:X.X.X"，表示版本 <= X.X.X 的用户必须强制升级。
# 在 pyproject.toml 的 keywords 中配置，例如:
#   keywords = ["force-upgrade-from:1.1.1"]
# 表示版本 <= 1.1.1 的用户会被强制要求升级。
# 如果 keywords 为空列表 [] 或不包含此前缀，则不触发强制升级。
FORCE_UPGRADE_KEYWORD_PREFIX = "force-upgrade-from:"

# Force upgrade threshold: if the latest version was released more than
# this many days ago and the user is still on an older version, browser
# skill commands will be blocked until the user runs 'upgrade'.
FORCE_UPGRADE_THRESHOLD_DAYS = 7

# Upgrade log file name
UPGRADE_LOG_FILE = "upgrade.log"

# 缓存 SSL 上下文，避免每次请求都重复检测
_cached_ssl_context: Optional[ssl.SSLContext] = None


def _get_ssl_context() -> ssl.SSLContext:
    """获取用于 HTTPS 请求的 SSL 上下文（结果会被缓存）。

    优先使用 certifi 包提供的 CA 证书（最可靠的跨平台方案）；
    如果 certifi 不可用，则使用系统默认 SSL 上下文；
    如果系统证书也不可用（常见于 macOS 未安装根证书、或公司内网/代理环境），
    最终回退到不验证证书（并记录警告）。
    """
    global _cached_ssl_context
    if _cached_ssl_context is not None:
        return _cached_ssl_context

    # 1. 优先尝试使用 certifi 提供的 CA 证书
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        logger.debug("Using certifi CA bundle for SSL: %s", certifi.where())
        _cached_ssl_context = ctx
        return ctx
    except ImportError:
        logger.debug("certifi not installed, trying system default SSL context")
    except Exception as e:
        logger.debug("certifi SSL context creation failed: %s", e)

    # 2. 尝试系统默认 SSL 上下文
    try:
        ctx = ssl.create_default_context()
        # 验证系统 CA 证书是否可用：尝试连接 PyPI
        import urllib.request
        test_req = urllib.request.Request(
            PYPI_HOST,
            method="HEAD",
            headers={"User-Agent": "chrome-skill-upgrader"},
        )
        urllib.request.urlopen(test_req, timeout=3, context=ctx)
        logger.debug("System default SSL context verified successfully")
        _cached_ssl_context = ctx
        return ctx
    except Exception as e:
        logger.debug("System default SSL verification failed: %s", e)

    # 3. SSL 验证失败，拒绝继续（不降级为 CERT_NONE 以防止 MITM 攻击）
    logger.error(
        "SSL certificate verification failed. Cannot perform secure upgrade check. "
        "To fix this, run: pip install certifi  OR  on macOS run: "
        "/Applications/Python\\ 3.x/Install\\ Certificates.command"
    )
    raise RuntimeError(
        "SSL certificate verification failed. Install certifi or system CA certificates to enable upgrade checks."
    )

# Module-level file handler reference for cleanup
_upgrade_file_handler: logging.FileHandler | None = None


def _setup_upgrade_logging(log_dir: str):
    """Add a dedicated file handler to persist upgrade logs to *log_dir*/upgrade.log.

    The handler is attached to the root logger so that all sub-loggers
    (including those in child modules) benefit.
    """
    global _upgrade_file_handler

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, UPGRADE_LOG_FILE)
        _upgrade_file_handler = logging.FileHandler(log_path, encoding="utf-8")
        _upgrade_file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logging.getLogger().addHandler(_upgrade_file_handler)
        logger.info("Upgrade log file: %s", log_path)
    except Exception as exc:
        print(f"⚠️  Could not create upgrade log file in {log_dir}: {exc}", file=sys.stderr)


def _teardown_upgrade_logging():
    """Remove the upgrade-specific file handler (if any)."""
    global _upgrade_file_handler
    if _upgrade_file_handler is not None:
        _upgrade_file_handler.flush()
        _upgrade_file_handler.close()
        logging.getLogger().removeHandler(_upgrade_file_handler)
        _upgrade_file_handler = None


def _log_and_print(msg: str, level: int = logging.INFO):
    """Print *msg* to stderr AND write it to the upgrade log.

    输出到 stderr 而非 stdout，确保 stdout 只包含 [RESULT] 结果，
    避免 MCP server 通过 subprocess 调用时日志混入工具执行结果。
    """
    print(msg, file=sys.stderr)
    logger.log(level, msg)


# ---------------------------------------------------------------------------
# Version check frequency control (every CHECK_INTERVAL_SECONDS)
# ---------------------------------------------------------------------------

def _get_check_state_path() -> str:
    """返回版本检查状态文件路径。

    使用统一的数据目录 DEFAULT_DATA_DIR：
      - Windows: %LOCALAPPDATA%/chrome-skill/upgrade_check.json
      - Linux/macOS: ~/.chrome-skill/upgrade_check.json
    """
    from .constants import DEFAULT_DATA_DIR
    os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)
    return os.path.join(DEFAULT_DATA_DIR, "upgrade_check.json")


def _read_check_state() -> dict:
    """读取版本检查状态。"""
    path = _get_check_state_path()
    if not os.path.exists(path):
        logger.info("Version check state file not found: %s", path)
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read version check state file %s: %s", path, e)
        return {}


def _write_check_state(state: dict):
    """写入版本检查状态。"""
    path = _get_check_state_path()
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning(f"Failed to write version check state file: {e}")


def _should_check_now() -> bool:
    """判断是否需要检查更新（每 CHECK_INTERVAL_SECONDS 秒最多一次）。"""
    state = _read_check_state()
    last_check = state.get("last_check_timestamp", 0)
    now = time.time()
    elapsed = now - last_check
    should_check = elapsed >= CHECK_INTERVAL_SECONDS
    if not should_check:
        logger.info(
            "Skipping update check: last check %.0fs ago, interval=%ds",
            elapsed, CHECK_INTERVAL_SECONDS,
        )
    return should_check


def _mark_checked(
    latest_version: Optional[str] = None,
    force_upgrade_from: Optional[str] = None,
    upload_time: Optional[str] = None,
):
    """标记本次版本检查已完成，记录时间戳和结果。

    Args:
        latest_version: 最新版本号。
        force_upgrade_from: 强制升级边界版本号（如 "1.1.1"），
            表示 <= 该版本的用户需要强制升级。None 表示不更新此字段，
            空字符串 "" 表示清除强制升级标记。
        upload_time: 最新版本的上传时间。
    """
    state = _read_check_state()
    state["last_check_timestamp"] = time.time()
    state["last_check_date"] = datetime.now(timezone.utc).isoformat()
    if latest_version:
        state["latest_version"] = latest_version
    if force_upgrade_from is not None:
        state["force_upgrade_from"] = force_upgrade_from
    # 清理旧版字段（兼容迁移）
    state.pop("force_upgrade", None)
    if upload_time:
        state["latest_upload_time"] = upload_time
    _write_check_state(state)
    logger.info(
        "Marked check complete: version=%s, force_upgrade_from=%s, upload_time=%s",
        latest_version, force_upgrade_from, upload_time,
    )


# ---------------------------------------------------------------------------
# PyPI version query
# ---------------------------------------------------------------------------

def _get_current_version() -> str:
    """获取当前安装的 chrome-skill 版本。"""
    try:
        from importlib.metadata import version
        ver = version("chrome-skill")
        logger.info("Current installed version: %s", ver)
        return ver
    except Exception as e:
        logger.warning("Failed to get current version, defaulting to 0.0.0: %s", e)
        return "0.0.0"


def _fetch_latest_version() -> Optional[str]:
    """从 PyPI 查询 chrome-skill 的最新版本号。

    Returns:
        最新版本号字符串，查询失败时返回 None。
    """
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(
            PYPI_JSON_URL,
            headers={"Accept": "application/json", "User-Agent": "chrome-skill-upgrader"},
        )
        ssl_ctx = _get_ssl_context()
        with urllib.request.urlopen(req, timeout=5, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latest = data.get("info", {}).get("version")
            logger.info("PyPI query [%s] returned latest version: %s", PYPI_JSON_URL, latest)
            return latest
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to query PyPI for latest version (url=%s): %s", PYPI_JSON_URL, e)
        return None


def _compare_versions(current: str, latest: str) -> int:
    """比较两个版本号。

    Returns:
        -1 表示 current < latest（有新版本）
         0 表示相同
         1 表示 current > latest
    """
    def _parse(v: str):
        try:
            return tuple(int(x) for x in v.split("."))
        except (ValueError, AttributeError):
            return (0,)

    c = _parse(current)
    l = _parse(latest)

    if c < l:
        return -1
    elif c > l:
        return 1
    return 0


def check_for_update() -> Tuple[bool, str, str]:
    """检查是否有新版本可用。

    Returns:
        (has_update, current_version, latest_version)
    """
    current = _get_current_version()
    latest = _fetch_latest_version()

    if latest is None:
        return False, current, "unknown"

    has_update = _compare_versions(current, latest) < 0
    return has_update, current, latest


def _fetch_latest_version_metadata() -> Tuple[Optional[str], Optional[str]]:
    """从 PyPI 一次性查询最新版本的强制升级边界和上传时间。

    从最新版本的 keywords 中解析 "force-upgrade-from:X.X.X" 标记，
    该标记表示版本 <= X.X.X 的用户需要强制升级。

    Returns:
        (force_upgrade_from, upload_time)
        - force_upgrade_from: 强制升级边界版本号字符串（如 "1.1.1"），
          未标记时返回空字符串 ""，查询失败时返回 None
        - upload_time: ISO-8601 上传时间字符串，或 None
    """
    import urllib.request
    import urllib.error

    try:
        # Step 1: 获取最新版本号和上传时间
        req = urllib.request.Request(
            PYPI_JSON_URL,
            headers={"Accept": "application/json", "User-Agent": "chrome-skill-upgrader"},
        )
        ssl_ctx = _get_ssl_context()
        with urllib.request.urlopen(req, timeout=5, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latest_version = data.get("info", {}).get("version")
            # 获取上传时间（从 releases 字段取）
            upload_time = None
            if latest_version:
                releases = data.get("releases", {})
                version_files = releases.get(latest_version, [])
                if version_files:
                    upload_time = version_files[0].get("upload_time")

        if not latest_version:
            logger.warning("Could not determine latest version from PyPI")
            return None, None

        # Step 2: 从最新版本的 keywords 中解析 force-upgrade-from:X.X.X
        version_url = f"{PYPI_HOST}/pypi/chrome-skill/{latest_version}/json"
        req2 = urllib.request.Request(
            version_url,
            headers={"Accept": "application/json", "User-Agent": "chrome-skill-upgrader"},
        )
        with urllib.request.urlopen(req2, timeout=5, context=ssl_ctx) as resp2:
            version_data = json.loads(resp2.read().decode("utf-8"))
            keywords_raw = version_data.get("info", {}).get("keywords") or ""
            # PyPI JSON API 中 keywords 是逗号分隔的字符串
            if isinstance(keywords_raw, list):
                keywords_list = [k.strip() for k in keywords_raw if k.strip()]
            else:
                keywords_list = [k.strip() for k in keywords_raw.split(",") if k.strip()]

            # 解析 force-upgrade-from:X.X.X
            force_upgrade_from = ""
            for kw in keywords_list:
                if kw.startswith(FORCE_UPGRADE_KEYWORD_PREFIX):
                    force_upgrade_from = kw[len(FORCE_UPGRADE_KEYWORD_PREFIX):]
                    break

            logger.info(
                "Version %s metadata: keywords_raw=%r, parsed=%s, force_upgrade_from=%s, upload_time=%s",
                latest_version, keywords_raw, keywords_list, force_upgrade_from, upload_time,
            )
            return force_upgrade_from, upload_time
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError,
            json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning(f"Failed to fetch latest version metadata from PyPI: {e}")
        return None, None


def is_force_upgrade_required() -> Tuple[bool, str]:
    """检查当前版本是否需要强制升级。

    满足以下任一条件即触发强制升级：
      1. 最新版本的 keywords 中包含 "force-upgrade-from:X.X.X"，
         且当前版本 <= X.X.X（发布者主动标记的版本边界）。
      2. 最新版本发布超过 FORCE_UPGRADE_THRESHOLD_DAYS 天，用户仍未升级（时间阈值）。

    Returns:
        (is_required, message) — *message* 是适合打印到 stderr 的提示信息。
    """
    # 优先使用缓存状态，避免每次调用都请求 PyPI。
    # periodic_update_check 每隔 CHECK_INTERVAL_SECONDS 秒刷新一次状态文件。
    state = _read_check_state()
    cached_latest = state.get("latest_version")
    cached_force_upgrade_from = state.get("force_upgrade_from", "")
    cached_upload_time = state.get("latest_upload_time")

    current = _get_current_version()

    logger.info(
        "Force upgrade check: current=%s, cached_latest=%s, cached_force_upgrade_from=%s, cached_upload_time=%s",
        current, cached_latest, cached_force_upgrade_from, cached_upload_time,
    )

    # 如果没有缓存数据，无法判断 — 放行。
    if not cached_latest:
        logger.info("No cached latest version, allowing execution")
        return False, ""

    # 如果用户已是最新版本，不阻塞。
    if _compare_versions(current, cached_latest) >= 0:
        logger.info("Current version is up to date, allowing execution")
        return False, ""

    upgrade_cmd = get_upgrade_command()
    block_msg = (
        "❌ Browser skill commands are blocked until you upgrade.\n"
        f"   Run: '{upgrade_cmd}'"
    )

    # 条件一：最新版本标记了 force-upgrade-from:X.X.X，且当前版本 <= X.X.X。
    if cached_force_upgrade_from:
        if _compare_versions(current, cached_force_upgrade_from) <= 0:
            logger.warning(
                "Force upgrade BLOCKED: current=%s <= force_upgrade_from=%s (latest=%s)",
                current, cached_force_upgrade_from, cached_latest,
            )
            return True, block_msg
        else:
            logger.info(
                "Force upgrade not triggered: current=%s > force_upgrade_from=%s",
                current, cached_force_upgrade_from,
            )

    # 条件二：最新版本发布超过阈值天数，用户仍未升级。
    if cached_upload_time:
        try:
            upload_dt = datetime.fromisoformat(cached_upload_time.replace("Z", "+00:00"))
            # 确保 upload_dt 是 offset-aware；缓存的时间字符串可能不含时区信息
            if upload_dt.tzinfo is None:
                upload_dt = upload_dt.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            days_since_release = (now_utc - upload_dt).days

            logger.info(
                "Days since latest release: %d (threshold=%d)",
                days_since_release, FORCE_UPGRADE_THRESHOLD_DAYS,
            )
            if days_since_release >= FORCE_UPGRADE_THRESHOLD_DAYS:
                logger.warning(
                    "Force upgrade BLOCKED: latest version %s released %d days ago, exceeds %d-day threshold (current=%s)",
                    cached_latest, days_since_release, FORCE_UPGRADE_THRESHOLD_DAYS, current,
                )
                return True, block_msg
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to parse cached upload time '{cached_upload_time}': {e}")

    return False, ""


# ---------------------------------------------------------------------------
# Periodic update check (called on serve startup / command execution)
# ---------------------------------------------------------------------------

def periodic_update_check():
    """定期更新检查。如果有新版本，打印提示信息。

    仅在距离上次检查超过 CHECK_INTERVAL_SECONDS（默认 10 分钟）时执行检查。
    此函数不会阻塞或执行升级操作，仅打印提示。
    """
    if not _should_check_now():
        logger.info("Periodic update check skipped (within check interval)")
        return

    try:
        has_update, current, latest = check_for_update()

        if has_update:
            # 一次请求同时获取强制升级边界和上传时间
            force_from, upload_time = _fetch_latest_version_metadata()
            _mark_checked(
                latest,
                force_upgrade_from=force_from if force_from is not None else "",
                upload_time=upload_time,
            )

            print(f"\n⚠️  A new version of chrome-skill is available: {current} → {latest}", file=sys.stderr)
            if force_from and _compare_versions(current, force_from) <= 0:
                print(f"   ❗ This version requires a mandatory upgrade! (versions <= {force_from} must upgrade)", file=sys.stderr)
            upgrade_cmd = get_upgrade_command()
            print(f"   Run '{upgrade_cmd}' to upgrade\n", file=sys.stderr)
            logger.info(f"New version available: {current} -> {latest} (force_upgrade_from={force_from})")
        else:
            # PyPI 查询失败时 latest 为 "unknown"，此时不覆盖已有的版本缓存，
            # 仅更新检查时间戳，避免丢失之前成功查询到的真实版本信息。
            if latest and latest != "unknown":
                _mark_checked(latest, force_upgrade_from="")
            else:
                _mark_checked()
    except Exception as e:
        logger.warning(f"Periodic update check failed: {e}")
        # 即使检查失败也标记为已检查，避免反复重试
        _mark_checked()


# ---------------------------------------------------------------------------
# Stop browser and daemon service
# ---------------------------------------------------------------------------

def _stop_daemon_service(_daemon_server=None) -> bool:
    """停止 chrome-skill daemon 服务。

    Args:
        _daemon_server: Pre-imported daemon_server module to avoid loading
                        new code after pip upgrade.

    Returns:
        True 表示成功停止或本来就没有在运行。
    """
    if _daemon_server is None:
        from . import daemon_server as _daemon_server

    daemon_server = _daemon_server

    if not daemon_server.is_daemon_running():
        _log_and_print("ℹ️  Daemon service is not running, skipping stop step")
        return True

    _log_and_print("🛑 Stopping daemon service...")
    success = daemon_server.stop_daemon()
    if success:
        # Wait for process to fully exit
        time.sleep(1)
        if not daemon_server.is_daemon_running():
            _log_and_print("✅ Daemon service stopped")
            return True
        else:
            _log_and_print("⚠️  Daemon service may not have fully exited, waiting...")
            time.sleep(2)
            return not daemon_server.is_daemon_running()
    else:
        _log_and_print("⚠️  Failed to stop daemon service, continuing upgrade...", logging.WARNING)
        return True


def _stop_browser_processes() -> bool:
    """停止所有 Chrome Browser 进程。

    Returns:
        True 表示成功停止或没有正在运行的浏览器进程。
    """
    from .constants import BROWSER_PROCESS_NAME

    _log_and_print(f"🛑 Stopping Chrome Browser processes ({BROWSER_PROCESS_NAME})...")

    if sys.platform == "win32":
        return _stop_browser_windows(BROWSER_PROCESS_NAME)
    else:
        return _stop_browser_unix(BROWSER_PROCESS_NAME)


def _stop_browser_unix(process_name: str) -> bool:
    """Unix 平台下停止浏览器进程。

    注意：使用 pkill -f 时会匹配所有命令行包含 process_name 的进程，
    这可能误杀当前正在执行升级的 Python 进程（例如 chrome-skill upgrade）。
    因此需要通过 --ignore-ancestors (如果可用) 或手动过滤排除自身进程。
    """
    my_pid = str(os.getpid())

    def _find_browser_pids() -> list:
        """查找浏览器进程 PID，排除当前进程及其子进程。"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", process_name],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                return []
            pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]
            # 排除当前进程自身及 pgrep 产生的短暂进程
            excluded = {my_pid}
            return [p for p in pids if p not in excluded]
        except FileNotFoundError:
            return []

    try:
        target_pids = _find_browser_pids()

        if not target_pids:
            _log_and_print("ℹ️  No running Chrome Browser processes found")
            return True

        # 使用 kill 对特定 PID 发送 SIGTERM，避免 pkill -f 误杀自身
        _log_and_print(f"   Sending SIGTERM to browser PIDs: {', '.join(target_pids)}")
        for pid in target_pids:
            try:
                os.kill(int(pid), 15)  # SIGTERM
            except (ProcessLookupError, PermissionError) as e:
                logger.warning(f"Failed to send SIGTERM to PID {pid}: {e}")

        _log_and_print("   Termination signal sent, waiting for browser to exit...")
        time.sleep(2)

        # 检查是否还有残留进程
        remaining_pids = _find_browser_pids()
        if not remaining_pids:
            _log_and_print("✅ All Chrome Browser processes stopped")
            return True
        else:
            # 对残留进程发送 SIGKILL 强制终止
            _log_and_print("⚠️  Browser processes still running, force killing...", logging.WARNING)
            for pid in remaining_pids:
                try:
                    os.kill(int(pid), 9)  # SIGKILL
                except (ProcessLookupError, PermissionError) as e:
                    logger.warning(f"Failed to send SIGKILL to PID {pid}: {e}")
            time.sleep(1)
            _log_and_print("✅ Chrome Browser processes force killed")
            return True
    except Exception as e:
        logger.warning(f"Failed to stop browser processes: {e}")
        # 回退到 killall（killall 按进程名匹配，不会匹配 python 进程）
        try:
            subprocess.run(["killall", process_name], capture_output=True)
            time.sleep(2)
            _log_and_print("✅ Chrome Browser processes stopped")
            return True
        except FileNotFoundError:
            logger.warning("killall is not available")
            return False


def _stop_browser_windows(process_name: str) -> bool:
    """Windows 平台下停止浏览器进程。"""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", process_name],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            _log_and_print("✅ Chrome Browser processes stopped")
            time.sleep(1)
            return True
        elif "not found" in result.stderr.lower() or result.returncode == 128:
            _log_and_print("ℹ️  No running Chrome Browser processes found")
            return True
        else:
            logger.warning(f"taskkill returned: {result.returncode}, stderr: {result.stderr}")
            return True
    except FileNotFoundError:
        logger.warning("taskkill is not available")
        return False


# ---------------------------------------------------------------------------
# 安装方式检测与升级命令生成
# ---------------------------------------------------------------------------

# 缓存 pipx 安装检测结果，避免重复执行子进程
_cached_is_pipx: Optional[bool] = None


def _is_installed_via_pipx() -> bool:
    """检查 chrome-skill 是否通过 pipx 安装。

    通过执行 `pipx list --short` 并检查输出中是否包含 `chrome-skill`
    来判断。结果会被缓存，避免重复调用子进程。

    Returns:
        True 表示包由 pipx 管理，False 表示不是或 pipx 不可用。
    """
    global _cached_is_pipx
    if _cached_is_pipx is not None:
        return _cached_is_pipx

    if sys.platform == "win32":
        _cached_is_pipx = False
        return False

    try:
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # pipx list --short 输出格式: "package_name X.X.X"（每行一个包）
            for line in result.stdout.strip().splitlines():
                # 取每行第一个空格前的包名进行精确匹配
                pkg_name = line.strip().split()[0] if line.strip() else ""
                if pkg_name == "chrome-skill":
                    logger.info("chrome-skill is managed by pipx")
                    _cached_is_pipx = True
                    return True
        logger.info("chrome-skill is NOT managed by pipx")
        _cached_is_pipx = False
        return False
    except FileNotFoundError:
        logger.debug("pipx command not found")
        _cached_is_pipx = False
        return False
    except Exception as e:
        logger.debug("Failed to check pipx list: %s", e)
        _cached_is_pipx = False
        return False


def get_upgrade_command() -> str:
    """根据当前平台和安装方式，返回正确的升级命令字符串。

    规则：
      - IS_TEST 测试环境：使用 pip 从 TestPyPI 安装
      - Windows：使用 pip install --upgrade
      - 非 Windows + pipx 安装：使用 pipx upgrade
      - 非 Windows + 非 pipx 安装：使用 pip install --upgrade

    Returns:
        可直接在 shell 中执行的升级命令字符串。
    """
    if IS_TEST:
        return (
            "pip install --upgrade "
            "--index-url https://test.pypi.org/simple/ "
            "--extra-index-url https://pypi.org/simple/ "
            "chrome-skill"
        )

    if sys.platform == "win32":
        return "pip install --upgrade chrome-skill"

    if _is_installed_via_pipx():
        return "pipx upgrade chrome-skill"

    return "pip install --upgrade chrome-skill"


# ---------------------------------------------------------------------------
# Post-Upgrade Hook — 版本变更时自动执行更新逻辑
# ---------------------------------------------------------------------------

def _reinstall_browser() -> bool:
    """重装 Chrome Browser。

    Linux 下使用 --force 强制重装，非 Linux 下执行普通安装。

    Returns:
        True 表示安装成功。
    """
    _log_and_print("📦 Reinstalling Chrome Browser (post-upgrade)...")
    try:
        cmd = ["chrome-skill", "install"]
        # 仅 Linux 下传递 --force 强制重装参数
        if sys.platform == "linux":
            cmd.append("--force")
        # Windows 下隐藏子进程窗口
        extra_kwargs = {}
        if sys.platform == "win32":
            extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            **extra_kwargs,
        )
        # 将子进程输出记录到日志
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                _log_and_print(f"   {line}")
        if result.stderr:
            logger.info("[install stderr] %s", result.stderr.rstrip())

        if result.returncode == 0:
            _log_and_print("✅ Chrome Browser installed successfully")
            return True
        else:
            _log_and_print("❌ Chrome Browser installation failed", logging.ERROR)
            if result.stderr:
                _log_and_print(f"   Error: {result.stderr.strip()}", logging.ERROR)
            return False
    except Exception as e:
        _log_and_print(f"❌ Chrome Browser installation error: {e}", logging.ERROR)
        return False


def _write_check_state_verified(state: dict) -> bool:
    """写入状态文件并验证写入是否成功。

    Returns:
        True 表示写入并验证成功，False 表示写入或验证失败。
    """
    _write_check_state(state)
    # 回读验证
    try:
        readback = _read_check_state()
        expected_version = state.get("installed_version")
        actual_version = readback.get("installed_version")
        if expected_version and actual_version == expected_version:
            return True
        else:
            logger.warning(
                "State file verification failed: expected installed_version=%s, got=%s",
                expected_version, actual_version,
            )
            return False
    except Exception as e:
        logger.warning("State file readback verification failed: %s", e)
        return False


def run_post_upgrade_hook(log_dir: str):
    """Post-Upgrade Hook：检测版本变更并自动执行更新后逻辑。

    在程序启动执行浏览器相关命令（serve 或 skill 命令）时调用。
    比较当前安装版本与状态文件中记录的 installed_version 字段：
      - 如果字段不存在（首次安装），仅记录当前版本，不触发任何操作。
      - 如果当前版本 > 记录版本，执行 post-upgrade 逻辑。
      - 如果版本相同或更低，不触发任何操作。

    Args:
        log_dir: 日志目录路径。
    """
    current = _get_current_version()
    state = _read_check_state()
    recorded_version = state.get("installed_version")

    logger.info(
        "Post-upgrade hook: current=%s, recorded_installed_version=%s",
        current, recorded_version,
    )

    # 版本获取失败（返回 0.0.0），跳过 post-upgrade hook，避免误触发
    if current == "0.0.0":
        logger.warning(
            "Current version is 0.0.0 (metadata unavailable), skipping post-upgrade hook"
        )
        return

    # 首次安装：状态文件中没有 installed_version 字段
    if recorded_version is None:
        logger.info("No installed_version recorded (first install), recording current version")
        state["installed_version"] = current
        _write_check_state(state)
        return

    # 记录的版本是 0.0.0（上次版本获取失败时记录的），视为首次安装，仅更新版本号
    if recorded_version == "0.0.0":
        logger.info(
            "Recorded version is 0.0.0 (previous metadata failure), updating to %s without triggering hook",
            current,
        )
        state["installed_version"] = current
        _write_check_state(state)
        return

    # 版本未变更或降级，不触发
    if _compare_versions(current, recorded_version) <= 0:
        logger.info(
            "No version upgrade detected (current=%s, recorded=%s), skipping post-upgrade hook",
            current, recorded_version,
        )
        return

    # 检测到版本升级，执行 post-upgrade 逻辑
    _log_and_print(f"🔄 Version upgrade detected: {recorded_version} → {current}")
    _log_and_print("   Running post-upgrade tasks...")

    _setup_upgrade_logging(log_dir)
    try:
        # Step 1: 停止 daemon 服务
        _log_and_print("   Step 1/3: Stopping daemon service...")
        _stop_daemon_service()

        # Step 2: 停止浏览器进程
        _log_and_print("   Step 2/3: Stopping Chrome Browser processes...")
        _stop_browser_processes()

        # Step 3: 重装浏览器
        _log_and_print("   Step 3/3: Reinstalling Chrome Browser...")
        browser_success = _reinstall_browser()
        if not browser_success:
            _log_and_print(
                "⚠️  Chrome Browser reinstallation failed during post-upgrade. "
                "You can manually run 'chrome-skill install --force' to retry.",
                logging.WARNING,
            )

        # 无论浏览器重装是否成功，都记录当前版本，避免下次启动重复执行
        state["installed_version"] = current
        if not _write_check_state_verified(state):
            _log_and_print(
                "⚠️  Failed to persist installed_version to state file. "
                "Post-upgrade hook may re-execute on next startup.",
                logging.WARNING,
            )

        _log_and_print("✅ Post-upgrade tasks completed")
    except Exception as e:
        logger.warning("Post-upgrade hook failed: %s", e)
        _log_and_print(
            f"⚠️  Post-upgrade hook encountered an error: {e}\n"
            "   You can manually run 'chrome-skill install --force' to reinstall the browser.",
            logging.WARNING,
        )
        # 即使出错也记录版本，避免反复重试
        state["installed_version"] = current
        if not _write_check_state_verified(state):
            _log_and_print(
                "⚠️  Failed to persist installed_version to state file. "
                "Post-upgrade hook may re-execute on next startup.",
                logging.WARNING,
            )
    finally:
        _teardown_upgrade_logging()



