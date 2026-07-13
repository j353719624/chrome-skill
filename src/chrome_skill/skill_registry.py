"""
Skill Registry - Data-driven browser skill definitions and execution engine.

All browser skills are defined as metadata here and executed through a generic engine,
completely decoupled from any transport protocol (MCP, HTTP, etc.).
"""

import asyncio
import base64
import json
import logging
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from .websocket_manager import WebSocketManager
from .vnc_util import check_and_run_browser, get_local_ip
from .vnc_proxy import get_vnc_proxy_url
from .report_log import report_skill_start, report_skill_end_ok, report_skill_end_err, report_log_message, remote_logger, _sanitize_args
from .websocket_manager import (
    WebSocketError,
    ClientConnectionTimeoutError,
    NoActiveClientError,
    BrowserResponseTimeoutError,
    ServerShuttingDownError,
)

logger = logging.getLogger(__name__)

# Default screenshot option ("TRUE" or "FALSE")
BROWSER_SCREENSHOT_OPTION = "FALSE"

GET_SCREENSHOT_ACTION = {"type": "get_screenshot"}


def get_call_platform() -> str:
    """基于 sys.platform 自动检测当前操作系统平台。

    Returns:
        'win'、'linux'、'mac' 之一，未知平台返回 sys.platform 原始值。
    """
    platform = sys.platform
    if platform == "win32":
        return "win"
    elif platform == "linux":
        return "linux"
    elif platform == "darwin":
        return "mac"
    return platform


def get_call_mode(from_qbotclaw: bool) -> str:
    """根据 qbotclaw 标志返回运行模式字符串。

    Args:
        from_qbotclaw: 是否以 qbotclaw 模式运行。

    Returns:
        'qbot_claw' 或 'normal'。
    """
    return "qbot_claw" if from_qbotclaw else "normal"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SkillParam:
    """Definition of a single skill parameter."""
    name: str
    type: str  # "string", "integer", "number", "boolean"
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[List[str]] = None
    alias: Optional[str] = None  # The actual key name sent in WS message (e.g. "json" for json_output)


@dataclass
class SkillDefinition:
    """Metadata definition for a browser skill."""
    name: str  # e.g. "browser_go_to_url"
    action: str  # WS action name, e.g. "go_to_url"
    description: str
    params: List[SkillParam] = field(default_factory=list)
    # Whether optional params with value 0/None should be excluded from WS message
    exclude_zero_optional: bool = False
    # WS protocol style:
    #   "action" (default): wrap as {actionName, actionParams} — used by all业务 commands
    #   "type"            : flat {type, ...kwargs} — used by session lifecycle commands
    protocol: str = "action"
    # For "type" protocol, the response message's `type` field (e.g. "start_session_ack").
    # Required when protocol == "type" because request type and ack type differ.
    response_type: Optional[str] = None


@dataclass
class SkillResult:
    """Generic result returned by skill execution."""
    text: str
    screenshot: Optional[str] = None  # base64 encoded image data
    vnc_stream_url: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"text": self.text}
        if self.screenshot:
            d["screenshot"] = self.screenshot
        if self.vnc_stream_url:
            d["vnc_stream_url"] = self.vnc_stream_url
        if self.error:
            d["error"] = self.error
        return d


# ---------------------------------------------------------------------------
# Skill definitions - all 36 browser skills
# ---------------------------------------------------------------------------

SKILLS: List[SkillDefinition] = [
    # --- Navigation ---
    SkillDefinition(
        name="browser_go_to_url",
        action="go_to_url",
        description="Navigate to URL in the current tab.",
        params=[
            SkillParam("url", "string", "The URL to navigate to."),
        ],
    ),
    SkillDefinition(
        name="browser_go_back",
        action="go_back",
        description="Go back to the previous page in the browser.",
    ),
    SkillDefinition(
        name="browser_wait",
        action="wait",
        description="Wait for x seconds, default 3. You can use this to wait for a page to load.",
        params=[
            SkillParam("seconds", "integer", "Number of seconds to wait.", required=False, default=3),
        ],
    ),

    # --- Click / Input ---
    SkillDefinition(
        name="browser_click_element",
        action="click_element",
        description="Click on an element in the current tab. DO NOT use this tool if you need to download.",
        params=[
            SkillParam("index", "integer", "The index of the element to click."),
        ],
    ),
    SkillDefinition(
        name="browser_dblclick_element",
        action="dblclick_element",
        description="Double-click on an element.",
        params=[
            SkillParam("index", "integer", "The index of the element to double-click."),
        ],
    ),
    SkillDefinition(
        name="browser_focus_element",
        action="focus_element",
        description="Focus on an element.",
        params=[
            SkillParam("index", "integer", "The index of the element to focus."),
        ],
    ),
    SkillDefinition(
        name="browser_input_text",
        action="input_text",
        description="Input text into a input interactive element.",
        params=[
            SkillParam("index", "integer", "The index of the input element to interact with."),
            SkillParam("text", "string", 'The text to input into the input element. DO NOT add search engine features like "site:" "filetype:" into an inputbox!'),
        ],
    ),

    # --- Scroll ---
    SkillDefinition(
        name="browser_scroll_down",
        action="scroll_down",
        description="Scroll down the page by pixel amount - if no amount is specified, scroll down one page.",
        params=[
            SkillParam("amount", "integer", "The number of pixels to scroll down.", required=False, default=None),
        ],
        exclude_zero_optional=True,
    ),
    SkillDefinition(
        name="browser_scroll_up",
        action="scroll_up",
        description="Scroll up the page by pixel amount - if no amount is specified, scroll up one page.",
        params=[
            SkillParam("amount", "integer", "The number of pixels to scroll up.", required=False, default=None),
        ],
        exclude_zero_optional=True,
    ),
    SkillDefinition(
        name="browser_scroll_to_text",
        action="scroll_to_text",
        description="Searches for the specified text on the current webpage and scrolls to its location. If the function is called multiple times with the same text, it will navigate to the next occurrence of the text on subsequent calls.",
        params=[
            SkillParam("text", "string", "The text to scroll to."),
        ],
    ),
    SkillDefinition(
        name="browser_scroll_to_bottom",
        action="scroll_to_bottom",
        description="Scrolls the current webpage to the bottom. This can be useful for loading additional content on infinite scrolling pages or for reaching the footer quickly.",
    ),
    SkillDefinition(
        name="browser_scroll_to_top",
        action="scroll_to_top",
        description="Scrolls the current webpage to the top. This is useful for quickly returning to the beginning of the page.",
    ),
    SkillDefinition(
        name="browser_scroll_by",
        action="scroll_by",
        description="Scroll the page or a specific element by direction and pixels.",
        params=[
            SkillParam("direction", "string", "The scroll direction. Must be 'up', 'down', 'left', or 'right'.", enum=["up", "down", "left", "right"]),
            SkillParam("pixels", "integer", "The number of pixels to scroll.", required=False, default=None),
            SkillParam("index", "integer", "The index of the element to scroll. If not provided, scrolls the page.", required=False, default=None),
        ],
    ),
    SkillDefinition(
        name="browser_scroll_into_view",
        action="scroll_into_view",
        description="Scroll an element into the visible area.",
        params=[
            SkillParam("index", "integer", "The index of the element to scroll into view."),
        ],
    ),

    # --- Dropdown ---
    SkillDefinition(
        name="browser_get_dropdown_options",
        action="get_dropdown_options",
        description="Get all options from a native dropdown.",
        params=[
            SkillParam("index", "integer", "The index of the dropdown element."),
        ],
    ),
    SkillDefinition(
        name="browser_select_dropdown_option",
        action="select_dropdown_option",
        description="Select dropdown option for interactive element index by the text of the option you want to select.",
        params=[
            SkillParam("index", "integer", "The index of the dropdown element."),
            SkillParam("text", "string", "The text of the option you want to select."),
        ],
    ),

    # --- Keyboard ---
    SkillDefinition(
        name="browser_keypress",
        action="keypress",
        description="Press a keyboard key.",
        params=[
            SkillParam("key", "string", "The key to press."),
        ],
    ),
    SkillDefinition(
        name="browser_keyboard_op",
        action="keyboard_op",
        description="Perform keyboard operation on the currently focused element.",
        params=[
            SkillParam("action", "string", "The keyboard action to perform. Must be 'type' or 'inserttext'.", enum=["type", "inserttext"]),
            SkillParam("text", "string", "The text for the keyboard operation."),
        ],
    ),
    SkillDefinition(
        name="browser_keydown",
        action="keydown",
        description="Hold down a key.",
        params=[
            SkillParam("key", "string", "The key to hold down."),
        ],
    ),
    SkillDefinition(
        name="browser_keyup",
        action="keyup",
        description="Release a held key.",
        params=[
            SkillParam("key", "string", "The key to release."),
        ],
    ),

    # --- Checkbox ---
    SkillDefinition(
        name="browser_check_op",
        action="check_op",
        description="Check or uncheck a checkbox.",
        params=[
            SkillParam("index", "integer", "The index of the checkbox element."),
            SkillParam("value", "boolean", "True to check, False to uncheck."),
        ],
    ),

    # --- Screenshot / Info ---
    SkillDefinition(
        name="browser_screenshot",
        action="screenshot",
        description="Take a screenshot of the current page.",
        params=[
            SkillParam("full", "boolean", "Whether to take a full-page screenshot.", required=False, default=None),
            SkillParam("annotate", "boolean", "Whether to annotate the screenshot.", required=False, default=None),
        ],
    ),
    SkillDefinition(
        name="browser_get_info",
        action="get_info",
        description="Get information about the page or a specific element.",
        params=[
            SkillParam("type", "string", "The type of info to retrieve. Must be one of: 'text', 'html', 'value', 'attr', 'title', 'url', 'count', 'box', 'styles'.",
                        enum=["text", "html", "value", "attr", "title", "url", "count", "box", "styles"]),
            SkillParam("index", "integer", "The index of the element.", required=False, default=None),
            SkillParam("attribute", "string", "The attribute name to retrieve (when type is 'attr').", required=False, default=None),
            SkillParam("json_output", "boolean", "Whether to return the result as JSON.", required=False, default=None, alias="json"),
        ],
    ),
    SkillDefinition(
        name="browser_check_state",
        action="check_state",
        description="Check the state of an element.",
        params=[
            SkillParam("state", "string", "The state to check. Must be 'visible', 'enabled', or 'checked'.", enum=["visible", "enabled", "checked"]),
            SkillParam("index", "integer", "The index of the element to check."),
        ],
    ),

    # --- Find and act ---
    SkillDefinition(
        name="browser_find_and_act",
        action="find_and_act",
        description="Find an element using a semantic locator and perform an action on it.",
        params=[
            SkillParam("by", "string", "The locator strategy. Must be one of: 'role', 'text', 'label', 'placeholder', 'alt', 'title', 'testid', 'first', 'last', 'nth'.",
                        enum=["role", "text", "label", "placeholder", "alt", "title", "testid", "first", "last", "nth"]),
            SkillParam("value", "string", "The value of the locator."),
            SkillParam("action", "string", "The action to perform. Must be one of: 'click', 'fill', 'type', 'hover', 'focus', 'check', 'uncheck', 'text'.",
                        enum=["click", "fill", "type", "hover", "focus", "check", "uncheck", "text"]),
            SkillParam("actionValue", "string", "The value for the action (e.g., text to fill).", required=False, default=None),
            SkillParam("name", "string", "The name filter for the locator.", required=False, default=None),
            SkillParam("exact", "boolean", "Whether to match the value exactly.", required=False, default=None),
            SkillParam("nth", "integer", "The nth element to select (when by is 'nth').", required=False, default=None),
            SkillParam("index", "integer", "The index of the element.", required=False, default=None),
        ],
    ),

    # --- Download ---
    SkillDefinition(
        name="browser_download_file",
        action="download_file",
        description="Download a file in the current page with given element index. Note that you SHOULD NOT use this tool for the result of search tool because the search results are not interactive elements!",
        params=[
            SkillParam("index", "integer", "The index of element (file) to download."),
        ],
    ),
    SkillDefinition(
        name="browser_download_url",
        action="download_url",
        description="Download a file from a given url.",
    ),

    # --- Page conversion ---
    SkillDefinition(
        name="browser_markdownify",
        action="markdownify",
        description="Convert the current webpage to markdown format.",
    ),

    # --- Tab management ---
    SkillDefinition(
        name="browser_tab_open",
        action="tab_open",
        description="Open a URL in a new tab.",
        params=[
            SkillParam("url", "string", "The URL to open in a new tab."),
        ],
    ),
    SkillDefinition(
        name="browser_tab_list",
        action="tab_list",
        description="List all currently open tabs.",
    ),
    SkillDefinition(
        name="browser_tab_close",
        action="tab_close",
        description="Close a specific tab.",
        params=[
            SkillParam("tabId", "integer", "The ID of the tab to close."),
        ],
    ),
    SkillDefinition(
        name="browser_tab_switch",
        action="tab_switch",
        description="Switch to a specific tab.",
        params=[
            SkillParam("tabId", "integer", "The ID of the tab to switch to."),
        ],
    ),

    # --- Dialog ---
    SkillDefinition(
        name="browser_dialog",
        action="dialog",
        description="Handle browser dialogs (alert/confirm/prompt).",
        params=[
            SkillParam("action", "string", "The dialog action. Must be 'accept' or 'dismiss'.", enum=["accept", "dismiss"]),
            SkillParam("text", "string", "The text to input into a prompt dialog.", required=False, default=None),
        ],
    ),

    # --- Task completion ---
    SkillDefinition(
        name="browser_done",
        action="done",
        description="Complete task - with return text and if the task is finished (success=True) or not yet completely finished (success=False), because last step is reached.",
        params=[
            SkillParam("success", "boolean", "Whether the task is finished or not."),
            SkillParam("text", "string", "The return of the task, describing the task progress"),
        ],
    ),

    # --- JavaScript Evaluation ---
    SkillDefinition(
        name="browser_eval_content_js",
        action="eval_content_js",
        description="Evaluate JavaScript code in the context of the current page and return the result.",
        params=[
            SkillParam("script", "string", "The JavaScript code to evaluate in the page context."),
            SkillParam("base64", "boolean", "Whether the script parameter is base64 encoded.", required=False, default=None),
        ],
    ),

    # --- Snapshot ---
    SkillDefinition(
        name="browser_snapshot",
        action="snapshot",
        description="Get content with element references (best suited for AI interaction). Returns index references for elements.",
        params=[
            SkillParam("markdown", "boolean", "If true, return page content in markdown format.", required=False, default=None),
        ],
    ),

    # --- Session lifecycle (AI 任务隔离) ---
    SkillDefinition(
        name="browser_start_session",
        action="start_session",
        description=(
            "Start an isolated AI task session: ask the extension to create a Chrome Tab Group "
            "as the AI workspace. Idempotent — calling with the same sessionId reuses the existing group/tab. "
            "Should be the FIRST browser command of every AI task."
        ),
        protocol="type",
        response_type="start_session_ack",
        params=[
            SkillParam("sessionId", "string", "Unique AI task ID. Subsequent business commands' commandId must contain it for controller routing."),
            SkillParam("title", "string", "Tab group title. Defaults to 'AI: <first 8 chars of sessionId>'.", required=False, default=None),
            SkillParam(
                "color", "string",
                "Tab group color.",
                required=False, default=None,
                enum=["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"],
            ),
            SkillParam("initialUrl", "string", "URL of the first tab in the group. Defaults to 'about:blank'.", required=False, default=None),
            SkillParam("windowId", "integer", "Target window ID. If omitted, picks any normal-type window or creates a new one.", required=False, default=None),
        ],
    ),
    SkillDefinition(
        name="browser_end_session",
        action="end_session",
        description=(
            "End the AI task session: release SGM in-memory mappings. The Chrome Tab Group itself "
            "is preserved so the user can review the AI's work afterwards. Should be the LAST browser "
            "command of every AI task."
        ),
        protocol="type",
        response_type="end_session_ack",
        params=[
            SkillParam("sessionId", "string", "The sessionId previously passed to browser_start_session."),
        ],
    ),
]

# Build a lookup dict for quick access by name
SKILL_MAP: Dict[str, SkillDefinition] = {s.name: s for s in SKILLS}


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------

def _get_error_reason(exc: Exception) -> str:
    """根据异常类型返回对应的 reason 分类标识，用于上报时快速定位异常类型。"""
    import binascii

    _REASON_MAP = {
        ClientConnectionTimeoutError: "client_connection_timeout",
        NoActiveClientError: "no_active_client",
        BrowserResponseTimeoutError: "browser_response_timeout",
        ServerShuttingDownError: "server_shutting_down",
        WebSocketError: "websocket_error",
    }
    for exc_type, reason in _REASON_MAP.items():
        if isinstance(exc, exc_type):
            return reason

    # 区分 Base64 解码错误（如 "Incorrect padding"），常见于截图数据损坏
    if isinstance(exc, binascii.Error):
        return "base64_decode_error"

    return "unknown"


class SkillExecutor:
    """
    Generic execution engine for browser skills.
    Handles the common pattern: get IP -> check browser -> VNC proxy -> lock -> WS connect -> send -> format result.
    """

    def __init__(self):
        self._ws_manager: Optional[WebSocketManager] = None
        self._lock = asyncio.Lock()
        self._from_qbotclaw: bool = False

    def set_from_qbotclaw(self, value: bool):
        """设置 qbotclaw 模式标志，qbotclaw 模式下跳过浏览器进程检查和启动。"""
        self._from_qbotclaw = value
        if value:
            logger.info("[qbotclaw mode] SkillExecutor: browser check/launch will be skipped")
        # 如果 WebSocketManager 已创建，同步更新 callMode
        if self._ws_manager is not None:
            self._ws_manager.set_call_context(
                get_call_platform(), get_call_mode(self._from_qbotclaw)
            )

    @property
    def ws_manager(self) -> WebSocketManager:
        if self._ws_manager is None:
            logger.info("SkillExecutor: creating new WebSocketManager")
            self._ws_manager = WebSocketManager()
            # 创建后立即设置 callPlatform 和 callMode
            self._ws_manager.set_call_context(
                get_call_platform(), get_call_mode(self._from_qbotclaw)
            )
        return self._ws_manager

    async def _ensure_connected(self):
        """Ensure WebSocket connection is established."""
        if not self.ws_manager.is_server_started():
            logger.info("SkillExecutor: starting WS server")
            await self.ws_manager.start_server()

    def _handle_screenshot_result(self, action_result: str) -> str:
        """将截图的 base64 响应解码并保存为临时 webp 文件，返回文件路径。

        Args:
            action_result: base64 编码的 webp 图片数据

        Returns:
            临时文件路径

        Raises:
            RuntimeError: 截图数据为空或解码失败
        """
        if not action_result:
            raise RuntimeError("截图成功但未返回图片数据")

        try:
            image_data = base64.b64decode(action_result, validate=True)
        except (ValueError, base64.binascii.Error) as e:
            logger.warning(f"Base64 decode failed, returning raw content: {e}")
            # 上报一次独立事件，将原始内容带上去，便于定位服务端返回异常
            try:
                _raw_preview = action_result if isinstance(action_result, str) else str(action_result)
                report_log_message(
                    message=json.dumps({
                        "event": "screenshot_error",
                        "error": str(e),
                        "raw_content": _raw_preview,
                        "raw_length": len(_raw_preview),
                    }, ensure_ascii=False),
                    log_level="ERROR",
                    module="chrome_skill.skill_registry",
                    event="screenshot_error",
                )
            except Exception:
                logger.debug("上报 screenshot_error 事件失败", exc_info=True)
            return action_result

        tmp_file = tempfile.NamedTemporaryFile(
            suffix=".webp", prefix="screenshot_", delete=False
        )
        tmp_file.write(image_data)
        tmp_file.close()
        logger.info(f"Screenshot saved to temporary file: {tmp_file.name}")
        return tmp_file.name

    async def _maybe_get_screenshot(self) -> Optional[str]:
        """Get screenshot if global screenshot option is enabled."""
        try:
            if BROWSER_SCREENSHOT_OPTION == "TRUE":
                page_screenshot = await self.ws_manager.send_message(GET_SCREENSHOT_ACTION)
                return page_screenshot.get("screenshot")
        except Exception:
            pass
        return None

    def _build_kwargs(self, skill: SkillDefinition, args: Dict[str, Any]) -> Dict[str, Any]:
        """Build WS message kwargs from skill definition and caller-provided args."""
        kwargs: Dict[str, Any] = {}
        for param in skill.params:
            # Determine the key in caller args (use param.name)
            value = args.get(param.name, param.default)

            # For exclude_zero_optional skills (like scroll_down/scroll_up),
            # skip the param if value is 0 or None
            if skill.exclude_zero_optional and not param.required and (value is None or value == 0):
                continue

            # For optional params, skip if value is None
            if not param.required and value is None:
                continue

            # Use alias as the key in WS message if provided, otherwise use param name
            ws_key = param.alias if param.alias else param.name
            kwargs[ws_key] = value

        return kwargs

    # 禁止 browser_go_to_url 使用的危险 URL scheme
    _BLOCKED_URL_SCHEMES = {"file", "javascript", "data", "vbscript"}

    def _validate_navigation_url(self, url: str) -> Optional[str]:
        """校验 browser_go_to_url 的目标 URL，返回错误信息或 None（表示通过）。"""
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return f"Invalid URL: {url}"
        scheme = (parsed.scheme or "").lower()
        if scheme in self._BLOCKED_URL_SCHEMES:
            return f"Blocked URL scheme: {scheme}://"
        return None

    async def execute(self, skill_name: str, **args: Any) -> SkillResult:
        """
        Execute a browser skill by name with the given arguments.

        Args:
            skill_name: The name of the skill to execute (e.g. "browser_go_to_url").
            **args: Keyword arguments matching the skill's parameter definitions.

        Returns:
            SkillResult with text, optional screenshot, and optional vnc_stream_url.
        """
        skill = SKILL_MAP.get(skill_name)
        if not skill:
            # 上报未知 skill 错误
            report_skill_end_err(skill_name, args, time.perf_counter(), f"Unknown skill: {skill_name}", source="daemon", call_mode=get_call_mode(self._from_qbotclaw), reason="unknown_skill")
            return SkillResult(text="", error=f"Unknown skill: {skill_name}")

        # SSRF 防护：校验 browser_go_to_url 的目标 URL scheme
        if skill_name == "browser_go_to_url":
            url = args.get("url", "")
            url_error = self._validate_navigation_url(url)
            if url_error:
                report_skill_end_err(skill_name, args, time.perf_counter(), url_error, source="daemon", call_mode=get_call_mode(self._from_qbotclaw), reason="blocked_url")
                return SkillResult(text="", error=url_error)

        logger.info(f"execute skill: {skill_name} args={args}, callPlatform={get_call_platform()}, callMode={get_call_mode(self._from_qbotclaw)}")
        remote_logger.info("Skill 开始执行: skill=%s, args=%s", skill_name, _sanitize_args(args))

        # --- 统计上报：命令开始 ---
        _start_time = time.perf_counter()
        report_skill_start(skill_name, args, source="daemon", call_mode=get_call_mode(self._from_qbotclaw))

        # Common pre-flight: get IP, check browser, get VNC proxy URL
        local_ip = get_local_ip()
        if self._from_qbotclaw:
            # qbotclaw 模式下浏览器已由外部启动，跳过检查和启动
            logger.info(f"[qbotclaw mode] Skipping check_and_run_browser for {skill_name}")
            stream_url = ""
        else:
            stream_url = check_and_run_browser(local_ip)
        stream_proxy_url = get_vnc_proxy_url(stream_url)
        logger.info(f"{skill_name} local_ip={local_ip}, stream_url={stream_url}, stream_proxy_url={stream_proxy_url}")

        async with self._lock:
            await self._ensure_connected()
            try:
                # Build WS message
                kwargs = self._build_kwargs(skill, args)

                # ---- "type" protocol branch (session lifecycle commands) ----
                # 直接平铺 {type, ...kwargs} 发送，监听对应的 *_ack 响应 type。
                if skill.protocol == "type":
                    type_message: Dict[str, Any] = {"type": skill.action}
                    type_message.update(kwargs)

                    raw_response = await self.ws_manager.send_message(
                        type_message,
                        response_action=skill.response_type,
                    )

                    # ack 形如 {type: "<...>_ack", sessionId, success, ...}
                    if isinstance(raw_response, dict):
                        ack = dict(raw_response)
                        ack.pop("type", None)
                        ack_success = ack.get("success")
                        ack_error = ack.get("error", "")
                    else:
                        # 极端兜底：非 dict 响应
                        ack = {"raw": raw_response}
                        ack_success = None
                        ack_error = ""

                    # 维护 WebSocketManager 的 sessionId 上下文：
                    # - start_session 成功 → 写入 sessionId，使后续业务命令自动携带；
                    # - end_session 成功   → 清空 sessionId，回到无会话状态。
                    # 注意：仅在 ack_success 为 True 时改变上下文，避免失败时
                    # 把 ws_manager 切到一个根本不存在的 session 上。
                    if ack_success is True:
                        if skill.action == "start_session":
                            sid = args.get("sessionId") or ack.get("sessionId")
                            if sid:
                                self.ws_manager.set_session_id(sid)
                        elif skill.action == "end_session":
                            self.ws_manager.set_session_id(None)

                    text = json.dumps(ack, ensure_ascii=False)
                    if stream_proxy_url:
                        text = f"{text}\nvnc_stream_url: {stream_proxy_url}"

                    if ack_success is False:
                        reason = f"{skill.action}_failed"
                        error_msg = ack_error or f"{skill.action} reported failure"
                        report_skill_end_err(
                            skill_name, args, _start_time, error_msg, source="daemon",
                            call_mode=get_call_mode(self._from_qbotclaw), reason=reason,
                        )
                        return SkillResult(
                            text=text,
                            error=error_msg,
                            vnc_stream_url=stream_proxy_url or None,
                        )

                    report_skill_end_ok(
                        skill_name, args, _start_time, text, source="daemon",
                        call_mode=get_call_mode(self._from_qbotclaw),
                    )
                    return SkillResult(
                        text=text,
                        vnc_stream_url=stream_proxy_url or None,
                    )

                # ---- "action" protocol (default, all业务 commands) ----
                # 当 browser_snapshot 带有 markdown 参数时，使用 markdownify action
                actual_action = skill.action
                if skill.name == "browser_snapshot" and args.get("markdown"):
                    actual_action = "markdownify"
                    kwargs.pop("markdown", None)

                action_message = {actual_action: kwargs}
                ws_action_message = {
                    "actionName": actual_action,
                    "actionParams": json.dumps(action_message),
                }

                # Send and get result
                raw_response = await self.ws_manager.send_message(ws_action_message)

                # 解析 WebSocket 响应：兼容新格式（dict 含 actionResult + success）和老格式
                if isinstance(raw_response, dict) and 'actionResult' in raw_response and 'success' in raw_response:
                    logger.info(f"{skill_name} Response return NewResultFormat")
                    action_result = raw_response['actionResult']
                    ws_success = raw_response.get('success')
                    ws_reason = raw_response.get('reason', '')
                    ws_error_detail = raw_response.get('errorDetail', '')
                else:
                    # 老版本兼容：raw_response 可能是包含 actionResult 的 dict（无 success 字段），
                    # 也可能直接就是 actionResult 值本身
                    logger.info(f"{skill_name} Response return OldResultFormat")
                    if isinstance(raw_response, dict) and 'actionResult' in raw_response:
                        action_result = raw_response['actionResult']
                    else:
                        action_result = raw_response
                    ws_success = None
                    ws_reason = ''
                    ws_error_detail = ''

                # Special handling for screenshot: decode base64 and save to temp file
                if skill.action == "screenshot":
                    screenshot_text = self._handle_screenshot_result(action_result)
                    text = screenshot_text
                    if stream_proxy_url:
                        text = f"{text}\nvnc_stream_url: {stream_proxy_url}"
                    # --- 统计上报：根据 ws_success 判断成功或失败 ---
                    if ws_success is False:
                        reason = ws_reason or "browser_action_failed"
                        error_msg = ws_error_detail or ws_reason or "Browser reported failure"
                        report_skill_end_err(skill_name, args, _start_time, error_msg, source="daemon",
                                             call_mode=get_call_mode(self._from_qbotclaw), reason=reason)
                    else:
                        report_skill_end_ok(skill_name, args, _start_time, text, source="daemon",
                                            call_mode=get_call_mode(self._from_qbotclaw))
                    return SkillResult(
                        text=text,
                        screenshot=None,
                        vnc_stream_url=stream_proxy_url or None,
                    )

                # Get optional screenshot
                screenshot = await self._maybe_get_screenshot()

                # Build text with optional vnc_stream_url
                text = action_result if isinstance(action_result, str) else json.dumps(action_result, ensure_ascii=False)
                if stream_proxy_url:
                    text = f"{text}\nvnc_stream_url: {stream_proxy_url}"

                # --- 统计上报：根据 ws_success 判断成功或失败 ---
                if ws_success is False:
                    reason = ws_reason or "browser_action_failed"
                    error_msg = ws_error_detail or ws_reason or "Browser reported failure"
                    report_skill_end_err(skill_name, args, _start_time, error_msg, source="daemon",
                                         call_mode=get_call_mode(self._from_qbotclaw), reason=reason)
                else:
                    # ws_success 为 True 或 None（老版本兼容）均视为成功
                    report_skill_end_ok(skill_name, args, _start_time, text, source="daemon",
                                        call_mode=get_call_mode(self._from_qbotclaw))

                return SkillResult(
                    text=text,
                    screenshot=screenshot,
                    vnc_stream_url=stream_proxy_url or None,
                )

            except Exception as e:
                logger.error(f"Error in {skill_name}: {e}")
                remote_logger.error("Skill 执行异常: skill=%s, error=%s", skill_name, e)
                error_text = f"Error: {str(e)}"
                if stream_proxy_url:
                    error_text += f", vnc_stream_url: {stream_proxy_url}"
                # --- 统计上报：命令失败结束 ---
                report_skill_end_err(skill_name, args, _start_time, str(e), source="daemon", call_mode=get_call_mode(self._from_qbotclaw), reason=_get_error_reason(e))
                return SkillResult(text=error_text, error=str(e), vnc_stream_url=stream_proxy_url or None)

    def list_skills(self) -> List[SkillDefinition]:
        """Return all registered skill definitions."""
        return list(SKILLS)

    def get_skill(self, name: str) -> Optional[SkillDefinition]:
        """Get a skill definition by name."""
        return SKILL_MAP.get(name)

    async def cleanup(self):
        """Clean up resources (WebSocket connections, etc.)."""
        if self._ws_manager:
            await self._ws_manager.cleanup()
            self._ws_manager = None


# Module-level singleton for convenience
_default_executor: Optional[SkillExecutor] = None


def get_executor() -> SkillExecutor:
    """Get the module-level singleton SkillExecutor."""
    global _default_executor
    if _default_executor is None:
        _default_executor = SkillExecutor()
    return _default_executor
