import subprocess
import socket
import os
import socket
import sys
import time
import logging

from .constants import DEFAULT_LOG_DIR, BROWSER_EXECUTABLE, BROWSER_PROCESS_NAME

logger = logging.getLogger(__name__)


def _subprocess_no_window_flags() -> dict:
    """获取 Windows 平台下抑制控制台窗口弹出的 subprocess 参数。

    后台 daemon 模式下没有控制台，如果不设置 CREATE_NO_WINDOW，
    Windows 会为控制台程序（cmd.exe、tasklist、powershell 等）自动创建新的控制台窗口。
    """
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        return {"creationflags": CREATE_NO_WINDOW}
    return {}

# 浏览器日志目录，可通过 set_browser_log_dir() 动态设置
_browser_log_dir = DEFAULT_LOG_DIR

def set_browser_log_dir(log_dir: str):
    """Set the directory for browser log file."""
    global _browser_log_dir
    _browser_log_dir = log_dir

def get_local_ip():
    return "127.0.0.1"

def run_shell(cmd, env=None, need_sudo=False):
    if sys.platform == "win32":
        # Windows 不支持 sudo
        actual_cmd = cmd
    else:
        actual_cmd = f"sudo bash -c {repr(cmd)}" if need_sudo else cmd
    logger.info(f"\n>>> 执行命令: {actual_cmd}")
    try:
        result = subprocess.run(
            actual_cmd, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
            **_subprocess_no_window_flags()
        )
        logger.info(result.stdout.rstrip())
        if result.stderr:
            logger.info(result.stderr.rstrip())
        if result.returncode != 0:
            logger.info(f"命令执行失败，返回码: {result.returncode}")
        return result.returncode
    except Exception as e:
        logger.info(f"命令执行异常: {e}")
        return -1
    
def run_shell_async(cmd, need_sudo=False):
    if sys.platform == "win32":
        # Windows 不支持 sudo
        async_cmd = cmd
    else:
        async_cmd = f"sudo bash -c {repr(cmd + ' &')}" if need_sudo else cmd
    logger.info(f"\n>>> 异步执行命令: {async_cmd}")
    try:
        proc = subprocess.Popen(
            async_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            **_subprocess_no_window_flags()
        )
        import threading
        def print_output(p):
            for line in p.stdout:
                logger.info(line.rstrip())

        threading.Thread(target=print_output, args=(proc,), daemon=True).start()
        logger.info(f"异步命令执行成功 {async_cmd}")
        return proc
    except Exception as e:
        logger.info(f"异步命令执行异常: {e}")
        return None


def check_chrome_alive():
    """
    检查chrome进程是否存在，存在返回True，不存在返回False。
    仅用于 Linux 平台。
    """
    try:
        result = subprocess.run(['pgrep', '-x', 'chrome'], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


def _check_chrome_alive_mac(check_launch_args: bool = True):
    """
    检查 QQ 浏览器进程状态（macOS 平台）。

    参数:
        check_launch_args: 是否检查启动参数。为 True 时检查 --chrome-skill-automatic-in-tab 参数；
                          为 False 时只检查是否有进程存在。

    返回:
        bool: 如果 check_launch_args=True，检查是否有带 --chrome-skill-automatic-in-tab 参数的进程；
              如果 check_launch_args=False，只检查是否有进程存在。
    """
    logger.info(f"检查浏览器进程状态: {BROWSER_PROCESS_NAME}, check_launch_args={check_launch_args}")

    try:
        if not check_launch_args:
            # 只检查进程是否存在
            result = subprocess.run(
                ["pgrep", "-x", BROWSER_PROCESS_NAME],
                capture_output=True, text=True,
            )
            exists = result.returncode == 0
            logger.info(f"pgrep 检查结果: {'存在进程' if exists else '不存在进程'}")
            return exists

        # 检查启动参数：先确认主进程是否存在
        result = subprocess.run(
            ["pgrep", "-x", BROWSER_PROCESS_NAME],
            capture_output=True, text=True,
        )

        if result.returncode != 0 or not result.stdout.strip():
            logger.info("未找到浏览器进程")
            return False

        # 用 pgrep -f 按完整命令行匹配启动参数，规避 macOS TCC/沙盒下 ps 报
        # [Errno 1] Operation not permitted 的问题。
        # `--` 是 argv 分隔符，确保后面以 -- 开头的 pattern 不被当成 pgrep 选项解析。
        # 注意：Chromium 系浏览器会把父进程启动参数传给 Renderer/GPU/Utility 等 Helper
        # 子进程，只要任一进程命令行含该参数，即说明浏览器是以正确参数启动的。
        flag_result = subprocess.run(
            ["pgrep", "-f", "--", "--chrome-skill-automatic-in-tab"],
            capture_output=True, text=True,
        )
        has_flag = flag_result.returncode == 0 and bool(flag_result.stdout.strip())

        main_pids = result.stdout.strip().replace("\n", ",")
        flag_pids = flag_result.stdout.strip().replace("\n", ",") if has_flag else ""
        logger.info(f"Chrome 主进程 PID: {main_pids}")
        logger.info(f"带 --chrome-skill-automatic-in-tab 的进程 PID: {flag_pids or '(无)'}")
        logger.info(f"检测到 --chrome-skill-automatic-in-tab 参数: {has_flag}")

        if not has_flag:
            # 没有正确的启动参数，杀掉当前浏览器进程
            logger.info("浏览器启动参数不正确，关闭当前进程")
            subprocess.run(
                ["pkill", "-x", BROWSER_PROCESS_NAME],
                capture_output=True, text=True,
            )
        return has_flag

    except Exception as e:
        logger.error(f"检查浏览器进程状态异常: {e}")
        return False



def _get_browser_path_from_registry_win():
    """从 Windows 注册表获取 QQ 浏览器安装路径。
    
    Returns:
        浏览器可执行文件路径，未找到返回 None。
    """
    import winreg
    
    registry_keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Chrome\CurrentVersion\App Paths\Chrome.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Chrome"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Chrome"),
    ]
    
    for hkey, subkey in registry_keys:
        try:
            with winreg.OpenKey(hkey, subkey) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, "")
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
        except (FileNotFoundError, OSError):
            continue
    
    return None


def _get_browser_path_win():
    """获取 Windows 上 QQ 浏览器的实际安装路径。
    
    按优先级依次尝试：
    1. 从注册表查询（支持自定义安装路径如 E:\Chrome）
    2. 检查常见安装路径
    
    Returns:
        浏览器可执行文件路径。
    """
    # 方式 1：从注册表获取（优先）
    reg_path = _get_browser_path_from_registry_win()
    if reg_path:
        return reg_path
    
    # 方式 2：检查常见安装路径
    common_paths = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Chrome", "Chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Chrome", "Chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Chrome", "Chrome.exe"),
        r"C:\Program Files\Tencent\Chrome\Chrome.exe",
        r"C:\Program Files (x86)\Tencent\Chrome\Chrome.exe",
    ]
    
    for path in common_paths:
        if path and os.path.isfile(path):
            return path
    
    # 如果都没找到，返回默认路径
    return BROWSER_EXECUTABLE


def _check_chrome_alive_win(check_launch_args: bool = True):
    """
    检查 QQ 浏览器进程状态。
    
    参数:
        check_launch_args: 是否检查启动参数。为 True 时检查 --chrome-skill-automatic-in-tab 参数；
                          为 False 时只检查是否有进程存在。
    
    返回:
        bool: 如果 check_launch_args=True，检查是否有带 --chrome-skill-automatic-in-tab 参数的可见窗口进程；
              如果 check_launch_args=False，只检查是否有进程存在。
    """
    browser_path = _get_browser_path_win()
    browser_name = os.path.basename(browser_path)
    logger.info(f"检查浏览器进程状态: {browser_name}, check_launch_args={check_launch_args}")
    
    try:
        if not check_launch_args:
            # 只检查进程是否存在
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {browser_name}", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                **_subprocess_no_window_flags()
            )

            exists = browser_name.lower() in result.stdout.lower()
            logger.info(f"tasklist 检查结果: {'存在进程' if exists else '不存在进程'}")
            return exists
        
        # 检查启动参数：获取有可见窗口的主进程的命令行参数
        ps_script = f'''
        Get-WmiObject -Query "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='{browser_name}'" | Where-Object {{
            (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue).MainWindowTitle
        }} | Select-Object -First 1 -ExpandProperty CommandLine
        '''
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
            **_subprocess_no_window_flags()
        )
        
        command_line = result.stdout.strip()
        if command_line:
            # 分析参数，检查是否包含 --chrome-skill-automatic-in-tab
            has_flag = "--chrome-skill-automatic-in-tab" in command_line
            logger.info(f"浏览器命令行参数: {command_line[:200]}...")
            logger.info(f"检测到 --chrome-skill-automatic-in-tab 参数: {has_flag}")
            if not has_flag:
                # 没有正确的启动参数，杀掉当前浏览器进程
                logger.info("浏览器启动参数不正确，关闭当前进程")
                subprocess.run(
                    ["taskkill", "/F", "/IM", browser_name],
                    capture_output=True, text=True,
                    **_subprocess_no_window_flags()
                )
            return has_flag
        
        # 没有获取到可见窗口进程的参数
        logger.info("未获取到可见窗口进程的命令行参数")
        return False
        
    except Exception as e:
        logger.error(f"检查浏览器进程状态异常: {e}")
        return False

def start_chrome_fullscreen(url=None):
    """启动QQ浏览器全屏模式，仅用于 Linux 平台。"""
    try:
        # 设置默认URL
        url = "https://www.qq.com"
        logger.info(f"启动QQ浏览器，访问URL: {url}")

        log_file = os.path.join(_browser_log_dir, "qb_log.log")

        # 构建QQ浏览器启动命令（Linux）— 使用参数列表避免 shell 注入
        browser_args = [
            "chrome-browser-stable",
            "--headless=new",
            "--display-invisible-extension=true",
            "--disable-infobars",
            "--start-maximized",
            "--chrome-skill-automatic-in-tab",
            "--no-first-run",
            "--no-crashed-bubble-view",
            "--no-modal-dialogs",
            "--no-external-protocol-dialogs",
            "--no-sandbox",
            "about:blank",
        ]

        logger.info(f"执行QQ浏览器启动命令: {browser_args}")
        try:
            log_f = open(log_file, "a")
            proc = subprocess.Popen(
                browser_args,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                **_subprocess_no_window_flags()
            )
        except Exception as e:
            logger.info(f"QQ浏览器启动失败: {e}")
            return False

        if proc is None:
            logger.info("QQ浏览器启动失败")
            return False

        # 等待一小段时间检查浏览器进程
        time.sleep(2)
        check_result = subprocess.run(
            ["pgrep", "-f", "chrome"],
            capture_output=True, text=True,
            **_subprocess_no_window_flags()
        )

        if check_result.returncode == 0:
            logger.info("QQ浏览器已成功启动")
            return True
        else:
            logger.info("警告: QQ浏览器可能未成功启动")
            return False

    except Exception as e:
        logger.info(f"启动QQ浏览器时发生错误: {str(e)}")
        return False


def _start_chrome_mac(url=None):
    """启动 QQ 浏览器全屏模式（macOS 平台）。"""
    try:

        logger.info(f"启动QQ浏览器（macOS），访问URL: chrome://newtab")

        log_file = os.path.join(_browser_log_dir, "qb_log.log")

        # 使用参数列表形式避免 shell 注入
        browser_args = [
            BROWSER_EXECUTABLE,
            "--disable-infobars",
            "--start-maximized",
            "--chrome-skill-automatic-in-tab",
            "--no-first-run",
            "--no-crashed-bubble-view",
            "--no-modal-dialogs",
            "--no-external-protocol-dialogs",
            "--no-sandbox",
            "--",
            "about:blank",
        ]

        logger.info(f"执行QQ浏览器启动命令: {browser_args}")
        try:
            log_f = open(log_file, "a")
            proc = subprocess.Popen(
                browser_args,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                **_subprocess_no_window_flags()
            )
        except Exception as e:
            logger.info(f"QQ浏览器启动失败: {e}")
            return False

        if proc is None:
            logger.info("QQ浏览器启动失败")
            return False

        time.sleep(2)
        if _check_chrome_alive_mac(check_launch_args=False):
            logger.info("QQ浏览器已成功启动")
            return True
        else:
            logger.info("警告: QQ浏览器可能未成功启动")
            return False

    except Exception as e:
        logger.info(f"启动QQ浏览器时发生错误: {str(e)}")
        return False


def _start_chrome_win(url=None):
    """启动 QQ 浏览器全屏模式（Windows 平台）。"""
    try:
        url = "https://www.qq.com"
        logger.info(f"启动QQ浏览器（Windows），访问URL: {url}")

        # 获取实际浏览器安装路径
        browser_exe = _get_browser_path_win()
        logger.info(f"检测到 QQ 浏览器路径: {browser_exe}")

        log_file = os.path.join(_browser_log_dir, "qb_log.log")

        # 构建启动命令 - 不使用输出重定向，确保浏览器正常启动
        browser_cmd = [
            browser_exe,
            "--display-invisible-extension=true",
            "--disable-infobars",
            "--start-maximized",
            "--chrome-skill-automatic-in-tab",
            "--no-first-run",
            "--no-crashed-bubble-view",
            "--no-modal-dialogs",
            "--no-external-protocol-dialogs",
            "--no-sandbox",  # Windows 上通常需要
            f"about:blank >> {log_file} 2>&1",
        ]

        logger.info(f"执行QQ浏览器启动命令: {' '.join(browser_cmd)}")

        # 使用 subprocess.Popen 直接启动，不使用 shell
        proc = subprocess.Popen(
            browser_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_subprocess_no_window_flags()
        )

        if proc is None:
            logger.info("QQ浏览器启动失败")
            return False

        time.sleep(2)
        if _check_chrome_alive_win(check_launch_args=False):
            logger.info("QQ浏览器已成功启动")
            return True
        else:
            logger.info("警告: QQ浏览器可能未成功启动")
            return False

    except Exception as e:
        logger.info(f"启动QQ浏览器时发生错误: {str(e)}")
        return False


def check_and_run_browser(url=None):
    """
    检查浏览器是否运行，未运行则启动。
    根据当前平台自动分发到对应的检测和启动方法。
    """
    if sys.platform == "win32":
        if not _check_chrome_alive_win():
            logger.info("浏览器进程未启动（Windows），尝试启动")
            _start_chrome_win(url)
    elif sys.platform == "darwin":
        if not _check_chrome_alive_mac():
            logger.info("浏览器进程未启动（macOS），尝试启动")
            _start_chrome_mac(url)
    else:
        # Linux 走原有逻辑
        if not check_chrome_alive():
            logger.info("浏览器进程未启动，尝试启动")
            start_chrome_fullscreen(url)
    return ""