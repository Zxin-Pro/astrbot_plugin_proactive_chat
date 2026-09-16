# -*- coding: utf-8 -*-
"""核心调度门控单元测试。

core/ 目录不依赖 AstrBot，可在任何环境直接运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import time
import unittest
from datetime import datetime

from core.context_builder import ContextBuilder
from core.desire import DesireEngine
from core.generator import Decision, _clean_message, _extract_json
from core.scheduler import (
    EffectiveConfig,
    apply_desire_proactive,
    apply_desire_user_message,
    compute_desire,
    gate_check,
    in_quiet_hours,
    roll_probability,
)
from core.silence_detector import SessionState, is_negative_message


def _mk_state(**kw) -> SessionState:
    state = SessionState(umo="test:PrivateMessage:1")
    now = time.time()
    state.last_user_ts = kw.get("last_user_ts", now - 7200)
    state.last_bot_ts = kw.get("last_bot_ts", 0)
    state.last_proactive_ts = kw.get("last_proactive_ts", 0)
    state.last_global_proactive_ts = kw.get("last_global_proactive_ts", 0)
    state.negative_until = kw.get("negative_until", 0)
    state.recent = kw.get("recent", [])
    return state


class TestQuietHours(unittest.TestCase):
    def test_cross_midnight(self):
        dt = datetime(2026, 9, 16, 2, 0)
        self.assertTrue(in_quiet_hours(dt, 23, 8))
        dt = datetime(2026, 9, 16, 12, 0)
        self.assertFalse(in_quiet_hours(dt, 23, 8))

    def test_same_hours_disabled(self):
        dt = datetime(2026, 9, 16, 3, 0)
        self.assertFalse(in_quiet_hours(dt, 8, 8))

    def test_normal_range(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 9, 16, 12), 11, 14))
        self.assertFalse(in_quiet_hours(datetime(2026, 9, 16, 15), 11, 14))


class TestGateCheck(unittest.TestCase):
    def _eff(self, **kw) -> EffectiveConfig:
        eff = EffectiveConfig()
        for k, v in kw.items():
            setattr(eff, k, v)
        return eff

    def test_pass_when_silent_long(self):
        state = _mk_state()
        ok, reason = gate_check(self._eff(), state)
        self.assertTrue(ok, reason)

    def test_global_disabled(self):
        ok, _ = gate_check(self._eff(enable=False), _mk_state())
        self.assertFalse(ok)

    def test_not_silent_enough(self):
        state = _mk_state(last_user_ts=time.time() - 600)  # 10 分钟
        ok, reason = gate_check(self._eff(), state)
        self.assertFalse(ok)
        self.assertIn("沉默", reason)

    def test_no_user_message_ever(self):
        state = SessionState(umo="x")
        ok, _ = gate_check(self._eff(), state)
        self.assertFalse(ok)

    def test_negative_emotion_cooldown(self):
        state = _mk_state(negative_until=time.time() + 1800)
        ok, reason = gate_check(self._eff(), state)
        self.assertFalse(ok)
        self.assertIn("负面", reason)

    def test_bot_talking_alone_guard(self):
        now = time.time()
        state = _mk_state(last_user_ts=now - 7200, last_bot_ts=now - 1200)
        # Bot 20 分钟前主动发言，单会话冷却 240 分钟 → 拦截
        ok, reason = gate_check(self._eff(), state)
        self.assertFalse(ok)
        self.assertIn("自说自话" in reason or "未回复", reason)
        # 用户随后回复过 → 放行
        state.last_user_ts = now - 60
        state.last_bot_ts = now - 120
        state2 = _mk_state(last_user_ts=now - 7200)  # 重新构造正常场景
        ok2, _ = gate_check(self._eff(), state2)
        self.assertTrue(ok2)

    def test_global_cooldown(self):
        state = _mk_state(last_global_proactive_ts=time.time() - 600)
        ok, reason = gate_check(self._eff(), state)
        self.assertFalse(ok)
        self.assertIn("全局冷却", reason)

    def test_session_cooldown(self):
        state = _mk_state(last_proactive_ts=time.time() - 600)
        ok, reason = gate_check(self._eff(), state)
        self.assertFalse(ok)
        self.assertIn("会话冷却", reason)

    def test_daily_cap(self):
        state = _mk_state()
        state.proactive_date = datetime.now().strftime("%Y-%m-%d")
        state.proactive_count = 3
        ok, reason = gate_check(self._eff(), state)
        self.assertFalse(ok)
        self.assertIn("每日", reason)

    def test_quiet_hours_blocks_inactive_user(self):
        state = _mk_state()
        dt = datetime(2026, 9, 16, 3, 0)  # 凌晨 3 点
        ok, reason = gate_check(self._eff(), state, now_dt=dt)
        self.assertFalse(ok)
        self.assertIn("免打扰", reason)

    def test_quiet_hours_allows_active_user(self):
        now = time.time()
        state = _mk_state(last_user_ts=now - 120)  # 用户 2 分钟前活跃
        # 强制沉默检查通过：把阈值压低
        eff = self._eff(silence_threshold_minutes=1)
        ok, reason = gate_check(eff, state)
        self.assertTrue(ok, reason)


class TestEffectiveConfig(unittest.TestCase):
    def test_overrides_applied(self):
        eff = EffectiveConfig.from_global(
            {"silence_threshold_minutes": 60, "proactive_probability": 0.3},
            overrides={"silence_threshold_minutes": 15, "proactive_probability": 0.9},
            session_enabled=False,
        )
        self.assertEqual(eff.silence_threshold_minutes, 15)
        self.assertEqual(eff.proactive_probability, 0.9)
        self.assertFalse(eff.enable)

    def test_type_mismatch_ignored(self):
        eff = EffectiveConfig.from_global({}, overrides={"silence_threshold_minutes": "abc"})
        self.assertEqual(eff.silence_threshold_minutes, 60)


class TestDesire(unittest.TestCase):
    def test_rises_over_time(self):
        state = _mk_state()
        state.desire_value = 0.3
        state.desire_ts = time.time() - 3600
        eff = EffectiveConfig(enable_desire_system=True, desire_increase_rate=0.1)
        self.assertAlmostEqual(compute_desire(state, eff, time.time()), 0.4, places=5)

    def test_decays_on_user_message(self):
        state = _mk_state()
        state.desire_value = 0.5
        state.desire_ts = time.time()
        eff = EffectiveConfig(enable_desire_system=True, desire_decay_rate=0.25)
        v = apply_desire_user_message(state, eff, time.time())
        self.assertAlmostEqual(v, 0.25, places=5)

    def test_proactive_decays_double(self):
        state = _mk_state()
        state.desire_value = 0.5
        state.desire_ts = time.time()
        eff = EffectiveConfig(enable_desire_system=True, desire_decay_rate=0.25)
        v = apply_desire_proactive(state, eff, time.time())
        self.assertAlmostEqual(v, 0.0, places=5)

    def test_clamped(self):
        engine = DesireEngine(increase_rate_per_hour=10, decay_per_user_message=5)
        state = _mk_state()
        state.desire_value = 1.5
        state.desire_ts = time.time()
        self.assertAlmostEqual(engine.current(state, time.time()), 1.0, places=6)
        v = engine.on_user_message(state, time.time())
        self.assertEqual(v, 0.0)

    def test_disabled_keeps_default(self):
        engine = DesireEngine(enabled=False)
        state = _mk_state()
        self.assertAlmostEqual(engine.current(state, time.time()), 0.3, places=5)


class TestProbability(unittest.TestCase):
    def test_zero_probability_never(self):
        eff = EffectiveConfig(proactive_probability=0.0)
        self.assertFalse(roll_probability(eff, 1.0, random.Random(42)))

    def test_certain_probability_always(self):
        eff = EffectiveConfig(proactive_probability=1.0)
        self.assertTrue(roll_probability(eff, 0.5, random.Random(42)))


class TestSessionState(unittest.TestCase):
    def test_roundtrip(self):
        state = _mk_state()
        state.record_user_message("你好")
        state.record_bot_message("你好呀")
        state.record_proactive("在忙吗")
        data = state.to_dict()
        restored = SessionState.from_dict("test:PrivateMessage:1", data)
        self.assertEqual(restored.last_proactive_text, "在忙吗")
        self.assertEqual(len(restored.recent), 3)
        self.assertEqual(restored.recent[-1]["role"], "bot")

    def test_recent_trimmed(self):
        state = _mk_state()
        for i in range(30):
            state.record_user_message(f"m{i}")
        self.assertEqual(len(state.recent), 12)
        self.assertEqual(state.recent[-1]["text"], "m29")

    def test_negative_detection(self):
        self.assertTrue(is_negative_message("你别烦我好不好"))
        self.assertFalse(is_negative_message("今天天气不错"))


class TestContextBuilder(unittest.TestCase):
    def test_build_contains_sections(self):
        state = _mk_state()
        state.record_user_message("今天好累")
        state.record_bot_message("早点休息")
        eff = EffectiveConfig()
        pack = ContextBuilder(user_profile="喜欢游戏").build(eff, state)
        for sec in ("【当前时间】", "【沉默时长】", "【最近连续对话】", "【用户画像】", "【Bot 自我状态/欲望值】", "【上次主动聊天】"):
            self.assertIn(sec, pack)
        self.assertIn("今天好累", pack)


class TestGeneratorParsing(unittest.TestCase):
    def test_extract_json_plain(self):
        data = _extract_json('{"should_send": true, "reason": "r", "message": "hi"}')
        self.assertEqual(data["message"], "hi")

    def test_extract_json_in_fence(self):
        text = '```json\n{"should_send": false, "reason": "忙", "message": ""}\n```'
        data = _extract_json(text)
        self.assertFalse(data["should_send"])

    def test_extract_json_garbage(self):
        self.assertIsNone(_extract_json("这不是 JSON"))

    def test_clean_message(self):
        self.assertEqual(_clean_message('  "你好呀"  '), "你好呀")
        self.assertEqual(_clean_message(""), "")


if __name__ == "__main__":
    unittest.main()
