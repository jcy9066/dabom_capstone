(function (global) {
    'use strict';

    const state = {
        editing: false,
        waypoints: [],
        routeId: null,
        patrol: null,
        group: null,
        pollTimer: null,
        csrf: null,
    };

    const $ = id => document.getElementById(id);
    const viewer = () => global.dabomLidar3D;
    const canvas = () => $('lidar-3d-canvas');
    const controlsRoot = () => document.querySelector('.lidar-view-controls');

    async function csrfToken() {
        if (state.csrf) return state.csrf;
        const response = await fetch('/api/auth/csrf', { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok || !data.csrf_token) throw new Error('CSRF token unavailable');
        state.csrf = data.csrf_token;
        return state.csrf;
    }

    async function api(path, options = {}) {
        const init = { credentials: 'same-origin', ...options };
        if ((init.method || 'GET').toUpperCase() !== 'GET') {
            init.headers = { ...(init.headers || {}), 'X-CSRF-Token': await csrfToken() };
            if (init.body && typeof init.body !== 'string') {
                init.headers['Content-Type'] = 'application/json';
                init.body = JSON.stringify(init.body);
            }
        }
        const response = await fetch(path, init);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
            const error = new Error(data.error || data.detail || `HTTP ${response.status}`);
            error.code = data.error_code;
            throw error;
        }
        return data;
    }

    function controlButton(label, variant, id) {
        const button = document.createElement('button');
        button.type = 'button';
        button.id = id;
        button.textContent = label;
        button.dataset.controlButton = variant;
        global.DabomDashboardComponents?.controls?.controlButton?.enhance?.(button);
        return button;
    }

    function ensureLayer() {
        if (state.group || !viewer()?.THREE || !viewer()?.world) return state.group;
        state.group = new viewer().THREE.Group();
        state.group.name = 'patrol-waypoints';
        viewer().world.add(state.group);
        return state.group;
    }

    function disposeObject(object) {
        object.traverse?.(child => {
            child.geometry?.dispose?.();
            if (Array.isArray(child.material)) child.material.forEach(item => item?.dispose?.());
            else child.material?.dispose?.();
        });
    }

    function redraw() {
        const v = viewer();
        const group = ensureLayer();
        if (!v || !group) return;
        while (group.children.length) {
            const child = group.children.pop();
            disposeObject(child);
        }
        const THREE = v.THREE;
        const positions = [];
        state.waypoints.forEach((point, index) => {
            const marker = new THREE.Group();
            marker.position.set(point.x, point.y, 0.06);
            const ring = new THREE.Mesh(
                new THREE.RingGeometry(0.10, 0.16, 28),
                new THREE.MeshBasicMaterial({ color: index === 0 ? 0x22c55e : 0x38bdf8, side: THREE.DoubleSide }),
            );
            marker.add(ring);
            const stem = new THREE.Mesh(
                new THREE.CylinderGeometry(0.018, 0.018, 0.16, 12),
                new THREE.MeshBasicMaterial({ color: 0xe2e8f0 }),
            );
            stem.rotation.x = Math.PI / 2;
            stem.position.z = 0.08;
            marker.add(stem);
            group.add(marker);
            positions.push(point.x, point.y, 0.075);
        });
        if (state.waypoints.length >= 2) {
            const geometry = new THREE.BufferGeometry();
            geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
            group.add(new THREE.Line(
                geometry,
                new THREE.LineBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.9 }),
            ));
        }
        global.navigationMapView?.requestRender?.();
        document.dispatchEvent(new CustomEvent('dabom:patrol-waypoints-changed', {
            detail: state.waypoints.map(point => ({ ...point })),
        }));
        syncButtons();
    }

    function activeMapName() {
        return global.dabomNavigationControlState?.active_map?.name
            || global.dabomNavigationControlState?.active_map?.map_name
            || null;
    }

    function setEditing(enabled) {
        state.editing = Boolean(enabled);
        const v = viewer();
        if (v?.controls) v.controls.enabled = !state.editing;
        $('patrol-waypoint-edit')?.classList.toggle('active', state.editing);
        if (state.editing) canvas()?.focus?.({ preventScroll: true });
        syncButtons();
    }

    function addWaypoint(event) {
        if (!state.editing || event.button !== 0) return;
        const point = global.navigationMapView?.screenToGround?.(event);
        if (!point) return;
        event.preventDefault();
        event.stopPropagation();
        const previous = state.waypoints[state.waypoints.length - 1];
        state.waypoints.push({
            x: Number(point.x),
            y: Number(point.y),
            yaw: previous ? Math.atan2(point.y - previous.y, point.x - previous.x) : 0,
            wait_sec: 3,
        });
        if (previous && state.waypoints.length >= 2) {
            previous.yaw = Math.atan2(point.y - previous.y, point.x - previous.x);
        }
        redraw();
    }

    function undo() {
        if (!state.waypoints.length) return;
        state.waypoints.pop();
        redraw();
    }

    function clear() {
        state.waypoints = [];
        state.routeId = null;
        redraw();
    }

    async function saveRoute() {
        if (!state.waypoints.length) {
            alert('Waypoint를 하나 이상 지정해주세요.');
            return;
        }
        const name = (prompt('순찰 경로 이름', '기본 순찰') || '').trim();
        if (!name) return;
        const result = await api('/api/navigation/patrol/routes', {
            method: 'POST',
            body: {
                route_id: state.routeId,
                name,
                map_name: activeMapName(),
                loop: true,
                waypoints: state.waypoints,
            },
        });
        state.routeId = result.route.route_id;
        syncButtons();
    }

    async function chooseRoute() {
        if (state.routeId) return state.routeId;
        const data = await api('/api/navigation/patrol/routes');
        const routes = Array.isArray(data.routes) ? data.routes : [];
        if (!routes.length) throw new Error('저장된 순찰 경로가 없습니다.');
        if (routes.length === 1) return routes[0].route_id;
        const menu = routes.map((route, index) => `${index + 1}. ${route.name}`).join('\n');
        const selected = Number(prompt(`실행할 경로 번호를 입력하세요.\n${menu}`, '1'));
        if (!Number.isInteger(selected) || selected < 1 || selected > routes.length) {
            throw new Error('경로 선택을 취소했습니다.');
        }
        return routes[selected - 1].route_id;
    }

    async function startPatrol() {
        const routeId = await chooseRoute();
        state.routeId = routeId;
        state.patrol = await api('/api/navigation/patrol/start', {
            method: 'POST',
            body: { route_id: routeId },
        });
        syncButtons();
    }

    async function togglePause() {
        if (!state.patrol?.running) return;
        const path = state.patrol.paused
            ? '/api/navigation/patrol/resume'
            : '/api/navigation/patrol/pause';
        state.patrol = await api(path, { method: 'POST', body: {} });
        syncButtons();
    }

    async function stopPatrol() {
        state.patrol = await api('/api/navigation/patrol/stop', { method: 'POST', body: {} });
        syncButtons();
    }

    async function poll() {
        try {
            state.patrol = await api('/api/navigation/patrol/state');
            syncButtons();
        } catch (_) {
            // Dashboard must remain usable if patrol API is temporarily unavailable.
        } finally {
            state.pollTimer = global.setTimeout(poll, document.hidden ? 4000 : 1000);
        }
    }

    function syncButtons() {
        const edit = $('patrol-waypoint-edit');
        const save = $('patrol-route-save');
        const start = $('patrol-route-start');
        const pause = $('patrol-route-pause');
        const stop = $('patrol-route-stop');
        if (edit) edit.textContent = state.editing ? `WAYPOINT (${state.waypoints.length})` : 'WAYPOINT';
        if (save) save.disabled = !state.waypoints.length || Boolean(state.patrol?.running);
        if (start) start.disabled = Boolean(state.patrol?.running);
        if (pause) {
            pause.disabled = !state.patrol?.running;
            pause.textContent = state.patrol?.paused ? 'RESUME' : 'PAUSE';
        }
        if (stop) stop.disabled = !state.patrol?.running;
    }

    function guarded(handler) {
        return async event => {
            event?.preventDefault?.();
            try { await handler(); }
            catch (error) { alert(error.message || '순찰 요청에 실패했습니다.'); }
        };
    }

    function mount() {
        const root = controlsRoot();
        const goal = root?.querySelector('[data-lidar-interaction="set-goal"]');
        const targetCanvas = canvas();
        if (!root || !goal || !targetCanvas || $('patrol-waypoint-edit')) return;

        const edit = controlButton('WAYPOINT', 'goal', 'patrol-waypoint-edit');
        const save = controlButton('SAVE ROUTE', 'secondary', 'patrol-route-save');
        const start = controlButton('START PATROL', 'accent', 'patrol-route-start');
        const pause = controlButton('PAUSE', 'secondary', 'patrol-route-pause');
        const stop = controlButton('STOP', 'danger', 'patrol-route-stop');

        goal.insertAdjacentElement('afterend', edit);
        edit.insertAdjacentElement('afterend', save);
        save.insertAdjacentElement('afterend', start);
        start.insertAdjacentElement('afterend', pause);
        pause.insertAdjacentElement('afterend', stop);

        edit.addEventListener('click', () => setEditing(!state.editing));
        save.addEventListener('click', guarded(saveRoute));
        start.addEventListener('click', guarded(startPatrol));
        pause.addEventListener('click', guarded(togglePause));
        stop.addEventListener('click', guarded(stopPatrol));

        targetCanvas.addEventListener('pointerdown', addWaypoint, true);
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && state.editing) setEditing(false);
            if (state.editing && event.ctrlKey && event.key.toLowerCase() === 'z') {
                event.preventDefault();
                undo();
            }
            if (state.editing && event.key === 'Delete') {
                event.preventDefault();
                clear();
            }
        });
        document.querySelectorAll('[data-lidar-interaction]').forEach(button => {
            button.addEventListener('click', () => {
                if (button !== edit) setEditing(false);
            });
        });

        global.dabomPatrolControl = {
            getWaypoints: () => state.waypoints.map(point => ({ ...point })),
            clearWaypoints: clear,
            undoWaypoint: undo,
            setEditing,
        };
        redraw();
        poll();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => global.setTimeout(mount, 0), { once: true });
    } else {
        global.setTimeout(mount, 0);
    }
})(window);
