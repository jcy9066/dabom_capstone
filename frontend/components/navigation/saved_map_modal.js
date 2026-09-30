(function initializeSavedMapModal(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const navigationMaps = components.navigationMaps = components.navigationMaps || {};
    const VIEW_NAME = 'savedNavigationMaps';
    const labels = {
        title: '\uc800\uc7a5 \uc9c0\ub3c4 \uc120\ud0dd',
        description: '\uc9c0\ub3c4\ub97c \ubd88\ub7ec\uc624\uba74 \ud604\uc7ac localization \uc704\uce58\uac00 \ucd08\uae30\ud654\ub429\ub2c8\ub2e4.',
        cancel: '\ucde8\uc18c',
        load: '\uc120\ud0dd \uc9c0\ub3c4 \ubd88\ub7ec\uc624\uae30',
        saveCurrent: '\ud604\uc7ac \uc9c0\ub3c4 \uc800\uc7a5',
        loading: '\uc9c0\ub3c4 \ubd88\ub7ec\uc624\ub294 \uc911...',
        resetting: 'AMCL \ucd08\uae30 \uc704\uce58 \uc124\uc815 \uc911...',
        verifying: '\uc9c0\ub3c4 \ubc0f localization \ud655\uc778 \uc911...',
        unavailable: '\uc0c1\ud0dc \ud655\uc778 \ubd88\uac00',
        notSelected: '\uc120\ud0dd\ub418\uc9c0 \uc54a\uc74c',
        positionX: 'X \uc704\uce58 (m)',
        positionY: 'Y \uc704\uce58 (m)',
        heading: '\ubc29\ud5a5 (degree)',
        noMaps: '\ubd88\ub7ec\uc62c \uc218 \uc788\ub294 \uc800\uc7a5 \uc9c0\ub3c4\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.',
        confirmSuffix: ' \uc9c0\ub3c4\ub97c \ubd88\ub7ec\uc624\uc2dc\uaca0\uc2b5\ub2c8\uae4c?\n\ud604\uc7ac localization \uc704\uce58\uac00 \ucd08\uae30\ud654\ub429\ub2c8\ub2e4.',
        success: '\uc9c0\ub3c4\ub97c \ubd88\ub7ec\uc654\uc2b5\ub2c8\ub2e4.',
        renameHint: '\uc6b0\ud074\ub9ad\ud558\uc5ec \uc774\ub984 \ubcc0\uacbd',
        renamePrompt: '\uc0c8 \uc9c0\ub3c4 \uc774\ub984\uc744 \uc785\ub825\ud558\uc138\uc694.',
        renaming: '\uc9c0\ub3c4 \uc774\ub984 \ubcc0\uacbd \uc911...',
        renameSuccess: '\uc9c0\ub3c4 \uc774\ub984\uc744 \ubcc0\uacbd\ud588\uc2b5\ub2c8\ub2e4.',
        selectRequired: '\uc800\uc7a5 \uc9c0\ub3c4\ub97c \uc9c1\uc811 \uc120\ud0dd\ud558\uc138\uc694.',
        mapPrefix: 'MAP: '
    };
    const state = {
        maps: [],
        active: null,
        activeApiAvailable: true,
        busy: false,
        closeTimer: null,
        progressTimers: [],
        pollTimer: null,
    };
    let manager = null;
    let controller = null;

    const create = (tag, className, value) => {
        const element = global.document.createElement(tag);
        if (className) element.className = className;
        if (value !== undefined) element.textContent = value;
        return element;
    };

    const formatSavedAt = value => {
        if (!value) return '--';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ko-KR', { hour12: false });
    };

    function setMinimapMapLabel(activeResponse) {
        let label = global.document.getElementById('lidar-active-map-status');
        const minimap = global.document.getElementById('minimap-overlay');
        if (!label && minimap) {
            label = create('div', 'lidar-active-map-status');
            label.id = 'lidar-active-map-status';
            minimap.append(label);
        }
        if (!label) return;
        if (activeResponse?.state === 'active' && activeResponse.active_map?.map_name) {
            label.textContent = `${labels.mapPrefix}${activeResponse.active_map.map_name}`;
        } else if (['loading', 'resetting_pose', 'verifying'].includes(activeResponse?.state)) {
            label.textContent = `${labels.mapPrefix}${labels.loading}`;
        } else if (activeResponse?.state === 'unavailable') {
            label.textContent = `${labels.mapPrefix}${labels.unavailable}`;
        } else {
            label.textContent = `${labels.mapPrefix}${labels.notSelected}`;
        }
    }

    async function requestJson(url, options = {}) {
        const response = await global.fetch(url, { credentials: 'same-origin', ...options });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.ok === false) {
            const error = new Error(payload.error || payload.detail || `HTTP ${response.status}`);
            error.status = response.status;
            error.code = payload.error_code;
            throw error;
        }
        return payload;
    }

    async function refreshActiveMap() {
        if (!state.activeApiAvailable) return null;
        try {
            const payload = await requestJson('/api/navigation/maps/active');
            state.active = payload;
            setMinimapMapLabel(payload);
            global.document.dispatchEvent(new CustomEvent('dabom:active-map-status', {
                detail: { available: true, payload }
            }));
            return payload;
        } catch (error) {
            if (error.status === 404) state.activeApiAvailable = false;
            setMinimapMapLabel({ state: 'unavailable' });
            global.document.dispatchEvent(new CustomEvent('dabom:active-map-status', {
                detail: { available: false, error: error.message }
            }));
            return null;
        }
    }

    async function csrfToken() {
        const payload = await requestJson('/api/auth/csrf');
        if (!payload.csrf_token) throw new Error(labels.unavailable);
        return payload.csrf_token;
    }

    function selectedMapName(container) {
        return container?.querySelector?.('input[name="saved-navigation-map"]:checked')?.value || null;
    }

    function showMessage(target, value, isError = false) {
        if (!target) return;
        target.textContent = value;
        target.style.color = isError ? 'var(--danger-color)' : '';
    }

    function friendlyLoadError(error) {
        const messages = {
            MAP_SERVER_UNAVAILABLE: '\ub9f5 \uc11c\ubc84\uac00 \uc2e4\ud589 \uc911\uc778\uc9c0 \ud655\uc778\ud558\uc138\uc694.',
            LOCALIZATION_NOT_ACTIVE: 'map_server\uc640 AMCL\uc774 \ud65c\uc131 \uc0c1\ud0dc\uc778\uc9c0 \ud655\uc778\ud558\uc138\uc694.',
            MAPPING_MODE_ACTIVE: 'Mapping \ubaa8\ub4dc\uc5d0\uc11c\ub294 \uc800\uc7a5 \uc9c0\ub3c4\ub97c \ubd88\ub7ec\uc62c \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.',
            MAP_VERIFICATION_FAILED: '\uc9c0\ub3c4 \uba54\ud0c0\ub370\uc774\ud130 \ud655\uc778\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4.',
            AMCL_VERIFICATION_FAILED: 'AMCL \ucd08\uae30 \uc704\uce58\ub97c \ud655\uc778\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.',
            INITIAL_POSE_OUT_OF_BOUNDS: '\ucd08\uae30 \uc704\uce58\uac00 \uc120\ud0dd\ud55c \uc9c0\ub3c4 \ubc94\uc704\ub97c \ubc97\uc5b4\ub0a9\ub2c8\ub2e4.',
            MAP_LOAD_IN_PROGRESS: '\ub2e4\ub978 \uc9c0\ub3c4 \ubd88\ub7ec\uc624\uae30\uac00 \ucc98\ub9ac \uc911\uc785\ub2c8\ub2e4.',
            MAP_OPERATION_IN_PROGRESS: '\ub2e4\ub978 \uc800\uc7a5 \uc9c0\ub3c4 \uc791\uc5c5\uc774 \ucc98\ub9ac \uc911\uc785\ub2c8\ub2e4.',
            MAP_NAME_ALREADY_EXISTS: '\uc774\ubbf8 \uac19\uc740 \uc774\ub984\uc758 \uc800\uc7a5 \uc9c0\ub3c4\uac00 \uc788\uc2b5\ub2c8\ub2e4.',
            MAP_NAME_UNCHANGED: '\uc0c8 \uc9c0\ub3c4 \uc774\ub984\uc774 \uae30\uc874 \uc774\ub984\uacfc \uac19\uc2b5\ub2c8\ub2e4.',
            INVALID_MAP_NAME: '\uc9c0\ub3c4 \uc774\ub984\uc740 \uc601\ubb38, \uc22b\uc790, \ub9c8\uce68\ud45c, \ud558\uc774\ud508, \ubc11\uc904\ub9cc \uc0ac\uc6a9\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.'
        };
        return messages[error.code] || error.message || labels.unavailable;
    }

    function captureViewState(container = manager?.body) {
        const value = id => container?.querySelector?.(`#${id}`)?.value ?? '0';
        return {
            selectedName: selectedMapName(container),
            pose: {
                x: value('saved-map-pose-x'),
                y: value('saved-map-pose-y'),
                yaw: value('saved-map-pose-yaw'),
            },
        };
    }

    function setControlsDisabled(container, disabled, pending = false) {
        container?.classList?.toggle('is-pending', pending);
        container?.querySelectorAll?.('button, input').forEach(element => { element.disabled = disabled; });
        if (!disabled) {
            const load = container?.querySelector?.('.saved-map-load');
            if (load) load.disabled = !selectedMapName(container);
        }
    }

    function clearProgressTimers() {
        state.progressTimers.forEach(timer => global.clearTimeout(timer));
        state.progressTimers.length = 0;
    }

    function scheduleProgress(target, value, delay) {
        state.progressTimers.push(global.setTimeout(() => {
            if (manager?.activeView?.name === VIEW_NAME) showMessage(target, value);
        }, delay));
    }

    function replaceCurrentView(context, snapshot = null) {
        if (manager?.activeView?.name !== VIEW_NAME) return;
        manager.activeView.context = context;
        manager.activeView.state = snapshot;
        manager.setBody(renderView(context, snapshot));
    }

    async function renameSavedMap(map, container, progress) {
        if (state.busy) return;
        const snapshot = captureViewState(container);
        const requestedName = global.prompt(labels.renamePrompt, map.map_name);
        if (requestedName === null) return;
        const newMapName = requestedName.trim();
        if (!newMapName) {
            showMessage(progress, friendlyLoadError({ code: 'INVALID_MAP_NAME' }), true);
            return;
        }

        state.busy = true;
        setControlsDisabled(container, true, true);
        showMessage(progress, labels.renaming);
        try {
            const token = await csrfToken();
            const payload = await requestJson('/api/navigation/maps/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                body: JSON.stringify({ map_name: map.map_name, new_map_name: newMapName })
            });
            const [active, maps] = await Promise.all([
                refreshActiveMap(),
                requestJson('/api/navigation/maps')
            ]);
            state.maps = maps.maps || [];
            if (snapshot.selectedName === map.map_name) snapshot.selectedName = payload.map.map_name;
            replaceCurrentView({
                maps: state.maps,
                active,
                feedback: `${payload.map.map_name} ${labels.renameSuccess}`,
            }, snapshot);
        } catch (error) {
            showMessage(progress, `\uc9c0\ub3c4 \uc774\ub984 \ubcc0\uacbd \uc2e4\ud328: ${friendlyLoadError(error)}`, true);
            setControlsDisabled(container, false);
        } finally {
            state.busy = false;
        }
    }

    async function loadSelectedMap(container, progress) {
        const mapName = selectedMapName(container);
        if (state.busy) return;
        if (!mapName) {
            showMessage(progress, labels.selectRequired, true);
            return;
        }
        if (!global.confirm(`${mapName}${labels.confirmSuffix}`)) return;

        const x = Number(container.querySelector('#saved-map-pose-x').value);
        const y = Number(container.querySelector('#saved-map-pose-y').value);
        const yawDegrees = Number(container.querySelector('#saved-map-pose-yaw').value);
        if (![x, y, yawDegrees].every(Number.isFinite)) {
            showMessage(progress, '\ucd08\uae30 \uc704\uce58\uc5d0\ub294 \uc720\ud55c \uc22b\uc790\ub9cc \uc785\ub825\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.', true);
            return;
        }

        state.busy = true;
        setControlsDisabled(container, true, true);
        showMessage(progress, labels.loading);
        scheduleProgress(progress, labels.resetting, 700);
        scheduleProgress(progress, labels.verifying, 1800);
        let succeeded = false;
        try {
            const token = await csrfToken();
            const payload = await requestJson('/api/navigation/control/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                body: JSON.stringify({
                    mode: 'DRIVING',
                    source: 'existing',
                    map_name: mapName,
                    initial_pose: { x, y, yaw_degrees: yawDegrees }
                })
            });
            global.navigationControl?.applyState?.(payload);
            showMessage(progress, `${payload.active_map.map_name} ${labels.success}`);
            await refreshActiveMap();
            succeeded = true;
            setControlsDisabled(container, true, false);
            state.closeTimer = global.setTimeout(() => {
                if (manager?.activeView?.name === VIEW_NAME) manager.close();
            }, 700);
        } catch (error) {
            showMessage(progress, `\uc9c0\ub3c4 \ubd88\ub7ec\uc624\uae30 \uc2e4\ud328: ${friendlyLoadError(error)}`, true);
            setControlsDisabled(container, false);
        } finally {
            clearProgressTimers();
            state.busy = false;
            if (!succeeded) container?.classList?.remove('is-pending');
        }
    }

    function renderView(context = {}, snapshot = null) {
        const container = create('div', 'saved-map-modal');
        if (context.error) {
            const progress = create('div', 'saved-map-progress');
            showMessage(progress, `\uc9c0\ub3c4 \ubaa9\ub85d \uc870\ud68c \uc2e4\ud328: ${context.error}`, true);
            container.append(progress);
            return container;
        }

        const maps = context.maps || [];
        container.append(create('p', 'saved-map-description', `${labels.description} ${labels.selectRequired} ${labels.renameHint}`));
        const list = create('div', 'saved-map-list');
        const activeName = context.active?.active_map?.map_name
            || maps.find(map => map.active === true)?.map_name
            || null;
        const selectedName = snapshot?.selectedName || activeName;
        let progress = null;
        for (const map of maps) {
            const option = create('label', 'saved-map-option');
            option.title = labels.renameHint;
            const radio = create('input');
            radio.type = 'radio';
            radio.name = 'saved-navigation-map';
            radio.value = map.map_name;
            radio.checked = map.map_name === selectedName;
            const details = create('div');
            const heading = create('div', 'saved-map-name-row');
            heading.append(create('span', 'saved-map-name', map.map_name));
            if (map.active || map.map_name === activeName) heading.append(create('span', 'saved-map-active', 'ACTIVE'));
            const resolution = Number(map.resolution);
            const resolutionText = Number.isFinite(resolution) ? resolution.toFixed(2) : '--';
            const mapDetails = `${formatSavedAt(map.saved_at)} | ${map.width} x ${map.height} | ${resolutionText}m`;
            details.append(heading, create('div', 'saved-map-details', mapDetails));
            option.append(radio, details);
            option.addEventListener('contextmenu', event => {
                event.preventDefault();
                renameSavedMap(map, container, progress);
            });
            list.append(option);
        }
        if (!maps.length) list.append(create('div', 'saved-map-details', labels.noMaps));
        container.append(list);

        const pose = create('div', 'saved-map-pose');
        const poseValues = snapshot?.pose || { x: '0', y: '0', yaw: '0' };
        for (const [id, label, value] of [
            ['saved-map-pose-x', labels.positionX, poseValues.x],
            ['saved-map-pose-y', labels.positionY, poseValues.y],
            ['saved-map-pose-yaw', labels.heading, poseValues.yaw]
        ]) {
            const field = create('label', '', label);
            const input = create('input');
            input.id = id;
            input.type = 'number';
            input.step = 'any';
            input.value = value;
            field.append(input);
            pose.append(field);
        }
        container.append(pose);
        progress = create('div', 'saved-map-progress', context.feedback || '');
        if (context.feedback) showMessage(progress, context.feedback, context.feedbackIsError);
        container.append(progress);

        const actions = create('div', 'saved-map-actions');

        const save = create('button', 'saved-map-save-current', labels.saveCurrent);
        save.type = 'button';
        save.dataset.controlButton = 'accent';
        components.controls?.controlButton?.enhance?.(save);
        save.addEventListener('click', async () => {
            const savedName = await global.saveCurrentNavigationMap?.(save);
            if (savedName) await controller?.open?.();
        });

        const cancel = create('button', '', labels.cancel);
        cancel.type = 'button';
        cancel.dataset.controlButton = 'secondary';
        components.controls?.controlButton?.enhance?.(cancel);
        cancel.addEventListener('click', () => manager?.close());

        const load = create('button', 'saved-map-load', labels.load);
        load.type = 'button';
        load.dataset.controlButton = 'accent';
        components.controls?.controlButton?.enhance?.(load);
        load.disabled = !selectedMapName(container);
        load.addEventListener('click', () => loadSelectedMap(container, progress));

        container.addEventListener('change', event => {
            if (event.target?.name !== 'saved-navigation-map') return;
            load.disabled = !selectedMapName(container);
            showMessage(progress, '');
        });

        actions.append(save, cancel, load);
        container.append(actions);
        return container;
    }

    function cleanupView() {
        clearProgressTimers();
        if (state.closeTimer !== null) global.clearTimeout(state.closeTimer);
        state.closeTimer = null;
    }

    navigationMaps.mount = function mountSavedMapModal(options = {}) {
        if (controller) return controller;
        manager = options.manager || components.modal?.getDefault?.() || null;
        if (!manager) return null;

        manager.register(VIEW_NAME, {
            title: labels.title,
            render: renderView,
            captureState: () => captureViewState(),
            onClose: cleanupView,
        });

        controller = {
            async open() {
                if (state.busy) return null;
                try {
                    const [active, maps] = await Promise.all([
                        refreshActiveMap(),
                        requestJson('/api/navigation/maps')
                    ]);
                    state.maps = maps.maps || [];
                    manager.close();
                    return manager.open(VIEW_NAME, { maps: state.maps, active }, { replace: true });
                } catch (error) {
                    manager.close();
                    return manager.open(VIEW_NAME, { error: error.message }, { replace: true });
                }
            },
            close() {
                if (manager.activeView?.name === VIEW_NAME) manager.close();
            },
            refreshActiveMap,
        };

        const trigger = options.trigger || global.document.getElementById('lidarMapSelectBtn');
        if (trigger) {
            trigger.removeAttribute?.('onclick');
            trigger.addEventListener('click', event => {
                event.preventDefault();
                controller.open();
            });
        }
        global.openSavedMapModal = () => controller.open();
        refreshActiveMap();
        state.pollTimer = global.setInterval(refreshActiveMap, 3000);
        navigationMaps.controller = controller;
        return controller;
    };
})(window);
