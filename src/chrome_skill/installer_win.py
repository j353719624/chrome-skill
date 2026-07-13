"""
Chrome Browser Windows installer.

从官方下载服务器下载 Chrome Browser 的 Windows 安装包，
使用静默安装参数进行安装。
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

from .installer_common import (
    _setup_install_logging,
    _teardown_install_logging,
    _log_and_print,
    _run,
    _download_with_urllib,
)
from .constants import BROWSER_EXECUTABLE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://dldir1v6.qq.com/invc/tt/QB/Public/qbskill"
QB_EXE = "ChromeSetup.exe"
QB_UPGRADER_TXT = "QBUpgrader.txt"

# 最低要求的浏览器版本
MIN_VERSION = "21.0.8293.400"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_platform():
    """确保当前运行在 Windows 平台上。"""
    if sys.platform != "win32":
        _log_and_print(f"❌ Unsupported platform: {sys.platform}. This installer is for Windows only.", logging.ERROR)
        sys.exit(1)


def _is_admin() -> bool:
    """检查当前进程是否以管理员权限运行。
    
    Returns:
        True 表示当前有管理员权限，False 表示没有。
    """
    try:
        # 使用 net session 命令检查
        result = subprocess.run(
            ["net", "session"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_admin_or_skip() -> bool:
    """检查管理员权限，如果不是管理员则提示并退出。
    
    Returns:
        True 表示是管理员或浏览器已安装。
    """
    # 如果浏览器已安装且版本符合要求，不需要管理员权限
    needs_upgrade, _, _ = _needs_upgrade()
    if not needs_upgrade:
        return True
    
    # 需要安装或升级，检查管理员权限
    if not _is_admin():
        print("❌ Administrator privileges are required to install Chrome Browser.", file=sys.stderr)
        print("   Please run this command as Administrator.", file=sys.stderr)
        sys.exit(1)
    
    logger.info("✅ Running with Administrator privileges")
    return True


def _get_browser_path_from_registry() -> str | None:
    """从 Windows 注册表获取 QQ 浏览器安装路径。
    
    查询注册表键：
    - HKCU\Software\Tencent\Chrome\CurrentVersion\App Paths\Chrome.exe
    - HKLM\SOFTWARE\Tencent\Chrome (系统级安装)
    
    Returns:
        浏览器可执行文件路径，未找到返回 None。
    """
    import winreg
    
    # 注册表键列表，按优先级排序
    registry_keys = [
        # 用户级安装
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Chrome\CurrentVersion\App Paths\Chrome.exe"),
        # 系统级安装
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Chrome"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Chrome"),  # 32位程序在64位系统上
    ]
    
    for hkey, subkey in registry_keys:
        try:
            with winreg.OpenKey(hkey, subkey) as key:
                # 尝试读取默认值或 EXE 路径
                try:
                    value, _ = winreg.QueryValueEx(key, "")  # 默认值
                    if value and os.path.isfile(value):
                        return value
                except FileNotFoundError:
                    pass
                
                try:
                    value, _ = winreg.QueryValueEx(key, "ExePath")
                    if value and os.path.isfile(value):
                        return value
                except FileNotFoundError:
                    pass
                
                try:
                    value, _ = winreg.QueryValueEx(key, "InstallPath")
                    if value:
                        exe_path = os.path.join(value, "Chrome.exe")
                        if os.path.isfile(exe_path):
                            return exe_path
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug(f"Registry lookup failed for {subkey}: {e}")
            continue
    
    return None


def _get_installed_browser_path() -> str | None:
    """获取已安装的 QQ 浏览器路径。
    
    按优先级依次尝试：
    1. 从注册表查询（最准确，支持自定义安装路径）
    2. 检查默认可执行文件路径
    3. 在 PATH 中查找
    4. 检查常见安装路径
    
    Returns:
        浏览器可执行文件路径，未找到返回 None。
    """
    # 方式 1：从注册表获取（优先，支持自定义安装路径如 E:\Chrome）
    reg_path = _get_browser_path_from_registry()
    if reg_path:
        return reg_path

    # 方式 2：检查默认可执行文件路径
    if os.path.isfile(BROWSER_EXECUTABLE):
        return BROWSER_EXECUTABLE

    # 方式 3：在 PATH 中查找
    path_result = shutil.which("Chrome.exe")
    if path_result is not None:
        return path_result

    # 方式 4：检查常见安装路径
    common_paths = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Chrome", "Chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Chrome", "Chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Chrome", "Chrome.exe"),
        # 腾讯软件中心/QQ浏览器官网默认安装路径
        r"C:\Program Files\Tencent\Chrome\Chrome.exe",
        r"C:\Program Files (x86)\Tencent\Chrome\Chrome.exe",
    ]
    for path in common_paths:
        if path and os.path.isfile(path):
            return path
    
    return None


def _get_browser_version(browser_path: str) -> str | None:
    """获取 QQ 浏览器版本号（Windows 平台）。
    
    使用 Windows API 读取可执行文件的版本信息。
    
    Args:
        browser_path: 浏览器可执行文件路径。
        
    Returns:
        版本号字符串，获取失败返回 None。
    """
    try:
        import ctypes
        from ctypes import wintypes
        
        # 获取版本信息大小
        size = ctypes.windll.version.GetFileVersionInfoSizeW(browser_path, None)
        if size == 0:
            return None
        
        # 分配缓冲区并获取版本信息
        buffer = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(browser_path, None, size, buffer)
        
        # 查询 VS_FIXEDFILEINFO
        value = wintypes.LPVOID()
        value_size = wintypes.UINT()
        
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)
        ):
            return None
        
        # 解析版本信息
        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]
        
        info = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        major = (info.dwFileVersionMS >> 16) & 0xFFFF
        minor = info.dwFileVersionMS & 0xFFFF
        patch = (info.dwFileVersionLS >> 16) & 0xFFFF
        build = info.dwFileVersionLS & 0xFFFF
        
        return f"{major}.{minor}.{patch}.{build}"
    except Exception as e:
        logger.warning(f"Failed to get browser version: {e}")
        return None


def _compare_versions(v1: str, v2: str) -> int:
    """比较两个版本号。
    
    Returns:
        -1 if v1 < v2
         0 if v1 == v2
         1 if v1 > v2
    """
    try:
        parts1 = [int(x) for x in v1.split(".")]
        parts2 = [int(x) for x in v2.split(".")]
        
        # 补齐长度
        max_len = max(len(parts1), len(parts2))
        parts1.extend([0] * (max_len - len(parts1)))
        parts2.extend([0] * (max_len - len(parts2)))
        
        for p1, p2 in zip(parts1, parts2):
            if p1 < p2:
                return -1
            if p1 > p2:
                return 1
        return 0
    except Exception:
        return 0


def _fetch_target_version() -> str | None:
    """从远程获取目标升级版本号。
    
    从 https://dldir1v6.qq.com/invc/tt/QB/Public/qbskill/QBUpgrader.txt 获取更新信息。
    
    Returns:
        target_version 字符串，获取失败或解析失败返回 None。
    """
    import json
    import urllib.request
    
    url = f"{BASE_URL}/QBUpgrader.txt"
    try:
        _log_and_print(f"🔍 Checking for updates from {url}")
        
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Chrome-Skill-Installer/1.0"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read().decode("utf-8").strip()
            
        if not data:
            logger.info("No update info available (empty response)")
            return None
        
        # 解析 JSON
        update_info = json.loads(data)
        target_version = update_info.get("target_version")
        
        if target_version:
            logger.info(f"Remote target version: {target_version}")
        else:
            logger.info("No target_version found in update info")
        
        return target_version
        
    except urllib.error.URLError as e:
        logger.warning(f"Failed to fetch update info: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse update info JSON: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error fetching update info: {e}")
        return None


def _needs_upgrade() -> tuple[bool, str | None, str | None]:
    """检查是否需要升级浏览器。
    
    Returns:
        (needs_upgrade, current_version, installed_path)
        - needs_upgrade: True 表示需要升级或安装
        - current_version: 当前版本号
        - installed_path: 已安装的路径
    """
    installed_path = _get_installed_browser_path()
    if installed_path is None:
        # 浏览器未安装 → 触发安装
        return True, None, None
    
    current_version = _get_browser_version(installed_path)
    if current_version is None:
        # 无法获取版本，保守起见不升级
        logger.warning("Cannot determine browser version, skipping upgrade check")
        return False, None, installed_path
    
    # 已安装版本 < 最低版本 → 触发升级
    if _compare_versions(current_version, MIN_VERSION) < 0:
        return True, current_version, installed_path
    
    # 版本 >= 最低版本，检查远程更新信息
    target_version = _fetch_target_version()
    if target_version is None:
        # 无法获取更新信息，不触发安装
        logger.info("No update info available, skipping upgrade")
        return False, current_version, installed_path
    
    # 如果 target_version > 本地版本 → 触发升级
    if _compare_versions(target_version, current_version) > 0:
        _log_and_print(f"⬆️ New version available: {target_version} > {current_version}")
        return True, current_version, installed_path
    
    # 版本已是最新
    logger.info(f"Browser is up to date: {current_version}")
    return False, current_version, installed_path


def needs_upgrade() -> tuple[bool, str | None, str | None]:
    """检查是否需要升级浏览器（公开接口）。

    Returns:
        (needs_upgrade, current_version, installed_path)
        - needs_upgrade: True 表示需要升级或安装
        - current_version: 当前版本号
        - installed_path: 已安装的路径
    """
    return _needs_upgrade()


def is_installed() -> bool:
    """检查 Chrome Browser 是否已安装。

    通过检查注册表、默认安装路径和常见路径来判断。
    """
    return _get_installed_browser_path() is not None


def close_browser_process() -> bool:
    """关闭正在运行的浏览器进程（公开接口）。

    Returns:
        True 表示成功关闭或没有运行中的进程，False 表示关闭失败。
    """
    return _close_browser_process()


# ---------------------------------------------------------------------------
# Install logic
# ---------------------------------------------------------------------------

def _close_browser_process() -> bool:
    """关闭正在运行的 QQ 浏览器进程。
    
    Returns:
        True 表示成功关闭或没有运行中的进程，False 表示关闭失败。
    """
    try:
        # 检查是否有运行中的进程
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Chrome.exe", "/NH"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        
        if "Chrome.exe" not in result.stdout:
            logger.info("No running Chrome Browser process found")
            return True
        
        _log_and_print("🔄 Closing running Chrome Browser process...")
        
        # 直接强制关闭，避免弹窗确认
        subprocess.run(
            ["taskkill", "/F", "/IM", "Chrome.exe"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        
        # 等待进程关闭
        import time
        time.sleep(2)
        
        # 检查是否关闭成功
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Chrome.exe", "/NH"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        
        if "Chrome.exe" not in result.stdout:
            logger.info("Chrome Browser process closed successfully")
            return True
        else:
            logger.warning("Failed to close Chrome Browser process")
            return False
        
    except Exception as e:
        logger.warning(f"Failed to close browser process: {e}")
        return False


def _install_exe(tmp_dir: Path):
    """下载并静默安装 Chrome Browser（Windows）。"""
    pkg_path = tmp_dir / QB_EXE
    url = f"{BASE_URL}/{QB_EXE}"

    _log_and_print(f"📦 Downloading Windows installer: {QB_EXE}")
    _download_with_urllib(url, pkg_path)

    _log_and_print("📦 Installing Chrome Browser (silent mode)...")
    # 使用 /S 进行静默安装（NSIS 标准参数）
    # 不同安装包可能使用不同的静默参数，常见的有 /S、/silent、/quiet
    result = _run(
        [str(pkg_path), "/S"],
        check=False,
    )
    if result.returncode != 0:
        logger.warning("Installer returned code %d, trying alternative silent flags...", result.returncode)
        # 尝试备选静默参数
        _run(
            [str(pkg_path), "/silent", "/norestart"],
            check=False,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install(dry_run: bool = False, log_dir: str | None = None, force: bool = False) -> bool:
    """安装 Chrome Browser（Windows 版）。

    Args:
        dry_run: 如果为 True，仅打印将要执行的操作，不实际安装。
        log_dir: 日志目录。
        force: 如果为 True，即使已安装也强制重新安装。

    Returns:
        True 表示安装后 Chrome Browser 可用。
    """
    from .constants import DEFAULT_LOG_DIR
    if log_dir is None:
        log_dir = DEFAULT_LOG_DIR

    _setup_install_logging(log_dir)

    try:
        return _install_inner(dry_run, force=force)
    finally:
        _teardown_install_logging()


def _install_inner(dry_run: bool, force: bool = False) -> bool:
    """核心安装逻辑。"""
    _check_platform()

    # 检查是否需要安装/升级
    needs_upgrade, current_version, installed_path = _needs_upgrade()
    
    if force:
        if installed_path:
            _log_and_print(f"🔄 Chrome Browser already installed at {installed_path}, force reinstalling...")
        else:
            _log_and_print("🔄 Force installing Chrome Browser...")
    elif not needs_upgrade:
        # 已安装且版本符合要求
        _log_and_print(f"✅ Chrome Browser already installed: {installed_path}")
        _log_and_print(f"   Version: {current_version} (minimum required: {MIN_VERSION})")
        return True
    else:
        # 需要升级
        if current_version:
            _log_and_print(f"⬆️ Chrome Browser version {current_version} is below minimum {MIN_VERSION}, upgrading...")
        else:
            _log_and_print("📦 Chrome Browser not found, installing...")

    # 检查管理员权限
    if not _check_admin_or_skip():
        return False

    if dry_run:
        _log_and_print(f"🔍 [dry-run] Would download: {BASE_URL}/{QB_EXE}")
        _log_and_print("🔍 [dry-run] Would install via: silent exe installer")
        return True

    # 安装前关闭正在运行的浏览器进程
    if not _close_browser_process():
        _log_and_print("⚠️ Failed to close browser, installation may hang or fail", logging.WARNING)

    # 在临时目录中下载并安装
    tmp_dir = Path(tempfile.mkdtemp(prefix="chrome-install-"))
    try:
        _install_exe(tmp_dir)
    except subprocess.CalledProcessError as exc:
        _log_and_print(f"❌ Installation command failed (exit code {exc.returncode}).", logging.ERROR)
        logger.error("Install failed: %s", exc)
        return False
    except Exception as exc:
        _log_and_print(f"❌ Installation failed: {exc}", logging.ERROR)
        logger.error("Install failed: %s", exc)
        return False
    finally:
        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 验证安装结果
    installed_path = _get_installed_browser_path()
    if not installed_path:
        _log_and_print("❌ Chrome Browser installation failed: executable not found.", logging.ERROR)
        _log_and_print("   Please try installing Chrome Browser manually from https://browser.qq.com/", logging.ERROR)
        return False

    # 获取安装后的版本
    new_version = _get_browser_version(installed_path)
    if new_version:
        _log_and_print(f"✅ Chrome Browser installed successfully: {installed_path}")
        _log_and_print(f"   Version: {new_version}")
    else:
        _log_and_print(f"✅ Chrome Browser installed successfully: {installed_path}")
    return True
