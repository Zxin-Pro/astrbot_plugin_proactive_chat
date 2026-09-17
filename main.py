# -*- coding: utf-8 -*-
"""astrbot_plugin_proactive_chat —— 主动聊天插件主入口。

功能概览：
- 后台调度器：周期性扫描会话，按沉默时长 / 概率 / 冷却 / 免打扰 / 每日上限
  / 欲望值决定是否主动发起聊天；
- 消息监听：记录每个会话最后消息时间与最近对话，实现沉默检测与多会话隔离；
- LLM 决策与生成：先决策（输出 JSON），必要时兜底生成消息；
- 欲望驱动系统：可开关的内部"想聊天欲望值"；
- 中文管理命令：/主动聊天 状态|开启|关闭|测试|冷却|设置 ...|重置。

API 均已对照 AstrBot master 源码核对（2026-09-16），详见 adapter.py 顶部注释。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import (
    EventMessageType,
    after_message_sent,
    command,
    event_message_type,
)
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from .adapter import BotAdapter
from .core import (
    ContextBuilder,
    DesireEngine,
    EffectiveConfig,
    SessionStore,
    apply_desire_proactive,
    apply_desire_user_message,
    compute_desire,
    gate_check,
    roll_probability,
)
from .core.generator import MessageGenerator

# KV 存储键
_KV_SESSIONS = "proactive_chat_sessions"
_KV_GLOBAL_LAST = "proactive_chat_global_last_ts"

# 版本信息
_VERSION = "v1.1.0"

# WebUI 后端 API 使用的插件名前缀
PLUGIN_NAME = "astrbot_plugin_proactive_chat"

# "设置"子命令支持的项目 → 覆盖字段映射
_SET_KEY_MAP = {"间隔", "概率", "免打扰", "每日上限"}

_HELP_TEXT = (
    "主动聊天插件命令：\n"
    "/主动聊天 状态 —— 查看当前状态\n"
    "/主动聊天 开启|关闭 —— 开关本会话主动聊天\n"
    "/主动聊天 测试 —— 立即触发一次主动聊天（跳过门控）\n"
    "/主动聊天 冷却 —— 查看冷却剩余\n"
    "/主动聊天 设置 间隔 <分钟>\n"
    "/主动聊天 设置 概率 <0-1>\n"
    "/主动聊天 设置 免打扰 <开始>-<结束>（小时，如 23-8）\n"
    "/主动聊天 设置 每日上限 <次数>\n"
    "/主动聊天 重置 —— 清空本会话状态与覆盖"
)


@dataclass
class ProactiveContext:
    """一次主动聊天尝试所需的运行时打包（预留扩展）。"""

    umo: str
    eff: EffectiveConfig


class ProactiveChatPlugin(Star):
    """主动聊天插件（Star）。

    说明：本插件注册了一个 EventMessageType.ALL 监听器用于记录消息。
    已核对 WakingCheckStage / ProcessStage 源码：监听器命中只会置
    event.is_wake = True，而 ProcessStage 的 LLM 分支要求
    is_at_or_wake_command（@ / 唤醒前缀 / 私聊），因此本监听器
    不会导致普通群消息被意外送入 LLM，也不会拦截既有管线。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self.adapter = BotAdapter(context)

        # 会话状态容器（多会话隔离）
        negative_kw = list(self.config.get("negative_keywords") or [])
        self.store = SessionStore(negative_keywords=negative_kw)

        # 后台任务
        self._scheduler_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # 随机源
        self._rng = random.Random()
        # 全局最后主动时间戳（内存缓存，启动时从 KV 恢复）
        self._global_last_ts: float = 0.0
        # 持久化脏标记
        self._dirty = False

    # =================================================================
    # 生命周期
    # =================================================================
    async def initialize(self) -> None:
        """插件加载：恢复状态并启动后台调度任务。"""
        try:
            data = await self.get_kv_data(_KV_SESSIONS, default={})
            for umo, st in (data or {}).items():
                self.store.load(umo, st)
        except Exception:
            logger.exception("[proactive_chat] 恢复会话状态失败，使用空状态启动")
        try:
            self._global_last_ts = float(
                await self.get_kv_data(_KV_GLOBAL_LAST, default=0.0)
            )
        except Exception:
            self._global_last_ts = 0.0

        self._stop_event = asyncio.Event()
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="proactive-chat-scheduler"
        )
        self._register_web_apis()
        logger.info(
            "[proactive_chat] 插件已启动，跟踪 %d 个会话，调度间隔 %ds",
            len(self.store.known_umos()),
            int(self.config.get("check_interval_seconds", 300)),
        )

    async def terminate(self) -> None:
        """插件卸载：停止调度任务并持久化状态。"""
        self._stop_event.set()
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[proactive_chat] 停止调度任务时出现异常")
        await self._flush(force=True)
        logger.info("[proactive_chat] 插件已停止")

    # =================================================================
    # 持久化（PluginKVStoreMixin，异步）
    # =================================================================
    async def _flush(self, force: bool = False) -> None:
        if not (force or self._dirty):
            return
        try:
            # 欲望值统一结算为快照后再落盘
            now = time.time()
            if self.config.get("enable_desire_system", True):
                engine = DesireEngine(
                    increase_rate_per_hour=float(
                        self.config.get("desire_increase_rate", 0.08)
                    ),
                    decay_per_user_message=float(
                        self.config.get("desire_decay_rate", 0.25)
                    ),
                )
                for umo in self.store.known_umos():
                    engine.snapshot(self.store.get(umo), now)
            await self.put_kv_data(_KV_SESSIONS, self.store.dump())
            await self.put_kv_data(_KV_GLOBAL_LAST, self._global_last_ts)
            self._dirty = False
        except Exception:
            logger.exception("[proactive_chat] 持久化会话状态失败")

    # =================================================================
    # 消息监听（记录时间线，不回复、不拦截）
    # =================================================================
    @event_message_type(EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """记录所有用户消息，用于沉默检测 / 上下文 / 欲望更新。"""
        try:
            umo = event.unified_msg_origin
            if not umo:
                return
            text = (event.message_str or "").strip()
            if text:
                self.store.on_user_message(umo, text)
                eff = self._effective_config(umo)
                apply_desire_user_message(self.store.get(umo), eff, time.time())
                self._dirty = True
        except Exception:
            logger.exception("[proactive_chat] on_any_message 处理异常")

    @after_message_sent()
    async def on_bot_sent(self, event: AstrMessageEvent) -> None:
        """记录 Bot 发出的消息（含正常回复），用于自说自话防护。"""
        try:
            umo = event.unified_msg_origin
            if not umo:
                return
            text = ""
            result = event.get_result()
            if result is not None:
                try:
                    text = "".join(
                        getattr(c, "text", "") for c in result.chain if c is not None
                    )
                except Exception:
                    text = ""
            self.store.on_bot_message(umo, text)
            self._dirty = True
        except Exception:
            logger.exception("[proactive_chat] on_bot_sent 处理异常")

    # =================================================================
    # 配置辅助
    # =================================================================
    def _effective_config(self, umo: str) -> EffectiveConfig:
        state = self.store.get(umo)
        cfg = dict(self.config)
        return EffectiveConfig.from_global(
            cfg,
            overrides=state.overrides,
            session_enabled=state.enabled,
        )

    def _is_session_allowed(self, umo: str) -> bool:
        """白名单 / 黑名单过滤。

        - blacklist_sessions 命中 → 永不允许（支持前缀匹配平台段）；
        - target_sessions 非空 → 仅允许列表内的会话（白名单）。
        """
        blacklist = [str(x).strip() for x in (self.config.get("blacklist_sessions") or [])]
        if any(umo == b or umo.startswith(b) for b in blacklist if b):
            return False
        targets = [str(x).strip() for x in (self.config.get("target_sessions") or [])]
        if not targets:
            return True
        return any(umo == t or umo.startswith(t) for t in targets if t)

    def _active_umos(self) -> list[str]:
        """参与调度的会话：有用户消息记录 ∩ 白名单 − 黑名单。"""
        return [
            umo
            for umo in self.store.known_umos()
            if self.store.get(umo).last_user_ts > 0 and self._is_session_allowed(umo)
        ]

    # =================================================================
    # 后台调度器
    # =================================================================
    async def _scheduler_loop(self) -> None:
        """周期调度主循环。所有异常都被吞掉并记日志，绝不让循环崩掉。"""
        while not self._stop_event.is_set():
            interval = max(30, int(self.config.get("check_interval_seconds", 300)))
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[proactive_chat] 调度循环异常（将在下个周期重试）")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        """一轮调度：扫描所有候选会话，命中则尝试主动聊天。"""
        now = time.time()
        candidates: list[tuple[str, float]] = []  # (umo, desire)
        for umo in self._active_umos():
            state = self.store.get(umo)
            # 同步全局最后主动时间戳（门控的全局冷却需要）
            state.last_global_proactive_ts = max(
                state.last_global_proactive_ts, self._global_last_ts
            )
            eff = self._effective_config(umo)
            ok, reason = gate_check(eff, state, now=now)
            if not ok:
                logger.debug("[proactive_chat] 跳过 %s: %s", umo, reason)
                continue
            desire = compute_desire(state, eff, now)
            if not roll_probability(eff, desire, self._rng):
                logger.debug(
                    "[proactive_chat] 概率未命中 %s (desire=%.2f)", umo, desire
                )
                continue
            candidates.append((umo, desire))

        if candidates:
            # 每轮最多主动发起一次，避免同一周期轰炸多个会话
            umo, desire = candidates[0]
            logger.info(
                "[proactive_chat] 命中候选会话 %s (desire=%.2f)，开始 LLM 决策",
                umo,
                desire,
            )
            await self._try_proactive(umo, desire)
        await self._flush()

    # =================================================================
    # 主动聊天执行
    # =================================================================
    def _build_generator_for(self, umo: str) -> MessageGenerator:
        """构建绑定指定会话 Provider 的 LLM 生成器。"""

        async def llm_call(system_prompt: str, user_prompt: str) -> str | None:
            prov = await self.adapter.resolve_provider(
                umo, str(self.config.get("llm_provider_id") or "")
            )
            return await self.adapter.llm_text(prov, system_prompt, user_prompt)

        return MessageGenerator(
            llm_call=llm_call,
            prompt_override=str(self.config.get("prompt_override") or ""),
            desire_enabled=bool(self.config.get("enable_desire_system", True)),
        )

    async def _try_proactive(
        self, umo: str, desire: float, *, forced: bool = False
    ) -> bool:
        """对指定会话执行一次完整主动聊天（上下文 → LLM → 发送）。

        Args:
            desire: 当前欲望值（仅注入提示词，不影响门控）。
            forced: True 时跳过门控（管理命令"测试"），但仍走 LLM 决策。
        Returns:
            是否实际发送了消息。
        """
        state = self.store.get(umo)
        eff = self._effective_config(umo)
        builder = ContextBuilder(
            user_profile=str(self.config.get("user_profile") or ""),
            desire_enabled=bool(self.config.get("enable_desire_system", True)),
        )
        pack_text = builder.build(eff, state)
        generator = self._build_generator_for(umo)
        generator.set_desire(desire)

        decision = await generator.decide(pack_text)
        if decision is None:
            logger.info("[proactive_chat] %s: LLM 决策失败，跳过本次主动聊天", umo)
            return False
        if not decision.should_send:
            logger.info(
                "[proactive_chat] %s: LLM 判断暂不适合主动（原因: %s）",
                umo,
                decision.reason,
            )
            return False

        # 发送前再检查一次开关（防止管理命令竞态）
        if not forced and not eff.enable:
            return False

        ok = await self.adapter.send_text(umo, decision.message)
        if not ok:
            logger.warning("[proactive_chat] %s: 消息发送失败（无匹配平台）", umo)
            return False

        # 成功后结算：时间 / 每日计数 / 欲望衰减 / 全局时间戳
        now = time.time()
        state.record_proactive(decision.message, now)
        date_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if state.proactive_date != date_str:
            state.proactive_date = date_str
            state.proactive_count = 0
        state.proactive_count += 1
        apply_desire_proactive(state, eff, now)
        self._global_last_ts = now
        self._dirty = True
        await self._flush(force=True)
        logger.info(
            "[proactive_chat] %s: 已主动发送（今日第 %d 次）: %s",
            umo,
            state.proactive_count,
            decision.message[:50],
        )
        return True

    # =================================================================
    # 管理命令：/主动聊天 ...
    # =================================================================
    def _gate_admin(self, event: AstrMessageEvent) -> bool:
        """权限检查：admin_only_commands 开启时仅管理员可用。"""
        if bool(self.config.get("admin_only_commands", True)):
            return getattr(event, "role", "member") == "admin"
        return True

    @command("主动聊天")
    async def proactive_command(
        self, event: AstrMessageEvent, sub: str = "", a: str = "", b: str = ""
    ) -> None:
        """主动聊天管理命令。

        子命令：状态 / 开启 / 关闭 / 测试 / 冷却 / 设置 / 重置
        """
        if not self._gate_admin(event):
            # 权限不足时静默忽略，不暴露插件存在
            return
        try:
            umo = event.unified_msg_origin
            sub = (sub or "").strip()
            a = (a or "").strip()
            b = (b or "").strip()

            if sub == "状态":
                reply = await self._cmd_status(umo)
            elif sub == "开启":
                reply = await self._cmd_toggle(umo, True)
            elif sub == "关闭":
                reply = await self._cmd_toggle(umo, False)
            elif sub == "测试":
                reply = await self._cmd_test(umo)
            elif sub == "冷却":
                reply = await self._cmd_cooldown(umo)
            elif sub == "设置":
                reply = await self._cmd_setting(umo, a, b)
            elif sub == "重置":
                reply = await self._cmd_reset(umo)
            else:
                reply = _HELP_TEXT
            yield event.plain_result(reply)
        except Exception:
            logger.exception("[proactive_chat] 管理命令执行异常")
            yield event.plain_result("主动聊天命令执行出错，请查看后台日志")

    # ---------------- 各子命令实现（返回回复文本） ----------------
    async def _cmd_status(self, umo: str) -> str:
        eff = self._effective_config(umo)
        state = self.store.get(umo)
        now = time.time()
        silence = (
            "无记录" if state.last_user_ts <= 0 else _fmt_minutes(now - state.last_user_ts)
        )
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [
            f"全局开关: {'开' if self.config.get('enable') else '关'}",
            f"本会话开关: {'跟随全局' if state.enabled is None else ('开' if state.enabled else '关')}",
            f"本会话是否允许: {'是' if self._is_session_allowed(umo) else '否（黑名单或不在白名单）'}",
            f"沉默时长: {silence}（阈值 {eff.silence_threshold_minutes} 分钟）",
            f"基础概率: {eff.proactive_probability}  欲望值: {compute_desire(state, eff, now):.2f}",
            f"今日已主动: {state.today_count(today)}/{eff.max_daily_proactive} 次",
            f"免打扰: {eff.quiet_hours_start:02d}:00-{eff.quiet_hours_end:02d}:00",
            f"跟踪会话总数: {len(self.store.known_umos())}",
            f"上次主动: {_fmt_minutes(now - state.last_proactive_ts) if state.last_proactive_ts else '从未'}",
        ]
        if state.last_proactive_text:
            lines.append(f"上次主动内容: {state.last_proactive_text[:60]}")
        return "\n".join(lines)

    async def _cmd_toggle(self, umo: str, on: bool) -> str:
        state = self.store.get(umo)
        state.enabled = on
        self._dirty = True
        await self._flush(force=True)
        return f"本会话主动聊天已{'开启' if on else '关闭'}"

    async def _cmd_test(self, umo: str) -> str:
        state = self.store.get(umo)
        eff = self._effective_config(umo)
        desire = compute_desire(state, eff, time.time())
        ok = await self._try_proactive(umo, desire, forced=True)
        if ok:
            return "测试完成：主动消息已发送 ↑"
        return "测试未发送：LLM 判断现在不适合 / 生成失败 / 平台不可达，详见后台日志"

    async def _cmd_cooldown(self, umo: str) -> str:
        eff = self._effective_config(umo)
        state = self.store.get(umo)
        now = time.time()
        global_remain = (
            max(0, int(eff.cooldown_minutes * 60 - (now - self._global_last_ts))) // 60
            if self._global_last_ts
            else 0
        )
        session_remain = (
            max(
                0,
                int(eff.session_cooldown_minutes * 60 - (now - state.last_proactive_ts)),
            )
            // 60
            if state.last_proactive_ts
            else 0
        )
        negative_remain = (
            max(0, int(state.negative_until - now)) // 60 if state.negative_until else 0
        )
        return (
            f"全局冷却剩余: {global_remain} 分钟\n"
            f"本会话冷却剩余: {session_remain} 分钟\n"
            f"负面情绪冷却剩余: {negative_remain} 分钟"
        )

    async def _cmd_setting(self, umo: str, key: str, value: str) -> str:
        if key not in _SET_KEY_MAP:
            return "可设置项: 间隔 / 概率 / 免打扰 / 每日上限\n示例: /主动聊天 设置 概率 0.5"
        state = self.store.get(umo)

        if key == "免打扰":
            start, end = _parse_hour_range(value)
            if start is None:
                return "格式错误，示例: /主动聊天 设置 免打扰 23-8"
            state.overrides["quiet_hours_start"] = start
            state.overrides["quiet_hours_end"] = end
            reply = f"本会话免打扰时段已设为 {start}:00-{end}:00"
        elif key == "间隔":
            minutes = _parse_int(value, 1, 1440)
            if minutes is None:
                return "格式错误，示例: /主动聊天 设置 间隔 90"
            state.overrides["silence_threshold_minutes"] = minutes
            reply = f"本会话沉默阈值已设为 {minutes} 分钟"
        elif key == "概率":
            try:
                p = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                return "格式错误，示例: /主动聊天 设置 概率 0.5"
            state.overrides["proactive_probability"] = p
            reply = f"本会话基础概率已设为 {p}"
        else:  # 每日上限
            times = _parse_int(value, 0, 100)
            if times is None:
                return "格式错误，示例: /主动聊天 设置 每日上限 3"
            state.overrides["max_daily_proactive"] = times
            reply = f"本会话每日上限已设为 {times} 次"

        self._dirty = True
        await self._flush(force=True)
        return reply

    async def _cmd_reset(self, umo: str) -> str:
        self.store.remove(umo)
        self._dirty = True
        await self._flush(force=True)
        return "本会话主动聊天状态与覆盖配置已重置"

    # =================================================================
    # WebUI 插件页面后端 API
    # 路由约定：必须带插件名前缀（官方 plugin-pages 文档）。
    # 页面内通过 bridge.apiGet("overview") 访问，Dashboard 自动拼接前缀。
    # =================================================================
    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[proactive_chat] 当前 AstrBot 版本不支持 register_web_api，WebUI 页面不可用"
            )
            return
        try:
            register(
                f"/{PLUGIN_NAME}/overview",
                self.api_overview,
                ["GET"],
                "主动聊天总览数据",
            )
            register(
                f"/{PLUGIN_NAME}/session",
                self.api_session_action,
                ["POST"],
                "主动聊天会话操作（开关/测试/重置）",
            )
            register(
                f"/{PLUGIN_NAME}/settings/save",
                self.api_save_settings,
                ["POST"],
                "保存主动聊天全局设置",
            )
        except Exception:
            logger.exception("[proactive_chat] 注册 WebUI API 失败")

    # ---------------- GET /overview：总览数据 ----------------
    async def api_overview(self):
        try:
            now = time.time()
            date_str = datetime.now().strftime("%Y-%m-%d")
            sessions = []
            for umo in sorted(self.store.known_umos()):
                state = self.store.get(umo)
                eff = self._effective_config(umo)
                sessions.append(
                    {
                        "umo": umo,
                        "enabled": state.enabled,
                        "allowed": self._is_session_allowed(umo),
                        "silence_minutes": (
                            int((now - state.last_user_ts) // 60)
                            if state.last_user_ts
                            else None
                        ),
                        "silence_threshold": eff.silence_threshold_minutes,
                        "desire": round(compute_desire(state, eff, now), 3),
                        "probability": eff.proactive_probability,
                        "today_count": state.today_count(date_str),
                        "max_daily": eff.max_daily_proactive,
                        "last_proactive_ts": state.last_proactive_ts,
                        "last_proactive_text": state.last_proactive_text[:80],
                        "negative_cooling": state.is_negative_cooling(now),
                        "quiet_hours": [eff.quiet_hours_start, eff.quiet_hours_end],
                    }
                )
            global_remain_min = (
                max(0, int((now - self._global_last_ts) // 60))
                if self._global_last_ts
                else None
            )
            return json_response(
                {
                    "enable": bool(self.config.get("enable", True)),
                    "desire_enabled": bool(
                        self.config.get("enable_desire_system", True)
                    ),
                    "check_interval_seconds": int(
                        self.config.get("check_interval_seconds", 300)
                    ),
                    "global_cooldown_minutes": int(
                        self.config.get("cooldown_minutes", 120)
                    ),
                    "global_last_proactive_ts": self._global_last_ts,
                    "global_cooldown_passed_min": global_remain_min,
                    "tracked_sessions": len(sessions),
                    "sessions": sessions,
                    "settings": {
                        "enable": bool(self.config.get("enable", True)),
                        "enable_desire_system": bool(
                            self.config.get("enable_desire_system", True)
                        ),
                        "check_interval_seconds": int(
                            self.config.get("check_interval_seconds", 300)
                        ),
                        "silence_threshold_minutes": int(
                            self.config.get("silence_threshold_minutes", 60)
                        ),
                        "proactive_probability": float(
                            self.config.get("proactive_probability", 0.3)
                        ),
                        "max_daily_proactive": int(
                            self.config.get("max_daily_proactive", 3)
                        ),
                        "cooldown_minutes": int(self.config.get("cooldown_minutes", 120)),
                        "session_cooldown_minutes": int(
                            self.config.get("session_cooldown_minutes", 240)
                        ),
                        "quiet_hours_start": int(
                            self.config.get("quiet_hours_start", 23)
                        ),
                        "quiet_hours_end": int(self.config.get("quiet_hours_end", 8)),
                        "desire_increase_rate": float(
                            self.config.get("desire_increase_rate", 0.08)
                        ),
                        "desire_decay_rate": float(
                            self.config.get("desire_decay_rate", 0.25)
                        ),
                    },
                }
            )
        except Exception:
            logger.exception("[proactive_chat] WebUI overview 接口异常")
            return error_response("获取总览数据失败", status_code=500)

    # ---------------- POST /session：会话操作 ----------------
    async def api_session_action(self):
        try:
            payload = await request.json(default={})
            umo = str(payload.get("umo") or "").strip()
            action = str(payload.get("action") or "").strip()
            if not umo:
                return error_response("缺少 umo 参数", status_code=400)

            if action in ("enable", "disable", "auto"):
                state = self.store.get(umo)
                state.enabled = (
                    True if action == "enable" else False if action == "disable" else None
                )
                self._dirty = True
                await self._flush(force=True)
                return json_response({"ok": True, "action": action, "umo": umo})

            if action == "reset":
                self.store.remove(umo)
                self._dirty = True
                await self._flush(force=True)
                return json_response({"ok": True, "action": "reset", "umo": umo})

            if action == "test":
                state = self.store.get(umo)
                eff = self._effective_config(umo)
                desire = compute_desire(state, eff, time.time())
                sent = await self._try_proactive(umo, desire, forced=True)
                return json_response(
                    {"ok": True, "action": "test", "umo": umo, "sent": sent}
                )

            return error_response(f"未知操作: {action}", status_code=400)
        except Exception:
            logger.exception("[proactive_chat] WebUI session 接口异常")
            return error_response("会话操作失败", status_code=500)

    # ---------------- POST /settings/save：保存全局设置 ----------------
    _ALLOWED_INT_SETTINGS = {
        "check_interval_seconds": (30, 86400),
        "silence_threshold_minutes": (1, 10080),
        "max_daily_proactive": (0, 100),
        "cooldown_minutes": (0, 10080),
        "session_cooldown_minutes": (0, 20160),
        "quiet_hours_start": (0, 23),
        "quiet_hours_end": (0, 23),
        "quiet_active_window_minutes": (0, 120),
    }
    _ALLOWED_FLOAT_SETTINGS = {
        "proactive_probability": (0.0, 1.0),
        "desire_increase_rate": (0.0, 1.0),
        "desire_decay_rate": (0.0, 1.0),
    }
    _ALLOWED_BOOL_SETTINGS = {
        "enable",
        "enable_desire_system",
        "admin_only_commands",
    }

    async def api_save_settings(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)

            applied: dict[str, Any] = {}
            for key, value in payload.items():
                if key in self._ALLOWED_INT_SETTINGS:
                    lo, hi = self._ALLOWED_INT_SETTINGS[key]
                    try:
                        v = int(value)
                    except (TypeError, ValueError):
                        return error_response(f"{key} 必须是整数", status_code=400)
                    if not lo <= v <= hi:
                        return error_response(
                            f"{key} 取值范围 {lo}~{hi}", status_code=400
                        )
                    self.config[key] = v
                    applied[key] = v
                elif key in self._ALLOWED_FLOAT_SETTINGS:
                    lo, hi = self._ALLOWED_FLOAT_SETTINGS[key]
                    try:
                        v = float(value)
                    except (TypeError, ValueError):
                        return error_response(f"{key} 必须是数字", status_code=400)
                    if not lo <= v <= hi:
                        return error_response(
                            f"{key} 取值范围 {lo}~{hi}", status_code=400
                        )
                    self.config[key] = v
                    applied[key] = v
                elif key in self._ALLOWED_BOOL_SETTINGS:
                    self.config[key] = bool(value)
                    applied[key] = bool(value)
                else:
                    return error_response(f"不支持的配置项: {key}", status_code=400)

            try:
                await self.config.save_config_async()
            except Exception:
                self.config.save_config()
            logger.info("[proactive_chat] WebUI 已保存配置: %s", applied)
            return json_response({"saved": True, "applied": applied})
        except Exception:
            logger.exception("[proactive_chat] WebUI settings 接口异常")
            return error_response("保存设置失败", status_code=500)


# =====================================================================
# 工具函数
# =====================================================================
def _fmt_minutes(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} 分钟"
    return f"{m // 60} 小时 {m % 60} 分钟"


def _parse_int(value: str, lo: int, hi: int) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def _parse_hour_range(value: str) -> tuple[int | None, int | None]:
    """解析 "23-8" / "23-08" / "23:00-08:00" 形式的小时区间。

    Returns:
        (起始小时, 结束小时)；解析失败返回 (None, None)。
    """
    if not value:
        return None, None
    text = str(value).replace("：", ":").replace("～", "-").replace("~", "-")
    try:
        for sep in ("-", "到"):
            if sep in text:
                raw_start, raw_end = text.split(sep, 1)
                start = _parse_hour(raw_start)
                end = _parse_hour(raw_end)
                if start is not None and end is not None:
                    return start, end
                return None, None
    except Exception:
        pass
    return None, None


def _parse_hour(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    if ":" in text:
        text = text.split(":", 1)[0]
    try:
        h = int(text)
    except ValueError:
        return None
    return h if 0 <= h <= 23 else None
