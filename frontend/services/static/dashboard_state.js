(() => {
    'use strict';

    const DEFAULT_ENDPOINTS = {
        alertLog: '',
        deviceLogs: '/api/logs/system-status',
        patrolLogs: '/api/logs/events',
        actionLogs: '/api/logs/actions'
    };
    const endpoints = {
        ...DEFAULT_ENDPOINTS,
        ...(window.DABOM_DASHBOARD_ENDPOINTS || {})
    };
    const unavailableEndpoints = new Set();
    let alertClearCutoffMs = 0;

    const byId = id => document.getElementById(id);
    const asObject = value => value && typeof value === 'object' ? value : {};
    const firstValue = (...values) => values.find(value => value !== undefined && value !== null && value !== '');

    function parseDate(value) {
        if (value === undefined || value === null || value === '') return null;
        const numeric = Number(value);
        const parsed = Number.isFinite(numeric)
            ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
            : new Date(value);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function setText(id, value, stateClass = '') {
        const element = byId(id);
        if (!element) return;
        element.textContent = value;
        element.classList.remove('state-danger', 'state-success');
        if (stateClass) element.classList.add(stateClass);
        element.title = String(value);
    }

    function markUpdated(timestamp) {
        const element = byId('server-state-updated');
        if (!element) return;
        const parsed = parseDate(timestamp) || new Date();
        element.textContent = parsed.toLocaleTimeString('ko-KR', { hour12: false });
    }

    function setPiState(value, available = true) {
        const element = byId('pi-connection-status');
        if (!element) return;
        element.classList.remove('state-online', 'state-offline', 'state-unknown');
        if (!available || typeof value !== 'boolean') {
            element.textContent = 'UNKNOWN';
            element.classList.add('state-unknown');
        } else if (value) {
            element.textContent = 'ONLINE';
            element.classList.add('state-online');
        } else {
            element.textContent = 'OFFLINE';
            element.classList.add('state-offline');
        }
    }

    function setRobotMode(value) {
        const element = byId('robot-mode-status');
        if (!element) return;
        const mode = String(value || '').trim().toLowerCase();
        element.classList.remove('state-auto', 'state-manual', 'state-unknown');
        if (mode === 'auto' || mode === 'manual') {
            element.textContent = mode.toUpperCase();
            element.classList.add(mode === 'auto' ? 'state-auto' : 'state-manual');
        } else {
            element.textContent = 'UNKNOWN';
            element.classList.add('state-unknown');
        }
    }

    function formatOperationMode(value) {
        const mode = String(value || '').trim().toLowerCase();
        const labels = {
            mapping: 'MAPPING',
            driving: 'DRIVING',
            localization_nav2: 'LOCALIZATION + NAV2',
            localization: 'LOCALIZATION',
            scan_only: 'SCAN ONLY'
        };
        return labels[mode] || (mode ? mode.toUpperCase() : 'UNKNOWN');
    }

    function summarizeGoal(value) {
        if (!value) return 'UNAVAILABLE';
        if (typeof value === 'string' || typeof value === 'number') return String(value);
        const goal = asObject(value);
        const label = firstValue(goal.label, goal.name, goal.id, goal.goal_id);
        if (label) return String(label);
        const pose = asObject(firstValue(goal.pose, goal.position, goal));
        if (Number.isFinite(Number(pose.x)) && Number.isFinite(Number(pose.y))) {
            return `X ${Number(pose.x).toFixed(2)} / Y ${Number(pose.y).toFixed(2)}`;
        }
        return 'AVAILABLE';
    }

    function summarizePath(value) {
        if (!value) return 'UNAVAILABLE';
        if (typeof value === 'string') return value;
        const path = Array.isArray(value)
            ? value
            : firstValue(value.poses, value.points, value.path);
        if (Array.isArray(path)) return `${path.length} POINTS`;
        return 'AVAILABLE';
    }

    function renderAlerts(items) {
        const target = byId('alertBox');
        if (!target || !Array.isArray(items)) return;
        const visibleItems = items.filter(item => {
            if (!alertClearCutoffMs) return true;
            const source = asObject(item);
            const timestamp = firstValue(source.timestamp, source.created_at, source.time);
            const parsed = parseDate(timestamp);
            return Boolean(parsed && parsed.getTime() > alertClearCutoffMs);
        });
        target.replaceChildren();
        if (!visibleItems.length) {
            const empty = document.createElement('div');
            empty.className = 'alert-entry alert-info';
            const message = document.createElement('span');
            message.className = 'alert-message';
            message.textContent = '조회된 실시간 알림이 없습니다.';
            empty.append(message);
            target.append(empty);
            return;
        }
        for (const item of visibleItems.slice(-100).reverse()) {
            const alert = asObject(item);
            const level = String(firstValue(alert.level, alert.severity, alert.type, 'info')).toLowerCase();
            const row = document.createElement('div');
            row.className = `alert-entry ${level.includes('danger') || level.includes('critical') ? 'alert-danger' : level.includes('warn') ? 'alert-warning' : 'alert-info'}`;
            const time = document.createElement('span');
            time.className = 'alert-time';
            const timestamp = firstValue(alert.timestamp, alert.created_at, alert.time);
            const parsed = parseDate(timestamp);
            time.textContent = parsed
                ? parsed.toLocaleTimeString('ko-KR', { hour12: false })
                : String(timestamp || '--:--:--');
            const message = document.createElement('span');
            message.className = 'alert-message';
            message.textContent = String(firstValue(alert.message, alert.content, alert.description, '-'));
            row.append(time, message);
            target.append(row);
        }
    }

    function applyNavigation(payload) {
        const source = asObject(payload);
        setText('operation-mode-status', formatOperationMode(firstValue(source.operation_mode, source.mode)));
        setText('navigation-state-status', String(firstValue(source.navigation_state, source.state, source.status, 'UNKNOWN')).toUpperCase());
        markUpdated(firstValue(source.updated_at, source.timestamp));
    }

    function applyControlState(payload) {
        const source = asObject(payload);
        setPiState(source.connected, typeof source.connected === 'boolean');
        setRobotMode(source.robot_mode);
        applyNavigation({
            operation_mode: source.navigation_mode,
            navigation_state: source.navigation_state,
            updated_at: source.updated_at,
        });
        setText('current-goal-status', summarizeGoal(source.active_goal));
        setText('planned-path-status', summarizePath(source.planned_path));
        if (typeof source.emergency_stop === 'boolean') {
            setText(
                'emergency-stop-status',
                source.emergency_stop ? 'ACTIVE' : 'CLEAR',
                source.emergency_stop ? 'state-danger' : 'state-success',
            );
        } else {
            setText('emergency-stop-status', 'UNAVAILABLE');
        }
        const activeMap = asObject(source.active_map);
        if (activeMap.map_name) setText('active-map-status', activeMap.map_name, 'state-success');
        else setText('active-map-status', 'NOT SELECTED');
    }

    async function requestJson(endpoint) {
        if (!endpoint || unavailableEndpoints.has(endpoint)) return { state: 'unavailable' };
        try {
            const response = await fetch(endpoint, { credentials: 'same-origin', cache: 'no-store' });
            const payload = await response.json().catch(() => ({}));
            if (response.status === 404 || response.status === 405) {
                unavailableEndpoints.add(endpoint);
                return { state: 'unavailable' };
            }
            if (!response.ok || payload.ok === false) {
                return { state: 'error', message: payload.detail || payload.error || `HTTP ${response.status}` };
            }
            return { state: 'ready', payload };
        } catch (error) {
            return { state: 'error', message: error.message };
        }
    }

    function extractRows(payload) {
        if (Array.isArray(payload)) return payload;
        const source = asObject(payload);
        return firstValue(source.rows, source.items, source.logs, source.records, source.data, []);
    }

    function splitTimestamp(row) {
        const source = asObject(row);
        if (source.date && source.time) return { date: source.date, time: source.time };
        const value = firstValue(source.detected_at, source.timestamp, source.created_at, source.recorded_at, source.updated_at);
        const parsed = parseDate(value);
        if (!parsed) return { date: source.date || '-', time: source.time || '-' };
        return {
            date: parsed.toLocaleDateString('sv-SE'),
            time: parsed.toLocaleTimeString('ko-KR', { hour12: false })
        };
    }

    function normalizeStatusRow(value) {
        const row = asObject(value);
        const timestamp = splitTimestamp(row);
        return {
            ...timestamp,
            status_id: row.status_id,
            cpu_usage: firstValue(row.cpu_usage, '-'),
            cpu_temperature: firstValue(row.cpu_temperature, '-'),
            ram_usage: firstValue(row.ram_usage, '-'),
            ping: firstValue(row.ping, '-'),
            is_autonomous: row.is_autonomous,
            speed: firstValue(row.speed, '-'),
            gps_lat: firstValue(row.gps_lat, '-'),
            gps_lng: firstValue(row.gps_lng, '-'),
            gps_alt: firstValue(row.gps_alt, '-'),
            lidar_x: firstValue(row.lidar_x, '-'),
            lidar_y: firstValue(row.lidar_y, '-')
        };
    }

    function normalizePatrolRow(value) {
        const row = asObject(value);
        const imageUrl = typeof row.image_url === 'string' && row.image_url ? row.image_url : null;
        return {
            ...splitTimestamp(row),
            event_id: row.event_id,
            event_source: firstValue(row.event_source, '-'),
            event_type: firstValue(row.event_type, '-'),
            confidence: row.confidence,
            gps_lat: firstValue(row.gps_lat, '-'),
            gps_lng: firstValue(row.gps_lng, '-'),
            gps_alt: firstValue(row.gps_alt, '-'),
            lidar_x: firstValue(row.lidar_x, '-'),
            lidar_y: firstValue(row.lidar_y, '-'),
            is_resolved: row.is_resolved,
            is_reported: row.is_reported,
            is_alerted: row.is_alerted,
            is_false_alarm: row.is_false_alarm === true || row.is_false_alarm === 1 || row.is_false_alarm === '1',
            has_image: row.has_image === true && Boolean(imageUrl),
            image_url: row.has_image === true ? imageUrl : null,
        };
    }

    function normalizeActionRow(value) {
        const row = asObject(value);
        const imageUrl = typeof row.image_url === 'string' && row.image_url ? row.image_url : null;
        const administratorName = firstValue(row.user_name, row.administrator_name, row.name, row.user_id, '-');
        const administratorEmail = firstValue(row.user_email, row.email);
        return {
            ...splitTimestamp(row),
            action_id: row.action_id,
            user_id: row.user_id,
            user_name: administratorName,
            user_email: administratorEmail || '',
            administrator_name: administratorEmail
                ? `${administratorName} (${administratorEmail})`
                : String(administratorName),
            event_id: row.event_id,
            action_type: firstValue(row.action_type, '-'),
            description_content: firstValue(row.description_content, '-'),
            has_image: row.has_image === true && Boolean(imageUrl),
            image_url: row.has_image === true ? imageUrl : null,
        };
    }

    // Keep the 50-row log API contract; paginate the 10-row view locally.
    const LOG_API_PAGE_SIZE = 50;
    const LOG_DISPLAY_PAGE_SIZE = 10;

    function logPaging(filters = {}) {
        const displayPage = Math.max(1, Math.floor(Number(filters.page) || 1));
        const startIndex = (displayPage - 1) * LOG_DISPLAY_PAGE_SIZE;
        return {
            displayPage,
            apiPage: Math.floor(startIndex / LOG_API_PAGE_SIZE) + 1,
            offset: startIndex % LOG_API_PAGE_SIZE,
        };
    }

    function buildLogQuery(type, filters = {}) {
        const query = new URLSearchParams();
        if (filters.startAt) query.set('start_at', filters.startAt);
        if (filters.endAt) query.set('end_at', filters.endAt);
        query.set('page', String(logPaging(filters).apiPage));
        query.set('page_size', String(LOG_API_PAGE_SIZE));
        if (filters.sortBy) query.set('sort_by', filters.sortBy);
        if (filters.sortDirection) query.set('sort_direction', filters.sortDirection);
        if (type === 'patrolModal') {
            if (filters.eventType) query.set('event_type', filters.eventType);
            if (filters.confidenceMin !== '' && filters.confidenceMin != null) {
                query.set('confidence_min', String(filters.confidenceMin));
            }
            if (filters.confidenceMax !== '' && filters.confidenceMax != null) {
                query.set('confidence_max', String(filters.confidenceMax));
            }
            if (filters.isResolved !== '' && filters.isResolved != null) query.set('is_resolved', String(filters.isResolved));
            if (filters.isReported !== '' && filters.isReported != null) query.set('is_reported', String(filters.isReported));
            if (filters.isAlerted !== '' && filters.isAlerted != null) query.set('is_alerted', String(filters.isAlerted));
            if (filters.isFalseAlarm !== '' && filters.isFalseAlarm != null) query.set('is_false_alarm', String(filters.isFalseAlarm));
        } else if (type === 'actionsModal') {
            const userName = String(filters.userName || '').trim();
            if (userName) query.set('user_name', userName);
            if (filters.actionType) query.set('action_type', filters.actionType);
        }
        return query;
    }

    async function fetchLogRows(type, filters = {}) {
        const endpoint = type === 'statusModal'
            ? endpoints.deviceLogs
            : type === 'patrolModal'
                ? endpoints.patrolLogs
                : endpoints.actionLogs;
        if (!endpoint) {
            return {
                state: 'unavailable',
                rows: [],
                message: '조회 API가 아직 연결되지 않았습니다.'
            };
        }
        const paging = logPaging(filters);
        const query = buildLogQuery(type, filters);
        const queryString = query.toString();
        const requestEndpoint = queryString ? `${endpoint}?${queryString}` : endpoint;
        const result = await requestJson(requestEndpoint);
        if (result.state !== 'ready') {
            return {
                state: result.state,
                rows: [],
                message: result.message || '조회 API를 사용할 수 없습니다.'
            };
        }
        const rows = extractRows(result.payload);
        if (!Array.isArray(rows)) {
            return { state: 'error', rows: [], message: '서버 응답 형식을 확인할 수 없습니다.' };
        }
        const total = Math.max(0, Number(result.payload?.total) || 0);
        return {
            state: 'ready',
            rows: rows.slice(paging.offset, paging.offset + LOG_DISPLAY_PAGE_SIZE).map(
                type === 'statusModal'
                    ? normalizeStatusRow
                    : type === 'patrolModal'
                        ? normalizePatrolRow
                        : normalizeActionRow
            ),
            page: paging.displayPage,
            page_size: LOG_DISPLAY_PAGE_SIZE,
            total,
            total_pages: Math.ceil(total / LOG_DISPLAY_PAGE_SIZE),
            message: ''
        };
    }

    async function pollAlerts() {
        if (!endpoints.alertLog) return;
        const result = await requestJson(endpoints.alertLog);
        if (result.state === 'ready') {
            const rows = extractRows(result.payload);
            if (Array.isArray(rows)) renderAlerts(rows);
        }
    }

    document.addEventListener('dabom:navigation-control-state', event => {
        applyControlState(event.detail);
    });

    document.addEventListener('dabom:alerts-cleared', event => {
        const clearedAt = Number(event.detail?.clearedAt);
        alertClearCutoffMs = Number.isFinite(clearedAt) ? clearedAt : Date.now();
    });

    window.DabomDashboardState = {
        fetchLogRows,
        applyControlState,
        configure(nextEndpoints = {}) {
            Object.assign(endpoints, nextEndpoints);
            Object.values(nextEndpoints).forEach(endpoint => unavailableEndpoints.delete(endpoint));
        },
        refresh() {
            pollAlerts();
        }
    };

    pollAlerts();
    window.setInterval(pollAlerts, 2000);
})();
