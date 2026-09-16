# astrbot_plugin_proactive_chat

让 AstrBot 不再永远被动等待：在合适的时机、以自然的方式主动找用户聊天。

## 功能特性

- **主动触发调度**：后台异步任务按 `check_interval_seconds` 周期扫描所有会话，绝不阻塞消息管线
- **沉默检测**：记录每个会话最后用户消息 / Bot 消息时间，沉默超过阈值才进入候选；Bot 刚发过消息而用户未回复时，强制等待额外冷却，避免自说自话
- **上下文感知**：主动前构建上下文包（当前时间 / 沉默时长 / 最近对话 / 用户画像 / 欲望值 / 上次主动内容）交给 LLM 决策"现在适不适合说话"
- **自然消息生成**：决策 + 生成两级 LLM 流程，消息短、自然、不查户口、不重复；失败自动跳过并记日志
- **欲望驱动系统**（可开关）：0~1 内部欲望值，随时间上升、用户回复 / 主动聊天后下降，欲望越高触发概率越高
- **冷却与免打扰**：全局冷却 + 单会话冷却 + 每日上限 + 免打扰时段（用户活跃可豁免）+ 负面情绪延长冷却
- **多会话隔离**：每个 unified_msg_origin 一份独立状态，持久化到插件 KV 存储
- **中文管理命令** + WebUI 可视化配置

## 安装

1. 将本目录放入 AstrBot 的 `data/plugins/astrbot_plugin_proactive_chat/`
2. 重启 AstrBot 或在 WebUI 插件管理中重载
3. WebUI → 插件 → 主动聊天 → 配置，按需调整参数

依赖：仅 Python 3.10+ 标准库 + AstrBot 自带能力，无第三方依赖。

## 前置要求

- 已配置至少一个 LLM 提供商（OpenAI 兼容接口等）
- 私聊默认即会唤醒 AstrBot；群聊中插件通过 @ 或唤醒词收到的消息来"感知"用户活跃

## 配置说明（WebUI 可视化）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| enable | true | 全局总开关 |
| check_interval_seconds | 300 | 调度检查间隔（秒） |
| silence_threshold_minutes | 60 | 沉默阈值（分钟） |
| proactive_probability | 0.3 | 基础触发概率，实际 = 基础 × (0.5 + 欲望值) |
| max_daily_proactive | 3 | 每会话每日主动上限 |
| cooldown_minutes | 120 | 全局冷却（分钟，跨会话） |
| session_cooldown_minutes | 240 | 单会话冷却（分钟），兼作自说自话防护等待 |
| quiet_hours_start / end | 23 / 8 | 免打扰时段（小时），相同值视为不启用 |
| quiet_active_window_minutes | 10 | 免打扰豁免：用户最近 N 分钟活跃则允许 |
| enable_desire_system | true | 欲望驱动开关 |
| desire_increase_rate | 0.08 | 欲望每小时上升量 |
| desire_decay_rate | 0.25 | 用户每次回复欲望下降量（主动后 ×2） |
| llm_provider_id | 空 | 指定 LLM 提供商 ID，留空用会话默认 |
| target_sessions | [] | 白名单（unified_msg_origin），留空对所有已知会话生效 |
| blacklist_sessions | [] | 黑名单，支持平台前缀（如 `aiocqhttp:`） |
| prompt_override | 空 | 自定义提示词，追加在内置人格提示词后 |
| admin_only_commands | true | 仅管理员可用管理命令 |
| user_profile | 空 | 用户画像文本，注入 LLM 上下文 |
| negative_keywords | [...] | 命中后 1 小时内不主动 |

## 命令说明

所有命令均为中文，形如 `/主动聊天 状态`（群里需带唤醒前缀或 @）：

| 命令 | 说明 |
| --- | --- |
| `/主动聊天 状态` | 查看全局/本会话开关、沉默时长、欲望值、今日次数等 |
| `/主动聊天 开启` / `关闭` | 开关本会话主动聊天（单会话覆盖，不影响全局） |
| `/主动聊天 测试` | 跳过门控立即走一次完整决策+发送流程 |
| `/主动聊天 冷却` | 查看全局/会话/负面情绪冷却剩余 |
| `/主动聊天 设置 间隔 <分钟>` | 设置本会话沉默阈值 |
| `/主动聊天 设置 概率 <0-1>` | 设置本会话基础概率 |
| `/主动聊天 设置 免打扰 <开始>-<结束>` | 如 `设置 免打扰 23-8` |
| `/主动聊天 设置 每日上限 <次数>` | 设置本会话每日上限 |
| `/主动聊天 重置` | 清空本会话状态与覆盖配置 |

默认仅管理员可用；将 `admin_only_commands` 设为 false 后所有人可用。

## 触发条件（全满足才可能主动）

1. 全局开关开启，且本会话未关闭、不在黑名单（白名单非空时需在白名单内）
2. 用户未处于负面情绪冷却（命中"别烦我"等关键词后 1 小时静默）
3. 不在免打扰时段，或用户最近 10 分钟内活跃
4. 距用户最后一条消息 ≥ `silence_threshold_minutes`
5. Bot 最后发言晚于用户最后发言时，需再等 ≥ `session_cooldown_minutes`（防自说自话）
6. 距全局上次主动 ≥ `cooldown_minutes`，距本会话上次主动 ≥ `session_cooldown_minutes`
7. 本会话今日主动次数 < `max_daily_proactive`
8. 概率掷骰命中：`proactive_probability × (0.5 + 欲望值)`
9. LLM 决策认为适合（输出 `should_send: true` 且生成出消息）

## 架构

```
astrbot_plugin_proactive_chat/
├── main.py                  # Star 入口：监听、调度循环、命令
├── adapter.py               # AstrBot API 适配层（所有框架调用集中于此）
├── core/
│   ├── scheduler.py         # 门控纯函数（沉默/冷却/免打扰/概率）
│   ├── silence_detector.py  # 会话状态与沉默检测
│   ├── context_builder.py   # 上下文包构建
│   ├── generator.py         # LLM 决策 + 消息生成
│   ├── desire.py            # 欲望驱动引擎
│   └── prompts.py           # 内置提示词
└── tests/test_scheduler.py  # 核心逻辑单元测试
```

core/ 目录不依赖 AstrBot，可独立测试；所有框架调用隔离在 adapter.py。

## 本地测试

```bash
cd astrbot_plugin_proactive_chat
python -m pytest tests/ -v
# 或
python -m unittest discover -s tests -v
```

## 已核对的 AstrBot API

对照 AstrBot master 分支源码核对（2026-09-16）：

- `context.send_message(session, message_chain)` —— 主动发消息
- `context.get_provider_by_id()` / `context.get_using_provider_async(umo)` —— Provider 解析
- `provider.text_chat(prompt, session_id, system_prompt)` → `LLMResponse.completion_text`
- `@event_message_type(ALL)` / `@after_message_sent()` / `@command()` 装饰器
- 插件生命周期 `initialize()` / `terminate()`，KV 存储 `get_kv_data` / `put_kv_data`

若你使用的 AstrBot 版本较旧（< 3.5.x），重点确认 `send_message` 是否存在。
