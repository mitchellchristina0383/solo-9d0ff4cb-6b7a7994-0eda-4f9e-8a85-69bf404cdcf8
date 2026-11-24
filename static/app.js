/* VPN MTU 演练台前端：原生 JS，无外部依赖。 */
"use strict";

let STATE = null;
let refreshSeq = 0;
let batchRunning = false;

const $ = (id) => document.getElementById(id);

function showToast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, body) {
  const opts = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? "{}" : JSON.stringify(body),
  };
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_e) { /* 非 JSON */ }
  if (!resp.ok || (data && data.ok === false)) {
    throw new Error((data && data.error) || `请求失败（HTTP ${resp.status}）`);
  }
  return data;
}

/* 所有写操作统一走 act：报错弹 toast，成功用响应里的 state 渲染。 */
async function act(path, body, okMsg) {
  try {
    const data = await api(path, body);
    if (data.state) {
      /* 使写操作之前发出、可能晚到的轮询响应失效，避免旧 state 覆盖新界面 */
      refreshSeq += 1;
      render(data.state);
    } else {
      loadState();
    }
    if (okMsg) showToast(okMsg);
    return data;
  } catch (err) {
    showToast(err.message, true);
    /* 出错后仍刷新一次，保证界面与服务端一致 */
    loadState();
    return null;
  }
}

async function loadState() {
  const seq = ++refreshSeq;
  try {
    const resp = await fetch("/api/state");
    const data = await resp.json();
    if (seq !== refreshSeq || !data.ok) return;
    render(data.state);
  } catch (_e) { /* 轮询失败静默 */ }
}

/* ---------------- 参数表单 ---------------- */

function readConfig() {
  return {
    total_bytes: Number($("cfg-total").value),
    start_payload: Number($("cfg-start").value),
    timeout_step: Number($("cfg-step").value),
    lines: {
      A: {
        mtu: Number($("cfg-A-mtu").value),
        overhead: Number($("cfg-A-overhead").value),
        bandwidth: Number($("cfg-A-bandwidth").value),
        notify: $("cfg-A-notify").checked,
      },
      B: {
        mtu: Number($("cfg-B-mtu").value),
        overhead: Number($("cfg-B-overhead").value),
        bandwidth: Number($("cfg-B-bandwidth").value),
        notify: $("cfg-B-notify").checked,
      },
    },
  };
}

function refreshEffLabels() {
  for (const k of ["A", "B"]) {
    const mtu = Number($(`cfg-${k}-mtu`).value);
    const oh = Number($(`cfg-${k}-overhead`).value);
    const eff = mtu - oh;
    const start = Number($("cfg-start").value);
    const fit = eff >= start;
    $(`eff-${k}`).innerHTML =
      `最大载荷 <b style="color:${eff > 0 ? "#334155" : "#dc2626"}">${eff}</b>` +
      (start > 0 ? ` · 起始包${fit ? "<span style='color:#059669'>可过</span>"
                                  : "<span style='color:#dc2626'>必超</span>"}` : "");
  }
}

function applyPreset(name) {
  const set = (id, v) => { $(id).value = v; };
  if (name === "blackhole") {
    set("cfg-total", 10000); set("cfg-start", 1400); set("cfg-step", 200);
    set("cfg-A-mtu", 1500); set("cfg-A-overhead", 120); set("cfg-A-bandwidth", 100);
    $("cfg-A-notify").checked = false;
    set("cfg-B-mtu", 1500); set("cfg-B-overhead", 60); set("cfg-B-bandwidth", 100);
    $("cfg-B-notify").checked = true;
  } else {
    set("cfg-A-mtu", 1500); set("cfg-A-overhead", 120); set("cfg-A-bandwidth", 100);
    $("cfg-A-notify").checked = true;
    set("cfg-B-mtu", 1500); set("cfg-B-overhead", 60); set("cfg-B-bandwidth", 100);
    $("cfg-B-notify").checked = true;
  }
  refreshEffLabels();
}

/* ---------------- 渲染 ---------------- */

const KIND_TEXT = {
  new: "新发", missing: "重发缺口", timeout: "超时重发", switch: "换线重发",
};

function pct(part, all) { return all > 0 ? (part / all * 100) : 0; }

function render(s) {
  STATE = s;
  renderChips(s);
  renderProgress(s);
  renderPackets(s);
  renderTimeline(s);
  renderSchedule(s.schedule);
  $("completeBanner").classList.toggle("hidden", !s.complete);
  for (const id of ["btnSendOne", "btnBurst", "btnBurstAll", "btnResendMissing",
                    "btnSwitchA", "btnSwitchB"]) {
    $(id).disabled = s.complete;
  }
}

function renderChips(s) {
  const lc = s.config.lines[s.active_line];
  $("statusChips").innerHTML = `
    <span class="chip line-${s.active_line}">当前线路：<b>${lc.label}</b>
      （MTU ${lc.mtu} / 开销 ${lc.overhead} / 最大载荷 ${lc.max_payload} /
      带宽 ${fmtNum(lc.bandwidth)}Mbps / 通知 ${lc.notify ? "开" : "关"}）</span>
    <span class="chip">当前试发载荷：<b>${s.cur_payload}</b> 字节</span>
    <span class="chip">起始载荷：<b>${s.config.start_payload}</b></span>
    <span class="chip">当前轮次：<b>第 ${s.round} 轮</b></span>
    <span class="chip">新字节推进位置：<b>${s.next_offset}</b> / ${s.config.total_bytes}</span>`;
}

function fmtNum(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function renderProgress(s) {
  const total = s.config.total_bytes;
  $("ackedBytes").textContent = s.acked_count;
  $("totalBytes").textContent = total;
  const p = pct(s.acked_count, total);
  $("ackedPct").textContent = p.toFixed(1) + "%";
  $("progressBar").style.width = p + "%";

  const ranges = s.acked_ranges || [];
  const shown = ranges.slice(0, 24);
  let chips = shown.map(([a, b]) => `<span class="range-chip">[${a}, ${b})</span>`).join("");
  if (ranges.length > shown.length) chips += `<span class="range-chip">…另有 ${ranges.length - shown.length} 段</span>`;
  if (!ranges.length) chips = `<span class="hint">尚无确认区间</span>`;
  $("rangeChips").innerHTML = chips;

  /* 覆盖图：绿色为已确认区间，彩色描边小条为每个报文的字节范围 */
  const ackSegs = ranges.map(([a, b]) =>
    `<div class="seg" style="left:${pct(a, total)}%;width:${pct(b - a, total)}%"></div>`).join("");
  const ticks = s.packets.map((pkt) => {
    const color = pkt.line === "A" ? "var(--a)" : "var(--b)";
    const op = pkt.status === "acked" ? ".25" : pkt.status === "timeout" ? ".45" : ".9";
    return `<div class="pkt-tick" title="#${pkt.id} ${pkt.line}线 [${pkt.start},${pkt.end}) ${pkt.status}"
      style="left:${pct(pkt.start, total)}%;width:${Math.max(pct(pkt.end - pkt.start, total), .25)}%;
             border-color:${color};opacity:${op}"></div>`;
  }).join("");
  $("coverageMap").innerHTML = ackSegs + ticks;
}

function statusCell(p) {
  let proto = "";
  if (p.status === "outstanding") proto = `<span class="tag st-out">在途未回执</span>`;
  if (p.status === "acked") proto = `<span class="tag st-acked">已确认</span>`;
  if (p.status === "timeout") proto = `<span class="tag st-timeout">已超时</span>`;
  if (p.late_ack) proto += `<span class="tag late">迟到回执</span>`;

  let phys = "";
  if (p.physical === "in_flight") phys = `<span class="tag ph-ok">实际在途·可回执</span>`;
  if (p.physical === "silent_drop") phys = `<span class="tag ph-silent">上帝视角：静默丢弃</span>`;
  if (p.physical === "notify_drop") phys = `<span class="tag ph-notify">上帝视角：已丢弃·有超长通知</span>`;

  let notice = "";
  if (p.notice_state === "pending") notice = `<span class="tag notice-pending">超长通知待投递</span>`;
  if (p.notice_state === "applied") notice = `<span class="tag notice-applied">通知已用于缩包</span>`;
  if (p.notice_state === "ignored") notice = `<span class="tag notice-ignored">过期通知·已忽略</span>`;

  return `<div style="display:flex;flex-direction:column;gap:3px;align-items:flex-start">
            <div>${proto}</div><div>${phys}</div>${notice ? `<div>${notice}</div>` : ""}
          </div>`;
}

function actionsCell(p) {
  if (p.status === "acked") return `<span class="hint">—</span>`;
  const btns = [];
  if (p.physical === "in_flight") {
    const label = p.status === "timeout" ? "补到迟到回执" : "回执 ACK";
    btns.push(`<button class="btn tiny ok" data-act="ack" data-id="${p.id}">${label}</button>`);
  }
  if (p.status === "outstanding") {
    btns.push(`<button class="btn tiny danger" data-act="timeout" data-id="${p.id}">触发超时</button>`);
  }
  if (p.notice_state === "pending") {
    btns.push(`<button class="btn tiny warn" data-act="notice" data-id="${p.id}">投递超长通知</button>`);
  }
  return `<div class="actions-cell">${btns.join("") || '<span class="hint">无可用操作</span>'}</div>`;
}

function renderPackets(s) {
  const tb = $("packetRows");
  if (!s.packets.length) {
    tb.innerHTML = `<tr><td colspan="9" class="empty">还没有报文，先在上方发包。</td></tr>`;
    return;
  }
  const rows = s.packets.slice().reverse().map((p) => {
    const rowCls = p.status === "acked" ? "row-acked"
      : p.status === "timeout" ? "row-timeout"
      : p.physical !== "in_flight" ? "row-drop" : "";
    const overWire = p.wire_size > s.config.lines[p.line].mtu;
    return `<tr class="${rowCls}">
      <td class="mono"><b>#${p.id}</b></td>
      <td><span class="tag round">第 ${p.round} 轮</span></td>
      <td><span class="tag line-${p.line}">线路 ${p.line}</span></td>
      <td><span class="tag kind">${KIND_TEXT[p.kind] || p.kind}</span></td>
      <td class="mono">${p.size}</td>
      <td class="mono" style="${overWire ? "color:#dc2626;font-weight:700" : ""}">
        ${p.wire_size}${overWire ? " ✗超MTU" : ""}</td>
      <td class="mono">[${p.start}, ${p.end})</td>
      <td>${statusCell(p)}</td>
      <td>${actionsCell(p)}</td>
    </tr>`;
  }).join("");
  tb.innerHTML = rows;
}

function renderTimeline(s) {
  const el = $("timeline");
  const events = (s.events || []).slice().reverse();
  el.innerHTML = events.map((e) => `
    <div class="ev ${e.kind}">
      <span class="t">${e.time}</span><span class="dot"></span>
      <span class="ev-text">${escapeHtml(e.text)}</span>
    </div>`).join("");
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- 夜间备份排班 ---------------- */

async function postJSON(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? "{}" : JSON.stringify(body),
  });
  let data = null;
  try { data = await resp.json(); } catch (_e) { /* 非 JSON */ }
  if (!resp.ok || (data && data.ok === false)) {
    const err = new Error((data && data.error) || `请求失败（HTTP ${resp.status}）`);
    err.data = data;
    err.status = resp.status;
    throw err;
  }
  return data;
}

function orderFormBody() {
  return {
    branch: $("ord-branch").value.trim(),
    start_minute: Number($("ord-start").value),
    end_minute: Number($("ord-end").value),
    payload: Number($("ord-payload").value),
    rate: Number($("ord-rate").value),
  };
}

function validateOrderForm(b) {
  if (!b.branch) return "请填写分支名称";
  if (!Number.isInteger(b.start_minute) || b.start_minute < 0 || b.start_minute > 1440)
    return "起始分钟需为 0~1440 的整数";
  if (!Number.isInteger(b.end_minute) || b.end_minute < 0 || b.end_minute > 1440)
    return "结束分钟需为 0~1440 的整数";
  if (b.start_minute >= b.end_minute) return "结束分钟必须晚于起始分钟";
  if (!Number.isInteger(b.payload) || b.payload < 1 || b.payload > 65535)
    return "固定报文载荷需为 1~65535 的整数";
  if (!Number.isFinite(b.rate) || b.rate <= 0) return "保底速率需为正数";
  return null;
}

function renderSchedule(sch) {
  if (!sch) return;
  const badge = $("orderCount");
  badge.textContent = `${sch.count}/${sch.max_orders} 单`;
  badge.classList.toggle("full", sch.count >= sch.max_orders);

  const tb = $("orderRows");
  if (!sch.orders.length) {
    tb.innerHTML = `<tr><td colspan="8" class="empty">还没有备份预约，先在上方录单，或点“灌入演示班次”。</td></tr>`;
  } else {
    tb.innerHTML = sch.orders.map((o) => `
      <tr>
        <td class="mono"><b>#${o.id}</b></td>
        <td>${escapeHtml(o.branch)}</td>
        <td class="mono">${o.start_minute} – ${o.end_minute}</td>
        <td class="mono muted">${fmtMinute(o.start_minute)}–${fmtMinute(o.end_minute)}</td>
        <td class="mono">${o.payload} B</td>
        <td class="mono">${fmtNum(o.rate)} Mbps</td>
        <td><span class="tag line-${o.line}">线路 ${o.line}</span></td>
        <td><button class="btn tiny danger" data-order-del="${o.id}">删除并重排</button></td>
      </tr>`).join("");
  }
  renderCapacity(sch);
}

function fmtMinute(m) {
  const h = Math.floor(m / 60), mm = m % 60;
  return `${String(h).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

function renderCapacity(sch) {
  const box = $("capacityBars");
  if (!sch.orders.length) {
    box.innerHTML = `<p class="hint">还没有预约单；录入后这里显示 A/B 两线的峰值速率/可用带宽与原子时段占用。</p>`;
    return;
  }
  box.innerHTML = ["A", "B"].map((k) => {
    const p = sch.profiles[k];
    const ratio = p.bandwidth > 0 ? p.peak / p.bandwidth : 0;
    const pctWidth = Math.min(100, ratio * 100);
    const over = ratio > 1 + 1e-9;
    const segs = p.segments.length
      ? p.segments.map((g) => `
          <span class="seg-chip ${g.fit ? "" : "over"}"
            title="${g.span_label}：${fmtNum(g.used)}/${fmtNum(p.bandwidth)} Mbps，单 ${g.order_ids.join(",")}">
            ${g.start_label}–${g.end_label} · ${fmtNum(g.used)}/${fmtNum(p.bandwidth)}
          </span>`).join("")
      : `<span class="hint">无占用</span>`;
    return `
      <div class="cap-line line-${k}-soft">
        <div class="cap-head">
          <span class="tag line-${k}">线路 ${k}</span>
          <b class="mono">峰值 ${fmtNum(p.peak)} / ${fmtNum(p.bandwidth)} Mbps
            （${(ratio * 100).toFixed(0)}%）</b>
        </div>
        <div class="cap-track">
          <div class="cap-fill line-${k}-bg" style="width:${pctWidth}%;
               background:${over ? "var(--danger)" : ""}"></div>
        </div>
        <div class="seg-chips">${segs}</div>
      </div>`;
  }).join("");
}

function renderPlanPanel(plan, opts = {}) {
  const panel = $("planPanel");
  if (!plan) { panel.classList.add("hidden"); panel.innerHTML = ""; return; }
  panel.classList.remove("hidden", "ok", "bad", "info");

  const titleBits = [];
  if (opts.preview) titleBits.push("🧪 试排结果（未提交，当前安排未改变）");
  else if (opts.paramRejected) titleBits.push("⛔ 新参数排不下，未生效");
  else if (opts.paramApply) titleBits.push("✅ 新参数已生效，整班已重排，发包演练已重置");
  else titleBits.push("✅ 已提交，整班排布如下");

  let body = "";
  if (plan.feasible) {
    panel.classList.add("ok");
    const assign = plan.assignment || {};
    const chips = Object.keys(assign).sort((a, b) => Number(a) - Number(b))
      .map((id) => `<span class="assign-chip line-${assign[id]}">#${id} → ${assign[id]}</span>`)
      .join("");
    body = `<div class="plan-title">${titleBits[0]}</div>
            <div class="assign-chips">${chips || '<span class="hint">（无预约单）</span>'}</div>
            <div class="hint">枚举 ${plan.checked_assignments ?? plan.checked ?? ""} 种合法候选后选定；
              走线会尽量保持稳定并均衡两线峰值。</div>`;
  } else {
    panel.classList.add("bad");
    const reasons = (plan.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
    body = `<div class="plan-title">${titleBits[0] || "⛔ 排不下"}</div>
            <ul class="reason-list">${reasons || "<li>未知冲突</li>"}</ul>`;
  }
  panel.innerHTML = body + `<button class="plan-close" title="关闭">×</button>`;
  panel.querySelector(".plan-close").addEventListener("click", () =>
    panel.classList.add("hidden"));
}

function handleScheduleFailure(err) {
  showToast(err.message, true);
  if (err.data && err.data.state) render(err.data.state);
  else loadState();
  const plan = (err.data && err.data.plan) ||
    { feasible: false, reasons: (err.data && err.data.reasons) || [err.message] };
  renderPlanPanel(plan, { paramRejected: /参数/.test(err.message) });
}

async function submitOrder() {
  const body = orderFormBody();
  const invalid = validateOrderForm(body);
  if (invalid) { showToast(invalid, true); return; }
  try {
    const data = await postJSON("/api/orders", body);
    refreshSeq += 1;
    render(data.state);
    renderPlanPanel(data.plan);
    showToast(`已加单 #${data.order.id}（${data.order.branch}）→ 线路 ${data.order.line}，整班已重排`);
    $("ord-branch").value = "";
    $("ord-branch").focus();
  } catch (err) {
    handleScheduleFailure(err);
  }
}

async function previewOrder() {
  const body = orderFormBody();
  const invalid = validateOrderForm(body);
  if (invalid) { showToast(invalid, true); return; }
  try {
    const data = await postJSON("/api/schedule/plan", { order: body });
    renderPlanPanel(data.plan, { preview: true });
    showToast(data.plan.feasible ? "试排可行：尚未提交，下方仍是当前安排"
                                : "试排不可行：当前安排保持不变",
              !data.plan.feasible);
    /* 显式拉取当前已提交安排，印证“排线时仍能查当前安排”且试排不落库 */
    loadState();
  } catch (err) {
    handleScheduleFailure(err);
  }
}

async function deleteOrder(id) {
  try {
    const data = await postJSON(`/api/orders/${id}/delete`, {});
    refreshSeq += 1;
    render(data.state);
    renderPlanPanel(data.plan);
    showToast(`已删除 #${id} 并重新排布`);
  } catch (err) {
    handleScheduleFailure(err);
  }
}

async function replanAll() {
  try {
    const data = await postJSON("/api/schedule/replan", {});
    refreshSeq += 1;
    render(data.state);
    renderPlanPanel(data.plan);
    showToast("已从全局重新整班排布");
  } catch (err) {
    handleScheduleFailure(err);
  }
}

/* 两击确认，避免误清空；3 秒内再点一次才真正执行 */
function armTwoClick(btn, armedText, action) {
  btn.addEventListener("click", async () => {
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      const old = btn.textContent;
      btn.textContent = armedText;
      setTimeout(() => { btn.dataset.armed = "0"; btn.textContent = old; }, 3000);
      return;
    }
    btn.dataset.armed = "0";
    await action();
  });
}

/* ---------------- 批量回执（任选顺序） ---------------- */

function eligiblePackets(s) {
  return s.packets.filter((p) => p.physical === "in_flight" && p.status === "outstanding");
}

async function ackOne(pid) {
  return act(`/api/packets/${pid}/ack`);
}

async function batchAck(mode) {
  if (batchRunning || !STATE) return;
  let list = eligiblePackets(STATE);
  if (!list.length) { showToast("当前没有可回执的在途包", true); return; }
  if (mode === "random-one") {
    const p = list[Math.floor(Math.random() * list.length)];
    await ackOne(p.id);
    return;
  }
  const order = $("ackOrder").value;
  if (mode === "all") {
    if (order === "desc") list.sort((a, b) => b.id - a.id);
    else if (order === "asc") list.sort((a, b) => a.id - b.id);
    else {
      for (let i = list.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [list[i], list[j]] = [list[j], list[i]];
      }
    }
    batchRunning = true;
    setBatchDisabled(true);
    $("ackHelper").textContent = `按${order === "random" ? "随机乱序" : order === "asc" ? "正序" : "逆序"}依次回执 ${list.length} 个包…`;
    for (const p of list) {
      /* 用最新状态再确认一次该包仍可回执 */
      const fresh = STATE && STATE.packets.find((x) => x.id === p.id);
      if (!fresh || fresh.physical !== "in_flight" || fresh.status !== "outstanding") continue;
      // eslint-disable-next-line no-await-in-loop
      const r = await ackOne(p.id);
      if (!r) break;
      // eslint-disable-next-line no-await-in-loop
      await new Promise((res) => setTimeout(res, 160));
    }
    $("ackHelper").textContent = "本轮批量回执完成。";
    setBatchDisabled(false);
    batchRunning = false;
  }
}

function setBatchDisabled(d) {
  $("btnAckAll").disabled = d;
  $("btnAckRandom").disabled = d;
}

/* ---------------- 事件绑定 ---------------- */

function bind() {
  document.querySelectorAll("[data-preset]").forEach((b) =>
    b.addEventListener("click", () => applyPreset(b.dataset.preset)));

  ["cfg-total", "cfg-start", "cfg-A-mtu", "cfg-A-overhead", "cfg-B-mtu", "cfg-B-overhead"]
    .forEach((id) => $(id).addEventListener("input", refreshEffLabels));

  $("btnReset").addEventListener("click", async () => {
    const cfg = readConfig();
    if (!Number.isFinite(cfg.total_bytes) || cfg.total_bytes < 1) {
      showToast("请输入合法的报表字节数", true); return;
    }
    if (![cfg.lines.A.bandwidth, cfg.lines.B.bandwidth].every((v) => v > 0)) {
      showToast("两条线路的可用带宽都必须是正数", true); return;
    }
    try {
      const data = await postJSON("/api/reset", cfg);
      refreshSeq += 1;
      render(data.state);
      refreshEffLabels();
      renderPlanPanel(data.plan, { paramApply: true });
      showToast("新参数已生效：整班按新参数重排，发包演练已重置");
    } catch (err) {
      /* 排不下：服务端保留了原演练与预约，用回传 state 重渲染并说明原因 */
      if (err.data && err.data.state) { refreshSeq += 1; render(err.data.state); }
      else loadState();
      renderPlanPanel((err.data && err.data.plan) ||
        { feasible: false, reasons: (err.data && err.data.reasons) || [err.message] },
        { paramRejected: true });
      showToast(err.message, true);
    }
  });

  /* 排班区控件 */
  $("btnAddOrder").addEventListener("click", submitOrder);
  $("btnPreviewOrder").addEventListener("click", previewOrder);
  $("btnReplan").addEventListener("click", replanAll);
  $("ord-branch").addEventListener("keydown", (e) => { if (e.key === "Enter") submitOrder(); });

  $("orderRows").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-order-del]");
    if (btn) deleteOrder(btn.dataset.orderDel);
  });

  armTwoClick($("btnClearOrders"), "再点一次确认清空", async () => {
    const data = await act("/api/schedule/clear", {}, "班次已清空");
    if (data) renderPlanPanel(null);
  });

  armTwoClick($("btnDemo"), "再点一次覆盖为演示班", async () => {
    try {
      const data = await postJSON("/api/schedule/demo", {});
      refreshSeq += 1;
      render(data.state);
      renderPlanPanel(data.plan);
      showToast("已灌入 4 单演示班次（默认参数下可见 A/B 分工）");
    } catch (err) {
      handleScheduleFailure(err);
    }
  });

  $("btnSendOne").addEventListener("click", () => act("/api/send-next"));
  $("btnBurst").addEventListener("click", () => {
    const n = Number($("burstCount").value);
    if (!Number.isInteger(n) || n < 1) { showToast("连发包数需为正整数", true); return; }
    act("/api/send-burst", { count: n }, `已连续发出 ${n} 包（或发到报表末尾）`);
  });
  $("btnBurstAll").addEventListener("click", () =>
    act("/api/send-burst", { count: "all" }, "已连续发完所有剩余新包"));
  $("btnResendMissing").addEventListener("click", () => act("/api/resend-missing"));

  $("btnSwitchA").addEventListener("click", () =>
    act("/api/switch", { line: "A" }, "已换到线路 A，载荷重置并重发"));
  $("btnSwitchB").addEventListener("click", () =>
    act("/api/switch", { line: "B" }, "已换到线路 B，载荷重置并重发"));

  $("btnAckAll").addEventListener("click", () => batchAck("all"));
  $("btnAckRandom").addEventListener("click", () => batchAck("random-one"));

  /* 报文行内按钮（事件委托） */
  $("packetRows").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const action = btn.dataset.act;
    if (action === "ack") await act(`/api/packets/${id}/ack`);
    if (action === "timeout") await act(`/api/packets/${id}/timeout`);
    if (action === "notice") await act(`/api/packets/${id}/deliver-notice`);
  });
}

bind();
refreshEffLabels();
loadState();
setInterval(loadState, 2500);
