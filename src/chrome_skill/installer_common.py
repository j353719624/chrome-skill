"""
Chrome Browser installer 公共工具函数。

提供日志管理、命令执行、文件下载等各平台安装脚本共享的功能。
"""

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from .constants import DEFAULT_LOG_DIR

INSTALL_LOG_FILE = "install.log"

# 模块级文件处理器引用，用于清理
_install_file_handler: logging.FileHandler | None = None


def _setup_install_logging(log_dir: str = DEFAULT_LOG_DIR):
    """添加专用的文件处理器，将安装日志写入 *log_dir*/install.log。

    该处理器附加到模块级 logger，以便在安装过程中产生的所有消息
    （包括子进程输出）都持久化到磁盘。
    """
    global _install_file_handler

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, INSTALL_LOG_FILE)
        _install_file_handler = logging.FileHandler(log_path, encoding="utf-8")
        _install_file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        # 附加到根 logger，使所有子 logger 都能受益
        logging.getLogger().addHandler(_install_file_handler)
        logger.info("Install log file: %s", log_path)
    except Exception as exc:
        # 非致命错误：输出到 stderr 并继续
        print(f"⚠️  Could not create install log file in {log_dir}: {exc}", file=sys.stderr)


def _teardown_install_logging():
    """移除安装专用的文件处理器（如果存在）。"""
    global _install_file_handler
    if _install_file_handler is not None:
        _install_file_handler.flush()
        _install_file_handler.close()
        logging.getLogger().removeHandler(_install_file_handler)
        _install_file_handler = None


def _log_and_print(msg: str, level: int = logging.INFO):
    """将 *msg* 同时打印到 stderr 并写入安装日志。

    输出到 stderr 而非 stdout，确保 stdout 只包含 [RESULT] 结果，
    避免 MCP server 通过 subprocess 调用时日志混入工具执行结果。
    """
    print(msg, file=sys.stderr)
    logger.log(level, msg)


def _run(cmd: list[str], *, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """运行命令，记录日志，并将 stdout/stderr 捕获到安装日志中。"""
    logger.info("Running: %s", " ".join(cmd))
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    result = subprocess.run(cmd, check=check, **kwargs)
    if result.stdout:
        logger.info("[stdout] %s", result.stdout.rstrip())
    if result.stderr:
        logger.info("[stderr] %s", result.stderr.rstrip())
    return result


def _download(url: str, dest: Path):
    """使用 curl（优先）或 urllib 下载文件。"""
    curl = shutil.which("curl")
    if curl:
        _run(["curl", "-fSL", "-o", str(dest), url])
    else:
        # 回退到 Python 标准库
        import urllib.request
        logger.info("Downloading %s -> %s", url, dest)
        urllib.request.urlretrieve(url, str(dest))


def _download_with_urllib(url: str, dest: Path):
    """使用 Python 标准库 urllib 下载文件（跨平台兼容），带进度显示。"""
    import urllib.request
    import urllib.error

    def _report_progress(block_num: int, block_size: int, total_size: int):
        """下载进度回调函数。"""
        if total_size <= 0:
            return
        downloaded = block_num * block_size
        # 避免超出 total_size
        if downloaded > total_size:
            downloaded = total_size
        percent = min(100, downloaded * 100 // total_size)
        downloaded_mb = downloaded / (1024 * 1024)
        total_mb = total_size / (1024 * 1024)
        # 使用 \r 回到行首进行更新，动态显示进度
        sys.stderr.write(f"\r⬇️  Downloading: {percent}% ({downloaded_mb:.1f}MB / {total_mb:.1f}MB)")
        sys.stderr.flush()
        if downloaded >= total_size:
            sys.stderr.write("\n")

    logger.info("Downloading %s -> %s", url, dest)
    _log_and_print(f"⬇️  Downloading: {url}")
    try:
        urllib.request.urlretrieve(url, str(dest), _report_progress)
    except urllib.error.URLError as e:
        _log_and_print(f"❌ Download failed: {e}", logging.ERROR)
        raise
    _log_and_print(f"✅ Downloaded to: {dest}")
