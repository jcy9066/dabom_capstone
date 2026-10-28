/* Session-only LOOP controls: shared DrawerToggle, NavigationModeToggle and ControlButton. */
(() => {
"use strict";
const $ = id => document.getElementById(id);
const trigger = $("loop-modal-trigger");
const drawer = $("loop-side-drawer");
if (!trigger || !drawer) return;
let latest = null, csrf = null, busy = false, generation = 0, barrier = 0;
let policy = "WAIT", order = [], selected = new Set(), chosenBag = "";
let obstacleToggle = null;

async function api(path, body) {
  const options = { credentials: "same-origin" };
  if (body !== undefined) {
    if (!csrf) {
      const response = await fetch("/api/auth/csrf", { credentials: "same-origin" });
      if (!response.ok) throw Error("CSRF unavailable");
      csrf = (await response.json()).csrf_token;
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
function feedback(message) { $("loop-feedback").textContent = message; }
function controls() { return window.DabomDashboardComponents?.controls; }
function isOpen() { return drawer.dataset.open === "true"; }
function setDrawerOpen(open) {
  const component = controls()?.drawerToggle;
  if (!component?.setOpen) {
    feedback("DrawerToggle component unavailable");
    return;
  }
  component.setOpen("loop-side-drawer", open);
  // DrawerToggle.setOpen() returns the requested open state; false is the
  // correct result on close, not a failure.
  if (isOpen() !== Boolean(open)) feedback("DrawerToggle component unavailable");
}
function syncNames(goals) {
  const names = goals.map(goal => goal.name);
  order = order.filter(name => names.includes(name));
  for (const name of names) if (!order.includes(name)) {
    order.push(name);
    selected.add(name);
  }
  for (const name of [...selected]) if (!names.includes(name)) selected.delete(name);
}
function addGoalControl(row, label, callback) {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.controlButton = "secondary";
  button.textContent = label;
  button.addEventListener("click", callback);
  row.appendChild(button);
  return button;
}
function enhanceControls() {
  controls()?.controlButton?.enhanceAll?.(drawer);
}
function render() {
  if (!latest || !isOpen()) return;
  $("loop-map").textContent = latest.map_name || "--";
  $("loop-mode").textContent = latest.mode || "--";
  const connected = latest.connected === true;
  const mapReady = Boolean(latest.map_name) && latest.navigation_mode === "DRIVING";
  $("loop-connection").textContent = connected ? "Pi: 연결됨" : "Pi: 미연결";
  const manual = latest.mode === "MANUAL";
  const playing = latest.playing === true, recording = latest.recording === true;
  syncNames(latest.goals || []);
  const list = $("loop-goals");
  list.replaceChildren();
  for (let i = 0; i < order.length; i++) {
    const name = order[i], row = document.createElement("div");
    row.className = "loop-goal-row";
    row.setAttribute("role", "listitem");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = selected.has(name);
    check.disabled = busy || playing || recording;
    check.setAttribute("aria-label", name + " 선택");
    check.addEventListener("change", () => {
      if (check.checked) selected.add(name);
      else selected.delete(name);
      render();
    });
    const caption = document.createElement("span");
    caption.className = "loop-goal-label";
    caption.textContent = name;
    row.append(check, caption);
    const up = addGoalControl(row, "↑", () => {
      if (i === 0) return;
      [order[i - 1], order[i]] = [order[i], order[i - 1]];
      render();
    });
    const down = addGoalControl(row, "↓", () => {
      if (i === order.length - 1) return;
      [order[i + 1], order[i]] = [order[i], order[i + 1]];
      render();
    });
    up.disabled = busy || playing || recording || i === 0;
    down.disabled = busy || playing || recording || i === order.length - 1;
    list.appendChild(row);
  }
  if (!order.length) list.textContent = "등록된 Goal 없음";
  $("loop-help").textContent = !connected
    ? "Pi 연결 전에는 Goal·기록 조회만 가능합니다."
    : !mapReady
      ? "DRIVING Map 준비 후 기록·주행할 수 있습니다."
      : manual
        ? "MANUAL: 모터 명령과 시간 간격을 반복 재생합니다."
        : "AUTO: 선택한 Goal을 순서대로 Nav2로 주행합니다.";
  const recordings = Array.isArray(latest.recordings) ? latest.recordings : [];
  const bagList = $("loop-bags");
  const showBagList = manual && recordings.length > 0;
  bagList.hidden = !showBagList;
  bagList.disabled = !showBagList || busy || playing || recording;
  bagList.setAttribute("aria-hidden", String(!showBagList));
  bagList.replaceChildren();
  for (const bag of showBagList ? recordings : []) {
    const item = document.createElement("option");
    item.value = bag.name;
    item.textContent = bag.name + " (" + bag.events + " events)";
    bagList.appendChild(item);
  }
  if (showBagList && [...bagList.options].some(option => option.value === chosenBag)) {
    bagList.value = chosenBag;
  }
  chosenBag = showBagList ? bagList.value || "" : "";
  $("loop-record").disabled = busy || playing || recording || !manual || !connected || !mapReady;
  $("loop-save").disabled = busy || !recording || !connected;
  $("loop-delete").disabled = busy || playing || !mapReady || (!recording && !chosenBag);
  $("loop-start").disabled = busy || playing || recording || !connected || !mapReady
    || (manual ? !chosenBag || policy === "REPLAN" : order.filter(name => selected.has(name)).length < 2);
  $("loop-stop").disabled = busy || !playing;
  $("loop-status").textContent = (latest.status || "IDLE")
    + (latest.lap ? " / Lap " + latest.lap : "")
    + (latest.error ? " / " + latest.error : "");
  obstacleToggle?.sync(policy === "REPLAN" ? "DRIVING" : "MAPPING");
  enhanceControls();
}
async function refresh() {
  const ticket = ++generation;
  try {
    const data = await api("state");
    if (ticket < generation || ticket <= barrier || busy) return;
    if (latest?.map_name !== data.map_name) {
      order = [];
      selected.clear();
      chosenBag = "";
    }
    latest = data;
    window.dabomLoopSessionState = data;
    document.dispatchEvent(new CustomEvent("dabom:loop-session-state", { detail: data }));
    const viewer = $("recordedLoopViewerStatus");
    if (viewer) {
      viewer.hidden = !["WAITING", "FAILED"].includes(data.status);
      viewer.textContent = viewer.hidden ? "" : (data.error || data.status);
      viewer.dataset.state = data.status || "IDLE";
    }
    render();
  } catch (error) {
    if (!isOpen()) return;
    feedback("LOOP 상태 조회 실패: " + error.message);
    $("loop-status").textContent = "상태 확인 불가";
    $("loop-connection").textContent = "Pi: 상태 확인 불가";
    for (const id of ["loop-record", "loop-save", "loop-start"]) $(id).disabled = true;
    // STOP must remain actionable when a previously started replay might
    // still be running despite a failed state request.
    $("loop-stop").disabled = false;
  }
}
async function mutate(path, body, message) {
  if (busy) return;
  busy = true;
  barrier = ++generation;
  try { await api(path, body); feedback(message); }
  catch (error) { feedback("실패: " + error.message); }
  finally { busy = false; await refresh(); }
}
function bindOnce() {
  $("loop-bags").addEventListener("change", event => { chosenBag = event.target.value; render(); });
  $("loop-record").addEventListener("click", () => void mutate("record", {}, "Record 시작"));
  $("loop-save").addEventListener("click", () => void mutate("save", {}, "Save 완료"));
  $("loop-delete").addEventListener("click", () => {
    const label = latest?.recording ? "현재 기록" : chosenBag;
    if (label && window.confirm(label + "을 삭제하시겠습니까?")) {
      void mutate("delete", { name: chosenBag }, "Delete 완료");
    }
  });
  $("loop-start").addEventListener("click", () => void mutate("start", {
    policy, name: chosenBag, goals: order.filter(name => selected.has(name))
  }, "Loop 시작"));
  $("loop-stop").addEventListener("click", () => void mutate("stop", {}, "Stop 요청"));
  $("loop-drawer-close").addEventListener("click", () => setDrawerOpen(false));
  // Reuse the existing navigation-mode Toggle component in isolated mode.
  // Obstacle policy cannot be mutated by ROS navigation-mode broadcasts.
  obstacleToggle = controls()?.mountNavigationMode?.(
    $("loop-obstacle-toggle-mount"),
    { request(value) {
      policy = value === "DRIVING" ? "REPLAN" : "WAIT";
      feedback(policy === "REPLAN" && latest?.mode === "MANUAL"
        ? "MANUAL Bag에서는 Wait만 지원합니다." : "");
      render();
      return true;
    }},
    { isolated: true, labels: { MAPPING: "Wait", DRIVING: "Replan" } }
  );
  if (!obstacleToggle) {
    feedback("NavigationModeToggle component unavailable");
  }
}
function init() {
  trigger.disabled = false;
  trigger.removeAttribute("aria-disabled");
  trigger.classList.remove("disabled");
  const drawerComponent = controls()?.drawerToggle;
  // The trigger is managed only by the existing DrawerToggle component.
  drawerComponent?.enhance?.(trigger);
  document.addEventListener("dabom:drawer-toggle", event => {
    if (event.detail?.targetId !== "loop-side-drawer") return;
    const opened = event.detail.open === true;
    drawer.inert = !opened;
    drawer.setAttribute("aria-hidden", String(!opened));
    if (opened) {
      enhanceControls();
      render();
      void refresh();
      $("loop-drawer-close").focus();
    } else {
      trigger.focus();
    }
  });
  bindOnce();
  enhanceControls();
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && isOpen()) {
      event.preventDefault();
      setDrawerOpen(false);
    }
  });
  document.addEventListener("dabom:loop-goal-picked", event => {
    if (event.detail?.pose) void mutate("goal", { pose: event.detail.pose }, "Goal 업데이트");
  });
  void refresh();
  setInterval(() => {
    if (!document.hidden && !busy) void refresh();
  }, 1000);
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
})();
