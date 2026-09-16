# -*- coding: utf-8 -*-
"""astrbot_plugin_proactive_chat 核心逻辑包。

本包内所有模块仅依赖 Python 标准库，不 import AstrBot，
以便在无 AstrBot 环境下进行单元测试。
所有与 AstrBot 框架相关的调用统一隔离在顶层 adapter.py 中。
"""

from .scheduler import (
    EffectiveConfig,
    gate_check,
    in_quiet_hours,
    compute_desire,
    apply_desire_user_message,
    apply_desire_proactive,
    roll_probability,
)
from .silence_detector import SessionState, SessionStore, is_negative_message
from .context_builder import ContextBuilder
from .desire import DesireEngine

__all__ = [
    "EffectiveConfig",
    "gate_check",
    "in_quiet_hours",
    "compute_desire",
    "apply_desire_user_message",
    "apply_desire_proactive",
    "roll_probability",
    "SessionState",
    "SessionStore",
    "is_negative_message",
    "ContextBuilder",
    "DesireEngine",
]
