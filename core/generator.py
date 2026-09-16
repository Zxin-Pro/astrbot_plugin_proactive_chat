# -*- coding: utf-8 -*-
"""主动消息生成器：LLM 决策 + 消息生成。

本模块不直接 import AstrBot：LLM 调用通过注入的异步回调完成，
由顶层 adapter.py 提供回调实现，从而保证核心逻辑可测试、可替换。

流程：
1. decide(): 用决策提示词让 LLM 输出 JSON {should_send, reason, message}；
2. 若 should_send=true 但 message 为空 → 用生成提示词兜底再生成一次；
3. 失败 / 为空时返回 None，由调度器跳过本次主动聊天并记日志。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from .prompts import CORE_PERSONA_PROMPT, DECISION_PROMPT, DESIRE_PROMPT_TEMPLATE, GENERATION_PROMPT

logger = logging.getLogger("astrbot.plugin.proactive_chat")

# LLM 回调类型：(system_prompt, user_prompt) -> 文本；失败或为空返回 None
LlmCaller = Callable[[str, str], Awaitable[str | None]]

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Decision:
    """一次主动聊天决策的结果。"""

    should_send: bool
    reason: str = ""
    message: str = ""


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中鲁棒地提取第一个 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    # 去掉 markdown 代码块围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _clean_message(text: str | None) -> str:
    """清理生成结果：去引号、去首尾空白、截断超长。"""
    if not text:
        return ""
    text = text.strip().strip("\"'“”「」『』`")
    # 多行 → 取第一段
    text = text.split("\n")[0].strip()
    return text[:200]


def build_system_prompt(prompt_override: str, desire: float, desire_enabled: bool) -> str:
    """组装 system_prompt：核心人格 + 欲望感知（可选）+ 用户自定义覆盖。"""
    parts = [CORE_PERSONA_PROMPT]
    if desire_enabled:
        parts.append(DESIRE_PROMPT_TEMPLATE.format(desire=desire))
    if prompt_override and prompt_override.strip():
        parts.append(prompt_override.strip())
    return "\n\n".join(parts)


class MessageGenerator:
    """基于 LLM 的主动消息决策与生成器。"""

    def __init__(
        self,
        llm_call: LlmCaller,
        prompt_override: str = "",
        desire_enabled: bool = True,
        max_retries: int = 1,
    ) -> None:
        """
        Args:
            llm_call: 异步 LLM 调用回调（由 adapter 注入）。
            prompt_override: 用户自定义提示词覆盖（追加到人格提示词后）。
            desire_enabled: 是否注入欲望值感知。
            max_retries: JSON 解析失败时的重试次数。
        """
        self.llm_call = llm_call
        self.prompt_override = prompt_override or ""
        self.desire_enabled = bool(desire_enabled)
        self.max_retries = max(0, int(max_retries))

    # -----------------------------------------------------------
    async def _call_decision(self, context_pack: str) -> Decision | None:
        system_prompt = build_system_prompt(
            self.prompt_override, self._last_desire, self.desire_enabled
        )
        raw = await self.llm_call(system_prompt, f"{DECISION_PROMPT}\n\n{context_pack}")
        if not raw:
            return None
        data = _extract_json(raw)
        if data is None:
            logger.warning("[proactive_chat] 决策输出无法解析为 JSON: %.200s", raw)
            return None
        should = bool(data.get("should_send", False))
        reason = str(data.get("reason", "")).strip()
        message = _clean_message(str(data.get("message", "") or ""))
        return Decision(should_send=should, reason=reason, message=message)

    # 由调度器在调用前设置的欲望值快照（避免每个方法都传参）
    _last_desire: float = 0.5

    def set_desire(self, desire: float) -> None:
        self._last_desire = float(desire)

    # -----------------------------------------------------------
    async def _call_generate(self, context_pack: str) -> str:
        system_prompt = build_system_prompt(
            self.prompt_override, self._last_desire, self.desire_enabled
        )
        raw = await self.llm_call(
            system_prompt, f"{GENERATION_PROMPT}\n\n{context_pack}"
        )
        return _clean_message(raw or "")

    # -----------------------------------------------------------
    async def decide(self, context_pack: str) -> Decision | None:
        """执行一次完整决策。返回 None 表示 LLM 调用失败（跳过本次）。"""
        for attempt in range(self.max_retries + 1):
            try:
                decision = await self._call_decision(context_pack)
            except Exception:
                logger.exception("[proactive_chat] 决策 LLM 调用异常（第 %d 次）", attempt + 1)
                decision = None
            if decision is not None:
                # 决策通过但消息为空 → 兜底生成
                if decision.should_send and not decision.message:
                    try:
                        msg = await self._call_generate(context_pack)
                    except Exception:
                        logger.exception("[proactive_chat] 生成 LLM 调用异常")
                        msg = ""
                    if not msg:
                        logger.info("[proactive_chat] 决策通过但消息生成为空，跳过本次")
                        return None
                    decision.message = msg
                return decision
        return None
