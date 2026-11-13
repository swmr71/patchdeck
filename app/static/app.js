"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { tab: "hosts", overview: null, jobId: null, upgradable: [], groups: [], containers: [] };

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PatchDeck": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* ignore */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}

function toast(msg, err = false) {
  const d = document.createElement("div");
  d.textContent = msg;
  if (err) d.className = "err";
  $("#toast").appendChild(d);
  setTimeout(() => d.remove(), err ? 8000 : 4000);
}

function ago(iso) {
  if (!iso) return "未スキャン";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "たった今";
  if (s < 3600) return `${Math.floor(s / 60)}分前`;
  if (s < 86400) return `${Math.floor(s / 3600)}時間前`;
  return `${Math.floor(s / 86400)}日前`;
}
const fmt = (iso) => (iso ? new Date(iso).toLocaleString("ja-JP") : "-");
const count = (n, cls) => `<span class="count ${n ? cls : "zero"}">${n}</span>`;

function svcPill(sv) {
  const cls = sv.state === "active" ? "ok" : sv.state === "inactive" ? "warn" : sv.state === "unknown" ? "" : "bad";
  return `<span class="pill ${cls}" title="${esc(sv.unit)}: ${esc(sv.state)}"><span class="dot"></span>${esc(sv.unit)}</span>`;
}

async function startJob(path, body, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try {
    const { job_id } = await api(path, body);
    toast(`ジョブ #${job_id} を開始しました`);
    state.jobId = job_id;
    if (path !== "/api/scan") switchTab("jobs");
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

// ------------------------------------------------------------ タブ
function switchTab(tab) {
  state.tab = tab;
  history.replaceState(null, "", `#${tab}`);
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${tab}`));
  refreshTab();
}
$$("nav button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

async function refreshTab() {
  try {
    if (state.tab === "hosts") renderHosts();
    if (state.tab === "groups") await loadGroups();
    if (state.tab === "packages") await loadPackages();
    if (state.tab === "docker") await loadDocker();
    if (state.tab === "jobs") await loadJobs();
  } catch (e) {
    toast(e.message, true);
  }
}

// ------------------------------------------------------------ ホスト
async function refreshOverview() {
  state.overview = await api("/api/overview");
  const o = state.overview;
  const badge = $("#job-badge");
  badge.textContent = o.running_jobs.length;
  badge.classList.toggle("hidden", !o.running_jobs.length);
  const nxt = o.next_scan_at ? `次の定期スキャン ${new Date(o.next_scan_at).toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" })}` : "";
  $("#meta").textContent = `${o.user} ・ ${nxt}`;
  if (state.tab === "hosts") renderHosts();
}

function selectedHostIds() {
  return $$("#hosts-body input[type=checkbox]:checked").map((c) => Number(c.value));
}

function renderHosts() {
  const o = state.overview;
  if (!o) return;
  const checked = new Set(selectedHostIds());
  $("#hosts-body").innerHTML = o.hosts.map((h) => {
    const status = h.busy ? `<span class="spin" title="処理中"></span>`
      : h.last_scan_ok === false ? `<span class="pill bad" title="${esc(h.last_error)}">エラー</span>`
      : h.last_error ? `<span class="pill warn" title="${esc(h.last_error)}">注意</span>` : "";
    const docker = h.has_docker
      ? `${count(h.docker_updates, "upd")} / ${h.docker_total}`
      : `<span class="muted">-</span>`;
    return `<tr>
      <td><input type="checkbox" value="${h.id}" ${checked.has(h.id) ? "checked" : ""}></td>
      <td class="num">${h.vmid}</td>
      <td><span class="name clickable" data-host="${h.id}">${esc(h.name)}</span>
          ${h.reboot_required ? '<span class="pill warn">再起動待ち</span>' : ""}
          <div class="sub mono">${esc(h.ip)}</div></td>
      <td class="sub">${esc(h.os_name || "-")}</td>
      <td class="num">${count(h.upgradable, "upd")}</td>
      <td class="num">${count(h.security, "sec")}</td>
      <td>${h.services.map(svcPill).join("") || '<span class="muted">-</span>'}</td>
      <td class="num">${docker}</td>
      <td class="sub">${status} ${esc(ago(h.last_scan_at))}</td>
    </tr>`;
  }).join("");
  $$("#hosts-body [data-host]").forEach((el) => el.addEventListener("click", () => showHost(Number(el.dataset.host))));
}

$("#hosts-all").addEventListener("change", (e) => $$("#hosts-body input[type=checkbox]").forEach((c) => (c.checked = e.target.checked)));
$("#btn-scan-all").addEventListener("click", () => startJob("/api/scan", {}));
$("#btn-scan-sel").addEventListener("click", () => {
  const ids = selectedHostIds();
  if (!ids.length) return toast("ホストを選んでください", true);
  startJob("/api/scan", { host_ids: ids });
});
$("#btn-full-sel").addEventListener("click", () => {
  const ids = selectedHostIds();
  if (!ids.length) return toast("ホストを選んでください", true);
  const names = state.overview.hosts.filter((h) => ids.includes(h.id)).map((h) => `${h.vmid} ${h.name}`);
  startJob("/api/update/full", { host_ids: ids },
    `次の ${ids.length} 台で apt upgrade を実行します。\n\n${names.join("\n")}\n\nよろしいですか？`);
});

async function showHost(id) {
  const d = await api(`/api/hosts/${id}`);
  $("#detail-title").textContent = `${d.vmid} ${d.name} (${d.ip})`;
  const upg = d.packages.filter((p) => p.upgradable);
  const watched = d.packages.filter((p) => !p.upgradable && p.group);
  const row = (p) => `<tr><td class="mono">${esc(p.name)}</td><td>${esc(p.group || "")}</td>
    <td class="ver">${esc(p.installed || "")}${p.candidate ? `<span class="arrow">→</span><span class="new">${esc(p.candidate)}</span>` : ""}</td>
    <td>${p.security ? '<span class="pill bad">security</span>' : ""}</td></tr>`;
  $("#detail-body").innerHTML = `
    <p class="sub">${esc(d.os_name || "")}${d.last_error ? ` ・ ⚠ ${esc(d.last_error)}` : ""}</p>
    <h4>更新可能 (${upg.length})</h4>
    ${upg.length ? `<table><tbody>${upg.map(row).join("")}</tbody></table>` : '<p class="muted">なし</p>'}
    <h4>監視中のソフト(最新)</h4>
    ${watched.length ? `<table><tbody>${watched.map(row).join("")}</tbody></table>` : '<p class="muted">なし</p>'}`;
  $("#detail").showModal();
}

// ------------------------------------------------------------ ソフト別
async function loadGroups() {
  state.groups = await api("/api/groups");
  renderGroups();
}
$("#groups-only-outdated").addEventListener("change", renderGroups);

function renderGroups() {
  const onlyOutdated = $("#groups-only-outdated").checked;
  const list = state.groups.filter((g) => g.hosts.length && (!onlyOutdated || g.outdated));
  if (!list.length) {
    $("#groups-body").innerHTML = `<p class="muted">${onlyOutdated ? "更新が必要なソフトはありません 🎉" : "まだデータがありません。スキャンしてください。"}</p>`;
    return;
  }
  $("#groups-body").innerHTML = list.map((g, gi) => `
    <div class="card" data-group="${esc(g.name)}">
      <h3>${esc(g.name)} ${g.outdated ? `<span class="pill warn">${g.outdated}台で更新あり</span>` : '<span class="pill ok">すべて最新</span>'}</h3>
      <table><tbody>
      ${g.hosts.map((h) => `<tr>
        <td><input type="checkbox" value="${h.host_id}" ${h.upgradable.length && !h.busy ? "checked" : ""} ${h.upgradable.length ? "" : "disabled"}></td>
        <td><span class="name">${esc(h.host)}</span> <span class="sub">${h.vmid}</span></td>
        <td class="ver" title="${esc(h.upgradable.join(", "))}">${esc(h.installed || "")}${h.candidate ? `<span class="arrow">→</span><span class="new">${esc(h.candidate)}</span>` : (h.upgradable.length ? `<span class="arrow">→</span><span class="new">関連 ${h.upgradable.length}件</span>` : "")}</td>
        <td>${h.security ? '<span class="pill bad">security</span>' : ""}${h.services.map(svcPill).join("")}${h.busy ? '<span class="spin"></span>' : ""}</td>
      </tr>`).join("")}
      </tbody></table>
      <div class="actions">${g.outdated ? `<button class="danger" data-gi="${gi}">選んだホストで ${esc(g.name)} を更新</button>` : ""}</div>
    </div>`).join("");
  $$("#groups-body button[data-gi]").forEach((b) => b.addEventListener("click", () => {
    const g = list[Number(b.dataset.gi)];
    const card = b.closest(".card");
    const ids = $$("input[type=checkbox]:checked", card).map((c) => Number(c.value));
    if (!ids.length) return toast("ホストを選んでください", true);
    const names = g.hosts.filter((h) => ids.includes(h.host_id)).map((h) => `${h.host}: ${h.upgradable.join(", ")}`);
    startJob("/api/update/packages", { host_ids: ids, group: g.name },
      `${g.name} を ${ids.length} 台で更新します。\n\n${names.join("\n")}\n\nよろしいですか？`);
  }));
}

// ------------------------------------------------------------ パッケージ
async function loadPackages() {
  state.upgradable = await api("/api/upgradable");
  renderPackages();
}
$("#pkg-filter").addEventListener("input", renderPackages);
$("#pkg-sec-only").addEventListener("change", renderPackages);
$("#pkg-all").addEventListener("change", (e) => $$("#pkg-body input[type=checkbox]").forEach((c) => (c.checked = e.target.checked)));

function renderPackages() {
  const q = $("#pkg-filter").value.trim().toLowerCase();
  const secOnly = $("#pkg-sec-only").checked;
  const checked = new Set($$("#pkg-body input:checked").map((c) => c.value));
  const list = state.upgradable.filter((p) => (!q || p.name.toLowerCase().includes(q)) && (!secOnly || p.security));
  $("#pkg-body").innerHTML = list.length ? list.map((p) => `<tr>
    <td><input type="checkbox" value="${esc(p.name)}" ${checked.has(p.name) ? "checked" : ""}></td>
    <td><span class="mono name">${esc(p.name)}</span> ${p.security ? '<span class="pill bad">security</span>' : ""}</td>
    <td class="sub">${esc(p.group || "")}</td>
    <td>${p.hosts.map((h) => `<span class="pill" title="${esc(h.installed)} → ${esc(h.candidate)}">${esc(h.host)}</span>`).join("")}</td>
  </tr>`).join("") : `<tr><td colspan="4" class="muted">更新可能なパッケージはありません</td></tr>`;
}

$("#btn-pkg-update").addEventListener("click", () => {
  const names = $$("#pkg-body input:checked").map((c) => c.value);
  if (!names.length) return toast("パッケージを選んでください", true);
  const hostIds = new Set();
  const hostNames = new Set();
  state.upgradable.filter((p) => names.includes(p.name)).forEach((p) => p.hosts.forEach((h) => { hostIds.add(h.host_id); hostNames.add(h.host); }));
  startJob("/api/update/packages", { host_ids: [...hostIds], packages: names },
    `${names.length} 個のパッケージを ${hostIds.size} 台で更新します。\n\nパッケージ: ${names.join(", ")}\nホスト: ${[...hostNames].join(", ")}\n\nよろしいですか？`);
});

// ------------------------------------------------------------ Docker
async function loadDocker() {
  state.containers = await api("/api/containers");
  renderDocker();
}
$("#docker-only-upd").addEventListener("change", renderDocker);

function renderDocker() {
  const only = $("#docker-only-upd").checked;
  const checked = new Set($$("#docker-body input:checked").map((c) => Number(c.value)));
  const list = state.containers.filter((c) => !only || c.update_available);
  $("#docker-body").innerHTML = list.length ? list.map((c) => {
    const st = c.update_available === true ? '<span class="pill warn">更新あり</span>'
      : c.update_available === false ? '<span class="pill ok">最新</span>'
      : '<span class="pill">不明</span>';
    return `<tr>
      <td><input type="checkbox" value="${c.id}" ${c.updatable ? "" : "disabled"} ${checked.has(c.id) ? "checked" : ""}></td>
      <td>${esc(c.host)} <span class="sub">${c.vmid}</span></td>
      <td class="name">${esc(c.name)}<div class="sub">${esc(c.state || "")}</div></td>
      <td class="mono">${esc(c.image)}</td>
      <td class="sub">${c.compose_project ? `${esc(c.compose_project)} / ${esc(c.compose_service)}` : "-"}</td>
      <td>${st}${c.note ? `<div class="sub">${esc(c.note)}</div>` : ""}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="6" class="muted">コンテナがありません</td></tr>`;
}

$("#btn-docker-update").addEventListener("click", () => {
  const ids = $$("#docker-body input:checked").map((c) => Number(c.value));
  if (!ids.length) return toast("コンテナを選んでください", true);
  const names = state.containers.filter((c) => ids.includes(c.id)).map((c) => `${c.host}: ${c.name} (${c.image})`);
  startJob("/api/update/docker", { container_ids: ids },
    `次のコンテナを pull して作り直します(数秒止まります)。\n\n${names.join("\n")}\n\nよろしいですか？`);
});

// ------------------------------------------------------------ ジョブ
const STATUS = {
  queued: ["待機中", "info"], running: ["実行中", "info"], success: ["成功", "ok"],
  partial: ["一部失敗", "warn"], failed: ["失敗", "bad"],
};
const statusPill = (s) => { const [t, c] = STATUS[s] || [s, ""]; return `<span class="pill ${c}">${s === "running" ? '<span class="spin"></span>' : ""}${t}</span>`; };

async function loadJobs() {
  const jobs = await api("/api/jobs?limit=60");
  if (!state.jobId && jobs.length) state.jobId = jobs[0].id;
  $("#jobs-list").innerHTML = jobs.map((j) => `<li data-id="${j.id}" class="${j.id === state.jobId ? "sel" : ""}">
    <div class="t">#${j.id} ${esc(j.title)}</div>
    <div class="sub">${statusPill(j.status)} ${esc(fmt(j.created_at))} ・ ${esc(j.requested_by || "")}</div></li>`).join("")
    || '<li class="muted">まだジョブはありません</li>';
  $$("#jobs-list li[data-id]").forEach((li) => li.addEventListener("click", () => { state.jobId = Number(li.dataset.id); loadJobs(); }));
  if (state.jobId) await loadJobDetail();
}

async function loadJobDetail() {
  const j = await api(`/api/jobs/${state.jobId}`);
  $("#job-title").textContent = `#${j.id} ${j.title}`;
  $("#job-meta").innerHTML = `${statusPill(j.status)} 開始 ${esc(fmt(j.started_at))} ・ 終了 ${esc(fmt(j.finished_at))} ・ ${esc(j.requested_by || "")}`;
  $("#job-summary").textContent = j.summary || "";
  const logEl = $("#job-log");
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 30;
  logEl.textContent = j.log || "";
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
}

// ------------------------------------------------------------ 定期更新
// ジョブタブは常に更新。ソフト別/パッケージ/Docker はチェック状態を崩さないよう、ジョブが終わったときだけ再読込
let wasBusy = false;
let tickTimer = null;
async function tick() {
  let busy = false;
  try {
    await refreshOverview();
    busy = state.overview.running_jobs.length > 0;
    if (state.tab === "jobs" || (wasBusy && !busy)) await refreshTab();
  } catch (e) {
    toast(e.message, true);
  }
  wasBusy = busy;
  clearTimeout(tickTimer);
  tickTimer = setTimeout(tick, busy ? 2500 : 20000);
}
const initial = location.hash.slice(1);
if (["hosts", "groups", "packages", "docker", "jobs"].includes(initial)) switchTab(initial); else refreshTab();
tick();
