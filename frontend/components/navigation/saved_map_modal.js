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
        startMapping: '\uc0c8 Mapping \uc2dc\uc791',
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
        locationTitle: '장소 / GPS',
        currentLocation: '현재 장소',
        gpsStatus: 'GPS',
        securityStatus: '보안 구역',
        noFix: 'NO FIX',
        unknown: '확인 불가',
        normal: '정상',
        outside: '구역 이탈',
        newLocation: '새 장소',
        locationName: '장소 이름',
        latitude: '위도',
        longitude: '경도',
        radius: '허용 반경 (m)',
        useCurrentGps: '현재 위치 사용',
        saveLocation: '장소 저장',
        deleteLocation: '장소 삭제'
    };
    const state = {
        maps: [],
        active: null,
        activeApiAvailable: true,
        locationStatus: null,
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

    function createControlButton(value, variant = 'secondary', className = '') {
        const button = create('button', className, value);
        button.type = 'button';
        button.dataset.controlButton = variant;
        components.controls?.controlButton?.enhance?.(button);
        return button;
    }

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

    function locationFallback(error = '') {
        return {
            unavailable: true,
            error,
            gps: { fix: false },
            security_state: 'UNKNOWN',
            last_location_id: null,
            last_location_name: null,
            locations: [],
        };
    }

    function coordinateText(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number.toFixed(6) : '--';
    }

    function renderLocationPanel(status = locationFallback()) {
        const panel = create('section', 'saved-map-location-panel');
        panel.append(create('div', 'saved-map-section-title', labels.locationTitle));

        const gps = status?.gps || { fix: false };
        const liveName = gps.matched_location_name;
        const lastName = status?.last_location_name;
        const placeText = liveName || (lastName ? `${lastName} (마지막 확인)` : labels.unknown);
        const satelliteText = Number.isFinite(Number(gps.satellites)) ? ` · 위성 ${Number(gps.satellites)}` : '';
        const gpsText = gps.fix ? `FIX${satelliteText}` : labels.noFix;
        const securityText = status?.security_state === 'OUT_OF_AREA'
            ? labels.outside
            : status?.security_state === 'NORMAL'
                ? labels.normal
                : labels.unknown;

        const statusGrid = create('div', 'saved-map-location-status');
        for (const [name, value, extraClass] of [
            [labels.currentLocation, placeText, ''],
            [labels.gpsStatus, gpsText, gps.fix ? 'is-ok' : ''],
            [labels.securityStatus, securityText, status?.security_state === 'OUT_OF_AREA' ? 'is-danger' : ''],
        ]) {
            const row = create('div', `saved-map-location-status-row ${extraClass}`.trim());
            row.append(create('span', 'saved-map-location-status-label', name));
            row.append(create('span', 'saved-map-location-status-value', value));
            statusGrid.append(row);
        }
        panel.append(statusGrid);

        if (status?.unavailable) {
            panel.append(create('div', 'saved-map-location-note', `GPS/장소 상태 확인 불가: ${status.error || labels.unavailable}`));
            return panel;
        }

        const locations = Array.isArray(status.locations) ? status.locations : [];
        const selector = create('select', 'saved-map-location-select');
        selector.id = 'saved-map-location-select';
        const blank = create('option', '', labels.newLocation);
        blank.value = '';
        selector.append(blank);
        for (const location of locations) {
            const option = create('option', '', location.name || location.location_id);
            option.value = location.location_id || '';
            selector.append(option);
        }
        if (status.last_location_id && locations.some(item => item.location_id === status.last_location_id)) {
            selector.value = status.last_location_id;
        }

        const fields = create('div', 'saved-map-location-fields');
        const fieldSpecs = [
            ['saved-map-location-name', labels.locationName, 'text'],
            ['saved-map-location-lat', labels.latitude, 'number'],
            ['saved-map-location-lng', labels.longitude, 'number'],
            ['saved-map-location-radius', labels.radius, 'number'],
        ];
        const inputs = {};
        for (const [id, label, type] of fieldSpecs) {
            const field = create('label', '', label);
            const input = create('input');
            input.id = id;
            input.type = type;
            if (type === 'number') input.step = 'any';
            field.append(input);
            fields.append(field);
            inputs[id] = input;
        }
        inputs['saved-map-location-radius'].value = '30';

        const loadSelectedLocation = () => {
            const location = locations.find(item => item.location_id === selector.value);
            inputs['saved-map-location-name'].value = location?.name || '';
            inputs['saved-map-location-lat'].value = location?.lat ?? '';
            inputs['saved-map-location-lng'].value = location?.lng ?? '';
            inputs['saved-map-location-radius'].value = location?.radius_m ?? '30';
            deleteButton.disabled = !location;
        };

        const actions = create('div', 'saved-map-location-actions');
        const useCurrent = createControlButton(labels.useCurrentGps, 'secondary');
        useCurrent.dataset.requiresGps = 'true';
        useCurrent.disabled = gps.fix !== true;
        useCurrent.addEventListener('click', () => {
            if (gps.fix !== true) return;
            inputs['saved-map-location-lat'].value = gps.lat ?? '';
            inputs['saved-map-location-lng'].value = gps.lng ?? '';
            if (!inputs['saved-map-location-name'].value && gps.matched_location_name) {
                inputs['saved-map-location-name'].value = gps.matched_location_name;
            }
        });

        const saveLocation = createControlButton(labels.saveLocation, 'accent');
        saveLocation.addEventListener('click', async () => {
            const name = inputs['saved-map-location-name'].value.trim();
            const latText = inputs['saved-map-location-lat'].value.trim();
            const lngText = inputs['saved-map-location-lng'].value.trim();
            const radiusText = inputs['saved-map-location-radius'].value.trim();
            if (!name || !latText || !lngText || !radiusText) {
                global.alert('장소 이름, 좌표, 허용 반경을 확인하세요.');
                return;
            }
            const lat = Number(latText);
            const lng = Number(lngText);
            const radius = Number(radiusText);
            if (![lat, lng, radius].every(Number.isFinite)) {
                global.alert('장소 이름, 좌표, 허용 반경을 확인하세요.');
                return;
            }
            try {
                saveLocation.disabled = true;
                const token = await csrfToken();
                await requestJson('/api/navigation/locations', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
                    body: JSON.stringify({
                        location_id: selector.value || null,
                        name,
                        lat,
                        lng,
                        radius_m: radius,
                    })
                });
                await controller?.open?.();
            } catch (error) {
                global.alert(`장소 저장 실패: ${error.message || labels.unavailable}`);
            } finally {
                saveLocation.disabled = false;
            }
        });

        const deleteButton = createControlButton(labels.deleteLocation, 'danger');
        deleteButton.disabled = true;
        deleteButton.addEventListener('click', async () => {
            if (!selector.value) return;
            const selected = locations.find(item => item.location_id === selector.value);
            if (!global.confirm(`${selected?.name || selector.value} 장소를 삭제하시겠습니까?`)) return;
            try {
                deleteButton.disabled = true;
                const token = await csrfToken();
                await requestJson(`/api/navigation/locations/${encodeURIComponent(selector.value)}`, {
                    method: 'DELETE',
                    headers: { 'X-CSRF-Token': token }
                });
                await controller?.open?.();
            } catch (error) {
                global.alert(`장소 삭제 실패: ${error.message || labels.unavailable}`);
            }
        });

        selector.addEventListener('change', loadSelectedLocation);
        actions.append(useCurrent, saveLocation, deleteButton);
        panel.append(selector, fields, actions);
        loadSelectedLocation();
        return panel;
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
            container?.querySelectorAll?.('[data-requires-gps="true"]').forEach(element => {
                element.disabled = state.locationStatus?.gps?.fix !== true;
            });
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

        const xText = container.querySelector('#saved-map-pose-x').value.trim();
        const yText = container.querySelector('#saved-map-pose-y').value.trim();
        const yawText = container.querySelector('#saved-map-pose-yaw').value.trim();
        if (!xText || !yText || !yawText) {
            showMessage(progress, '저장 지도를 불러올 때는 실제 Initial Pose를 입력해야 합니다.', true);
            return;
        }
        const x = Number(xText);
        const y = Number(yText);
        const yawDegrees = Number(yawText);
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
            global.navigationMapView?.restoreMapDisplay?.();
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
        if (context.error) {
            const progress = create('div', 'saved-map-progress');
            showMessage(progress, `\uc9c0\ub3c4 \ubaa9\ub85d \uc870\ud68c \uc2e4\ud328: ${context.error}`, true);
            container.append(progress);
            return container;
        }

        const maps = context.maps || [];
        const locationStatus = context.locationStatus || state.locationStatus || locationFallback();
        container.append(create('p', 'saved-map-description', `${labels.description} ${labels.selectRequired} ${labels.renameHint}`));
        container.append(renderLocationPanel(locationStatus));
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
            if (map.location && typeof map.location === 'object') {
                const savedGps = map.location.gps && typeof map.location.gps === 'object' ? map.location.gps : {};
                const savedPlace = map.location.name || '저장 위치';
                const coords = Number.isFinite(Number(savedGps.lat)) && Number.isFinite(Number(savedGps.lng))
                    ? ` · ${coordinateText(savedGps.lat)}, ${coordinateText(savedGps.lng)}` : '';
                details.append(create('div', 'saved-map-location-meta', `장소: ${savedPlace}${coords}`));
            }
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
        container.append(pose);
        progress = create('div', 'saved-map-progress', context.feedback || '');
        if (context.feedback) showMessage(progress, context.feedback, context.feedbackIsError);
        container.append(progress);

        const mappingActions = create('div', 'saved-map-mode-actions');

        const startMapping = create('button', 'saved-map-start-mapping', labels.startMapping);
        startMapping.type = 'button';
        startMapping.dataset.controlButton = 'success';
        components.controls?.controlButton?.enhance?.(startMapping);
        startMapping.addEventListener('click', () => startMappingFromModal(container, progress));

        const clearCurrentMap = create('button', 'saved-map-clear-current', labels.clearCurrentMap);
        clearCurrentMap.type = 'button';
        clearCurrentMap.dataset.controlButton = 'secondary';
        clearCurrentMap.title = '\uc800\uc7a5 \ud30c\uc77c\uc740 \uc720\uc9c0\ud558\uace0 3D Viewer\uc5d0\uc11c \ud604\uc7ac \uc9c0\ub3c4\ub9cc \uc81c\uac70\ud569\ub2c8\ub2e4.';
        components.controls?.controlButton?.enhance?.(clearCurrentMap);
        clearCurrentMap.disabled = !global.navigationMapView?.snapshot?.()?.map;
        clearCurrentMap.addEventListener('click', () => {
            clearCurrentMapFromViewer(progress);
            clearCurrentMap.disabled = true;
        });

        mappingActions.append(startMapping, clearCurrentMap);
        container.append(mappingActions);

        const actions = create('div', 'saved-map-actions');

        const save = createControlButton(labels.saveCurrent, 'accent', 'saved-map-save-current');
        save.addEventListener('click', async () => {
            const savedName = await global.saveCurrentNavigationMap?.(save);
            if (savedName) await controller?.open?.();
        });

        const cancel = createControlButton(labels.cancel, 'secondary');
        cancel.addEventListener('click', () => manager?.close());

        const load = createControlButton(labels.load, 'accent', 'saved-map-load');
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
                    const [active, maps, locationStatus] = await Promise.all([
                        refreshActiveMap(),
                        requestJson('/api/navigation/maps'),
                        requestJson('/api/navigation/locations').catch(error => locationFallback(error.message))
                    ]);
                    state.maps = maps.maps || [];
                    state.locationStatus = locationStatus;
                    manager.close();
                    return manager.open(VIEW_NAME, { maps: state.maps, active, locationStatus }, { replace: true });
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
