(() => {
    'use strict';

    const SYSTEM_CONTROL_POLL_INTERVAL_MS = 5000;
    const DASHBOARD_CONTROL_POLL_INTERVAL_MS = 500;
    const DASHBOARD_CLIENT_STORAGE_KEY = 'dabom.dashboardClientId';
    const AUTOMATIC_ESTOP_REASONS = new Set(['window_blur', 'page_hidden']);

    const state = {
        status: null,
        pending: new Set(),
        pollTimer: null,
        statusRequest: null,
        statusRefreshPending: false,
        statusForceRefreshPending: false,
        csrfPromise: null,
        dashboardClientId: null,
        dashboardControl: null,
        dashboardControlTimer: null,
        dashboardControlRequest: null,
    };
    const guideViewName = 'dashboard-usage-guide';

    const text = {
        gpu: '\u0047\u0050\u0055 \uc11c\ubc84 \uc81c\uc5b4',
        pi: '\u0050\u0049 \uc81c\uc5b4',
        checking: '\ud655\uc778 \uc911',
        unavailable: '\ud655\uc778 \ubd88\uac00',
        processing: '\ucc98\ub9ac \uc911',
        instances: '\uc2e4\ud589 \uc778스\ud134\uc2a4',
        normalize: '\uc911\ubcf5 \uc815\ub9ac',
        guide: '\ub300\uc2dc\ubcf4\ub4dc \uc0ac\uc6a9 \uc548\ub0b4',
        noStatus: '\uc0c1\ud0dc \uc815\ubcf4\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.',
        actionFailed: '\u0052\u004f\u0053 \uc81c\uc5b4 \uc2e4\ud328',
        start: '\uc2dc\uc791',
        stop: '\uc911\uc9c0',
        duplicate: '\uc911\ubcf5',
        error: '\uc624\ub958',
        guideTitle: '\ub300\uc2dc\ubcf4\ub4dc \uc0ac\uc6a9 \uc548\ub0b4',
        processUnavailable: '\uc0c1\ud0dc \uc870\ud68c \ud6c4 \ud504\ub85c\uc138\uc2a4 \uc21c\uc11c\ub97c \ud655\uc778\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.',
    };

    const make = (tag, className, value) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (value !== undefined) node.textContent = value;
        return node;
    };

    function stateLabel(component) {
        if (component.state === 'on') return 'ON';
        if (component.state === 'off') return 'OFF';
        if (component.state === 'duplicate') return text.duplicate;
        if (component.state === 'unreachable') return text.unavailable;
        if (component.state === 'error') return text.error;
        return text.processing;
    }

    function createPanel() {
        const body = document.querySelector('.sidebar-body');
        const slot = document.getElementById('systemControlPanelSlot');
        if (!body || document.getElementById('systemControlPanel')) return;

        const wrapper = make('div');
        wrapper.id = 'systemControlPanel';
        const gpu = make('section', 'system-control-panel');
        const gpuHeading = make('div', 'system-control-heading');
        gpuHeading.append(make('span', '', text.gpu));
        gpuHeading.append(make('span', 'system-control-refresh', text.checking));
        gpuHeading.lastChild.id = 'systemControlUpdated';
        gpu.append(gpuHeading, make('div', 'system-control-list'));
        gpu.lastChild.id = 'gpuSystemControls';

        const divider = make('div', 'sidebar-divider system-control-divider');
        const pi = make('section', 'system-control-panel');
        pi.append(make('div', 'system-control-heading', text.pi), make('div', 'system-control-list'));
        pi.lastChild.id = 'piSystemControls';

        const guide = make('button', 'system-control-manual', text.guide);
        guide.type = 'button';
        guide.addEventListener('click', openGuide);
        wrapper.append(gpu, divider, pi, guide);
        (slot || body).append(wrapper);
    }

    function renderGroup(targetId, components, group) {
        const target = document.getElementById(targetId);
        if (!target) return;
        target.replaceChildren();
        if (!Array.isArray(components) || !components.length) {
            target.append(make('div', 'system-control-message', text.noStatus));
            return;
        }
        for (const component of components) {
            const key = `${group}:${component.id}`;
            const pending = state.pending.has(key);
            const unreachable = component.control_available === false || component.state === 'unreachable';
            const card = make('article', `system-control-card${unreachable ? ' is-unreachable' : ''}`);
            const row = make('div', 'system-control-row');
            const details = make('div');
            details.append(make('div', 'system-control-name', component.label || component.id));
            details.append(make('div', 'system-control-description', component.description || ''));
            const action = component.state === 'on' || component.state === 'duplicate' ? 'stop' : 'start';
            const availabilityClass = pending ? ' is-pending' : unreachable ? ' is-unavailable' : '';
            const button = make(
                'button',
                `system-control-switch state-${component.state || 'error'}${availabilityClass}`,
                pending ? text.processing : stateLabel(component),
            );
            button.type = 'button';
            button.disabled = pending || unreachable;
            button.addEventListener('click', () => control(group, component.id, action));
            row.append(details, button);
            card.append(row);

            const count = Number.isInteger(component.instance_count)
                ? `${text.instances}: ${component.instance_count}`
                : `${text.instances}: ${text.unavailable}`;
            const pids = Array.isArray(component.pids) && component.pids.length
                ? ` | PID: ${component.pids.join(', ')}`
                : '';
            card.append(make('div', 'system-control-meta', `${count}${pids}`));
            if (component.message) card.append(make('div', 'system-control-message', component.message));
            if (component.duplicate) {
                const normalizeClass = pending ? ' is-pending' : unreachable ? ' is-unavailable' : '';
                const normalize = make('button', `system-control-normalize${normalizeClass}`, text.normalize);
                normalize.type = 'button';
                normalize.disabled = pending || unreachable;
                normalize.addEventListener('click', () => control(group, component.id, 'normalize'));
                card.append(normalize);
            }
            target.append(card);
        }
    }

    function render(status, available = true) {
        state.status = available ? status : null;
        renderGroup('gpuSystemControls', status.gpu, 'gpu');
        renderGroup('piSystemControls', status.pi, 'pi');
        const updated = document.getElementById('systemControlUpdated');
        if (updated) {
            updated.textContent = status.updated_at
                ? `\ucd5c\uc885 \ud655\uc778 ${new Date(status.updated_at).toLocaleTimeString('ko-KR')}`
                : text.unavailable;
        }
    }

    function isDocumentVisible() {
        return document.visibilityState !== 'hidden' && document.hidden !== true;
    }

    function isSidebarOpen() {
        return document.getElementById('sidebar')?.classList.contains('open') === true;
    }

    function shouldPollStatus() {
        return isDocumentVisible() && isSidebarOpen();
    }

    function clearStatusPollTimer() {
        if (state.pollTimer !== null) {
            window.clearTimeout(state.pollTimer);
            state.pollTimer = null;
        }
    }

    function scheduleStatusPoll(delay = SYSTEM_CONTROL_POLL_INTERVAL_MS) {
        clearStatusPollTimer();
        if (!shouldPollStatus()) return;
        state.pollTimer = window.setTimeout(() => {
            state.pollTimer = null;
            fetchStatus();
        }, delay);
    }

    function fetchStatus({ force = false } = {}) {
        if (!force && !shouldPollStatus()) {
            clearStatusPollTimer();
            return Promise.resolve();
        }
        if (state.statusRequest) {
            if (force) state.statusForceRefreshPending = true;
            else state.statusRefreshPending = true;
            return state.statusRequest;
        }

        clearStatusPollTimer();
        const request = (async () => {
            try {
                const response = await fetch('/api/system-control/status', { credentials: 'same-origin' });
                const payload = await response.json();
                if (!response.ok || !payload.ok) throw new Error(payload.detail || text.unavailable);
                render(payload);
                document.dispatchEvent(new CustomEvent('dabom:system-control-status', {
                    detail: { available: true, payload },
                }));
            } catch (error) {
                render({ updated_at: null, gpu: [], pi: [{
                    id: 'lidar_ros', label: 'LiDAR ROS Service', description: '', state: 'unreachable',
                    instance_count: null, control_available: false, message: error.message,
                }] }, false);
                document.dispatchEvent(new CustomEvent('dabom:system-control-status', {
                    detail: { available: false, error: error.message },
                }));
            }
        })();

        state.statusRequest = request.finally(async () => {
            state.statusRequest = null;
            const refreshPending = state.statusRefreshPending;
            const forceRefreshPending = state.statusForceRefreshPending;
            state.statusRefreshPending = false;
            state.statusForceRefreshPending = false;
            if (forceRefreshPending) {
                await fetchStatus({ force: true });
                return;
            }
            if (refreshPending && shouldPollStatus()) {
                await fetchStatus();
                return;
            }
            scheduleStatusPoll();
        });
        return state.statusRequest;
    }

    function pauseStatusPolling() {
        state.statusRefreshPending = false;
        clearStatusPollTimer();
    }

    function refreshStatusPolling() {
        clearStatusPollTimer();
        if (!shouldPollStatus()) return Promise.resolve();
        return fetchStatus();
    }

    function csrfToken() {
        if (!state.csrfPromise) {
            state.csrfPromise = fetch('/api/auth/csrf', { credentials: 'same-origin' })
                .then(async response => {
                    const payload = await response.json();
                    if (!response.ok || !payload.csrf_token) throw new Error(text.unavailable);
                    return payload.csrf_token;
                })
                .catch(error => {
                    state.csrfPromise = null;
                    throw error;
                });
        }
        return state.csrfPromise;
    }

    async function control(group, componentId, action) {
        const key = `${group}:${componentId}`;
        if (state.pending.has(key)) return;
        state.pending.add(key);
        if (state.status) render(state.status);
        try {
            const token = await csrfToken();
            const response = await fetch(`/api/system-control/${group}/${encodeURIComponent(componentId)}/${action}`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'X-CSRF-Token': token },
            });
            if (response.status === 403) state.csrfPromise = null;
            const payload = await response.json();
            if (!response.ok || !payload.ok) throw new Error(payload.detail || text.actionFailed);
        } catch (error) {
            window.alert(`${text.actionFailed}: ${error.message}`);
        } finally {
            state.pending.delete(key);
            await fetchStatus({ force: true });
        }
    }

    function appendGuideSection(container, titleValue, items, ordered = true) {
        const section = make('section', 'system-control-guide-section');
        section.append(make('h3', 'system-control-guide-heading', titleValue));
        const list = make(
            ordered ? 'ol' : 'ul',
            `system-control-guide-list${ordered ? '' : ' is-source-list'}`,
        );
        for (const item of items) list.append(make('li', '', item));
        section.append(list);
        container.append(section);
    }

    function processGuideItems() {
        if (!state.status) return [text.processUnavailable];
        const piComponents = Array.isArray(state.status.pi) ? state.status.pi : [];
        const gpuComponents = Array.isArray(state.status.gpu) ? state.status.gpu : [];
        const components = [...piComponents, ...gpuComponents];
        if (!components.length) return [text.processUnavailable];
        const normalStartOrder = components.filter(component => component.id !== 'map_bridge');
        const items = normalStartOrder.map(component => component.label || component.id).filter(Boolean);
        const mapBridge = components.find(component => component.id === 'map_bridge');
        if (mapBridge) {
            const label = mapBridge.label || mapBridge.id;
            items.push(`\ucc38\uace0: SLAM Mapping\uc740 ${label}\ub97c \ud568\uaed8 \uc2dc\uc791. ${label}\ub294 \ubcf5\uad6c\u00b7\ub3c5\ub9bd \uc2e4\ud589 \uc2dc\uc5d0\ub9cc \uc0ac\uc6a9`);
        }
        return items;
    }

    function renderGuide() {
        const guide = make('div', 'system-control-guide');
        appendGuideSection(guide, '상태 정보 출처', [
            '시스템 상태 — CPU / 온도 / RAM / Ping: Pi 상태 정보',
            '카메라 상태 — LIVE / OFFLINE: Camera Stream 상태',
            '로봇·주행 상태 — Pi 연결 / 자동·수동 / Mapping·Driving / Navigation / 긴급 정지 / Goal / Path / 활성 지도: Navigation Control 상태',
            '프로세스 상태 — LiDAR ROS Bridge / Encoder ROS Bridge / Wheel Odometry / SLAM Mapping / Map Bridge: 시스템 제어 상태',
            '기록 — 기기 상태 / 순찰 기록 / 관리자 조치: 서버 DB 기록',
        ], false);
        appendGuideSection(guide, 'Mapping', [
            '[Mapping] \uc120\ud0dd',
            '[\uc218\ub3d9] \uc120\ud0dd',
            'D-Pad \uc774\ub3d9',
            'LiDAR Mapping \uc218\ud589',
            '[SAVE] \uc9c0\ub3c4 \uc800\uc7a5',
        ]);
        appendGuideSection(guide, 'Driving', [
            '[Driving] \uc120\ud0dd',
            '[\uae30\uc874 \uc9c0\ub3c4 \uc120\ud0dd]',
            'Initial Pose \uc9c0\uc815',
            'Goal \uc9c0\uc815',
            '\uacbd\ub85c \ud655인',
            '[\uc8fc\ud589 \uc2dc\uc791]',
        ]);
        appendGuideSection(guide, '\uc704\ud5d8 \ub300\uc751', [
            '\uc2e4\uc2dc\uac04 \uc704\ud5d8/\uacbd\uace0 \uc54c\ub9bc \ud655\uc778',
            '[\uc21c\ucc30 \uae30\ub85d \uc870\ud68c]',
            '[\uc774\ubbf8\uc9c0 \uac24\ub7ec\ub9ac]',
            '\ud544\uc694 \uc2dc [\uc2e0\uace0] \ub610\ub294 [\u26a0\ufe0f \uacbd\uace0]',
            '\ud544\uc694 \uc2dc [\uae34\uae09 \uc815\uc9c0]',
            '[\ud604\uc7ac \uc0c1\ud669 \uc791\uc131]',
            '[\uad00\ub9ac\uc790 \uc870\uce58 \uc870\ud68c]',
        ]);
        appendGuideSection(guide, '\uae30\uae30 \uc0c1\ud0dc', [
            '\uc2dc\uc2a4\ud15c \uc9c4\ub2e8: [\uae30\uae30 \uc0c1\ud0dc \uc870\ud68c]',
        ]);
        appendGuideSection(guide, '\ud504\ub85c\uc138\uc2a4 \uc2e4\ud589 \uc21c\uc11c', processGuideItems());
        return guide;
    }

    function openGuide() {
        const modalApi = window.DabomDashboardComponents?.modal;
        const manager = modalApi?.getDefault?.() || modalApi?.mount?.();
        if (manager) {
            manager.register(guideViewName, { title: text.guideTitle, render: renderGuide });
            manager.open(guideViewName, {}, { replace: true });
            return;
        }
        const modal = document.getElementById('commonModal');
        const title = document.getElementById('modalTitle');
        const body = document.getElementById('modalBody');
        if (!modal || !title || !body) return;
        title.textContent = text.guideTitle;
        body.replaceChildren(renderGuide());
        modal.style.display = 'flex';
    }

    function dashboardClientId() {
        if (state.dashboardClientId) return state.dashboardClientId;
        let clientId = window.sessionStorage.getItem(DASHBOARD_CLIENT_STORAGE_KEY);
        if (!clientId) {
            if (window.crypto?.randomUUID) {
                clientId = window.crypto.randomUUID();
            } else {
                clientId = `dashboard-${Date.now()}-${Math.random().toString(16).slice(2)}`;
            }
            window.sessionStorage.setItem(DASHBOARD_CLIENT_STORAGE_KEY, clientId);
        }
        state.dashboardClientId = clientId;
        return clientId;
    }

    function ownsRobotControl() {
        return Boolean(
            state.dashboardControl?.owner_client_id
            && state.dashboardControl.owner_client_id === dashboardClientId(),
        );
    }

    function setControlFeedback(message, isError = false) {
        const feedback = document.getElementById('navigation-control-feedback');
        if (!feedback) return;
        feedback.textContent = message || '';
        feedback.classList.toggle('error', Boolean(isError));
    }

    function applyDashboardControl(payload) {
        if (!payload || typeof payload !== 'object') return;
        state.dashboardControl = payload.control || payload;
        if (typeof payload.robot_connected === 'boolean') {
            window.setDashboardRobotConnection?.(payload.robot_connected);
        }
        const mode = String(state.dashboardControl?.mode || '').toLowerCase();
        if (mode === 'manual' || mode === 'auto') {
            window.applyServerPatrolMode?.(mode);
        }
        document.dispatchEvent(new CustomEvent('dabom:dashboard-control-state', {
            detail: {
                clientId: dashboardClientId(),
                isOwner: ownsRobotControl(),
                control: state.dashboardControl,
            },
        }));
    }

    function clearDashboardControlTimer() {
        if (state.dashboardControlTimer !== null) {
            window.clearTimeout(state.dashboardControlTimer);
            state.dashboardControlTimer = null;
        }
    }

    function scheduleDashboardControlPoll(delay = DASHBOARD_CONTROL_POLL_INTERVAL_MS) {
        clearDashboardControlTimer();
        if (!isDocumentVisible()) return;
        state.dashboardControlTimer = window.setTimeout(() => {
            state.dashboardControlTimer = null;
            fetchDashboardControlState();
        }, delay);
    }

    async function fetchDashboardControlState() {
        if (!isDocumentVisible()) {
            clearDashboardControlTimer();
            return;
        }
        if (state.dashboardControlRequest) return state.dashboardControlRequest;

        state.dashboardControlRequest = (async () => {
            try {
                const response = await fetch('/api/dashboard-control/state', {
                    credentials: 'same-origin',
                    headers: { 'X-Dashboard-Client-Id': dashboardClientId() },
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok || !payload.ok) throw new Error(payload.detail || 'control state unavailable');
                applyDashboardControl(payload);
            } catch (error) {
                console.warn('dashboard control state sync failed:', error);
            }
        })().finally(() => {
            state.dashboardControlRequest = null;
            scheduleDashboardControlPoll();
        });
        return state.dashboardControlRequest;
    }

    async function sendDashboardRobotCommand(payload, keepalive = false) {
        try {
            const token = await csrfToken();
            const response = await fetch('/api/dashboard-control/command', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': token,
                    'X-Dashboard-Client-Id': dashboardClientId(),
                },
                body: JSON.stringify(payload),
                keepalive,
            });
            if (response.status === 403) state.csrfPromise = null;
            const data = await response.json().catch(() => ({}));
            if (data.control) applyDashboardControl({ control: data.control });

            if (!response.ok || !data.ok) {
                if (data.error_code === 'CONTROL_BUSY') {
                    setControlFeedback('다른 대시보드가 현재 로봇을 조종 중입니다.', true);
                } else {
                    setControlFeedback(data.detail || data.error || '로봇 명령 전송에 실패했습니다.', true);
                }
                return false;
            }
            setControlFeedback('', false);
            scheduleDashboardControlPoll(0);
            return true;
        } catch (error) {
            console.error('dashboard robot command failed:', error);
            return false;
        }
    }

    function installDashboardControlProxy() {
        if (typeof window.sendRobotCommand === 'function') {
            window.sendRobotCommand = sendDashboardRobotCommand;
        }

        if (typeof window.emergencyStopRobot === 'function') {
            window.emergencyStopRobot = function syncedEmergencyStopRobot(
                reason = 'dashboard_emergency_stop',
                keepalive = false,
            ) {
                if (AUTOMATIC_ESTOP_REASONS.has(reason) && !ownsRobotControl()) {
                    return Promise.resolve(true);
                }
                window.stopAllLocalInputs?.(false);
                return sendDashboardRobotCommand({
                    type: 'emergency_stop',
                    reason,
                }, keepalive);
            };
        }
    }

    function initialize() {
        createPanel();
        installDashboardControlProxy();

        document.addEventListener('dabom:sidebar-visibility', event => {
            if (event.detail?.open === false) {
                pauseStatusPolling();
                return;
            }
            refreshStatusPolling();
        });
        document.addEventListener('visibilitychange', () => {
            if (!isDocumentVisible()) {
                pauseStatusPolling();
                clearDashboardControlTimer();
                return;
            }
            refreshStatusPolling();
            scheduleDashboardControlPoll(0);
        });
        refreshStatusPolling();
        scheduleDashboardControlPoll(0);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, { once: true });
    } else {
        initialize();
    }
})();
