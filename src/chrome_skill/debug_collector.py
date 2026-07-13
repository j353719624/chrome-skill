"""
Debug information collector for Chrome Browser Skill.

收集系统信息、daemon 状态、QQ 浏览器安装状态以及日志文件，
打包为 tar.gz 归档，用于问题排查。
"""

import io
import logging
import os
import platform
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime


logger = logging.getLogger(__name__)


def _get_version() -> str:
    """获取当前包版本号。优先从 importlib.metadata 读取，失败则返回 unknown。"""
    try:
        from importlib.metadata import version
        return version("chrome-skill")
    except Exception:
        return "unknown"


def collect_debug_info(data_dir: str, output_path: str = None) -> str:
    """收集调试信息和数据目录下所有文件，打包为 tar.gz 归档。

    收集内容:
      - 系统信息（OS、架构、Python 版本等）
      - 包版本信息
      - Daemon 运行状态
      - QQ 浏览器安装状态
      - 数据目录下所有文件（日志、状态文件等）

    Args:
        data_dir: 数据目录路径（DEFAULT_DATA_DIR）。
        output_path: 输出文件路径，为 None 则自动生成。

    Returns:
        生成的归档文件绝对路径。
    """
    from . import daemon_server
    from . import installer
    from . import upgrader

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 确定输出路径
    if output_path is None:
        output_path = os.path.join(tempfile.gettempdir(), f"chrome-skill-debug-{timestamp}.tar.gz")

    # 确保输出路径以 .tar.gz 结尾
    if not output_path.endswith(".tar.gz"):
        output_path += ".tar.gz"

    logger.info(f"Collecting debug info, output: {output_path}")

    # --- 1. 收集系统信息 ---
    debug_info = []
    debug_info.append("=" * 60)
    debug_info.append("Chrome Browser Skill Debug Information")
    debug_info.append(f"Collected at: {datetime.now().isoformat()}")
    debug_info.append("=" * 60)

    # 包版本
    debug_info.append(f"\n[Package Version]")
    debug_info.append(f"  chrome-skill: {_get_version()}")

    # 系统信息
    debug_info.append(f"\n[System Information]")
    debug_info.append(f"  Platform     : {sys.platform}")
    debug_info.append(f"  OS           : {platform.platform()}")
    debug_info.append(f"  Architecture : {platform.machine()}")
    debug_info.append(f"  Python       : {sys.version}")
    debug_info.append(f"  Executable   : {sys.executable}")
    debug_info.append(f"  CWD          : {os.getcwd()}")

    # 环境变量（仅收集相关的）
    debug_info.append(f"\n[Environment Variables]")
    env_keys = ["PATH", "HOME", "USER", "DISPLAY", "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "LANG", "LC_ALL",
                "VIRTUAL_ENV", "CONDA_DEFAULT_ENV"]
    for key in env_keys:
        val = os.environ.get(key)
        if val is not None:
            debug_info.append(f"  {key}={val}")

    # QQ 浏览器安装状态
    debug_info.append(f"\n[Chrome Browser]")
    browser_installed = installer.is_installed()
    debug_info.append(f"  Installed: {browser_installed}")
    if browser_installed:
        browser_path = shutil.which("chrome-browser-stable")
        debug_info.append(f"  Path     : {browser_path}")

    # Linux 发行版信息
    if sys.platform == "linux":
        debug_info.append(f"\n[Linux Distribution]")
        try:
            distro = installer.detect_distro()
            debug_info.append(f"  ID     : {distro['id']}")
            debug_info.append(f"  ID_LIKE: {distro['id_like']}")
            debug_info.append(f"  Name   : {distro['name']}")
            debug_info.append(f"  Family : {distro['family']}")
        except Exception as e:
            debug_info.append(f"  Error: {e}")

    # Daemon 状态
    debug_info.append(f"\n[Daemon Status]")
    daemon_running = daemon_server.is_daemon_running()
    debug_info.append(f"  Running: {daemon_running}")
    state = daemon_server.read_state_file()
    if state:
        debug_info.append(f"  PID      : {state.get('pid', '?')}")
        debug_info.append(f"  WS Port  : {state.get('ws_port', '?')}")
        debug_info.append(f"  RPC Port : {state.get('rpc_port', '?')}")
    else:
        debug_info.append(f"  State file: not found")

    # 数据目录信息
    debug_info.append(f"\n[Data Directory]")
    debug_info.append(f"  Path  : {data_dir}")
    debug_info.append(f"  Exists: {os.path.isdir(data_dir)}")
    if os.path.isdir(data_dir):
        data_files = []
        for root, dirs, files in os.walk(data_dir):
            for f in files:
                fpath = os.path.join(root, f)
                try:
                    fsize = os.path.getsize(fpath)
                except OSError:
                    fsize = -1
                rel = os.path.relpath(fpath, data_dir)
                data_files.append((rel, fsize))
        debug_info.append(f"  Files ({len(data_files)}):")
        for rel, fsize in sorted(data_files):
            size_str = f"{fsize} bytes" if fsize >= 0 else "unknown"
            debug_info.append(f"    - {rel} ({size_str})")

    debug_info.append("\n" + "=" * 60)
    debug_info_text = "\n".join(debug_info)

    # 打印到终端
    print(debug_info_text)

    # --- 2. 打包归档 ---
    logger.info("Creating debug archive...")
    with tarfile.open(output_path, "w:gz") as tar:
        # 写入调试信息文本
        info_bytes = debug_info_text.encode("utf-8")
        info_tarinfo = tarfile.TarInfo(name="debug-info.txt")
        info_tarinfo.size = len(info_bytes)
        tar.addfile(info_tarinfo, io.BytesIO(info_bytes))

        # 写入数据目录下所有文件（日志、状态文件等）
        if os.path.isdir(data_dir):
            for root, dirs, files in os.walk(data_dir):
                for f in files:
                    fpath = os.path.join(root, f)
                    arcname = os.path.join("data", os.path.relpath(fpath, data_dir))
                    try:
                        tar.add(fpath, arcname=arcname)
                        logger.debug(f"Added to archive: {arcname}")
                    except Exception as e:
                        logger.warning(f"Failed to add {fpath} to archive: {e}")

        # 写入 guid.txt（如果存在）
        if sys.platform == "linux":
            guid_file = os.path.expanduser("~/.config/chrome/Default/guid.txt")
        elif sys.platform == "darwin":
            guid_file = os.path.expanduser("~/Library/Application Support/Chrome3/Default/guid.txt")
        else:
            guid_file = None
        if guid_file and os.path.exists(guid_file):
            tar.add(guid_file, arcname="guid.txt")
            logger.debug(f"Added to archive: guid.txt")

    logger.info(f"Debug archive created: {output_path}")
    return os.path.abspath(output_path)
