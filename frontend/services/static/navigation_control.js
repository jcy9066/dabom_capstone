(() => {
    'use strict';

    const POLL_MS = 750;
    const HIDDEN_POLL_MS = 2000;
    const CONTROL_REFRESH_TIMEOUT_MS = 700;
    const DRIVE_MODE_CONFIRM_TIMEOUT_MS = 5000;
    const MAX_HAZARD_ENTRIES = 50;
    const state = {
        control: null,
        draftGoal: null,
        pointerId: null,
        pointerStart: null,
        csrf: null,
        busy: false,
        drivePending: false,
        navigationPending: false,
        estopPending: false,
        warningPending: false,
        estopCooldownUntil: 0,
        controlWaiters: new Set(),
        previousConnected: null,
        previousEmergencyStop: null,
        lastHazardCode: null,
        hazardEntries: [],
        hazardSequence: 0,
        hazardObserver: null,
        hazardReconcileScheduled: false,
        controlRefreshPromise: null,
        controlPollTimer: null,
    };

    const $ = id => document.getElementById(id);

    function userMessageForError(code, fallback, status) {
        const messages = {
            PI_OFFLINE: 'Pi가 연결되지 않아 명령을 전달하지 못했습니다.',
            PI_RESUME_FAILED: 'Pi가 정지 해제 명령을 수락하지 않았습니다.',
            DRIVING_MODE_REQUIRED: 'Driving 모드에서만 실행할 수 있습니다.',
            NAVIGATION_NOT_READY: 'Localization과 Nav2 준비 상태를 확인해주세요.',
            NAVIGATION_STOP_CONFIRMATION_REQUIRED: '현재 주행을 중지한 뒤 Mapping 모드로 전환해야 합니다.',
            AUTO_MODE_REQUIRED: '자동 모드 전환을 확인한 뒤 다시 시도해주세요.',
            PI_DRIVING_MODE_REQUIRED: 'Pi의 Driving 모드 전환을 확인해주세요.',
            EMERGENCY_STOP_ACTIVE: '먼저 긴급 정지를 해제해주세요.',
            EMERGENCY_STOP_NOT_ACTIVE: '현재 긴급 정지 상태가 아닙니다.',
            PATH_REQUIRED: 'Goal을 지정하고 경로를 먼저 계산해주세요.',
            ACTIVE_MAP_REQUIRED: '저장 지도를 선택한 뒤 다시 시도해주세요.',
            GOAL_OUT_OF_BOUNDS: '활성 지도 안쪽에 Goal을 지정해주세요.',
            PATH_NOT_FOUND: '주행 가능한 경로를 찾지 못했습니다.',
            WATCHDOG_NOT_READY: 'Navigation 안전 상태를 확인해주세요.',
            NAV2_NOT_READY: 'Nav2 준비가 완료되지 않았습니다.',
        };
        return messages[String(code || '').toUpperCase()]
            || fallback
            || (status === 409 ? '현재 Navigation 상태와 요청이 충돌했습니다.' : `HTTP ${status}`);
    }

    async function requestJson(url, options = {}) {
        const response = await fetch(url, { credentials: 'same-origin', ...options });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.ok === false) {
            const error = new Error(userMessageForError(
                payload.error_code,
                payload.error || payload.detail,
                response.status,
            ));
            error.code = payload.error_code;
            error.status = response.status;
            throw error;
        }
        return payload;
    }

    async function csrfToken() {
        if (state.csrf) return state.csrf;
        const payload = await requestJson('/api/auth/csrf');
        state.csrf = payload.csrf_token;
        return state.csrf;
    }

    async function mutate(path, payload = {}, keepalive = false) {
        const token = await csrfToken();
        return requestJson(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
            body: JSON.stringify(payload),
            keepalive,
        });
    }

    function dashboardConfig(payload = state.control) {
        const config = payload?.dashboard_config || {};
        const estopCooldownSec = Number(config.estop_cooldown_sec);
        const goalReachedToleranceM = Number(config.goal_reached_tolerance_m);
        return {
            estopCooldownSec: Number.isFinite(estopCooldownSec) && estopCooldownSec >= 0
                ? estopCooldownSec
                : null,
            goalReachedToleranceM: Number.isFinite(goalReachedToleranceM) && goalReachedToleranceM >= 0
                ? goalReachedToleranceM
                : null,
        };
    }

    function waitForControl(predicate, timeoutMs = DRIVE_MODE_CONFIRM_TIMEOUT_MS) {
        if (predicate(state.control)) return Promise.resolve(state.control);
        return new Promise((resolve, reject) => {
            const waiter = { predicate, resolve, reject, timer: null };
            waiter.timer = window.setTimeout(() => {
                state.controlWaiters.delete(waiter);
                reject(new Error('서버 상태 확인 시간이 초과되었습니다.'));
            }, timeoutMs);
            state.controlWaiters.add(waiter);
        });
    }

    function resolveControlWaiters(payload) {
        for (const waiter of Array.from(state.controlWaiters)) {
            if (!waiter.predicate(payload)) continue;
            window.clearTimeout(waiter.timer);
            state.controlWaiters.delete(waiter);
            waiter.resolve(payload);
        }
    }

    function emitHazard(code, message, level = 'warning') {
        document.dispatchEvent(new CustomEvent('dabom:navigation-hazard', {
            detail: { code, message, level, timestamp: new Date().toISOString() },
        }));
    }

    function createNavigationHazardRow(detail) {
        const row = document.createElement('div');
        row.className = `alert-entry ${detail.level === 'danger' ? 'alert-danger' : 'alert-warning'}`;
        row.dataset.navigationHazard = detail.id;
        const time = document.createElement('span');
        time.className = 'alert-time';
        const timestamp = new Date(detail.timestamp || Date.now());
        time.textContent = Number.isNaN(timestamp.getTime())
            ? '--:--:--'
            : timestamp.toLocaleTimeString('ko-KR', { hour12: false });
        const message = document.createElement('span');
        message.className = 'alert-message';
        message.textContent = String(detail.message);
        row.append(time, message);
        return row;
    }

    function hasExplicitAlertClear(target) {
        return Array.from(target.querySelectorAll('.alert-message')).some(element => (
            element.textContent.includes('알림 내역이 삭제되었습니다')
        ));
    }

    function reconcileNavigationHazards(respectClear = true) {
        const target = $('alertBox');
        if (!target) return;
        if (respectClear && hasExplicitAlertClear(target)) {
            state.hazardEntries = [];
            target.querySelectorAll('[data-navigation-hazard]').forEach(element => element.remove());
            return;
        }
        const existing = Array.from(target.querySelectorAll('[data-navigation-hazard]'));
        const existingIds = existing.map(element => element.dataset.navigationHazard);
        const expectedIds = state.hazardEntries.map(entry => entry.id);
        if (
            existingIds.length === expectedIds.length
            && existingIds.every((id, index) => id === expectedIds[index])
        ) return;
        existing.forEach(element => element.remove());
        for (const entry of [...state.hazardEntries].reverse()) {
            target.prepend(createNavigationHazardRow(entry));
        }
    }

    function scheduleHazardReconcile() {
        if (state.hazardReconcileScheduled) return;
        state.hazardReconcileScheduled = true;
        const schedule = window.queueMicrotask || (callback => Promise.resolve().then(callback));
        schedule(() => {
            state.hazardReconcileScheduled = false;
            reconcileNavigationHazards(true);
        });
    }

    function renderNavigationHazard(event) {
        const detail = event?.detail || {};
        const target = $('alertBox');
        if (!target || !detail.message) return;
        target.querySelectorAll('.alert-entry').forEach(element => {
            if (element.querySelector('.alert-message')?.textContent.includes('알림 내역이 삭제되었습니다')) {
                element.remove();
            }
        });
        const entry = {
            code: String(detail.code || 'NAVIGATION_HAZARD'),
            id: `${detail.timestamp || new Date().toISOString()}:${detail.code || 'NAVIGATION_HAZARD'}:${state.hazardSequence++}`,
            level: detail.level === 'danger' ? 'danger' : 'warning',
            message: String(detail.message),
            timestamp: detail.timestamp || new Date().toISOString(),
        };
        state.hazardEntries.unshift(entry);
        state.hazardEntries.length = Math.min(state.hazardEntries.length, MAX_HAZARD_ENTRIES);
        reconcileNavigationHazards(false);
    }

    function updateHazardHooks(payload) {
        const connected = payload?.connected === true;
        const stopped = Boolean(payload?.emergency_stop);
        if (state.previousConnected !== connected && !connected) {
            emitHazard('PI_CONNECTION_LOSS', '⚠️ Pi 연결 끊김', 'warning');
        }
        if (state.previousEmergencyStop !== null && state.previousEmergencyStop !== stopped) {
            emitHazard(
                stopped ? 'EMERGENCY_STOP_ACTIVE' : 'EMERGENCY_STOP_CLEARED',
                stopped ? '긴급 정지 활성화' : '긴급 정지 해제',
                stopped ? 'danger' : 'warning',
            );
        }
        const code = String(payload?.emergency_reason || payload?.last_error || '').toUpperCase();
        const messages = {
            LIDAR_DATA_LOSS: '⚠️ LiDAR 데이터 손실',
            ODOMETRY_LOSS: '⚠️ Odometry 손실',
            LOCALIZATION_LOST: '⚠️ Localization 손실',
            REQUIRED_TF_FAILURE: '⚠️ 필수 TF 오류',
            PI_CONNECTION_LOSS: '⚠️ Pi 연결 끊김',
        };
        if (messages[code] && state.lastHazardCode !== code) emitHazard(code, messages[code], 'warning');
        state.lastHazardCode = messages[code] ? code : null;
        state.previousConnected = connected;
        state.previousEmergencyStop = stopped;
    }

    function syncControlComponents(payload = state.control) {
        if (!payload) return;
        const controls = window.DabomDashboardComponents?.controls;
        controls?.driveMode?.sync(payload.robot_mode, {
            connected: payload.connected === true,
            pending: state.drivePending,
        });
        controls?.syncNavigationMode?.(payload.navigation_mode, {
            pending: state.navigationPending,
        });
        controls?.emergencyStop?.sync({
            active: Boolean(payload.emergency_stop),
            connected: payload.connected === true && dashboardConfig(payload).estopCooldownSec !== null,
            pending: state.estopPending,
            cooldownUntil: state.estopCooldownUntil,
        });
        const warningButton = document.querySelector('.action-warning');
        if (warningButton) {
            warningButton.disabled = payload.connected !== true || state.warningPending;
            warningButton.setAttribute('aria-busy', String(state.warningPending));
            warningButton.title = payload.connected === true
                ? 'Pi 스피커와 LED로 경고합니다.'
                : 'Pi가 연결되어야 경고할 수 있습니다.';
        }
    }

    function setFeedback(message, error = false) {
        const element = $('navigation-control-feedback');
        if (!element) return;
        element.textContent = message || '';
        element.classList.toggle('error', error);
    }

    function applyControlState(payload) {
        state.control = payload;
        window.setDashboardRobotConnection?.(payload?.connected === true);
        window.applyServerPatrolMode?.(payload?.robot_mode);
        const mode = payload?.navigation_mode || 'UNKNOWN';
        const navState = payload?.navigation_state || 'UNKNOWN';
        const modeLabel = $('navigation-mode-label');
        const stateLabel = $('navigation-state-label');
        if (modeLabel) modeLabel.textContent = mode;
        if (stateLabel) stateLabel.textContent = navState;
        $('navigation-mode-mapping')?.classList.toggle('active', mode === 'MAPPING');
        $('navigation-mode-driving')?.classList.toggle('active', mode === 'DRIVING');

        const ready = mode === 'DRIVING' && payload.localization_ready && payload.nav2_ready;
        const pathReady = navState === 'PATH_READY' && Array.isArray(payload.planned_path) && payload.planned_path.length > 1;
        const stopped = Boolean(payload.emergency_stop);
        if ($('navigation-start')) $('navigation-start').hidden = !pathReady;
        if ($('navigation-cancel')) $('navigation-cancel').hidden = !payload.active_goal;
        if ($('navigation-resume')) $('navigation-resume').hidden = !stopped;
        if ($('navigation-resume')) $('navigation-resume').textContent = '정지 해제';
        if ($('navigation-estop')) $('navigation-estop').classList.toggle('latched', stopped);
        if ($('navigation-estop')) $('navigation-estop').disabled = payload?.connected !== true || state.estopPending || Date.now() < state.estopCooldownUntil;
        if ($('navigation-resume')) $('navigation-resume').disabled = payload?.connected !== true || state.estopPending || Date.now() < state.estopCooldownUntil;
        const hint = $('navigation-goal-hint');
        if (hint) {
            hint.textContent = ready
                ? '확대 지도에서 누른 뒤 드래그하여 Goal 방향을 지정하세요.'
                : 'DRIVING 및 localization/Nav2 준비 후 Goal을 지정할 수 있습니다.';
        }
        syncControlComponents(payload);
        updateHazardHooks(payload);
        resolveControlWaiters(payload);
        document.dispatchEvent(new CustomEvent('dabom:navigation-control-state', { detail: payload }));
        window.navigationMapView?.requestRender();
    }

    function refreshState() {
        if (state.controlRefreshPromise) return state.controlRefreshPromise;
        const controller = new AbortController();
        const timeoutId = window.setTimeout(
            () => controller.abort(),
            CONTROL_REFRESH_TIMEOUT_MS,
        );
        state.controlRefreshPromise = requestJson(
            '/api/navigation/control/state',
            { signal: controller.signal },
        )
            .then(payload => {
                applyControlState(payload);
                return payload;
            })
            .catch(error => {
                setFeedback(`상태 조회 실패: ${error.message}`, true);
                return null;
            })
            .finally(() => {
                window.clearTimeout(timeoutId);
                state.controlRefreshPromise = null;
            });
        return state.controlRefreshPromise;
    }

    function clearControlPollTimer() {
        if (state.controlPollTimer !== null) {
            window.clearTimeout(state.controlPollTimer);
            state.controlPollTimer = null;
        }
    }

    function controlPollDelayMs() {
        return document.hidden ? HIDDEN_POLL_MS : POLL_MS;
    }

    function scheduleControlPoll(delay = controlPollDelayMs()) {
        clearControlPollTimer();
        state.controlPollTimer = window.setTimeout(() => {
            state.controlPollTimer = null;
            pollControlState();
        }, delay);
    }

    async function pollControlState() {
        clearControlPollTimer();
        await refreshState();
        scheduleControlPoll();
    }

    function canSetGoal() {
        const control = state.control;
        const view = window.navigationMapView?.snapshot();
        return Boolean(
            view?.expanded
            && control?.navigation_mode === 'DRIVING'
            && control?.localization_ready
            && control?.nav2_ready
            && !control?.emergency_stop
            && control?.connected === true
            && !state.drivePending
        );
    }

    function beginGoal(event) {
        if (event.button !== 0 && event.button !== 2) return;
        const view = window.navigationMapView?.snapshot();
        if (view?.expanded && state.control?.navigation_mode === 'MAPPING') {
            setFeedback('Mapping 모드에서는 주행 목표를 설정할 수 없습니다. Driving 모드로 전환해주세요.', true);
            return;
        }
        if (!canSetGoal()) {
            if (view?.expanded) setFeedback('Driving 모드와 Pi·Localization·Nav2 상태를 확인해주세요.', true);
            return;
        }
        const point = window.navigationMapView?.canvasToWorld(event);
        if (!point) {
            setFeedback('지도 밖에는 Goal을 지정할 수 없습니다.', true);
            return;
        }
        event.preventDefault();
        state.pointerId = event.pointerId;
        state.pointerStart = point;
        state.draftGoal = { ...point, yaw: 0 };
        event.currentTarget.setPointerCapture?.(event.pointerId);
        window.navigationMapView?.requestRender();
    }

    function moveGoal(event) {
        if (state.pointerId !== event.pointerId || !state.pointerStart) return;
        const point = window.navigationMapView?.canvasToWorld(event);
        if (!point) return;
        state.draftGoal = {
            ...state.pointerStart,
            yaw: Math.atan2(point.y - state.pointerStart.y, point.x - state.pointerStart.x),
        };
        window.navigationMapView?.requestRender();
    }

    async function finishGoal(event) {
        if (state.pointerId !== event.pointerId || !state.draftGoal) return;
        event.preventDefault();
        const goal = { ...state.draftGoal };
        state.pointerId = null;
        state.pointerStart = null;
        setFeedback('경로를 계산하는 중입니다...');
        try {
            if (String(state.control?.robot_mode || '').toUpperCase() === 'MANUAL') {
                setFeedback('자동 모드 전환을 확인하는 중입니다...');
                await requestDriveMode('AUTO', { announce: false });
            }
            if (String(state.control?.robot_mode || '').toUpperCase() !== 'AUTO') {
                throw new Error('AUTO 모드 전환을 확인하지 못해 Goal 등록을 중단했습니다.');
            }
            applyControlState(await mutate('/api/navigation/control/goal', { goal }));
            state.draftGoal = null;
            setFeedback('경로 미리보기가 준비되었습니다. 주행 시작 전에는 로봇이 움직이지 않습니다.');
        } catch (error) {
            state.draftGoal = null;
            setFeedback(`경로 계산 실패: ${error.message}`, true);
        } finally {
            window.navigationMapView?.requestRender();
        }
    }

    function cancelGoalDraft(event) {
        if (state.pointerId !== event.pointerId) return;
        state.pointerId = null;
        state.pointerStart = null;
        state.draftGoal = null;
        setFeedback('Goal 지정을 취소했습니다.');
        window.navigationMapView?.requestRender();
    }

    function drawArrow(ctx, goal, worldToCanvas, layout, color) {
        if (!goal) return;
        const point = worldToCanvas(goal.x, goal.y, layout);
        const length = Math.max(20, 0.45 * layout.scale);
        const endX = point.x + Math.cos(goal.yaw) * length;
        const endY = point.y - Math.sin(goal.yaw) * length;
        ctx.save();
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(point.x, point.y);
        ctx.lineTo(endX, endY);
        ctx.stroke();
        ctx.translate(endX, endY);
        ctx.rotate(-goal.yaw);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(-11, -6);
        ctx.lineTo(-11, 6);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
    }

    function drawGoalFlag(ctx, goal, worldToCanvas, layout) {
        if (!goal) return;
        const point = worldToCanvas(goal.x, goal.y, layout);
        const size = Math.max(24, Math.min(40, 0.62 * layout.scale));
        ctx.save();
        ctx.font = `${size}px 'Noto Sans KR', 'Noto Sans', sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'bottom';
        ctx.fillText('🚩', point.x, point.y + 5);
        ctx.restore();
    }

    window.navigationControlOverlay = {
        draw(ctx, layout, worldToCanvas) {
            const path = state.control?.planned_path || [];
            if (path.length > 1) {
                ctx.save();
                ctx.strokeStyle = '#991b1b';
                ctx.lineWidth = 3;
                ctx.shadowColor = 'rgba(153, 27, 27, 0.55)';
                ctx.shadowBlur = 5;
                ctx.beginPath();
                path.forEach((item, index) => {
                    const point = worldToCanvas(item.x, item.y, layout);
                    if (index === 0) ctx.moveTo(point.x, point.y);
                    else ctx.lineTo(point.x, point.y);
                });
                ctx.stroke();
                ctx.restore();
            }
            if (state.control?.active_goal) drawGoalFlag(ctx, state.control.active_goal, worldToCanvas, layout);
            if (state.draftGoal) drawArrow(ctx, state.draftGoal, worldToCanvas, layout, '#f59e0b');
        },
    };

    async function requestDriveMode(mode, options = {}) {
        const target = String(mode || '').toUpperCase();
        if (!['AUTO', 'MANUAL'].includes(target) || state.drivePending) return false;
        if (state.control?.connected !== true) {
            setFeedback('Pi가 연결되어 있지 않아 주행 모드를 변경할 수 없습니다.', true);
            return false;
        }
        if (String(state.control?.robot_mode || '').toUpperCase() === target) return true;
        state.drivePending = true;
        state.pointerId = null;
        state.pointerStart = null;
        state.draftGoal = null;
        syncControlComponents();
        if (options.announce !== false) setFeedback(`${target === 'AUTO' ? '자동' : '수동'} 모드 전환 요청 중...`);
        try {
            if (target === 'MANUAL') {
                const latest = await refreshState();
                if (!latest) throw new Error('상태 조회 실패');
                const retainedGoal = latest.active_goal ? JSON.stringify(latest.active_goal) : null;
                const retainedPath = Array.isArray(latest.planned_path) && latest.planned_path.length
                    ? JSON.stringify(latest.planned_path)
                    : null;
                setFeedback('Navigation을 안전하게 일시 정지하는 중...');
                const paused = await mutate('/api/navigation/control/pause-for-manual');
                applyControlState(paused);
                if (paused.stop_delivered !== true) {
                    throw new Error('Pi 정지 명령 전달을 확인하지 못해 MANUAL 전환을 중단했습니다.');
                }
                const pausedState = String(paused.navigation_state || '').toUpperCase();
                const pausedRosState = String(paused.ros_navigation?.state || '').toUpperCase();
                if (
                    ['NAVIGATING', 'RESUMING'].includes(pausedState)
                    || ['NAVIGATING', 'RESUMING'].includes(pausedRosState)
                ) {
                    throw new Error('Navigation 일시 정지 완료를 확인하지 못했습니다.');
                }
                if (
                    retainedGoal
                    && (paused.goal_retained !== true || JSON.stringify(paused.active_goal) !== retainedGoal)
                ) {
                    throw new Error('수동 주행용 active Goal이 유지되지 않았습니다.');
                }
                if (retainedPath && JSON.stringify(paused.planned_path) !== retainedPath) {
                    throw new Error('수동 주행용 경로가 유지되지 않았습니다.');
                }
            }
            await mutate('/api/robots/pi-01/command', { type: 'mode', mode: target.toLowerCase() });
            await waitForControl(payload => (
                payload?.connected === true
                && String(payload?.robot_mode || '').toUpperCase() === target
            ));
            if (options.announce !== false) setFeedback(`${target === 'AUTO' ? '자동' : '수동'} 모드로 전환되었습니다.`);
            return true;
        } catch (error) {
            setFeedback(`주행 모드 전환 실패: ${error.message}`, true);
            return false;
        } finally {
            state.drivePending = false;
            syncControlComponents();
        }
    }

    function navigationIsMoving() {
        const navigationState = String(state.control?.navigation_state || '').toUpperCase();
        const rosState = String(state.control?.ros_navigation?.state || '').toUpperCase();
        return ['NAVIGATING', 'RESUMING'].includes(navigationState)
            || ['NAVIGATING', 'RESUMING'].includes(rosState);
    }

    async function setMappingMode() {
        if (state.busy || state.drivePending || state.navigationPending || state.control?.navigation_mode === 'MAPPING') return;
        const moving = navigationIsMoving();
        if (moving && !window.confirm('현재 자율주행 중입니다. 주행을 중지하고 Mapping 모드로 전환하시겠습니까?')) return;
        state.busy = true;
        state.navigationPending = true;
        syncControlComponents();
        setFeedback('Mapping 모드로 전환 중...');
        try {
            const mappingPayload = {
                mode: 'MAPPING',
                ...(moving ? { confirm_stop: true } : {}),
            };
            let response;
            try {
                response = await mutate('/api/navigation/control/mode', mappingPayload);
            } catch (error) {
                if (
                    error.code !== 'NAVIGATION_STOP_CONFIRMATION_REQUIRED'
                    || !window.confirm('현재 자율주행 중입니다. 주행을 중지하고 Mapping 모드로 전환하시겠습니까?')
                ) throw error;
                response = await mutate('/api/navigation/control/mode', { mode: 'MAPPING', confirm_stop: true });
            }
            applyControlState(response);
            setFeedback(moving
                ? '주행을 긴급 정지하고 Mapping 모드로 전환했습니다. 정지 해제는 직접 실행해주세요.'
                : (response.emergency_stop
                    ? '주행을 긴급 정지하고 Mapping 모드로 전환했습니다. 정지 해제는 직접 실행해주세요.'
                    : 'Mapping 모드로 전환했습니다.'));
        } catch (error) {
            setFeedback(`모드 전환 실패: ${error.message}`, true);
        } finally {
            state.busy = false;
            state.navigationPending = false;
            syncControlComponents();
        }
    }

    async function requestNavigationMode(mode) {
        const target = String(mode || '').toUpperCase();
        if (target === 'MAPPING') return setMappingMode();
        if (target !== 'DRIVING' || state.drivePending || state.navigationPending || state.control?.navigation_mode === 'DRIVING') return;
        if (!window.navigationMapView?.snapshot()?.expanded) window.toggleMinimapExpand?.();
        setFeedback('Driving 준비를 위해 저장 지도와 Initial Pose를 지정해주세요.');
        return window.openSavedMapModal?.();
    }

    async function simpleAction(path, progress) {
        if (state.busy || state.drivePending) return;
        state.busy = true;
        setFeedback(progress);
        try {
            const response = await mutate(path);
            if (response.navigation_mode && response.navigation_state) {
                applyControlState(response);
            }
            setFeedback('요청이 적용되었습니다.');
        } catch (error) {
            setFeedback(`요청 실패: ${error.message}`, true);
        } finally {
            state.busy = false;
        }
    }

    async function emergencyStop(reason = 'dashboard_emergency_stop', keepalive = false) {
        setFeedback('긴급 정지 및 Nav2 goal 취소 중...');
        try {
            applyControlState(await mutate('/api/navigation/control/emergency-stop', { reason }, keepalive));
            setFeedback('긴급 정지가 유지됩니다. 자동 재개하지 않습니다.');
            return true;
        } catch (error) {
            setFeedback(`긴급 정지 요청 실패: ${error.message}`, true);
            return false;
        }
    }

    async function requestEmergencyToggle(action) {
        const requestedAction = String(action || (state.control?.emergency_stop ? 'RESUME' : 'STOP')).toUpperCase();
        if (state.estopPending || Date.now() < state.estopCooldownUntil) return false;
        if (state.control?.connected !== true) {
            setFeedback('Pi가 연결되어 있지 않아 긴급 정지 상태를 변경할 수 없습니다.', true);
            return false;
        }
        const cooldownSec = dashboardConfig().estopCooldownSec;
        if (cooldownSec === null) {
            setFeedback('긴급 정지 cooldown 설정을 확인할 수 없습니다.', true);
            return false;
        }
        state.estopPending = true;
        state.estopCooldownUntil = Date.now() + cooldownSec * 1000;
        syncControlComponents();
        setFeedback(requestedAction === 'RESUME' ? '긴급 정지 해제 요청 중...' : '긴급 정지 요청 중...');
        try {
            const response = requestedAction === 'RESUME'
                ? await mutate('/api/navigation/control/resume')
                : await mutate('/api/navigation/control/emergency-stop', { reason: 'dashboard_emergency_stop' });
            applyControlState(response);
            setFeedback(requestedAction === 'RESUME'
                ? '긴급 정지를 해제했습니다.'
                : '긴급 정지가 유지됩니다. 자동 재개하지 않습니다.');
            return true;
        } catch (error) {
            setFeedback(`${requestedAction === 'RESUME' ? '정지 해제' : '긴급 정지'} 요청 실패: ${error.message}`, true);
            return false;
        } finally {
            state.estopPending = false;
            syncControlComponents();
        }
    }

    async function warning() {
        if (state.warningPending) return false;
        if (state.control?.connected !== true) {
            setFeedback('Pi가 연결되지 않아 경고 명령을 전달하지 못했습니다.', true);
            syncControlComponents();
            return false;
        }
        state.warningPending = true;
        syncControlComponents();
        try {
            await mutate('/api/navigation/control/warning', { led_duration_ms: 3000 });
            setFeedback('경고 방송과 LED 명령을 전송했습니다.');
            return true;
        } catch (error) {
            setFeedback(`경고 명령 실패: ${error.message}`, true);
            return false;
        } finally {
            state.warningPending = false;
            syncControlComponents();
        }
    }

    function clearDisplayedAlerts() {
        state.hazardEntries = [];
        $('alertBox')?.querySelectorAll('[data-navigation-hazard]').forEach(element => element.remove());
    }

    function initialize() {
        const canvas = $('lidar-map-canvas');
        canvas?.addEventListener('contextmenu', event => event.preventDefault());
        canvas?.addEventListener('pointerdown', beginGoal);
        canvas?.addEventListener('pointermove', moveGoal);
        canvas?.addEventListener('pointerup', finishGoal);
        canvas?.addEventListener('pointercancel', cancelGoalDraft);
        const controls = window.DabomDashboardComponents?.controls;
        const dashboardModes = $('dashboard-mode-controls-mount');
        controls?.mountDriveMode(dashboardModes, { request: requestDriveMode });
        const dashboardNavigation = $('dashboard-navigation-mode-controls-mount');
        controls?.mountNavigationMode(dashboardNavigation, { request: requestNavigationMode });
        controls?.mountNavigationMode($('navigation-control-panel'), { request: requestNavigationMode });
        controls?.mountEmergencyStop($('dpad-center-action-mount'), { request: requestEmergencyToggle });
        const warningButton = document.querySelector('.action-warning');
        if (warningButton) warningButton.disabled = true;
        document.addEventListener('dabom:navigation-hazard', renderNavigationHazard);
        document.addEventListener('dabom:alerts-cleared', clearDisplayedAlerts);
        document.addEventListener('visibilitychange', () => {
            clearControlPollTimer();
            if (document.hidden) scheduleControlPoll();
            else pollControlState();
        });
        const alertBox = $('alertBox');
        if (alertBox && window.MutationObserver) {
            state.hazardObserver = new MutationObserver(scheduleHazardReconcile);
            state.hazardObserver.observe(alertBox, { childList: true });
        }
        $('navigation-start')?.addEventListener('click', () => simpleAction('/api/navigation/control/start', 'NavigateToPose 시작 중...'));
        $('navigation-cancel')?.addEventListener('click', () => simpleAction('/api/navigation/control/cancel', '목표 취소 중...'));
        $('navigation-estop')?.addEventListener('click', () => requestEmergencyToggle('STOP'));
        $('navigation-resume')?.addEventListener('click', () => requestEmergencyToggle('RESUME'));
        $('navigation-led-test')?.addEventListener('click', () => simpleAction('/api/navigation/control/led-test', 'LED test 명령 전송 중...'));
        window.navigationControl = {
            applyState: applyControlState,
            emergencyStop,
            refreshState,
            requestDriveMode,
            requestEmergencyToggle,
            requestNavigationMode,
            warning,
        };
        csrfToken().catch(() => {});
        pollControlState();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true });
    else initialize();
})();
