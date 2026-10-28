/* Server-session LOOP modal; existing ModalManager and ControlButton components. */
(() => {
"use strict";
const trigger = document.getElementById("loop-modal-trigger");
if (!trigger) return;
const $ = id => document.getElementById(id);
let manager, latest, csrf, busy = false, generation = 0, barrier = 0;
let policy = "WAIT", order = [], selected = new Set(), chosenBag = "";
async function api(path, body) {
  const options = { credentials: "same-origin" };
  if (body !== undefined) {
    if (!csrf) {
      const r = await fetch("/api/auth/csrf", { credentials: "same-origin" });
      if (!r.ok) throw Error("CSRF unavailable");
      csrf = (await r.json()).csrf_token;
    }
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api/navigation/loop/" + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok !== true) throw Error(data.error || data.error_code || "Request failed");
  return data;
}
function feedback(msg) { if ($("loop-feedback")) $("loop-feedback").textContent = msg; }
function layout() {
  return [
    '<div class="loop-modal">',
    '<div class="loop-modal-info"><span>Map: <strong id="loop-map">--</strong></span><span>Mode: <strong id="loop-mode">--</strong></span><span id="loop-connection" role="status">Pi: 확인 중</span></div>',
    '<section class="loop-modal-section"><h3>GOALS (A–Z, a–z)</h3>',
    '<p class="loop-modal-hint">3D Viewer에서 SET LOOP을 켜고 SET GOAL로 위치를 선택합니다. 동일 위치를 다시 클릭하면 삭제합니다.</p>',
    '<div id="loop-goals" class="loop-goal-list" role="list"></div></section>',
    '<section class="loop-modal-section"><h3>RECORD / PLAY</h3>',
    '<p class="loop-modal-hint" id="loop-help"></p>',
    '<select id="loop-bags" aria-label="Recorded manual loops"></select>',
    '<div class="loop-modal-buttons">',
    '<button type="button" id="loop-record" disabled data-control-button="secondary">Record</button>',
    '<button type="button" id="loop-save" disabled data-control-button="success">Save</button>',
    '<button type="button" id="loop-delete" disabled data-control-button="danger">Delete</button>',
    '</div></section>',
    '<section class="loop-modal-section"><h3>OBSTACLE</h3>',
    '<div class="loop-policy" role="group" aria-label="Obstacle policy">',
    '<button type="button" id="loop-policy-toggle" data-control-button="view">Wait</button></div>',
    '<div class="loop-modal-buttons">',
    '<button type="button" id="loop-start" disabled data-control-button="accent">Start</button>',
    '<button type="button" id="loop-stop" disabled data-control-button="danger">Stop</button>',
    '</div></section>',
    '<div id="loop-status" class="loop-modal-status" role="status">IDLE</div>',
    '<div id="loop-feedback" class="loop-modal-feedback" role="status"></div></div>'
  ].join("");
}
function syncNames(goals) {
  const names = goals.map(g => g.name);
  order = order.filter(name => names.includes(name));
  for (const name of names) if (!order.includes(name)) { order.push(name); selected.add(name); }
  for (const name of [...selected]) if (!names.includes(name)) selected.delete(name);
}
function button(row, name, action) {
  const b = document.createElement("button");
  b.type = "button"; b.textContent = name;
  b.dataset.controlButton = "secondary";
  b.addEventListener("click", action); row.appendChild(b); return b;
}
function render() {
  if (!latest || manager?.activeView?.name !== "loopModal") return;
  $("loop-map").textContent = latest.map_name || "--";
  $("loop-mode").textContent = latest.mode || "--";
  const piConnected = latest.connected === true;
  const mapReady = Boolean(latest.map_name) && latest.navigation_mode === "DRIVING";
  $("loop-connection").textContent = piConnected
    ? "Pi: 연결됨" : "Pi: 미연결 (Modal 조회 가능)";
  syncNames(latest.goals || []);
  const manual = latest.mode === "MANUAL";
  const playing = latest.playing === true, recording = latest.recording === true;
  const list = $("loop-goals"); list.replaceChildren();
  for (let i = 0; i < order.length; i++) {
    const name = order[i], row = document.createElement("div");
    row.className = "loop-goal-row"; row.setAttribute("role", "listitem");
    const check = document.createElement("input"); check.type = "checkbox";
    check.checked = selected.has(name); check.disabled = busy || playing || recording;
    check.setAttribute("aria-label", name + " 선택");
    check.addEventListener("change", () => { if (check.checked) selected.add(name); else selected.delete(name); render(); });
    const label = document.createElement("span"); label.className = "loop-goal-label"; label.textContent = name;
    row.append(check, label);
    const up = button(row, "↑", () => { [order[i - 1], order[i]] = [order[i], order[i - 1]]; render(); });
    const down = button(row, "↓", () => { [order[i + 1], order[i]] = [order[i], order[i + 1]]; render(); });
    up.disabled = playing || recording || i === 0;
    down.disabled = playing || recording || i === order.length - 1;
    list.appendChild(row);
  }
  if (!order.length) list.textContent = "등록된 Goal 없음";
  $("loop-help").textContent = !piConnected
    ? "Pi 연결 전에는 Goal·기록 조회만 가능하며 Record/Start는 사용할 수 없습니다."
    : !mapReady
      ? "DRIVING Map이 준비되면 기록·주행할 수 있습니다."
      : manual
        ? "MANUAL: 모터 명령과 시간을 기록·반복 재생합니다. Goal은 시각화 전용입니다."
        : "AUTO: 선택한 Goal을 순서대로 Nav2로 순찰합니다.";
  const bags = $("loop-bags"); bags.hidden = !manual; bags.replaceChildren();
  for (const bag of latest.recordings || []) {
    const option = document.createElement("option");
    option.value = bag.name; option.textContent = bag.name + " (" + bag.events + " events)";
    bags.appendChild(option);
  }
  if ([...bags.options].some(x => x.value === chosenBag)) bags.value = chosenBag;
  chosenBag = bags.value || ""; bags.disabled = playing || recording;
  $("loop-record").disabled = busy || playing || recording || !manual || !piConnected || !mapReady;
  $("loop-save").disabled = busy || !recording || !piConnected;
  $("loop-delete").disabled = busy || playing || !mapReady || (!recording && !chosenBag);
  $("loop-start").disabled = busy || playing || recording || !piConnected || !mapReady ||
      (manual ? !chosenBag || policy === "REPLAN" : order.filter(n => selected.has(n)).length < 2);
  $("loop-stop").disabled = busy || !playing;
  $("loop-status").textContent = (latest.status || "IDLE") +
      (latest.lap ? " / Lap " + latest.lap : "") + (latest.error ? " / " + latest.error : "");
  $("loop-policy-toggle").textContent = policy === "WAIT" ? "Wait" : "Replan";
  window.DabomDashboardComponents?.controls?.controlButton?.enhanceAll?.(manager.body);
}
async function refresh() {
  const ticket = ++generation;
  try {
    const data = await api("state");
    if (ticket < generation || ticket <= barrier || busy) return;
    if (latest?.map_name !== data.map_name) { order = []; selected.clear(); chosenBag = ""; }
    latest = data; window.dabomLoopSessionState = data;
    document.dispatchEvent(new CustomEvent("dabom:loop-session-state", { detail: data }));
    const viewer = $("recordedLoopViewerStatus");
    if (viewer) {
      viewer.hidden = !["WAITING", "FAILED"].includes(data.status);
      viewer.textContent = viewer.hidden ? "" : (data.error || data.status);
      viewer.dataset.state = data.status || "IDLE";
    }
    render();
  } catch (error) {
    feedback("LOOP 상태 조회 실패: " + error.message);
    if ($("loop-status")) $("loop-status").textContent = "상태 확인 불가";
    if ($("loop-connection")) $("loop-connection").textContent = "Pi: 상태 확인 불가";
    for (const id of ["loop-record", "loop-save", "loop-start"]) {
      if ($(id)) $(id).disabled = true;
    }
    if ($("loop-stop")) $("loop-stop").disabled = false;
  }
}
async function mutate(path, data, message) {
  if (busy) return;
  busy = true; barrier = ++generation;
  try { await api(path, data); feedback(message); }
  catch (error) { feedback("실패: " + error.message); }
  finally { busy = false; await refresh(); }
}
function bind() {
  $("loop-bags").addEventListener("change", event => { chosenBag = event.target.value; render(); });
  $("loop-record").addEventListener("click", () => void mutate("record", {}, "Record 시작"));
  $("loop-save").addEventListener("click", () => void mutate("save", {}, "Save 완료"));
  $("loop-delete").addEventListener("click", () => {
    const label = latest?.recording ? "현재 기록" : chosenBag;
    if (label && window.confirm(label + "을 삭제하시겠습니까?"))
      void mutate("delete", { name: chosenBag }, "Delete 완료");
  });
  const Cycle = window.DabomDashboardComponents?.controls?.CycleFilterButton;
  if (typeof Cycle !== "function") throw Error("CycleFilterButton unavailable");
  new Cycle($("loop-policy-toggle"), {
    options: [{value:"WAIT",label:"Wait"},{value:"REPLAN",label:"Replan"}],
    index: policy === "REPLAN" ? 1 : 0,
    onChange(value) {
      policy = value;
      feedback(value === "REPLAN" && latest?.mode === "MANUAL" ? "MANUAL에서는 Wait만 지원합니다." : "");
      render();
    }
  });
  $("loop-start").addEventListener("click", () => void mutate("start", {
    policy, name: chosenBag, goals: order.filter(name => selected.has(name))
  }, "Loop 시작"));
  $("loop-stop").addEventListener("click", () => void mutate("stop", {}, "Stop 요청"));
  render();
}
let modalRegistered = false;
function ensureLoopModal() {
  if (modalRegistered && manager) return manager;
  const component = window.DabomDashboardComponents?.modal;
  manager = component?.getDefault?.() || null;
  if (!manager && document.getElementById("commonModal")) {
    // Dashboard initialization may fail for unrelated Pi/runtime reasons.
    // The shared modal is still available and can be mounted independently.
    manager = component?.mount?.({
      root: "#commonModal", title: "#modalTitle", body: "#modalBody"
    }) || null;
  }
  if (!manager) return null;
  manager.register("loopModal", { title: "LOOP", render: layout,
    onOpen: async () => { bind(); await refresh(); } });
  modalRegistered = true;
  return manager;
}
function init() {
  // A missing Pi, map or an unrelated dashboard init failure must never
  // disable the LOOP entry point. Retry mounting on the actual user click.
  trigger.disabled = false;
  trigger.removeAttribute("aria-disabled");
  trigger.classList.remove("disabled");
  ensureLoopModal();
  trigger.addEventListener("click", async () => {
    const modal = ensureLoopModal();
    if (!modal) {
      window.alert("LOOP Modal 컴포넌트를 초기화하지 못했습니다. 페이지를 새로고침해주세요.");
      return;
    }
    await modal.open("loopModal", {}, { replace: true });
  });
  document.addEventListener("dabom:loop-goal-picked", event => {
    if (event.detail?.pose) void mutate("goal", { pose: event.detail.pose }, "Goal 업데이트");
  });
  void refresh();
  setInterval(() => { if (!document.hidden && !busy) void refresh(); }, 1000);
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
else init();
})();
