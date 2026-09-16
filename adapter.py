# -*- coding: utf-8 -*-
"""AstrBot 框架适配层。

所有与 AstrBot 框架耦合的调用统一收敛到这里，核心逻辑（core/）不直接依赖框架。
若 AstrBot 版本升级导致接口变化，只需修改本文件。

API 核对情况（对照 AstrBot master 分支源码，核对日期 2026-09-16）：
- context.send_message(session: str | MessageSession, message_chain: MessageChain) -> bool
  —— 位于 astrbot/core/star/context.py，支持通过 unified_msg_origin 字符串主动发消息。
- context.get_provider_by_id(provider_id) -> Provider | None
  —— 位于 astrbot/core/star/context.py。
- context.get_using_provider_async(umo: str) -> Provider | None
  —— 位于 astrbot/core/star/context.py（会话隔离的提供商偏好）。
- provider.text_chat(prompt=..., session_id=..., system_prompt=...) -> LLMResponse
  —— 位于 astrbot/core/provider/provider.py；LLMResponse.completion_text 为纯文本结果。
- MessageChain().message(text)
  —— 位于 astrbot/core/message/message_event_result.py。

TODO(确认项)：以上接口在 AstrBot >= 3.5.x 稳定；若你使用的是 v4 之后的版本，
请重点确认 send_message 与 get_using_provider_async 的签名是否仍然一致。
"""

from __future__ import annotations

import logging
from typing import Any

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import MessageChain

logger = astrbot_logger


class BotAdapter:
    """封装 Context 的发送与 LLM 调用能力。"""

    def __init__(self, context: Any) -> None:
        self.context = context

    # -----------------------------------------------------------
    # 发送
    # -----------------------------------------------------------
    async def send_text(self, umo: str, text: str) -> bool:
        """向指定会话（unified_msg_origin）发送纯文本。

        Returns:
            是否成功找到平台并发送。
        """
        try:
            chain = MessageChain().message(text)
            return bool(await self.context.send_message(umo, chain))
        except Exception:
            logger.exception("[proactive_chat] 发送消息失败: umo=%s", umo)
            return False

    # -----------------------------------------------------------
    # Provider 解析
    # -----------------------------------------------------------
    async def resolve_provider(self, umo: str, provider_id: str = "") -> Any | None:
        """解析用于主动聊天的 LLM Provider。

        优先使用配置的 llm_provider_id；为空则回退到该会话当前使用的 Provider。
        """
        try:
            if provider_id:
                prov = self.context.get_provider_by_id(provider_id)
                if prov is None:
                    logger.warning(
                        "[proactive_chat] 配置的 llm_provider_id=%s 未找到，回退到会话默认 Provider",
                        provider_id,
                    )
                    return await self.context.get_using_provider_async(umo)
                return prov
            return await self.context.get_using_provider_async(umo)
        except Exception:
            logger.exception("[proactive_chat] 解析 Provider 失败: umo=%s", umo)
            return None

    # -----------------------------------------------------------
    # LLM 文本调用
    # -----------------------------------------------------------
    async def llm_text(
        self,
        provider: Any,
        system_prompt: str,
        user_prompt: str,
    ) -> str | None:
        """调用 Provider 生成纯文本。失败返回 None（不抛出）。

        注意：session_id 参数在当前版本已标记为废弃，但仍接受传入，
        用于给 LLM 网关一个稳定的伪会话 ID，避免与用户会话上下文互相污染。
        """
        if provider is None:
            logger.warning("[proactive_chat] 没有可用的 LLM Provider")
            return None
        try:
            resp = await provider.text_chat(
                prompt=user_prompt,
                session_id="astrbot_plugin_proactive_chat",
                system_prompt=system_prompt,
            )
            text = getattr(resp, "completion_text", None)
            return (text or "").strip() or None
        except Exception:
            logger.exception("[proactive_chat] LLM 调用失败")
            return None
