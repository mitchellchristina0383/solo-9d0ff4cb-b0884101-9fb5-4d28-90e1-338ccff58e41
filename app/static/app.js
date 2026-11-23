/* 虚拟磁盘热换盘演练台前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let blockSize = 64;

function toast(message, kind = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toast-wrap").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    method: options.method || "POST",
    headers: { "Content-Type": "application/json" },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await resp.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (data.state) state = data.state;
  if (!resp.ok || !data.ok) {
    const err = new Error(data.error || `HTTP ${resp.status}`);
    err.payload = data;
    throw err;
  }
  return data;
}

/* 字符串 -> 恰好 blockSize 字节（latin-1 透传，右侧补 0x2e 即 '.'） */
function toFixedPayload(text) {
  const raw = [];
  for (let i = 0; i < text.length; i++) {
    let code = text.charCodeAt(i);
    if (code > 255) code = 63; // 非 latin-1 字符降级为 '?'
    raw.push(code);
  }
  while (raw.length < blockSize) raw.push(46); // '.'
  return raw.slice(0, blockSize).map((b) => String.fromCharCode(b)).join("");
}

function bytesToHex(latin1String) {
  const parts = [];
  for (let i = 0; i < latin1String.length; i++) {
    parts.push(latin1String.charCodeAt(i).toString(16).padStart(2, "0"));
  }
  return parts.join(" ");
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

/* ------------------------------------------------------------- 渲染 */
function render() {
  if (!state) return;
  blockSize = state.block_size || blockSize;
  $("bsize").textContent = state.block_size;
  $("bcount").textContent = state.block_count;
  $("block-num").max = state.block_count - 1;

  const gen = $("gen-badge");
  gen.textContent = `挂载代次: ${state.mount_generation}`;
  gen.className = "badge";

  const gate = $("gate-badge");
  if (state.gate_open) {
    gate.textContent = "写入门: 放行(open)";
    gate.className = "badge ok";
  } else {
    gate.textContent = "写入门: 暂停(paused)";
    gate.className = "badge warn";
  }

  const fsyncBadge = $("fsync-badge");
  fsyncBadge.textContent = `累计 fsync: ${state.fsync_total ?? 0}`;
  fsyncBadge.className = "badge ok";

  const slot = $("slot-info");
  if (state.mounted) {
    slot.innerHTML =
      `当前挂载: <b>${state.mounted.name}</b>（文件 <code>${state.mounted.filename}</code>，` +
      `ID <code>${state.mounted.disk_id.slice(0, 8)}…</code>）<br/>挂载代次: <b>${state.mount_generation}</b>`;
  } else {
    slot.innerHTML = `<b style="color:#f87171">槽位为空</b>（代次 ${state.mount_generation}），请挂载一份盘`;
  }

  // 盘列表
  const tbody = $("disk-tbody");
  tbody.innerHTML = "";
  for (const disk of state.disks) {
    const tr = document.createElement("tr");
    const mounted = state.mounted && state.mounted.disk_id === disk.disk_id;
    tr.innerHTML = `
      <td>${disk.name}${mounted ? ' <span class="pill done">已挂载</span>' : ""}</td>
      <td><code>${disk.filename}</code></td>
      <td><code>${disk.disk_id.slice(0, 8)}…</code></td>
      <td></td>`;
    const btn = document.createElement("button");
    btn.className = "btn btn-primary";
    btn.textContent = mounted ? "重新挂载(代次+1)" : "挂载到槽位";
    btn.disabled = false;
    btn.onclick = () => doPlug(disk.disk_id);
    tr.lastElementChild.appendChild(btn);
    tbody.appendChild(tr);
  }
  $("btn-unplug").disabled = !state.mounted;

  // 失败模拟提示
  $("fail-hint").textContent = state.fail_simulation
    ? "⚠ 已开启落盘失败模拟：接下来的写入会在真正写文件前报错，原块内容保持不变。"
    : "失败模拟当前关闭。";
  $("fail-hint").style.color = state.fail_simulation ? "#fb923c" : "#94a3b8";

  // 排队队列
  const pt = $("pending-tbody");
  pt.innerHTML = "";
  if (!state.pending.length) {
    pt.innerHTML = '<tr><td colspan="8" class="hint">队列为空</td></tr>';
  } else {
    for (const w of state.pending) {
      const tr = document.createElement("tr");
      const preview = w.payload.replace(/\./g, "·").slice(0, 24);
      tr.innerHTML = `
        <td><code>${w.id}</code></td>
        <td>${w.disk_name}</td>
        <td>${w.block}</td>
        <td>${w.mount_generation}</td>
        <td><code>${preview}</code></td>
        <td>${w.delay}s</td>
        <td><span class="pill ${w.status}">${w.status}</span></td>
        <td></td>`;
      const td = tr.lastElementChild;
      const release = document.createElement("button");
      release.className = "btn btn-ok";
      release.textContent = "放行";
      release.onclick = () => doReleaseOne(w.id);
      const discard = document.createElement("button");
      discard.className = "btn btn-danger";
      discard.textContent = "丢弃";
      discard.style.marginLeft = "6px";
      discard.onclick = () => doDiscard(w.id);
      td.append(release, discard);
      pt.appendChild(tr);
    }
  }

  // 事件日志
  const log = $("event-log");
  log.innerHTML = state.event_log
    .map((e) => {
      const stale = e.kind.includes("stale") ? "log-stale" :
        e.kind.includes("fail") ? "log-fail" :
        e.kind === "write" ? "log-write" : "";
      return `<div><span class="hint">${fmtTime(e.ts)}</span> <span class="log-kind ${stale}">[${e.kind}]</span> ${e.message}</div>`;
    })
    .join("");
}

/* ------------------------------------------------------------- 动作 */
async function doPlug(diskId) {
  try {
    await api("/api/slot/plug", { body: { disk_id: diskId } });
    toast("挂载完成，挂载代次已 +1", "success");
  } catch (e) { toast(e.message, "error"); }
  render();
}

async function doUnplug() {
  try {
    await api("/api/slot/unplug");
    toast("已拔盘，槽位置空，挂载代次 +1", "success");
  } catch (e) { toast(e.message, "error"); }
  render();
}

async function doRead() {
  const block = Number($("block-num").value);
  try {
    const data = await api(`/api/blocks/${block}`, { method: "GET" });
    $("read-text").textContent = data.result.data;
    $("read-hex").textContent = bytesToHex(data.result.data);
    toast(`已读取 ${data.result.disk_name} 块 ${block}`, "success");
  } catch (e) { toast(e.message, "error"); }
}

async function doWrite() {
  const block = Number($("block-num").value);
  const delay = Number($("write-delay").value) || 0;
  const syncDelay = Number($("write-sync-delay").value) || 0;
  const payload = toFixedPayload($("write-payload").value);
  try {
    const data = await api(`/api/blocks/${block}/write`, {
      body: { payload, delay, sync_delay: syncDelay, note: "网页手动写入" },
    });
    if (data.result.queued) {
      toast(`写入已排队（门处于暂停），代次 ${data.result.write.mount_generation}`, "info");
    } else if (data.result.write.status === "done") {
      toast(`落盘成功（批次 ${data.result.write.batch_id || "-"}）`, "success");
    }
  } catch (e) {
    toast(`写入未成功：${e.message}`, "error", 7000);
  }
  render();
}

async function doReleaseOne(id) {
  try {
    await api(`/api/pending/${id}/release`);
    toast("该写入已放行并落盘", "success");
  } catch (e) { toast(`放行结果：${e.message}`, "error", 7000); }
  render();
}

async function doReleaseAll() {
  try {
    const data = await api("/api/pending/release-all");
    const stale = data.result.results.filter((r) => r.status === "stale").length;
    const done = data.result.results.filter((r) => r.status === "done").length;
    const failed = data.result.results.filter((r) => r.status === "failed").length;
    toast(`放行完成：成功 ${done}，失效 ${stale}，失败 ${failed}`, stale ? "error" : "success", 7000);
  } catch (e) { toast(e.message, "error"); }
  render();
}

async function doDiscard(id) {
  try {
    await api(`/api/pending/${id}/discard`);
    toast("排队写入已丢弃", "info");
  } catch (e) { toast(e.message, "error"); }
  render();
}

async function doConcurrent() {
  const ba = Number($("cc-block-a").value);
  const bb = Number($("cc-block-b").value);
  if (ba === bb) { toast("并发测试请选择两个不同的块号", "error"); return; }
  const pa = toFixedPayload($("cc-payload-a").value);
  const pb = toFixedPayload($("cc-payload-b").value);
  const syncDelay = Number($("cc-sync-delay").value) || 0;
  $("cc-result").innerHTML = '<span class="hint">两个请求已同时发出，等待结果…</span>';

  // 记录发起前的累计 fsync 次数，结束后核对两条写入实际用了几次同步
  const fsyncBefore = state.fsync_total ?? 0;
  const started = Date.now();
  const [ra, rb] = await Promise.allSettled([
    api(`/api/blocks/${ba}/write`, { body: { payload: pa, sync_delay: syncDelay, note: "并发-A" } }),
    api(`/api/blocks/${bb}/write`, { body: { payload: pb, sync_delay: syncDelay, note: "并发-B" } }),
  ]);
  const elapsed = Date.now() - started;
  const lines = [`<div>耗时 ${elapsed}ms</div>`];
  const fulfilled = [];
  for (const [tag, r] of [["A", ra], ["B", rb]]) {
    if (r.status === "fulfilled") {
      const w = r.value.result.write;
      fulfilled.push(w);
      lines.push(`<div>写入${tag}: <span class="pill done">成功</span> 状态=${w.status}，批次=<code>${w.batch_id || "-"}</code></div>`);
    } else {
      lines.push(`<div>写入${tag}: <span class="pill failed">报错</span> ${r.reason.message}</div>`);
    }
  }
  // 合批核对：两条成功写入若批次号相同，则合用了一次 fsync
  if (fulfilled.length === 2) {
    const sameBatch = fulfilled[0].batch_id && fulfilled[0].batch_id === fulfilled[1].batch_id;
    lines.push(
      sameBatch
        ? `<div>两条写入同属批次 <code>${fulfilled[0].batch_id}</code>：<b style="color:#6ee7b7">合用 1 次 fsync</b></div>`
        : `<div>两条写入分属不同批次（<code>${fulfilled[0].batch_id}</code> / <code>${fulfilled[1].batch_id}</code>）：各用 1 次 fsync，共 2 次</div>`,
    );
  }
  const fsyncAfter = state.fsync_total ?? 0;
  lines.push(`<div>本次并发实际新增同步次数：<b>${fsyncAfter - fsyncBefore}</b>（累计 ${fsyncBefore} → ${fsyncAfter}）</div>`);

  // 同步成功后才回读两个块，验证改动都保留（块锁保证回读发生在 fsync 之后）
  try {
    const [va, vb] = await Promise.all([
      api(`/api/blocks/${ba}`, { method: "GET" }),
      api(`/api/blocks/${bb}`, { method: "GET" }),
    ]);
    lines.push(`<div>回读块${ba}: <code>${va.result.data.slice(0, 24)}</code></div>`);
    lines.push(`<div>回读块${bb}: <code>${vb.result.data.slice(0, 24)}</code></div>`);
    const keepA = va.result.data.startsWith($("cc-payload-a").value);
    const keepB = vb.result.data.startsWith($("cc-payload-b").value);
    lines.push(`<div>两处改动均保留：<b style="color:${keepA && keepB ? "#6ee7b7" : "#f87171"}">${keepA && keepB ? "是 ✔" : "否 ✘"}</b></div>`);
  } catch (e) {
    lines.push(`<div>回读失败：${e.message}</div>`);
  }
  $("cc-result").innerHTML = lines.join("");
  render();
}

/* ------------------------------------------------------------- 绑定与轮询 */
function bind() {
  $("btn-unplug").onclick = doUnplug;
  $("btn-gate-open").onclick = async () => {
    try { await api("/api/gate", { body: { open: true } }); toast("写入门：放行", "success"); }
    catch (e) { toast(e.message, "error"); }
    render();
  };
  $("btn-gate-pause").onclick = async () => {
    try { await api("/api/gate", { body: { open: false } }); toast("写入门：暂停，后续写入将排队", "info"); }
    catch (e) { toast(e.message, "error"); }
    render();
  };
  $("btn-fail-on").onclick = async () => {
    try { await api("/api/fail-simulation", { body: { enabled: true } }); toast("已开启模拟落盘失败", "error"); }
    catch (e) { toast(e.message, "error"); }
    render();
  };
  $("btn-fail-off").onclick = async () => {
    try { await api("/api/fail-simulation", { body: { enabled: false } }); toast("已关闭失败模拟", "success"); }
    catch (e) { toast(e.message, "error"); }
    render();
  };
  $("btn-read").onclick = doRead;
  $("btn-write").onclick = doWrite;
  $("btn-release-all").onclick = doReleaseAll;
  $("btn-concurrent").onclick = doConcurrent;
  $("btn-reseed").onclick = async () => {
    if (!confirm("确定重新生成两份镜像？所有演练数据将恢复初始内容。")) return;
    try { await api("/api/reseed"); toast("已重新播种镜像并重置状态", "success"); }
    catch (e) { toast(e.message, "error"); }
    render();
  };
  const ta = $("write-payload");
  const counter = () => {
    const len = Math.min(ta.value.length, blockSize);
    $("payload-counter").textContent = `${len} / ${blockSize} 字节（不足自动补 '.'，超出截断）`;
  };
  ta.oninput = counter;
  counter();
}

async function poll() {
  try {
    const resp = await fetch("/api/state");
    const data = await resp.json();
    if (data.ok) { state = data.state; render(); }
  } catch (e) { /* 轮询失败静默，等下一轮 */ }
}

bind();
poll();
setInterval(poll, 1500);
