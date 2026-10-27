/* Recorded Loop controls: isolated from PR #93's navigation_control.js. */
(() => {
    "use strict";
    const root = document.getElementById("recordedLoopPanel");
    if (!root) return;
    const $ = id => root.querySelector("#" + id);
    const feedback = $("recorded-feedback");
    let state = null;
    let busy = false;
    let csrf = null;
    let refreshGeneration = 0;
    let refreshApplied = 0;
    let refreshBarrier = 0;
    const show = message => { if (feedback) feedback.textContent = message; };
    async function api(path, data) {
        const options = { credentials: "same-origin" };
        if (data !== undefined) {
            if (!csrf) {
                const result = await fetch("/api/auth/csrf", { credentials: "same-origin" });
                if (!result.ok) throw new Error("CSRF 인증 실패");
                csrf = (await result.json()).csrf_token;
            }
            options.method = "POST";
            options.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
            options.body = JSON.stringify(data);
        }
        const response = await fetch("/api/navigation/recorded/" + path, options);
        const body = await response.json().catch(() => ({}));
        if (!response.ok || body.ok !== true) {
            throw new Error(body.error || body.error_code || "요청 실패");
        }
        return body;
    }
    function render(data) {
        state = data;
        $("recorded-map").textContent = data.map_name || "--";
        const goals = Object.keys(data.data?.goals || {}).sort();
        const segments = Object.keys(data.data?.segments || {}).sort();
        $("recorded-goals").textContent = goals.join(", ") || "없음";
        $("recorded-segments").textContent = segments.join(", ") || "없음";
        $("recorded-status").textContent = data.recording?.active
            ? (data.recording.armed ? "Set Goal 시작 대기: " : "기록 중: ") +
              data.recording.segment_id +
              (data.recording.error ? " / 오류: " + data.recording.error : "")
            : "기록 대기";
        $("recorded-position").textContent = data.current_pose
            ? data.current_pose.x.toFixed(2) + ", " +
              data.current_pose.y.toFixed(2) : "위치 없음";
        const active = data.play_active === true;
        const replay = data.playback || {};
        const status = replay.label || "재생 준비";
        $("recorded-playback-state").textContent =
            status + (replay.lap ? " / " + replay.lap + "회 반복" : "") +
            (replay.reason ? " / " + replay.reason : "");
        $("recorded-policy").disabled = busy || active;
        $("recorded-play-route").disabled = busy || active;
        $("recorded-play-kind").disabled = busy || active;
        $("recorded-play-start").disabled = busy || active;
        $("recorded-play-stop").disabled = busy || !active;
        $("recorded-play-reset").disabled = busy || active;
        $("recorded-delete").disabled = busy || active || Boolean(data.recording?.active);
        const viewer = document.getElementById("recordedLoopViewerStatus");
        if (viewer) {
            const visible = ["WAITING", "REPLANNING", "FAILED"].includes(replay.state);
            viewer.hidden = !visible;
            viewer.textContent = visible ? status : "";
            viewer.dataset.state = String(replay.state || "IDLE");
        }
        for (const id of ["recorded-add-goal", "recorded-start", "recorded-save",
                          "recorded-discard", "recorded-save-route"]) {
            const button = $(id);
            if (button) button.disabled = busy;
        }
        $("recorded-start").disabled = busy || Boolean(data.recording?.active);
        $("recorded-save").disabled = busy || !data.recording?.active;
        $("recorded-discard").disabled = busy || !data.recording?.active;
    }
    async function refresh(silent = false) {
        const generation = ++refreshGeneration;
        try {
            const data = await api("state");
            if (generation < refreshApplied || generation <= refreshBarrier || busy) return;
            refreshApplied = generation;
            render(data);
        } catch (e) {
            if (generation < refreshApplied || generation <= refreshBarrier || busy) return;
            refreshApplied = generation;
            if (!silent) show(e.message);
            $("recorded-status").textContent = "연결 확인 필요";
            $("recorded-playback-state").textContent = "상태 확인 불가";
            // Playback may have started even if its state cannot be read.
            // Block changes, but let the operator attempt a STOP.
            for (const id of [
                "recorded-policy", "recorded-play-route", "recorded-play-kind",
                "recorded-play-start", "recorded-play-reset", "recorded-delete",
                "recorded-add-goal", "recorded-start", "recorded-save",
                "recorded-discard", "recorded-save-route"
            ]) {
                $(id).disabled = true;
            }
            $("recorded-play-stop").disabled = false;
        }
    }
    async function mutate(operation, payload, okText) {
        if (busy) return;
        busy = true;
        // Invalidate state requests started before this mutation.
        refreshBarrier = ++refreshGeneration;
        if (state) render(state);
        try {
            await api(operation, payload);
            show(okText);
        } catch (e) {
            show("실패: " + e.message);
        } finally {
            busy = false;
            await refresh(true);
        }
    }
    $("recorded-add-goal").addEventListener("click", () => {
        const id = $("recorded-goal-id").value.trim();
        if (!id || !state?.current_pose) {
            show("Goal 이름과 현재 AMCL 위치가 필요합니다.");
            return;
        }
        void mutate("goal", { id, pose: state.current_pose }, "Goal 저장: " + id);
    });
    $("recorded-start").addEventListener("click", () => {
        const data = {
            segment_id: $("recorded-segment-id").value.trim(),
            start: $("recorded-from").value.trim(),
            end: $("recorded-to").value.trim(),
            source: $("recorded-source").value
        };
        if (Object.values(data).some(v => !v)) {
            show("출발/도착 Goal 및 Segment 이름을 입력해주세요.");
            return;
        }
        void mutate("record/start", data, "주행 기록을 시작했습니다.");
    });
    $("recorded-save").addEventListener("click", () => void mutate(
        "record/stop", {}, "기록 및 경로 저장 완료"
    ));
    $("recorded-discard").addEventListener("click", () => void mutate(
        "record/discard", {}, "현재 기록을 폐기했습니다."
    ));
    $("recorded-save-route").addEventListener("click", () => {
        const ids = $("recorded-route-segments").value.split(",")
            .map(x => x.trim()).filter(Boolean);
        void mutate("route", {
            id: $("recorded-route-id").value.trim(),
            segments: ids
        }, "반복 경로 구성을 저장했습니다.");
    });
    $("recorded-play-start").addEventListener("click", () => {
        const kind = $("recorded-play-kind").value;
        void mutate("replay/start", {
            kind,
            route_id: $("recorded-play-route").value.trim(),
            policy: $("recorded-policy").value,
            max_laps: kind === "path" ? 1 : null
        }, kind === "path" ? "기록된 Path 1회 주행을 시작했습니다." : "반복 주행을 시작했습니다.");
    });
    $("recorded-play-stop").addEventListener("click", () =>
        void mutate("replay/stop", {}, "반복 주행 정지 요청 완료"));
    $("recorded-play-reset").addEventListener("click", () =>
        void mutate("replay/reset", {}, "재생 상태 초기화 완료"));
    $("recorded-delete").addEventListener("click", () => {
        const kind = $("recorded-delete-kind").value;
        const id = $("recorded-delete-id").value.trim();
        if (!id || !window.confirm(kind + " " + id + "를 삭제하시겠습니까?")) return;
        void mutate("delete", { kind, id }, "삭제 완료");
    });
    refresh(true);
    setInterval(() => { if (!document.hidden && !busy) void refresh(true); }, 1000);
})();
