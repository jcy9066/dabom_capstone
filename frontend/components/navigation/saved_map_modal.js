(function initializeSavedMapModal(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const navigationMaps = components.navigationMaps = components.navigationMaps || {};
    const VIEW_NAME = 'savedNavigationMaps';
    const labels = {
        title: '\uc9c0\ub3c4 \uc120\ud0dd',
        description: '\uc9c0\ub3c4\ub97c \ubd88\ub7ec\uc624\uba74 \ud604\uc7ac localization \uc704\uce58\uac00 \ucd08\uae30\ud654\ub429\ub2c8\ub2e4.',
        cancel: '\ucde8\uc18c',
        load: '\uc120\ud0dd \uc9c0\ub3c4 \ubd88\ub7ec\uc624\uae30',
        saveCurrent: '\ud604\uc7ac \uc9c0\ub3c4 \uc800\uc7a5',
        startMapping: '\uc0c8 \uc9c0\ub3c4 \uc0dd\uc131',
        clearCurrentMap: '\ud604\uc7ac \uc9c0\ub3c4 \uc81c\uac70',
        mappingStarting: 'Mapping \ubaa8\ub4dc\ub85c \uc804\ud658 \uc911...',
        mappingStarted: '\uc0c8 Mapping\uc744 \uc2dc\uc791\ud588\uc2b5\ub2c8\ub2e4.',
        currentMapCleared: '\ud604\uc7ac \uc9c0\ub3c4\ub97c 3D Viewer\uc5d0\uc11c \uc81c\uac70\ud588\uc2b5\ub2c8\ub2e4.',
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
        mapPrefix: 'MAP: ',
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
            AUTO_INITIAL_POSE_UNCERTAIN: '자동 위치 추정의 신뢰도가 부족합니다. X·Y·방향을 직접 입력해주세요.',
            AUTO_POSE_REQUIRES_STOP: '자동 위치 추정 전 로봇을 수동 정지 상태로 전환해주세요.',
            AUTO_POSE_MOVED: '위치 추정 중 로봇 움직임이 감지됐습니다. 정지 후 다시 시도해주세요.',
            MAP_LOAD_IN_PROGRESS: '\ub2e4\ub978 \uc9c0\ub3c4 \ubd88\ub7ec\uc624\uae30\uac00 \ucc98\ub9ac \uc911\uc785\ub2c8\ub2e4.',
            MAP_OPERATION_IN_PROGRESS: '\ub2e4\ub978 \uc800\uc7a5 \uc9c0\ub3c4 \uc791\uc5c5\uc774 \ucc98\ub9ac \uc911\uc785\ub2c8\ub2e4.',
            MAP_NAME_ALREADY_EXISTS: '\uc774\ubbf8 \uac19\uc740 \uc774\ub984\uc758 \uc800\uc7a5 \uc9c0\ub3c4\uac00 \uc788\uc2b5\ub2c8\ub2e4.',
            MAP_NAME_UNCHANGED: '\uc0c8 \uc9c0\ub3c4 \uc774\ub984\uc774 \uae30\uc874 \uc774\ub984\uacfc \uac19\uc2b5\ub2c8\ub2e4.',
            INVALID_MAP_NAME: '\uc9c0\ub3c4 \uc774\ub984\uc740 \uc601\ubb38, \uc22b\uc790, \ub9c8\uce68\ud45c, \ud558\uc774\ud508, \ubc11\uc904\ub9cc \uc0ac\uc6a9\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.'
        };
        return messages[error.code] || error.message || labels.unavailable;
    }

    function coordinateText(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number.toFixed(6) : '--';
    }

    function captureViewState(container = manager?.body) {
        const value = id => container?.querySelector?.(`#${id}`)?.value ?? '';
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
        container?.querySelectorAll?.('button, input, select').forEach(element => { element.disabled = disabled; });
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

    async function waitLocalizationSession(container, progress, sessionId) {
        for (let attempt = 0; attempt < 600; attempt++) {
            if (!container.isConnected) {
                // A closed modal must not leave an unobserved localization
                // attempt that might still activate Driving later.
                throw new Error('Localization 모달이 닫혔습니다. 상태를 다시 확인하세요.');
            }
            const state = await requestJson('/api/navigation/localization/state');
            if (state.session_id !== sessionId) {
                throw new Error('Localization 세션이 변경됐습니다.');
            }
            if (state.phase === 'READY') return state;
            if (state.phase === 'FAILED' || state.phase === 'CANCELLED') {
                const error = new Error(state.reason || 'Localization 실패');
                error.code = state.error_code || state.phase;
                throw error;
            }
            if (state.phase === 'WAITING_FOR_CLICK'
                || state.phase === 'POSE_CONFIRMATION_REQUIRED') return state;
            showMessage(progress,
                state.phase === 'APPLYING'
                    ? 'Initial Pose 검증 및 AMCL/Nav2 활성화 중...'
                    : 'LiDAR·Encoder를 이용해 Initial Pose를 추정 중...');
            await new Promise(resolve => global.setTimeout(resolve, 300));
        }
        throw new Error('Localization 상태 확인 시간이 초과됐습니다. 자동 주행을 시작하지 마세요.');
    }

    async function loadSelectedMap(container, progress) {
        const mapName = selectedMapName(container);
        if (state.busy) return;
        if (!mapName) {
            showMessage(progress, labels.selectRequired, true);
            return;
        }
        if (!global.confirm(`${mapName}${labels.confirmSuffix}`)) return;

        const xText = container.querySelector('#saved-map-pose-x').value.trim();
        const yText = container.querySelector('#saved-map-pose-y').value.trim();
        const yawText = container.querySelector('#saved-map-pose-yaw').value.trim();
        const manualPoseRequested = Boolean(xText || yText || yawText);
        if (manualPoseRequested && (!xText || !yText || !yawText)) {
            showMessage(progress, '수동 Initial Pose는 X, Y, 방향을 모두 입력해야 합니다.', true);
            return;
        }
        const x = Number(xText);
        const y = Number(yText);
        const yawDegrees = Number(yawText);
        if (manualPoseRequested && ![x, y, yawDegrees].every(Number.isFinite)) {
            showMessage(progress, '수동 Initial Pose는 유한한 숫자여야 합니다.', true);
            return;
        }

        state.busy = true;
        setControlsDisabled(container, true, true);
        showMessage(progress, manualPoseRequested
            ? labels.loading
            : '저장 지도와 최신 LiDAR를 정합해 자동 위치를 추정 중...');
        scheduleProgress(progress, labels.resetting, 700);
        scheduleProgress(progress, labels.verifying, 1800);
        let succeeded = false;
        try {
            global.navigationMapView?.restoreMapDisplay?.();
            const token = await csrfToken();
            let warningAcknowledged = false;
            let payload;
            const sessionId = container.dataset.localizationSessionId;
            const activeSession = sessionId
                && container.dataset.localizationMapName === mapName;
            if (!manualPoseRequested) {
                if (sessionId) {
                    await requestJson('/api/navigation/localization/cancel', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                        body: JSON.stringify({ session_id: sessionId }),
                    }).catch(() => null);
                }
                const started = await requestJson('/api/navigation/localization/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                    body: JSON.stringify({
                        source: 'existing', map_name: mapName,
                        ...(container.dataset.mapRevision
                            ? {map_revision: container.dataset.mapRevision} : {}),
                    }),
                });
                container.dataset.localizationSessionId = started.session_id;
                container.dataset.localizationMapName = mapName;
                const result = await waitLocalizationSession(
                    container, progress, started.session_id,
                );
                if (result.phase !== 'READY') {
                    showMessage(progress,
                        '자동 Initial Pose 추정 불확실: 지도에서 위치를 클릭하고 방향으로 드래그한 뒤 다시 불러오세요.',
                        true);
                    setControlsDisabled(container, false);
                    return;
                }
                payload = await requestJson('/api/navigation/control/state');
            } else if (activeSession) {
                for (;;) {
                    await requestJson('/api/navigation/localization/pose', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                        body: JSON.stringify({
                            session_id: sessionId,
                            initial_pose: {x, y, yaw_degrees: yawDegrees,
                                           warning_acknowledged: warningAcknowledged},
                        }),
                    });
                    const result = await waitLocalizationSession(
                        container, progress, sessionId,
                    );
                    if (result.phase === 'READY') break;
                    if (result.phase !== 'POSE_CONFIRMATION_REQUIRED'
                        || warningAcknowledged
                        || !global.confirm('미탐색 영역 또는 장애물 인접 위치입니다. 확인 후 AMCL 검증을 진행하시겠습니까?')) {
                        showMessage(progress,
                            'Initial Pose 지정이 필요합니다: ' + (result.reason || ''),
                            true);
                        setControlsDisabled(container, false);
                        return;
                    }
                    warningAcknowledged = true;
                }
                payload = await requestJson('/api/navigation/control/state');
            } else {
                // Preserve the original direct manual-pose route for users
                // who explicitly entered all three coordinates first.
                for (;;) {
                    try {
                        payload = await requestJson('/api/navigation/control/mode', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                            body: JSON.stringify({
                                mode: 'DRIVING', source: 'existing', map_name: mapName,
                                ...(container.dataset.mapRevision
                                    ? {map_revision: container.dataset.mapRevision} : {}),
                                initial_pose: {x, y, yaw_degrees: yawDegrees,
                                               warning_acknowledged: warningAcknowledged},
                            }),
                        });
                        break;
                    } catch (error) {
                        if (!warningAcknowledged
                            && error.code === 'INITIAL_POSE_CONFIRMATION_REQUIRED'
                            && global.confirm('미탐색 영역 또는 장애물 인접 위치입니다. 확인 후 AMCL 검증을 진행하시겠습니까?')) {
                            warningAcknowledged = true;
                            continue;
                        }
                        throw error;
                    }
                }
            }
            delete container.dataset.localizationSessionId;
            delete container.dataset.localizationMapName;
            global.navigationControl?.applyState?.(payload);
            showMessage(progress, `${payload.active_map?.map_name || mapName} ${labels.success}`);
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

    async function startMappingFromModal(container, progress) {
        if (state.busy) return;

        state.busy = true;
        setControlsDisabled(container, true, true);
        showMessage(progress, labels.mappingStarting);
        global.navigationMapView?.clearMapDisplay?.();

        try {
            const request = global.navigationControl?.requestNavigationMode;
            if (typeof request !== 'function') {
                throw new Error('Navigation control is unavailable.');
            }
            // "새 Mapping 시작"은 이미 MAPPING 상태여도 SLAM session을
            // 완전히 재시작해 이전 map/pose state를 남기지 않는다.
            const switched = await request('MAPPING', { restart: true });
            if (switched === false) {
                throw new Error('Mapping mode transition failed.');
            }
            showMessage(progress, labels.mappingStarted);
            state.closeTimer = global.setTimeout(() => {
                if (manager?.activeView?.name === VIEW_NAME) manager.close();
            }, 300);
        } catch (error) {
            global.navigationMapView?.restoreMapDisplay?.();
            showMessage(progress, `Mapping \uc2dc\uc791 \uc2e4\ud328: ${error.message || error}`, true);
            setControlsDisabled(container, false);
        } finally {
            state.busy = false;
        }
    }

    function clearCurrentMapFromViewer(progress) {
        const cleared = global.navigationMapView?.clearMapDisplay?.();
        if (cleared) {
            showMessage(progress, labels.currentMapCleared);
        } else {
            showMessage(progress, '\uc81c\uac70\ud560 \ud604\uc7ac \uc9c0\ub3c4\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.', true);
        }
    }

    function renderView(context = {}, snapshot = null) {
        const container = create('div', 'saved-map-modal');
        // Existing modal header remains the title owner; append only a visual hint.
        const headerTitle = manager?.title;
        if (headerTitle && !headerTitle.querySelector('.saved-map-title-hint')) {
            headerTitle.append(create('span', 'saved-map-title-hint',
                `${labels.description}`));
        }
        if (context.error) {
            const progress = create('div', 'saved-map-progress');
            showMessage(progress, `\uc9c0\ub3c4 \ubaa9\ub85d \uc870\ud68c \uc2e4\ud328: ${context.error}`, true);
            container.append(progress);
            return container;
        }

        const maps = context.maps || [];
        const layout = create('div', 'saved-map-content');
        const mapColumn = create('section', 'saved-map-primary-column');
        layout.append(mapColumn);
        container.append(layout);
        const list = create('div', 'saved-map-list');
        const activeName = context.active?.active_map?.map_name
            || maps.find(map => map.active === true)?.map_name
            || null;
        // An explicit null means the user cleared the selection; do not
        // silently restore the currently active map after a view refresh.
        const selectedName = snapshot?.selectedName !== undefined ? snapshot.selectedName : activeName;
        let currentSelection = selectedName;
        const radioButtons = [];
        let progress = null;
        for (const map of maps) {
            const option = create('label', 'saved-map-option');
            option.title = labels.renameHint;
            const radio = create('input');
            radio.type = 'radio';
            radio.name = 'saved-navigation-map';
            radio.value = map.map_name;
            radio.checked = map.map_name === selectedName;
            radioButtons.push(radio);
            const details = create('div');
            const heading = create('div', 'saved-map-name-row');
            heading.append(create('span', 'saved-map-name', map.map_name));
            if (map.active || map.map_name === activeName) heading.append(create('span', 'saved-map-active', 'ACTIVE'));
            const resolution = Number(map.resolution);
            const resolutionText = Number.isFinite(resolution) ? resolution.toFixed(2) : '--';
            const mapDetails = `${formatSavedAt(map.saved_at)} | ${map.width} x ${map.height} | ${resolutionText}m`;
            details.append(heading, create('div', 'saved-map-details', mapDetails));
            if (map.location && typeof map.location === 'object') {
                const savedGps = map.location.gps && typeof map.location.gps === 'object' ? map.location.gps : {};
                const savedPlace = map.location.name || '저장 위치';
                const coords = Number.isFinite(Number(savedGps.lat)) && Number.isFinite(Number(savedGps.lng))
                    ? ` · ${coordinateText(savedGps.lat)}, ${coordinateText(savedGps.lng)}` : '';
                details.append(create('div', 'saved-map-location-meta', `장소: ${savedPlace}${coords}`));
            }
            option.append(radio, details);
            option.addEventListener('click', event => {
                if (state.busy) return;
                // Prevent a label click from synthesizing a second radio click.
                // A direct input click must retain its native keyboard behavior.
                if (event.target !== radio) event.preventDefault();
                currentSelection = currentSelection === map.map_name ? null : map.map_name;
                for (const input of radioButtons) {
                    input.checked = input.value === currentSelection;
                }
                const loadButton = container.querySelector('.saved-map-load');
                if (loadButton) loadButton.disabled = !currentSelection;
                showMessage(progress, '');
                void refreshMapPreview();
            });
            option.addEventListener('contextmenu', event => {
                event.preventDefault();
                renameSavedMap(map, container, progress);
            });
            list.append(option);
        }
        if (!maps.length) list.append(create('div', 'saved-map-details', labels.noMaps));
        const pose = create('div', 'saved-map-pose');
        const poseValues = snapshot?.pose || { x: '', y: '', yaw: '' };
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
        mapColumn.append(pose);
        mapColumn.append(list);

        // Render the selected stored OccupancyGrid, not the active 3D map.
        // Canvas Y is inverted relative to ROS OccupancyGrid row order.
        const previewBox = create('div', 'saved-map-initialpose-preview');
        const previewInfo = create('div', 'saved-map-details',
            '지도를 선택하면 점유 지도가 표시됩니다. 클릭으로 위치, 드래그로 방향을 지정합니다.');
        const previewCanvas = create('canvas', 'saved-map-initialpose-canvas');
        previewCanvas.style.cssText =
            'display:block;max-width:420px;width:100%;height:auto;' +
            'border:1px solid #536578;border-radius:6px;image-rendering:pixelated;touch-action:none;cursor:crosshair;';
        previewBox.append(previewInfo, previewCanvas);
        mapColumn.append(previewBox);
        let previewData = null;
        let previewCells = null;
        let previewRequestId = 0;
        let dragStart = null;

        function gridPoint(event) {
            if (!previewData || !previewCells) return null;
            const rect = previewCanvas.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return null;
            const col = Math.min(previewData.width - 1, Math.max(0,
                Math.floor((event.clientX - rect.left) * previewCanvas.width / rect.width)));
            const imageRow = Math.min(previewData.height - 1, Math.max(0,
                Math.floor((event.clientY - rect.top) * previewCanvas.height / rect.height)));
            const row = previewData.height - 1 - imageRow;
            const localX = (col + 0.5) * previewData.resolution;
            const localY = (row + 0.5) * previewData.resolution;
            const theta = previewData.origin.yaw;
            return {
                x: previewData.origin.x + Math.cos(theta) * localX - Math.sin(theta) * localY,
                y: previewData.origin.y + Math.sin(theta) * localX + Math.cos(theta) * localY,
                col, imageRow,
            };
        }

        function renderPreview(end = null) {
            if (!previewData || !previewCells) return;
            const width = previewData.width, height = previewData.height;
            const ctx = previewCanvas.getContext('2d');
            if (!ctx) return;
            const image = ctx.createImageData(width, height);
            for (let i = 0; i < width * height; i++) {
                const col = i % width, row = Math.floor(i / width);
                const target = ((height - 1 - row) * width + col) * 4;
                const occupancy = previewCells[i];
                const grey = occupancy < 0 ? 116 : occupancy >= 65 ? 27 : 226;
                image.data[target] = grey;
                image.data[target + 1] = grey;
                image.data[target + 2] = grey;
                image.data[target + 3] = 255;
            }
            ctx.putImageData(image, 0, 0);
            if (dragStart) {
                ctx.lineWidth = Math.max(1, width / 170);
                ctx.strokeStyle = '#e85d58';
                ctx.fillStyle = '#e85d58';
                ctx.beginPath();
                ctx.arc(dragStart.col, dragStart.imageRow, Math.max(2, width / 90), 0, Math.PI * 2);
                ctx.fill();
                if (end) {
                    ctx.beginPath();
                    ctx.moveTo(dragStart.col, dragStart.imageRow);
                    ctx.lineTo(end.col, end.imageRow);
                    ctx.stroke();
                }
            }
        }

        async function refreshMapPreview() {
            const targetName = currentSelection;
            const requestId = ++previewRequestId;
            previewData = null;
            previewCells = null;
            dragStart = null;
            delete container.dataset.mapRevision;
            previewInfo.textContent = targetName
                ? '선택한 지도 점유 데이터를 불러오는 중...'
                : '지도를 선택하면 위치·방향을 지정할 수 있습니다.';
            if (!targetName) return;
            try {
                const data = await requestJson(
                    '/api/navigation/maps/' + encodeURIComponent(targetName) + '/preview');
                if (requestId !== previewRequestId || targetName !== currentSelection) return;
                const width = Number(data.width), height = Number(data.height);
                const n = width * height;
                if (!Number.isSafeInteger(n) || n <= 0 || n > 2000000
                    || !Array.isArray(data.data)) throw new Error('잘못된 지도 데이터');
                let cells;
                if (data.data_encoding === 'rle') {
                    cells = [];
                    for (const run of data.data) {
                        const count = Number(run[1]);
                        if (!Number.isSafeInteger(count) || count <= 0 || count > n - cells.length)
                            throw new Error('잘못된 RLE 데이터');
                        for (let k = 0; k < count; k++) cells.push(Number(run[0]));
                    }
                } else cells = data.data.map(Number);
                if (cells.length !== n) throw new Error('지도 크기 불일치');
                previewData = {
                    width, height, resolution: Number(data.resolution),
                    origin: {x: Number(data.origin.x), y: Number(data.origin.y),
                             yaw: Number(data.origin.yaw || 0)}
                };
                if (![previewData.resolution, ...Object.values(previewData.origin)]
                    .every(Number.isFinite) || previewData.resolution <= 0)
                    throw new Error('지도 원점·해상도 오류');
                previewCells = cells;
                previewCanvas.width = width;
                previewCanvas.height = height;
                previewCanvas.style.aspectRatio = width + ' / ' + height;
                container.dataset.mapRevision = data.map_revision;
                previewInfo.textContent =
                    '회색: 미탐색 · 검정: 장애물 · 밝은색: 이동 가능 영역. 클릭 후 방향으로 드래그하세요.';
                renderPreview();
            } catch (error) {
                if (requestId === previewRequestId) {
                    previewInfo.textContent = '지도 미리보기 실패: ' + error.message;
                }
            }
        }

        previewCanvas.addEventListener('pointerdown', event => {
            if (state.busy || !previewData) return;
            dragStart = gridPoint(event);
            if (!dragStart) return;
            previewCanvas.setPointerCapture(event.pointerId);
            renderPreview(dragStart);
        });
        previewCanvas.addEventListener('pointermove', event => {
            if (dragStart) renderPreview(gridPoint(event));
        });
        previewCanvas.addEventListener('pointerup', event => {
            if (!dragStart || !previewData) return;
            const end = gridPoint(event);
            if (!end) return;
            const start = dragStart;
            renderPreview(end);
            container.querySelector('#saved-map-pose-x').value = start.x.toFixed(3);
            container.querySelector('#saved-map-pose-y').value = start.y.toFixed(3);
            const length = Math.hypot(end.x - start.x, end.y - start.y);
            if (length >= Math.max(0.08, previewData.resolution)) {
                container.querySelector('#saved-map-pose-yaw').value =
                    (Math.atan2(end.y - start.y, end.x - start.x) * 180 / Math.PI).toFixed(2);
            } else {
                previewInfo.textContent = '위치를 선택했습니다. 방향을 드래그하거나 각도를 입력하세요.';
            }
            previewCanvas.releasePointerCapture(event.pointerId);
        });
        previewCanvas.addEventListener('pointercancel', () => { dragStart = null; renderPreview(); });
        global.setTimeout(() => { if (container.isConnected) void refreshMapPreview(); }, 0);

        progress = create('div', 'saved-map-progress', context.feedback || '');
        if (context.feedback) showMessage(progress, context.feedback, context.feedbackIsError);
        mapColumn.append(progress);

        const mappingActions = create('div', 'saved-map-mode-actions');

        const startMapping = create('button', 'mb-btn saved-map-start-mapping', labels.startMapping);
        startMapping.type = 'button';
        startMapping.addEventListener('click', () => startMappingFromModal(container, progress));

        const clearCurrentMap = create('button', 'mb-btn saved-map-clear-current', labels.clearCurrentMap);
        clearCurrentMap.type = 'button';
        clearCurrentMap.title = '\uc800\uc7a5 \ud30c\uc77c\uc740 \uc720\uc9c0\ud558\uace0 3D Viewer\uc5d0\uc11c \ud604\uc7ac \uc9c0\ub3c4\ub9cc \uc81c\uac70\ud569\ub2c8\ub2e4.';
        clearCurrentMap.disabled = !global.navigationMapView?.snapshot?.()?.map;
        clearCurrentMap.addEventListener('click', () => {
            clearCurrentMapFromViewer(progress);
            clearCurrentMap.disabled = true;
        });

        mappingActions.append(startMapping, clearCurrentMap);

        const actions = create('div', 'saved-map-actions');

        const save = create('button', 'mb-btn saved-map-save-current', labels.saveCurrent);
        save.type = 'button';
        // A rendered LiDAR Preview is not a persisted, verified SLAM map.
        save.disabled = true;
        async function refreshMapReadiness() {
            if (!container.isConnected) return;
            try {
                const health = await requestJson('/api/navigation/status');
                if (!container.isConnected) return;
                save.disabled = health?.map_saveable !== true;
                save.title = save.disabled
                    ? 'SLAM /map 검증 전에는 임시 Preview를 저장할 수 없습니다.'
                    : '검증된 마지막 SLAM 지도를 저장합니다.';
            } catch (error) {
                save.disabled = true;
            }
        }
        // All events share this listener; remove it when the modal is replaced.
        const onMapChanged = () => {
            if (!container.isConnected) {
                global.document.removeEventListener(
                    'dabom:navigation-visualization-state', onMapChanged,
                );
                return;
            }
            void refreshMapReadiness();
        };
        global.document.addEventListener(
            'dabom:navigation-visualization-state', onMapChanged,
        );
        global.setTimeout(() => void refreshMapReadiness(), 0);
        save.addEventListener('click', async () => {
            const health = await requestJson('/api/navigation/status').catch(() => null);
            if (!health || health.map_saveable !== true) {
                save.disabled = true;
                showMessage(progress, '검증된 SLAM 지도가 없습니다.', true);
                return;
            }
            const savedName = await global.saveCurrentNavigationMap?.(save);
            if (savedName) await controller?.open?.();
        });

        const load = create('button', 'mb-btn saved-map-load', labels.load);
        load.type = 'button';
        load.disabled = !selectedMapName(container);
        load.addEventListener('click', () => loadSelectedMap(container, progress));

        container.addEventListener('change', event => {
            if (event.target?.name !== 'saved-navigation-map') return;
            currentSelection = selectedMapName(container);
            load.disabled = !currentSelection;
            showMessage(progress, '');
        });

        actions.append(save, load);
        const primaryActions = create('div', 'saved-map-primary-actions');
        primaryActions.append(mappingActions, actions);
        mapColumn.append(primaryActions);
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
                        requestJson('/api/navigation/maps'),
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
