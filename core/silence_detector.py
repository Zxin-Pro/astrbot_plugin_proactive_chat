# -*- coding: utf-8 -*-
"""会话状态与沉默检测。

每个会话（unified_msg_origin）维护一份独立的 SessionState，
实现多用户、多会话数据隔离。状态以纯 dict 序列化，方便持久化到 KV 存储。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .prompts import DEFAULT_NEGATIVE_KEYWORDS

# 最近对话环形缓冲区上限
DEFAULT_RECENT_LIMIT = 12

# 负面情绪冷却时长（秒）：用户表达负面情绪后，额外静默该时长
NEGATIVE_COOLDOWN_SECONDS = 3600.0


def _now() -> float:
    return time.time()


@dataclass
class SessionState:
    """单个会话的运行时状态。

    Attributes:
        umo: 会话唯一标识 (unified_msg_origin)。
        last_user_ts: 最后一次用户消息的 Unix 时间戳。
        last_bot_ts: 最后一次 Bot 消息的 Unix 时间戳。
        last_proactive_ts: 最后一次主动消息的 Unix 时间戳。
        last_proactive_text: 最后一次主动消息的内容。
        last_global_proactive_ts: 全局最后一次主动消息时间戳（由调度器统一写入）。
        proactive_date: 当日计数对应的日期字符串 (YYYY-MM-DD)。
        proactive_count: 当日已主动发送次数。
        desire_value: 欲望值快照（0~1）。
        desire_ts: 欲望值快照对应的时间戳。
        negative_until: 负面情绪冷却截止时间戳。
        recent: 最近对话环形缓冲 [{role, text, ts}]。
        enabled: 单会话开关覆盖（None 表示跟随全局配置）。
        overrides: 单会话配置覆盖 dict（键同 EffectiveConfig 字段）。
    """

    umo: str
    last_user_ts: float = 0.0
    last_bot_ts: float = 0.0
    last_proactive_ts: float = 0.0
    last_proactive_text: str = ""
    last_global_proactive_ts: float = 0.0
    proactive_date: str = ""
    proactive_count: int = 0
    desire_value: float = 0.3
    desire_ts: float = field(default_factory=_now)
    negative_until: float = 0.0
    recent: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    # -----------------------------------------------------------
    # 序列化
    # -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "last_user_ts": self.last_user_ts,
            "last_bot_ts": self.last_bot_ts,
            "last_proactive_ts": self.last_proactive_ts,
            "last_proactive_text": self.last_proactive_text,
            "last_global_proactive_ts": self.last_global_proactive_ts,
            "proactive_date": self.proactive_date,
            "proactive_count": self.proactive_count,
            "desire_value": self.desire_value,
            "desire_ts": self.desire_ts,
            "negative_until": self.negative_until,
            "recent": self.recent[-DEFAULT_RECENT_LIMIT:],
            "enabled": self.enabled,
            "overrides": self.overrides,
        }

    @classmethod
    def from_dict(cls, umo: str, data: dict[str, Any]) -> "SessionState":
        state = cls(umo=umo)
        for key, value in (data or {}).items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state

    # -----------------------------------------------------------
    # 沉默检测相关
    # -----------------------------------------------------------
    @property
    def silence_seconds(self, now: float | None = None) -> float:
        """距离最后一次用户消息的沉默秒数。从未收到过消息返回 inf。"""
        if self.last_user_ts <= 0:
            return float("inf")
        return (now if now is not None else _now()) - self.last_user_ts

    def has_user_replied_after_bot(self) -> bool:
        """用户在 Bot 最近一次发言（含主动消息）之后是否有过回复。"""
        return self.last_user_ts >= self.last_bot_ts and self.last_user_ts > 0

    def is_bot_talking_alone(self, now: float | None = None) -> bool:
        """Bot 发完消息后用户一直没回（自说自话风险状态）。"""
        now = now if now is not None else _now()
        return self.last_bot_ts > self.last_user_ts

    def is_negative_cooling(self, now: float | None = None) -> bool:
        return (now if now is not None else _now()) < self.negative_until

    def today_count(self, date_str: str) -> int:
        return self.proactive_count if self.proactive_date == date_str else 0

    # -----------------------------------------------------------
    # 记录消息
    # -----------------------------------------------------------
    def record_user_message(self, text: str, ts: float | None = None) -> None:
        ts = ts if ts is not None else _now()
        self.last_user_ts = ts
        self.recent.append({"role": "user", "text": text[:500], "ts": ts})
        self._trim()

    def record_bot_message(self, text: str, ts: float | None = None) -> None:
        ts = ts if ts is not None else _now()
        self.last_bot_ts = ts
        self.recent.append({"role": "bot", "text": text[:500], "ts": ts})
        self._trim()

    def record_proactive(self, text: str, ts: float | None = None) -> None:
        ts = ts if ts is not None else _now()
        self.last_proactive_ts = ts
        self.last_proactive_text = text
        self.last_global_proactive_ts = ts
        self.record_bot_message(text, ts)

    def _trim(self) -> None:
        if len(self.recent) > DEFAULT_RECENT_LIMIT:
            del self.recent[:-DEFAULT_RECENT_LIMIT]


def is_negative_message(text: str, keywords: Iterable[str] | None = None) -> bool:
    """基于关键词的轻量负面情绪 / 拒绝打扰检测。

    仅做本地关键词匹配（不消耗 LLM 调用），命中即认为用户当前不想被打扰。
    """
    if not text:
        return False
    words = keywords if keywords is not None else DEFAULT_NEGATIVE_KEYWORDS
    for kw in words:
        if kw and kw in text:
            return True
    return False


class SessionStore:
    """多会话状态容器。内存为主，外部可挂接异步持久化回调。"""

    def __init__(self, negative_keywords: Iterable[str] | None = None) -> None:
        self._states: dict[str, SessionState] = {}
        self._negative_keywords = (
            list(negative_keywords) if negative_keywords is not None else None
        )

    # -----------------------------------------------------------
    def get(self, umo: str) -> SessionState:
        state = self._states.get(umo)
        if state is None:
            state = SessionState(umo=umo)
            self._states[umo] = state
        return state

    def known_umos(self) -> list[str]:
        return list(self._states.keys())

    def load(self, umo: str, data: dict[str, Any]) -> SessionState:
        state = SessionState.from_dict(umo, data)
        self._states[umo] = state
        return state

    def dump(self) -> dict[str, dict[str, Any]]:
        return {umo: st.to_dict() for umo, st in self._states.items()}

    def remove(self, umo: str) -> None:
        self._states.pop(umo, None)

    # -----------------------------------------------------------
    # 高层记录入口（供事件监听器调用）
    # -----------------------------------------------------------
    def on_user_message(self, umo: str, text: str) -> SessionState:
        """记录一条用户消息；命中负面关键词时设置负面冷却。"""
        state = self.get(umo)
        state.record_user_message(text)
        if is_negative_message(text, self._negative_keywords):
            state.negative_until = _now() + NEGATIVE_COOLDOWN_SECONDS
        return state

    def on_bot_message(self, umo: str, text: str) -> SessionState:
        state = self.get(umo)
        state.record_bot_message(text)
        return state
