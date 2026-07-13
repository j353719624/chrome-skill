"""
常量定义模块。

集中管理项目中使用的常量，避免硬编码分散到各处。
"""

import os
import sys


def _get_default_data_dir() -> str:
    """根据当前操作系统平台返回默认数据目录（统一存放日志、状态文件、配置等）。

    - Linux/macOS: ~/.chrome-skill
    - Windows:     %LOCALAPPDATA%/chrome-skill
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        return os.path.join(base, "chrome-skill")
    else:
        return os.path.expanduser("~/.chrome-skill")


# 默认数据目录（根据平台自动选择，统一存放日志、状态文件、配置等）
DEFAULT_DATA_DIR = _get_default_data_dir()


def _get_default_log_dir() -> str:
    """根据当前操作系统平台返回默认日志目录。

    日志目录为数据目录下的 logs 子目录：
    - Linux/macOS: ~/.chrome-skill/logs
    - Windows:     %LOCALAPPDATA%/chrome-skill/logs
    """
    return os.path.join(DEFAULT_DATA_DIR, "logs")


# 默认日志目录（根据平台自动选择）
DEFAULT_LOG_DIR = _get_default_log_dir()


def _get_browser_executable() -> str:
    """根据当前操作系统平台返回 QQ 浏览器可执行文件路径/名称。"""
    if sys.platform == "win32":
        # Windows 下默认安装路径
        local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        return os.path.join(local_app, "Chrome", "Chrome.exe")
    elif sys.platform == "darwin":
        return "/Applications/Chrome.app/Contents/MacOS/Chrome"
    else:
        return "chrome-browser-stable"


def _get_browser_process_name() -> str:
    """根据当前操作系统平台返回 QQ 浏览器进程名（用于进程检测）。"""
    if sys.platform == "win32":
        return "Chrome.exe"
    elif sys.platform == "darwin":
        return "Chrome"
    else:
        return "chrome"


# QQ 浏览器可执行文件路径（根据平台自动选择）
BROWSER_EXECUTABLE = _get_browser_executable()

# QQ 浏览器进程名（根据平台自动选择，用于进程检测）
BROWSER_PROCESS_NAME = _get_browser_process_name()