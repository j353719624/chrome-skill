"""
Chrome Browser macOS installer.

从官方下载服务器下载 Chrome Browser 的 macOS DMG 安装包，
挂载后将 .app 拷贝到 /Applications 完成安装。
"""

import logging
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .installer_common import (
    _setup_install_logging,
    _teardown_install_logging,
    _log_and_print,
    _run,
    _download,
)
from .constants import BROWSER_EXECUTABLE, BROWSER_PROCESS_NAME

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CDN 下载地址，按 CPU 架构区分
QB_DMG_URL_ARM = "https://pcchannel.imtt.qq.com/channel/Chrome_arm_1005.dmg"
QB_DMG_URL_INTEL = "https://pcchannel.imtt.qq.com/channel/Chrome_intel_1005.dmg"


# 最低要求的浏览器版本
MIN_VERSION = "20.1.6.202"

# Chrome.app 的 bundle 名称
APP_BUNDLE_NAME = "Chrome.app"

# 安装目标目录
INSTALL_DIR = "/Applications"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_all_architectures() -> dict[str, str]:
    """使用所有可用方法检测 CPU 架构，返回各方法的结果。

    Returns:
        字典，key 为方法名，value 为检测到的架构字符串（失败则为错误信息）。
    """
    results: dict[str, str] = {}

    # 方法 1: platform.machine()
    try:
        results["platform.machine()"] = platform.machine()
    except Exception as e:
        results["platform.machine()"] = f"ERROR: {e}"

    # 方法 2: platform.processor()
    try:
        results["platform.processor()"] = platform.processor()
    except Exception as e:
        results["platform.processor()"] = f"ERROR: {e}"

    # 方法 3: os.uname().machine
    try:
        results["os.uname().machine"] = os.uname().machine
    except Exception as e:
        results["os.uname().machine"] = f"ERROR: {e}"

    # 方法 4: sysctl -n hw.machine（真实硬件架构，不受 Rosetta 2 影响）
    try:
        r = subprocess.run(
            ["sysctl", "-n", "hw.machine"],
            capture_output=True, text=True, timeout=5,
        )
        results["sysctl hw.machine"] = r.stdout.strip() if r.returncode == 0 else f"exit={r.returncode}"
    except Exception as e:
        results["sysctl hw.machine"] = f"ERROR: {e}"

    # 方法 5: uname -m
    try:
        r = subprocess.run(
            ["uname", "-m"],
            capture_output=True, text=True, timeout=5,
        )
        results["uname -m"] = r.stdout.strip() if r.returncode == 0 else f"exit={r.returncode}"
    except Exception as e:
        results["uname -m"] = f"ERROR: {e}"

    # 方法 6: arch 命令
    try:
        r = subprocess.run(
            ["arch"],
            capture_output=True, text=True, timeout=5,
        )
        results["arch"] = r.stdout.strip() if r.returncode == 0 else f"exit={r.returncode}"
    except Exception as e:
        results["arch"] = f"ERROR: {e}"

    # 方法 7: sysctl hw.optional.arm64（直接查询是否支持 arm64）
    try:
        r = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            capture_output=True, text=True, timeout=5,
        )
        val = r.stdout.strip() if r.returncode == 0 else f"exit={r.returncode}"
        results["sysctl hw.optional.arm64"] = val  # 1=ARM, 0=Intel, 不存在=Intel
    except Exception as e:
        results["sysctl hw.optional.arm64"] = f"ERROR: {e}"

    return results


def _is_arm_cpu() -> bool:
    """检测当前 Mac 是否为 ARM 架构（Apple Silicon）。

    使用所有方法检测并输出日志以便对比，
    最终以 sysctl hw.machine 为准，获取不到则降级使用 platform.machine()。

    Returns:
        True 表示 ARM（Apple Silicon），False 表示 Intel x86_64。
    """
    # 执行所有检测方法并输出日志
    all_results = _detect_all_architectures()
    _log_and_print("🔍 CPU architecture detection results:")
    for method, value in all_results.items():
        _log_and_print(f"   {method:<30s} => {value}")

    # 优先方案：sysctl hw.optional.arm64（最可靠，Rosetta 2 下仍返回真实值）
    arm64_val = all_results.get("sysctl hw.optional.arm64", "")
    if arm64_val and not arm64_val.startswith(("ERROR", "exit")):
        is_arm = arm64_val.strip() == "1"
        _log_and_print(f"   ✅ Using sysctl hw.optional.arm64 => {arm64_val} (is_arm={is_arm})")
        return is_arm

    # 降级方案 1：sysctl hw.machine
    sysctl_val = all_results.get("sysctl hw.machine", "")
    if sysctl_val and not sysctl_val.startswith(("ERROR", "exit")):
        is_arm = sysctl_val.lower() in ("arm64", "aarch64")
        _log_and_print(f"   ⚠️ Fallback to sysctl hw.machine => {sysctl_val} (is_arm={is_arm})")
        return is_arm

    # 降级方案 2：platform.machine()
    pm_val = all_results.get("platform.machine()", "")
    if pm_val and not pm_val.startswith("ERROR"):
        is_arm = pm_val.lower() in ("arm64", "aarch64")
        _log_and_print(f"   ⚠️ Fallback to platform.machine() => {pm_val} (is_arm={is_arm})")
        return is_arm

    _log_and_print("   ❌ All detection methods failed, defaulting to Intel", logging.WARNING)
    return False


def _get_download_url() -> tuple[str, str]:
    """根据 CPU 架构返回对应的下载 URL 和文件名。

    Returns:
        (url, dmg_filename)
    """
    if _is_arm_cpu():
        url = QB_DMG_URL_ARM
    else:
        url = QB_DMG_URL_INTEL
    # 从 URL 中提取文件名
    dmg_filename = url.rsplit("/", 1)[-1]
    return url, dmg_filename

def _check_platform():
    """确保当前运行在 macOS 平台上。"""
    if sys.platform != "darwin":
        _log_and_print(
            f"❌ Unsupported platform: {sys.platform}. This installer is for macOS only.",
            logging.ERROR,
        )
        sys.exit(1)


def _check_write_permission() -> bool:
    """检查是否有写入 /Applications 的权限。

    Returns:
        True 表示有写权限，False 表示没有。
    """
    if os.access(INSTALL_DIR, os.W_OK):
        return True

    _log_and_print(
        "❌ No write permission to /Applications. Please run with sudo.",
        logging.ERROR,
    )
    return False


def _get_app_bundle_path(browser_path: str) -> str | None:
    """从浏览器可执行文件路径推导出 .app 包路径。

    Args:
        browser_path: 浏览器可执行文件路径，如
            /Applications/Chrome.app/Contents/MacOS/Chrome

    Returns:
        .app 包路径，如 /Applications/Chrome.app，无法推导返回 None。
    """
    # 向上查找 .app 目录
    p = Path(browser_path)
    for parent in [p] + list(p.parents):
        if parent.suffix == ".app" and parent.is_dir():
            return str(parent)
    return None


def _get_installed_browser_path() -> str | None:
    """获取已安装的 QQ 浏览器路径。

    按优先级依次尝试：
    1. 检查默认可执行文件路径（/Applications/Chrome.app/...）
    2. 检查用户 Applications 目录
    3. 使用 Spotlight (mdfind) 搜索
    4. 在 PATH 中查找

    Returns:
        浏览器可执行文件路径，未找到返回 None。
    """
    # 方式 1：检查默认可执行文件路径
    if os.path.isfile(BROWSER_EXECUTABLE):
        return BROWSER_EXECUTABLE

    # 方式 2：检查用户 Applications 目录
    user_app = os.path.expanduser(f"~/Applications/{APP_BUNDLE_NAME}/Contents/MacOS/Chrome")
    if os.path.isfile(user_app):
        return user_app

    # 方式 3：使用 Spotlight 搜索
    try:
        result = subprocess.run(
            ["mdfind", "kMDItemCFBundleIdentifier == 'com.tencent.Chrome'"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                app_path = line.strip()
                exe = os.path.join(app_path, "Contents", "MacOS", "Chrome")
                if os.path.isfile(exe):
                    return exe
    except Exception as e:
        _log_and_print(f"Spotlight search failed: {e}", logging.WARNING)

    # 方式 4：在 PATH 中查找
    path_result = shutil.which("Chrome")
    if path_result is not None:
        return path_result

    return None


def _get_browser_version(browser_path: str) -> str | None:
    """获取 QQ 浏览器版本号（macOS 平台）。

    从 .app 包的 Info.plist 读取 CFBundleShortVersionString。

    Args:
        browser_path: 浏览器可执行文件路径。

    Returns:
        版本号字符串，获取失败返回 None。
    """
    try:
        app_bundle = _get_app_bundle_path(browser_path)
        if app_bundle is None:
            _log_and_print(f"Cannot determine .app bundle path from: {browser_path}", logging.WARNING)
            return None

        plist_path = os.path.join(app_bundle, "Contents", "Info.plist")
        if not os.path.isfile(plist_path):
            _log_and_print(f"Info.plist not found: {plist_path}", logging.WARNING)
            return None

        with open(plist_path, "rb") as f:
            plist_data = plistlib.load(f)

        version = plist_data.get("CFBundleShortVersionString")
        if version:
            _log_and_print(f"Browser version from Info.plist: {version}")
            return version

        # 回退到 CFBundleVersion
        version = plist_data.get("CFBundleVersion")
        if version:
            _log_and_print(f"Browser version from CFBundleVersion: {version}")
            return version

        _log_and_print("No version info found in Info.plist", logging.WARNING)
        return None
    except Exception as e:
        _log_and_print(f"Failed to get browser version: {e}", logging.WARNING)
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
        _log_and_print(f"Cannot determine browser version, skipping upgrade check", logging.WARNING)
        return False, None, installed_path

    # 已安装版本 < 最低版本 → 触发升级
    if _compare_versions(current_version, MIN_VERSION) < 0:
        return True, current_version, installed_path

    # 版本已是最新
    _log_and_print(f"Browser is up to date: {current_version}")
    return False, current_version, installed_path


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------

def needs_upgrade() -> tuple[bool, str | None, str | None]:
    """检查是否需要升级浏览器（公开接口）。

    Returns:
        (needs_upgrade, current_version, installed_path)
    """
    return _needs_upgrade()


def is_installed() -> bool:
    """检查 Chrome Browser 是否已安装。"""
    return _get_installed_browser_path() is not None


def close_browser_process() -> bool:
    """关闭正在运行的浏览器进程（公开接口）。

    Returns:
        True 表示成功关闭或没有运行中的进程，False 表示关闭失败。
    """
    return _close_browser_process()


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def _close_browser_process() -> bool:
    """关闭正在运行的 QQ 浏览器进程。

    Returns:
        True 表示成功关闭或没有运行中的进程，False 表示关闭失败。
    """
    import time

    try:
        # 检查是否有运行中的进程
        result = subprocess.run(
            ["pgrep", "-x", BROWSER_PROCESS_NAME],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            _log_and_print("No running Chrome Browser process found")
            return True

        _log_and_print("🔄 Closing running Chrome Browser process...")

        # 先尝试优雅关闭（SIGTERM）
        subprocess.run(
            ["pkill", "-x", BROWSER_PROCESS_NAME],
            capture_output=True,
            text=True,
        )

        # 等待进程关闭
        time.sleep(3)

        # 检查是否关闭成功
        result = subprocess.run(
            ["pgrep", "-x", BROWSER_PROCESS_NAME],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            _log_and_print("Chrome Browser process closed successfully")
            return True

        # 进程仍在运行，强制关闭（SIGKILL）
        _log_and_print("⚠️ Browser still running, force killing...")
        subprocess.run(
            ["pkill", "-9", "-x", BROWSER_PROCESS_NAME],
            capture_output=True,
            text=True,
        )

        time.sleep(2)

        # 再次检查
        result = subprocess.run(
            ["pgrep", "-x", BROWSER_PROCESS_NAME],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            _log_and_print("Chrome Browser process force killed successfully")
            return True
        else:
            _log_and_print("Failed to close Chrome Browser process", logging.WARNING)
            return False

    except Exception as e:
        _log_and_print(f"Failed to close browser process: {e}", logging.WARNING)
        return False


# ---------------------------------------------------------------------------
# Install logic
# ---------------------------------------------------------------------------

def _install_dmg(tmp_dir: Path):
    """下载并通过 DMG 安装 Chrome Browser（macOS）。

    流程：download .dmg -> hdiutil attach -> cp -R .app -> hdiutil detach
    根据 CPU 架构自动选择 ARM 或 Intel 版本。
    """
    url, dmg_filename = _get_download_url()
    pkg_path = tmp_dir / dmg_filename

    arch_label = "ARM (Apple Silicon)" if _is_arm_cpu() else "Intel (x86_64)"
    _log_and_print(f"📦 Detected CPU architecture: {arch_label}")
    _log_and_print(f"📦 Downloading macOS installer: {dmg_filename}")
    _download(url, pkg_path)

    mount_point = None
    try:
        # 挂载 DMG（使用 -plist 获取结构化输出）
        _log_and_print("📦 Mounting DMG image...")
        result = _run(
            ["hdiutil", "attach", str(pkg_path), "-nobrowse", "-noverify", "-plist"],
            check=True,
        )

        # 解析 plist 输出获取挂载点
        mount_point = _parse_mount_point(result.stdout)
        if mount_point is None:
            raise RuntimeError("Failed to determine DMG mount point from hdiutil output")

        _log_and_print(f"📂 DMG mounted at: {mount_point}")

        # 在挂载点中查找 .app 包
        app_source = _find_app_in_mount(mount_point)
        if app_source is None:
            raise RuntimeError(
                f"No .app bundle found in mounted DMG at {mount_point}"
            )

        _log_and_print(f"📦 Found app bundle: {os.path.basename(app_source)}")

        # 拷贝 .app 到 /Applications
        dest = os.path.join(INSTALL_DIR, os.path.basename(app_source))

        # 如果已存在旧版本，先移除
        if os.path.exists(dest):
            _log_and_print(f"🔄 Removing existing installation: {dest}")
            shutil.rmtree(dest)

        _log_and_print(f"📦 Installing to {dest}...")
        _run(["cp", "-R", app_source, dest], check=True)

    finally:
        # 确保卸载 DMG
        if mount_point is not None:
            _log_and_print("📂 Unmounting DMG...")
            try:
                _run(["hdiutil", "detach", mount_point, "-force"], check=False)
            except Exception as e:
                _log_and_print(f"Failed to detach DMG: {e}", logging.WARNING)


def _parse_mount_point(plist_stdout: str) -> str | None:
    """从 hdiutil attach -plist 的输出中解析挂载点路径。

    Args:
        plist_stdout: hdiutil 的 plist 格式标准输出。

    Returns:
        挂载点路径字符串，解析失败返回 None。
    """
    try:
        plist_data = plistlib.loads(plist_stdout.encode("utf-8"))
        entities = plist_data.get("system-entities", [])
        for entity in entities:
            mp = entity.get("mount-point")
            if mp:
                return mp
    except Exception as e:
        _log_and_print(f"Failed to parse hdiutil plist output: {e}", logging.WARNING)

    # 回退方案：逐行解析文本输出查找 /Volumes/ 路径
    for line in plist_stdout.splitlines():
        line = line.strip()
        if "/Volumes/" in line:
            # 提取 /Volumes/... 路径
            idx = line.find("/Volumes/")
            if idx >= 0:
                return line[idx:].strip()

    return None


def _find_app_in_mount(mount_point: str) -> str | None:
    """在 DMG 挂载点中查找 .app 包。

    优先查找名称中包含 Chrome 的 .app，否则返回第一个 .app。

    Args:
        mount_point: DMG 挂载点路径。

    Returns:
        .app 包的完整路径，未找到返回 None。
    """
    try:
        entries = os.listdir(mount_point)
    except Exception as e:
        _log_and_print(f"Failed to list mount point: {e}", logging.WARNING)
        return None

    app_bundles = [e for e in entries if e.endswith(".app")]

    if not app_bundles:
        return None

    # 优先匹配 Chrome
    for app in app_bundles:
        if "Chrome" in app or "chrome" in app.lower():
            return os.path.join(mount_point, app)

    # 返回第一个 .app
    return os.path.join(mount_point, app_bundles[0])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install(dry_run: bool = False, log_dir: str | None = None, force: bool = False) -> bool:
    """安装 Chrome Browser（macOS 版）。

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
    upgrade_needed, current_version, installed_path = _needs_upgrade()

    if force:
        if installed_path:
            _log_and_print(
                f"🔄 Chrome Browser already installed at {installed_path}, force reinstalling..."
            )
        else:
            _log_and_print("🔄 Force installing Chrome Browser...")
    elif not upgrade_needed:
        # 已安装且版本符合要求
        _log_and_print(f"✅ Chrome Browser already installed: {installed_path}")
        _log_and_print(f"   Version: {current_version} (minimum required: {MIN_VERSION})")
        return True
    else:
        # 需要升级
        if current_version:
            _log_and_print(
                f"⬆️ Chrome Browser version {current_version} is below minimum {MIN_VERSION}, upgrading..."
            )
        else:
            _log_and_print("📦 Chrome Browser not found, installing...")

    # 检查写权限
    if not _check_write_permission():
        return False

    if dry_run:
        url, dmg_filename = _get_download_url()
        arch_label = "ARM (Apple Silicon)" if _is_arm_cpu() else "Intel (x86_64)"
        _log_and_print(f"🔍 [dry-run] CPU architecture: {arch_label}")
        _log_and_print(f"🔍 [dry-run] Would download: {url}")
        _log_and_print("🔍 [dry-run] Would install via: hdiutil attach + cp -R to /Applications")
        return True

    # 安装前关闭正在运行的浏览器进程
    if not _close_browser_process():
        _log_and_print(
            "⚠️ Failed to close browser, installation may fail",
            logging.WARNING,
        )

    # 在临时目录中下载并安装
    tmp_dir = Path(tempfile.mkdtemp(prefix="chrome-install-"))
    try:
        _install_dmg(tmp_dir)
    except subprocess.CalledProcessError as exc:
        _log_and_print(
            f"❌ Installation command failed (exit code {exc.returncode}).",
            logging.ERROR,
        )
        _log_and_print(f"Install failed: {exc}", logging.ERROR)
        return False
    except Exception as exc:
        _log_and_print(f"❌ Installation failed: {exc}", logging.ERROR)
        _log_and_print(f"Install failed: {exc}", logging.ERROR)
        return False
    finally:
        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 验证安装结果
    installed_path = _get_installed_browser_path()
    if not installed_path:
        _log_and_print(
            "❌ Chrome Browser installation failed: executable not found.",
            logging.ERROR,
        )
        _log_and_print(
            "   Please try installing Chrome Browser manually from https://browser.qq.com/",
            logging.ERROR,
        )
        return False

    # 获取安装后的版本
    new_version = _get_browser_version(installed_path)
    if new_version:
        _log_and_print(f"✅ Chrome Browser installed successfully: {installed_path}")
        _log_and_print(f"   Version: {new_version}")
    else:
        _log_and_print(f"✅ Chrome Browser installed successfully: {installed_path}")
    return True
