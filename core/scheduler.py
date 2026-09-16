# -*- coding: utf-8 -*-
"""主动触发调度核心。

包含：
- EffectiveConfig：合并全局配置与单会话覆盖后的有效配置；
- 纯函数形式的触发门控 gate_check()（沉默 / 冷却 / 免打扰 / 每日上限 / 负面情绪 / 概率）；
- 欲望值结算函数。

本模块为纯标准库实现，不依赖 AstrBot，可独立单元测试。
实际的 LLM 决策与发送由 main.py + adapter.py 完成。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .desire import DesireEngine

# -----------------------------------------------------------------
# 免打扰时段例外：用户"正在活跃"的定义窗口（分钟）
# 距用户最后一条消息 <= 该窗口，视为正在活跃，可豁免免打扰时段。
# -----------------------------------------------------------------
DEFAULT_QUIET_ACTIVE_WINDOW_MINUTES = 10


@dataclass
class EffectiveConfig:
    """单会话生效配置 = 全局配置 + 该会话的 overrides 覆盖。"""

    enable: bool = True
    silence_threshold_minutes: int = 60
    proactive_probability: float = 0.3
    max_daily_proactive: int = 3
    cooldown_minutes: int = 120
    session_cooldown_minutes: int = 240
    quiet_hours_start: int = 23
    quiet_hours_end: int = 8
    quiet_active_window_minutes: int = DEFAULT_QUIET_ACTIVE_WINDOW_MINUTES
    enable_desire_system: bool = True
    desire_increase_rate: float = 0.08
    desire_decay_rate: float = 0.25
    # 黑/白名单由 main.py 处理，这里只保留开关类字段
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_global(
        cls,
        cfg: dict[str, Any],
        overrides: dict[str, Any] | None = None,
        session_enabled: bool | None = None,
    ) -> "EffectiveConfig":
        """从 AstrBotConfig dict 构造，并应用单会话覆盖。

        Args:
            cfg: 插件全局配置 dict。
            overrides: 该会话的配置覆盖（管理命令写入）。
            session_enabled: 单会话开关（None=跟随全局 enable）。
        """
        eff = cls(
            enable=bool(cfg.get("enable", True)),
            silence_threshold_minutes=int(cfg.get("silence_threshold_minutes", 60)),
            proactive_probability=float(cfg.get("proactive_probability", 0.3)),
            max_daily_proactive=int(cfg.get("max_daily_proactive", 3)),
            cooldown_minutes=int(cfg.get("cooldown_minutes", 120)),
            session_cooldown_minutes=int(cfg.get("session_cooldown_minutes", 240)),
            quiet_hours_start=int(cfg.get("quiet_hours_start", 23)),
            quiet_hours_end=int(cfg.get("quiet_hours_end", 8)),
            quiet_active_window_minutes=int(
                cfg.get("quiet_active_window_minutes", DEFAULT_QUIET_ACTIVE_WINDOW_MINUTES)
            ),
            enable_desire_system=bool(cfg.get("enable_desire_system", True)),
            desire_increase_rate=float(cfg.get("desire_increase_rate", 0.08)),
            desire_decay_rate=float(cfg.get("desire_decay_rate", 0.25)),
        )
        for key, value in (overrides or {}).items():
            if hasattr(eff, key) and key != "extra":
                try:
                    current = getattr(eff, key)
                    setattr(eff, key, type(current)(value))
                except (TypeError, ValueError):
                    pass
        if session_enabled is not None:
            eff.enable = bool(session_enabled)
        return eff


# -----------------------------------------------------------------
# 免打扰时段判断
# -----------------------------------------------------------------
def in_quiet_hours(
    now: datetime, start_hour: int, end_hour: int
) -> bool:
    """判断 now 是否处于免打扰时段。支持跨午夜（如 23~8）。"""
    h = now.hour
    if start_hour == end_hour:
        return False  # 起止相同视为未启用
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    # 跨午夜
    return h >= start_hour or h < end_hour


def is_user_active_recently(state: Any, now: datetime) -> bool:
    """免打扰时段例外：用户最近活跃（在 quiet_active_window 内发过消息）。"""
    window = getattr(state, "_quiet_active_window_minutes", 0) or 0
    if state.last_user_ts <= 0:
        return False
    last_user_dt = datetime.fromtimestamp(state.last_user_ts)
    return (now - last_user_dt) <= timedelta(minutes=window)


# -----------------------------------------------------------------
# 欲望值结算
# -----------------------------------------------------------------
def compute_desire(state: Any, eff: EffectiveConfig, now: float) -> float:
    """计算（并返回，不落盘）当前欲望值。"""
    engine = DesireEngine(
        increase_rate_per_hour=eff.desire_increase_rate,
        decay_per_user_message=eff.desire_decay_rate,
        enabled=eff.enable_desire_system,
    )
    return engine.current(state, now)


def apply_desire_user_message(state: Any, eff: EffectiveConfig, now: float) -> float:
    engine = DesireEngine(
        increase_rate_per_hour=eff.desire_increase_rate,
        decay_per_user_message=eff.desire_decay_rate,
        enabled=eff.enable_desire_system,
    )
    return engine.on_user_message(state, now)


def apply_desire_proactive(state: Any, eff: EffectiveConfig, now: float) -> float:
    engine = DesireEngine(
        increase_rate_per_hour=eff.desire_increase_rate,
        decay_per_user_message=eff.desire_decay_rate,
        enabled=eff.enable_desire_system,
    )
    return engine.on_proactive(state, now)


# -----------------------------------------------------------------
# 概率掷骰
# -----------------------------------------------------------------
def roll_probability(
    eff: EffectiveConfig,
    desire: float,
    rng: random.Random | None = None,
) -> bool:
    """以 proactive_probability 为基础概率，欲望值作为加权（0.5 + desire）。"""
    rng = rng or random.Random()
    p = eff.proactive_probability * (0.5 + max(0.0, min(1.0, desire)))
    p = max(0.0, min(1.0, p))
    return rng.random() < p


# -----------------------------------------------------------------
# 门控检查（纯函数）
# -----------------------------------------------------------------
def gate_check(
    eff: EffectiveConfig,
    state: Any,
    now: float | None = None,
    now_dt: datetime | None = None,
    date_str: str | None = None,
) -> tuple[bool, str]:
    """判断指定会话此刻是否满足"硬性触发条件"。

    依次检查：总开关 → 单会话开关 → 负面情绪冷却 → 免打扰时段（含活跃例外）
    → 沉默时长 → 自说自话防护 → 全局冷却 → 单会话冷却 → 每日上限。

    概率掷骰（roll_probability）不在此函数内，由调度器在硬性条件通过后另行执行，
    便于测试与"测试"命令强制触发。

    Returns:
        (是否通过, 原因说明)。原因用于日志，便于排查为何不触发。
    """
    now = now if now is not None else time.time()
    now_dt = now_dt or datetime.fromtimestamp(now)
    date_str = date_str or now_dt.strftime("%Y-%m-%d")

    if not eff.enable:
        return False, "全局开关未开启"

    if not state.last_user_ts:
        return False, "该会话尚无用户消息记录"

    # 负面情绪 / 别烦我 → 延长冷却
    if state.is_negative_cooling(now):
        remain = int(state.negative_until - now)
        return False, f"用户此前表达负面情绪，冷却中（剩余 {remain}s）"

    # 免打扰时段（用户正在活跃则豁免）
    if in_quiet_hours(now_dt, eff.quiet_hours_start, eff.quiet_hours_end):
        active = (
            state.last_user_ts > 0
            and (now - state.last_user_ts)
            <= eff.quiet_active_window_minutes * 60
        )
        if not active:
            return False, (
                f"处于免打扰时段 {eff.quiet_hours_start:02d}:00-"
                f"{eff.quiet_hours_end:02d}:00 且用户不活跃"
            )

    # 沉默时长
    silence = now - state.last_user_ts
    if silence < eff.silence_threshold_minutes * 60:
        return False, (
            f"沉默 {int(silence // 60)} 分钟，未达到阈值 "
            f"{eff.silence_threshold_minutes} 分钟"
        )

    # 自说自话防护：Bot 最后一次发言晚于用户最后发言，且用户未回复
    # → 必须等用户回复（沉默检查已隐含等待）或超过单会话冷却的额外等待
    if state.last_bot_ts > state.last_user_ts:
        bot_alone = now - state.last_bot_ts
        if bot_alone < eff.session_cooldown_minutes * 60:
            return False, (
                f"Bot 已发言但用户未回复，需额外等待 "
                f"{eff.session_cooldown_minutes} 分钟（已等 {int(bot_alone // 60)} 分钟）"
            )

    # 全局冷却（跨会话）：用 state.last_global_proactive_ts，由调度器同步写入
    if state.last_global_proactive_ts > 0:
        since_global = now - state.last_global_proactive_ts
        if since_global < eff.cooldown_minutes * 60:
            return False, (
                f"全局冷却中（距上次主动 {int(since_global // 60)} 分钟，"
                f"要求 {eff.cooldown_minutes} 分钟）"
            )

    # 单会话冷却
    if state.last_proactive_ts > 0:
        since_session = now - state.last_proactive_ts
        if since_session < eff.session_cooldown_minutes * 60:
            return False, (
                f"会话冷却中（距上次主动 {int(since_session // 60)} 分钟，"
                f"要求 {eff.session_cooldown_minutes} 分钟）"
            )

    # 每日上限
    if state.today_count(date_str) >= eff.max_daily_proactive:
        return False, (
            f"已达每日主动上限 {eff.max_daily_proactive} 次"
            f"（今日 {state.today_count(date_str)} 次）"
        )

    return True, "全部硬性条件通过"
