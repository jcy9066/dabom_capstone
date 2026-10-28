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
    '<div class="loop-modal-info"><span>Map: <strong id="loop-map">--</strong></span><span>Mode: <strong id="loop-mode">--</strong></span></div>',
    '<section class="loop-modal-section"><h3>GOALS (A–Z, a–z)</h3>',
    '<p class="loop-modal-hint">3D Viewer에서 SET LOOP을 켜고 SET GOAL로 위치를 선택합니다. 동일 위치를 다시 클릭하면 삭제합니다.</p>',
    '<div id="loop-goals" class="loop-goal-list" role="list"></div></section>',
    '<section class="loop-modal-section"><h3>RECORD / PLAY</h3>',
    '<p class="loop-modal-hint" id="loop-help"></p>',
    '<select id="loop-bags" aria-label="Recorded manual loops"></select>',
    '<div class="loop-modal-buttons">',
    '<button type="button" id="loop-record" data-control-button="secondary">Record</button>',
    '<button type="button" id="loop-save" data-control-button="success">Save</button>',
    '<button type="button" id="loop-delete" data-control-button="danger">Delete</button>',
    '</div></section>',
    '<section class="loop-modal-section"><h3>OBSTACLE</h3>',
    '<div class="loop-policy" role="group" aria-label="Obstacle policy">',
    '<button type="button" id="loop-policy-wait" data-control-button="view" aria-pressed="true">Wait</button>',
    '<button type="button" id="loop-policy-replan" data-control-button="view" aria-pressed="false">Replan</button></div>',
    '<div class="loop-modal-buttons">',
    '<button type="button" id="loop-start" data-control-button="accent">Start</button>',
    '<button type="button" id="loop-stop" data-control-button="danger">Stop</button>',
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
  $("loop-help").textContent = manual
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
  $("loop-record").disabled = busy || playing || recording || !manual;
  $("loop-save").disabled = busy || !recording;
  $("loop-delete").disabled = busy || playing || (!recording && !chosenBag);
  $("loop-start").disabled = busy || playing || recording ||
      (manual ? !chosenBag || policy === "REPLAN" : order.filter(n => selected.has(n)).length < 2);
  $("loop-stop").disabled = busy || !playing;
  $("loop-status").textContent = (latest.status || "IDLE") +
      (latest.lap ? " / Lap " + latest.lap : "") + (latest.error ? " / " + latest.error : "");
  $("loop-policy-wait").setAttribute("aria-pressed", String(policy === "WAIT"));
  $("loop-policy-replan").setAttribute("aria-pressed", String(policy === "REPLAN"));
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
    feedback(error.message);
    if ($("loop-status")) $("loop-status").textContent = "상태 확인 불가";
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
  for (const [id, value] of [["loop-policy-wait", "WAIT"], ["loop-policy-replan", "REPLAN"]]) {
    $(id).addEventListener("click", () => {
      policy = value;
      feedback(value === "REPLAN" && latest?.mode === "MANUAL"
          ? "MANUAL Bag에는 Replan 목적지가 없습니다. Wait를 선택하세요." : "");
      render();
    });
  }
  $("loop-start").addEventListener("click", () => void mutate("start", {
    policy, name: chosenBag, goals: order.filter(name => selected.has(name))
  }, "Loop 시작"));
  $("loop-stop").addEventListener("click", () => void mutate("stop", {}, "Stop 요청"));
  render();
}
function init() {
  manager = window.DabomDashboardComponents?.modal?.getDefault?.();
  if (!manager) { trigger.disabled = true; return; }
  manager.register("loopModal", { title: "LOOP", render: layout,
    onOpen: async () => { bind(); await refresh(); } });
  trigger.addEventListener("click", () => void manager.open("loopModal", {}, { replace: true }));
  document.addEventListener("dabom:loop-goal-picked", event => {
    if (event.detail?.pose) void mutate("goal", { pose: event.detail.pose }, "Goal 업데이트");
  });
  void refresh();
  setInterval(() => { if (!document.hidden && !busy) void refresh(); }, 1000);
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
else init();
})();
