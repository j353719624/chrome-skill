"""
Chrome Browser Linux installer.

Detects the Linux distribution and installs the Chrome Browser package
from the official download server. Supports Debian/Ubuntu (.deb) and
RPM-based distributions (.rpm) such as CentOS, RHEL, Fedora, openSUSE, etc.
"""

import logging
import os
import platform
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
    _download,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://dldir1v6.qq.com/invc/tt/QB/Public/ubuntu_qb"
QB_DEB = "chrome-browser-stable-19.1.1.103-0318.deb"
QB_RPM = "chrome-browser-stable-19.1.1.103-0318.rpm"
QB_BINARY = "chrome-browser-stable"

# Linux 发行版分类
_DEB_DISTROS = {"ubuntu", "debian", "linuxmint", "pop", "elementary", "zorin", "kali", "deepin"}
_RPM_DISTROS = {"centos", "rhel", "fedora", "rocky", "almalinux", "ol", "amzn", "opensuse",
                "sles", "openeuler", "anolis", "tencentos", "kylin"}


# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

def detect_distro() -> dict:
    """Detect the current Linux distribution.

    Returns a dict with keys:
        id      – lowercase distro ID (e.g. "ubuntu", "centos")
        id_like – space-separated list of parent distro IDs
        name    – human-readable distro name
        family  – "deb" | "rpm" | "unknown"
    """
    info = {"id": "unknown", "id_like": "", "name": "Unknown Linux", "family": "unknown"}

    os_release = Path("/etc/os-release")
    if not os_release.exists():
        logger.warning("/etc/os-release not found, cannot detect distro")
        return info

    data = {}
    for line in os_release.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"')

    info["id"] = data.get("ID", "unknown").lower()
    info["id_like"] = data.get("ID_LIKE", "").lower()
    info["name"] = data.get("PRETTY_NAME", data.get("NAME", "Unknown Linux"))

    # Determine package family
    all_ids = {info["id"]} | set(info["id_like"].split())
    if all_ids & _DEB_DISTROS:
        info["family"] = "deb"
    elif all_ids & _RPM_DISTROS:
        info["family"] = "rpm"
    else:
        # Fallback heuristic: check for dpkg or rpm binary
        if shutil.which("dpkg"):
            info["family"] = "deb"
        elif shutil.which("rpm"):
            info["family"] = "rpm"

    return info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_platform():
    """确保当前运行在 Linux x86_64 平台上。"""
    if sys.platform != "linux":
        _log_and_print(f"❌ Unsupported platform: {sys.platform}. This installer is for Linux only.", logging.ERROR)
        sys.exit(1)

    arch = platform.machine()
    if arch not in ("x86_64", "amd64"):
        _log_and_print(f"❌ Unsupported architecture: {arch}. Chrome Browser requires x86_64/amd64.", logging.ERROR)
        sys.exit(1)


def is_installed() -> bool:
    """检查 Chrome Browser 是否已安装。"""
    return shutil.which(QB_BINARY) is not None


# ---------------------------------------------------------------------------
# Install logic per family
# ---------------------------------------------------------------------------

def _install_deb(tmp_dir: Path):
    """通过 .deb 包安装 Chrome Browser。"""
    pkg_path = tmp_dir / QB_DEB
    url = f"{BASE_URL}/{QB_DEB}"

    _log_and_print(f"📦 Downloading .deb package: {QB_DEB}")
    _download(url, pkg_path)

    _log_and_print("📦 Installing .deb package (force mode)...")
    result = _run(["dpkg", "--force-all", "-i", str(pkg_path)], check=False)
    if result.returncode != 0:
        # 尝试修复依赖
        logger.info("dpkg reported errors, attempting apt-get install -f")
        _run(["apt-get", "install", "-f", "-y"])


def _install_rpm(tmp_dir: Path):
    """通过 .rpm 包安装 Chrome Browser。"""
    pkg_path = tmp_dir / QB_RPM
    url = f"{BASE_URL}/{QB_RPM}"

    _log_and_print(f"📦 Downloading .rpm package: {QB_RPM}")
    _download(url, pkg_path)

    _log_and_print("📦 Installing .rpm package (force mode)...")
    # 优先使用 yum，其次 dnf，最后 rpm
    if shutil.which("yum"):
        _run(["yum", "install", "-y", "--nogpgcheck", str(pkg_path)])
    elif shutil.which("dnf"):
        _run(["dnf", "install", "-y", "--nogpgcheck", str(pkg_path)])
    else:
        _run(["rpm", "-ivh", "--force", "--nodeps", str(pkg_path)])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install(dry_run: bool = False, log_dir: str | None = None, force: bool = False) -> bool:
    """安装 Chrome Browser（Linux 版）。

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

    # 检查是否已安装（force 模式下跳过检查，强制重装）
    if not force and is_installed():
        path = shutil.which(QB_BINARY)
        _log_and_print(f"✅ Chrome Browser already installed: {path}, skipping...")
        return True

    if force and is_installed():
        path = shutil.which(QB_BINARY)
        _log_and_print(f"🔄 Chrome Browser already installed: {path}, force reinstalling...")

    distro = detect_distro()
    _log_and_print(f"🔍 Detected OS: {distro['name']} (family={distro['family']})")

    if distro["family"] == "unknown":
        _log_and_print("❌ Cannot determine package format (deb/rpm) for this distribution.", logging.ERROR)
        _log_and_print("   Please install Chrome Browser manually.", logging.ERROR)
        return False

    if dry_run:
        pkg = QB_DEB if distro["family"] == "deb" else QB_RPM
        _log_and_print(f"🔍 [dry-run] Would download: {BASE_URL}/{pkg}")
        _log_and_print(f"🔍 [dry-run] Would install via: {'dpkg' if distro['family'] == 'deb' else 'yum/dnf/rpm'}")
        return True

    # 确保 sbin 目录在 PATH 中（dpkg、ldconfig 等需要）
    sbin_dirs = "/usr/local/sbin:/usr/sbin:/sbin"
    current_path = os.environ.get("PATH", "")
    if "/sbin" not in current_path:
        os.environ["PATH"] = f"{sbin_dirs}:{current_path}"

    # 在临时目录中下载并安装
    tmp_dir = Path(tempfile.mkdtemp(prefix="chrome-install-"))
    try:
        if distro["family"] == "deb":
            _install_deb(tmp_dir)
        else:
            _install_rpm(tmp_dir)
    except subprocess.CalledProcessError as exc:
        _log_and_print(f"❌ Installation command failed (exit code {exc.returncode}).", logging.ERROR)
        logger.error("Install failed: %s", exc)
        return False
    finally:
        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 验证安装结果
    if not is_installed():
        _log_and_print(f"❌ Chrome Browser installation failed: '{QB_BINARY}' not found in PATH.", logging.ERROR)
        return False

    path = shutil.which(QB_BINARY)
    _log_and_print(f"✅ Chrome Browser installed successfully: {path}")
    return True
