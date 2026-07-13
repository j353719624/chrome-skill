"""
Chrome Browser dependency installer — 跨平台统一调度入口。

根据当前操作系统平台自动选择对应的安装模块：
  - Linux:   installer_linux  (deb/rpm)
  - macOS:   installer_mac    (dmg)
  - Windows: installer_win    (exe)

对外暴露的接口保持不变：
  - install(dry_run, log_dir) -> bool
  - is_installed() -> bool
  - detect_distro() -> dict  (仅 Linux 可用)

Usage (CLI):
    chrome-skill install          # install with default settings
    chrome-skill install --dry-run  # show what would be done without executing
"""

import sys

from .constants import DEFAULT_LOG_DIR


def _get_platform_installer():
    """根据当前操作系统返回对应的安装模块。"""
    if sys.platform == "linux":
        from . import installer_linux
        return installer_linux
    elif sys.platform == "darwin":
        from . import installer_mac
        return installer_mac
    elif sys.platform == "win32":
        from . import installer_win
        return installer_win
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def install(dry_run: bool = False, log_dir: str = DEFAULT_LOG_DIR, force: bool = False) -> bool:
    """安装 Chrome Browser。

    自动检测当前操作系统并调用对应平台的安装程序。

    Args:
        dry_run: 如果为 True，仅打印将要执行的操作，不实际安装。
        log_dir: 日志目录（默认根据平台自动选择）。
        force: 如果为 True，即使已安装也强制重新安装。

    Returns:
        True 表示安装后 Chrome Browser 可用。
    """
    mod = _get_platform_installer()
    return mod.install(dry_run=dry_run, log_dir=log_dir, force=force)


def is_installed() -> bool:
    """检查 Chrome Browser 是否已安装（跨平台）。"""
    mod = _get_platform_installer()
    return mod.is_installed()


def needs_upgrade() -> tuple[bool, str | None, str | None]:
    """检查浏览器是否需要升级。

    Windows 和 macOS 平台支持版本检查，Linux 返回 False。

    Returns:
        (needs_upgrade, current_version, installed_path)
        - needs_upgrade: True 表示需要升级或安装
        - current_version: 当前版本号
        - installed_path: 已安装的路径
    """
    mod = _get_platform_installer()
    if hasattr(mod, 'needs_upgrade'):
        return mod.needs_upgrade()
    return False, None, None


def detect_distro() -> dict:
    """检测 Linux 发行版信息（仅 Linux 可用）。

    Returns:
        dict: 包含 id、id_like、name、family 等字段。

    Raises:
        RuntimeError: 如果不在 Linux 平台上调用。
    """
    if sys.platform != "linux":
        raise RuntimeError("detect_distro() is only available on Linux")
    from . import installer_linux
    return installer_linux.detect_distro()


def close_browser_process() -> bool:
    """关闭正在运行的浏览器进程。

    Windows 和 macOS 平台支持，Linux 直接返回 True。

    Returns:
        True 表示成功关闭或没有运行中的进程，False 表示关闭失败。
    """
    mod = _get_platform_installer()
    if hasattr(mod, 'close_browser_process'):
        return mod.close_browser_process()
    return True