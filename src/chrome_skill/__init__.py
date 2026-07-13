"""
Chrome Browser Skill - Browser automation toolkit.

Provides a set of browser control skills that communicate with
the QQ browser extension via WebSocket.
"""

from .skill_registry import (
    SkillDefinition,
    SkillParam,
    SkillResult,
    SkillExecutor,
    get_executor,
    SKILLS,
    SKILL_MAP,
)

__all__ = [
    "SkillDefinition",
    "SkillParam",
    "SkillResult",
    "SkillExecutor",
    "get_executor",
    "SKILLS",
    "SKILL_MAP",
]
