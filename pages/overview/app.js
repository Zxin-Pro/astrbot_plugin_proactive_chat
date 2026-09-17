/* astrbot_plugin_proactive_chat WebUI 逻辑
 * 通过 window.AstrBotPluginPage 桥接与后端通信（Dashboard 自动注入 SDK）。
 */
(function () {
  "use strict";

  const bridge = window.AstrBotPluginPage;
  const $ = (id) => document.getElementById(id);

  let overview = null;
  let pollTimer = null;

  // ---------- 主题 ----------
  function applyTheme(isDark) {
    document.documentElement.setAttribute("data-theme", isDark ? "dark" : "light");
  }

  // ---------- Toast ----------
  let toastTimer = null;
  function toast(msg, ok) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast " + (ok ? "ok" : "err");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3000);
  }

  // ---------- 工具 ----------
  function fmtTime(ts) {
    if (!ts) return "从未";
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}-${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }
  function clampPct(v) {
    return Math.max(0, Math.min(100, v));
  }

  // ---------- 数据加载 ----------
  async function load() {
    try {
      overview = await bridge.apiGet("overview");
      render();
    } catch (e) {
      toast("加载失败: " + e.message, false);
    }
  }

  // ---------- 渲染 ----------
  function render() {
    if (!overview) return;
    const o = overview;

    // 全局状态卡
    const badge = $("globalEnableText");
    badge.textContent = o.enable ? "已开启" : "已关闭";
    badge.className = "badge " + (o.enable ? "on" : "off");
    $("checkInterval").textContent = o.check_interval_seconds + "s";
    $("trackedSessions").textContent = o.tracked_sessions;
    const cd = $("globalCooldown");
    if (o.global_last_proactive_ts) {
      const remain = o.global_cooldown_minutes - o.global_cooldown_passed_min;
      cd.textContent = remain > 0 ? `剩 ${remain} 分钟` : "已解除";
    } else {
      cd.textContent = "未触发过";
    }

    // 会话列表
    const list = $("sessionList");
    if (!o.sessions.length) {
      list.innerHTML = '<div class="card empty">暂无会话数据，等机器人收到消息后这里会出现会话</div>';
    } else {
      list.innerHTML = o.sessions.map(sessionCard).join("");
      bindSessionActions();
    }

    // 设置表单（仅在用户未编辑时回填，避免覆盖输入）
    fillSettings();
  }

  function fillSettings() {
    const map = {
      s_enable: overview.enable,
      s_enable_desire_system: overview.desire_enabled,
      s_check_interval_seconds: overview.check_interval_seconds,
    };
    const cfg = overview.settings || overview;
    const numeric = {
      s_silence_threshold_minutes: "silence_threshold_minutes",
      s_proactive_probability: "proactive_probability",
      s_max_daily_proactive: "max_daily_proactive",
      s_cooldown_minutes: "cooldown_minutes",
      s_session_cooldown_minutes: "session_cooldown_minutes",
      s_quiet_hours_start: "quiet_hours_start",
      s_quiet_hours_end: "quiet_hours_end",
      s_desire_increase_rate: "desire_increase_rate",
      s_desire_decay_rate: "desire_decay_rate",
    };
    Object.entries(map).forEach(([id, v]) => {
      const el = $(id);
      if (el && document.activeElement !== el) el.checked = !!v;
    });
    Object.entries(numeric).forEach(([id, key]) => {
      const el = $(id);
      if (el && cfg[key] !== undefined && document.activeElement !== el) el.value = cfg[key];
    });
  }

  function sessionCard(s) {
    const stateBadge = s.enabled === null || s.enabled === undefined
      ? '<span class="badge auto">跟随全局</span>'
      : s.enabled
        ? '<span class="badge on">已开启</span>'
        : '<span class="badge off">已关闭</span>';
    const allowedBadge = s.allowed
      ? ""
      : '<span class="badge warn">黑/白名单外</span>';
    const negBadge = s.negative_cooling
      ? '<span class="badge warn">负面冷却中</span>'
      : "";

    const silenceTxt =
      s.silence_minutes === null
        ? "无记录"
        : s.silence_minutes >= 60
          ? `${Math.floor(s.silence_minutes / 60)} 小时 ${s.silence_minutes % 60} 分`
          : `${s.silence_minutes} 分钟`;
    const silencePct = s.silence_minutes === null ? 0 : clampPct((s.silence_minutes / s.silence_threshold) * 100);

    const desirePct = clampPct(s.desire * 100);

    return `
      <div class="card session-card" data-umo="${esc(s.umo)}">
        <div class="session-head">
          <span class="umo">${esc(s.umo)}</span>
          <div class="session-actions">
            ${stateBadge}${allowedBadge}${negBadge}
            <button class="btn btn-sm" data-action="test">测试</button>
            <button class="btn btn-sm" data-action="enable">开启</button>
            <button class="btn btn-sm" data-action="disable">关闭</button>
            <button class="btn btn-sm btn-danger" data-action="reset">重置</button>
          </div>
        </div>
        <div class="session-metrics">
          <div>
            <div class="metric-label">沉默时长（阈值 ${s.silence_threshold} 分钟）</div>
            <div class="metric-value">${silenceTxt}</div>
            <div class="bar"><i style="width:${silencePct}%"></i></div>
          </div>
          <div>
            <div class="metric-label">欲望值</div>
            <div class="metric-value">${s.desire.toFixed(2)}</div>
            <div class="bar desire"><i style="width:${desirePct}%"></i></div>
          </div>
          <div>
            <div class="metric-label">今日主动 / 上限</div>
            <div class="metric-value">${s.today_count} / ${s.max_daily}</div>
          </div>
          <div>
            <div class="metric-label">基础概率</div>
            <div class="metric-value">${s.probability}</div>
          </div>
          <div>
            <div class="metric-label">免打扰时段</div>
            <div class="metric-value">${s.quiet_hours[0]}:00 - ${s.quiet_hours[1]}:00</div>
          </div>
        </div>
        ${s.last_proactive_text ? `<div class="last-proactive">上次主动（${fmtTime(s.last_proactive_ts)}）：${esc(s.last_proactive_text)}</div>` : ""}
      </div>`;
  }

  // ---------- 会话操作 ----------
  function bindSessionActions() {
    document.querySelectorAll(".session-card").forEach((card) => {
      const umo = card.getAttribute("data-umo");
      card.querySelectorAll("[data-action]").forEach((btn) => {
        btn.addEventListener("click", () => handleSessionAction(btn.getAttribute("data-action"), umo, btn));
      });
    });
  }

  async function handleSessionAction(action, umo, btn) {
    if (action === "reset") {
      if (!(await confirmDialog(`确定重置该会话的所有状态与覆盖配置？\n${umo}`))) return;
    }
    btn.disabled = true;
    try {
      const res = await bridge.apiPost("session", { umo, action });
      if (action === "test") {
        toast(res.sent ? "测试消息已发送 ↑" : "测试未发送：LLM 判断不适合 / 生成失败 / 平台不可达", res.sent);
      } else {
        toast("操作成功", true);
      }
      await load();
    } catch (e) {
      toast("操作失败: " + e.message, false);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- 全局开关 ----------
  $("btnToggleGlobal").addEventListener("click", async () => {
    if (!overview) return;
    try {
      await bridge.apiPost("settings/save", { enable: !overview.enable });
      toast(overview.enable ? "全局主动聊天已关闭" : "全局主动聊天已开启", true);
      await load();
    } catch (e) {
      toast("操作失败: " + e.message, false);
    }
  });

  // ---------- 保存设置 ----------
  $("btnSaveSettings").addEventListener("click", async () => {
    const payload = {
      enable: $("s_enable").checked,
      enable_desire_system: $("s_enable_desire_system").checked,
      check_interval_seconds: Number($("s_check_interval_seconds").value),
      silence_threshold_minutes: Number($("s_silence_threshold_minutes").value),
      proactive_probability: Number($("s_proactive_probability").value),
      max_daily_proactive: Number($("s_max_daily_proactive").value),
      cooldown_minutes: Number($("s_cooldown_minutes").value),
      session_cooldown_minutes: Number($("s_session_cooldown_minutes").value),
      quiet_hours_start: Number($("s_quiet_hours_start").value),
      quiet_hours_end: Number($("s_quiet_hours_end").value),
      desire_increase_rate: Number($("s_desire_increase_rate").value),
      desire_decay_rate: Number($("s_desire_decay_rate").value),
    };
    try {
      await bridge.apiPost("settings/save", payload);
      toast("设置已保存", true);
      await load();
    } catch (e) {
      toast("保存失败: " + e.message, false);
    }
  });

  // ---------- 刷新 ----------
  $("btnRefresh").addEventListener("click", load);
  $("autoRefresh").addEventListener("change", (e) => {
    clearInterval(pollTimer);
    if (e.target.checked) pollTimer = setInterval(load, 5000);
  });

  // ---------- 确认框 ----------
  function confirmDialog(text) {
    return new Promise((resolve) => {
      const dlg = $("confirmDialog");
      $("confirmText").textContent = text;
      const done = (v) => {
        dlg.close();
        $("confirmOk").removeEventListener("click", onOk);
        $("confirmCancel").removeEventListener("click", onCancel);
        resolve(v);
      };
      const onOk = () => done(true);
      const onCancel = () => done(false);
      $("confirmOk").addEventListener("click", onOk);
      $("confirmCancel").addEventListener("click", onCancel);
      dlg.showModal();
    });
  }

  // ---------- 初始化 ----------
  async function init() {
    try {
      const ctx = await bridge.ready();
      applyTheme(!!ctx.isDark);
      bridge.onContext((c) => applyTheme(!!c.isDark));
    } catch (e) {
      // 桥接不可用时（如直接浏览器打开调试）使用浅色主题继续
      applyTheme(false);
    }
    await load();
    pollTimer = setInterval(load, 5000);
  }

  init();
})();
