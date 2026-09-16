# -*- coding: utf-8 -*-
"""欲望驱动系统。

模拟 Bot 的"想聊天欲望值"：
- 范围 0~1，随时间缓慢上升（速率：每小时 desire_increase_rate）；
- 用户每回复一次下降 desire_decay_rate；
- Bot 每主动聊天一次下降 2 倍 desire_decay_rate；
- 欲望值越高，触发概率权重越大（见 scheduler.roll_probability）。

实现为纯计算模块，无任何外部依赖，便于测试。
"""

from __future__ import annotations

from typing import Any

MIN_DESIRE = 0.0
MAX_DESIRE = 1.0

# 全新会话的初始欲望值
DEFAULT_INITIAL_DESIRE = 0.3


def clamp(value: float) -> float:
    return max(MIN_DESIRE, min(MAX_DESIRE, value))


class DesireEngine:
    """欲望值引擎。所有方法基于 SessionState 就地更新，返回当前欲望值。"""

    def __init__(
        self,
        increase_rate_per_hour: float = 0.08,
        decay_per_user_message: float = 0.25,
        enabled: bool = True,
    ) -> None:
        self.increase_rate_per_hour = max(0.0, float(increase_rate_per_hour))
        self.decay_per_user_message = max(0.0, float(decay_per_user_message))
        self.enabled = bool(enabled)

    # -----------------------------------------------------------
    def current(self, state: Any, now: float) -> float:
        """计算当前欲望值 = 快照 + 时间增量（不写回快照）。"""
        if not self.enabled:
            return DEFAULT_INITIAL_DESIRE
        base = getattr(state, "desire_value", DEFAULT_INITIAL_DESIRE)
        ts = getattr(state, "desire_ts", now) or now
        elapsed_hours = max(0.0, now - ts) / 3600.0
        return clamp(base + elapsed_hours * self.increase_rate_per_hour)

    def _commit(self, state: Any, value: float, now: float) -> float:
        state.desire_value = value
        state.desire_ts = now
        return clamp(value)

    # -----------------------------------------------------------
    def on_user_message(self, state: Any, now: float) -> float:
        """用户回复：欲望值下降（快照落盘，防止重复衰减）。"""
        if not self.enabled:
            return self.current(state, now)
        value = self.current(state, now) - self.decay_per_user_message
        return self._commit(state, value, now)

    def on_proactive(self, state: Any, now: float) -> float:
        """主动聊天后：欲望值大幅下降。"""
        if not self.enabled:
            return self.current(state, now)
        value = self.current(state, now) - 2.0 * self.decay_per_user_message
        return self._commit(state, value, now)

    def snapshot(self, state: Any, now: float) -> float:
        """把当前计算值固化为快照（用于持久化前统一结算）。"""
        return self._commit(state, self.current(state, now), now)
