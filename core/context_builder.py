# -*- coding: utf-8 -*-
"""上下文感知：为主动聊天构建上下文包。

输出结构化文本，包含：
【当前时间】【沉默时长】【最近连续对话】【用户画像】
【Bot 自我状态/欲望值】【上次主动聊天时间与内容】

纯标准库实现，可直接测试。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .prompts import NO_PROFILE_PLACEHOLDER
from .scheduler import EffectiveConfig, compute_desire

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "无记录"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_silence(seconds: float) -> str:
    if seconds == float("inf"):
        return "无记录（本会话还没有用户消息）"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


class ContextBuilder:
    """把会话状态渲染成 LLM 可读的上下文包文本。"""

    def __init__(
        self,
        user_profile: str = "",
        desire_enabled: bool = True,
        timezone_offset_hours: float | None = None,
    ) -> None:
        """
        Args:
            user_profile: 用户画像文本（来自插件配置，可空）。
            desire_enabled: 是否在上下文包中暴露欲望值。
        """
        self.user_profile = (user_profile or "").strip()
        self.desire_enabled = bool(desire_enabled)

    # -----------------------------------------------------------
    def render_recent(self, state: Any, max_messages: int = 12) -> str:
        """渲染最近连续对话为 role: text 列表。"""
        recent = getattr(state, "recent", []) or []
        lines = []
        for item in recent[-max_messages:]:
            role = "用户" if item.get("role") == "user" else "Bot"
            text = str(item.get("text", "")).strip().replace("\n", " ")
            if text:
                lines.append(f"[{_fmt_ts(item.get('ts', 0))}] {role}: {text}")
        return "\n".join(lines) if lines else "（最近没有对话记录）"

    # -----------------------------------------------------------
    def build(
        self,
        eff: EffectiveConfig,
        state: Any,
        now: float | None = None,
        max_messages: int = 12,
    ) -> str:
        """构建完整上下文包文本。"""
        now = now if now is not None else time.time()
        now_dt = datetime.fromtimestamp(now)
        silence = (
            float("inf")
            if state.last_user_ts <= 0
            else now - state.last_user_ts
        )

        desire = compute_desire(state, eff, now)

        sections = [
            f"【当前时间】{now_dt.strftime('%Y-%m-%d %H:%M')}（{_WEEKDAY_CN[now_dt.weekday()]}）",
            f"【沉默时长】{_fmt_silence(silence)}"
            f"（用户最后发言：{_fmt_ts(state.last_user_ts)}）",
            f"【最近连续对话】\n{self.render_recent(state, max_messages)}",
            f"【用户画像】{self.user_profile or NO_PROFILE_PLACEHOLDER}",
        ]

        if self.desire_enabled:
            level = "低（倾向于安静等待）"
            if desire >= 0.75:
                level = "高（很想主动找用户说话）"
            elif desire >= 0.45:
                level = "中等"
            sections.append(
                f"【Bot 自我状态/欲望值】{desire:.2f}（{level}）"
            )

        if state.last_proactive_ts > 0:
            sections.append(
                f"【上次主动聊天】{_fmt_ts(state.last_proactive_ts)}，"
                f"内容：{state.last_proactive_text or '（无内容）'}"
            )
        else:
            sections.append("【上次主动聊天】从未主动发起过")

        return "\n\n".join(sections)
