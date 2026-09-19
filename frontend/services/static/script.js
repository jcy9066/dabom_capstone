// ===================================================
// 시간 유틸리티
// ===================================================
function getCurrentTime() {
    return new Date().toLocaleTimeString('ko-KR', { hour12: false, hour: '2-digit', minute:'2-digit', second:'2-digit' });
}

function getFormattedDateTime() {
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function todayStr() {
    const n = new Date();
    return `${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')}`;
}
function nowTimeStr() { return new Date().toTimeString().slice(0,8); } // HH:MM:SS
function startOfDayTimeStr() { return '00:00:00'; }

// 1초마다 카메라 상단 시간 업데이트
setInterval(() => {
    document.getElementById('camera-datetime').innerText = getFormattedDateTime();
}, 1000);

// ===================================================
// 서버 상태 폴링
// ===================================================
const TELEMETRY_POLL_INTERVAL_MS = 2000;
let telemetryPollTimer = null;
let telemetryStatusRequest = null;
let telemetryRefreshPending = false;

function isDashboardDocumentVisible() {
    return document.visibilityState !== 'hidden' && document.hidden !== true;
}

function clearTelemetryPollTimer() {
    if (telemetryPollTimer !== null) {
        clearTimeout(telemetryPollTimer);
        telemetryPollTimer = null;
    }
}

function scheduleTelemetryStatusPoll(delay = TELEMETRY_POLL_INTERVAL_MS) {
    clearTelemetryPollTimer();
    if (!isDashboardDocumentVisible()) return;
    telemetryPollTimer = setTimeout(() => {
        telemetryPollTimer = null;
        fetchRobotStatus();
    }, delay);
}

function fetchRobotStatus() {
    if (!isDashboardDocumentVisible()) {
        clearTelemetryPollTimer();
        return Promise.resolve();
    }
    if (telemetryStatusRequest) {
        telemetryRefreshPending = true;
        return telemetryStatusRequest;
    }

    clearTelemetryPollTimer();
    const request = fetch('/get_status')
        .then(response => {
            if (!response.ok) {
                throw new Error(
                    `status HTTP ${response.status}`,
                );
            }

            return response.json();
        })
        .then(data => {
            document.getElementById(
                'sys-cpu-usage',
            ).innerText = data.cpu_usage;

            document.getElementById(
                'sys-cpu-temp',
            ).innerText = data.cpu_temp;

            document.getElementById(
                'sys-ram',
            ).innerText = data.ram_usage;

            document.getElementById(
                'sys-internet',
            ).innerText = data.internet;

            document.dispatchEvent(new CustomEvent(
                'dabom:telemetry-status',
                { detail: { available: true, payload: data } },
            ));
        })
        .catch(error => {
            document.dispatchEvent(new CustomEvent(
                'dabom:telemetry-status',
                { detail: { available: false, error: error.message } },
            ));
            console.error(
                '상태 업데이트 오류:',
                error,
            );
        });

    telemetryStatusRequest = request.finally(() => {
        telemetryStatusRequest = null;
        const delay = telemetryRefreshPending
            ? 0
            : TELEMETRY_POLL_INTERVAL_MS;
        telemetryRefreshPending = false;
        scheduleTelemetryStatusPoll(delay);
    });
    return telemetryStatusRequest;
}

function pauseTelemetryStatusPolling() {
    telemetryRefreshPending = false;
    clearTelemetryPollTimer();
}

function refreshTelemetryStatusPolling() {
    clearTelemetryPollTimer();
    if (!isDashboardDocumentVisible()) return Promise.resolve();
    return fetchRobotStatus();
}

// ===================================================
// 카메라 연결 상태 폴링
// ===================================================
const CAMERA_STATUS_POLL_INTERVAL_MS = 4000;
let cameraStatusPollTimer = null;
let cameraStatusRequest = null;
let cameraStatusRefreshPending = false;

function clearCameraStatusPollTimer() {
    if (cameraStatusPollTimer !== null) {
        clearTimeout(cameraStatusPollTimer);
        cameraStatusPollTimer = null;
    }
}

function scheduleCameraStatusPoll(delay = CAMERA_STATUS_POLL_INTERVAL_MS) {
    clearCameraStatusPollTimer();
    if (!isDashboardDocumentVisible()) return;
    cameraStatusPollTimer = setTimeout(() => {
        cameraStatusPollTimer = null;
        fetchCameraStatus();
    }, delay);
}

function setLiveBadge(isLive) {
    const badge = document.querySelector('.live-badge');
    if (!badge) return;
    badge.classList.toggle('offline', !isLive);
    badge.textContent = isLive ? 'LIVE' : 'OFFLINE';
}

function showCameraLive() {
    const videoWrapper = document.getElementById('video-wrapper');
    const cameraStream = document.getElementById('camera-stream');
    const messageBox = document.getElementById('no-camera-msg');

    if (videoWrapper) videoWrapper.classList.remove('camera-offline');
    if (cameraStream) cameraStream.style.display = '';
    if (messageBox) messageBox.style.display = 'none';
    setLiveBadge(true);
}

function showCameraDisconnected(message, hideStream = false) {
    const videoWrapper = document.getElementById('video-wrapper');
    const cameraStream = document.getElementById('camera-stream');
    const messageBox = document.getElementById('no-camera-msg');

    if (videoWrapper) videoWrapper.classList.add('camera-offline');
    if (cameraStream) cameraStream.style.display = hideStream ? 'none' : '';
    if (messageBox) {
        messageBox.innerText = message || '카메라 연결 상태를 확인할 수 없습니다';
        messageBox.style.display = 'flex';
    }
    setLiveBadge(false);
}

function updateCameraStatus(data) {
    if (data.camera_state === 'live') {
        showCameraLive();
        return;
    }

    showCameraDisconnected(data.message, true);
}

function fetchCameraStatus() {
    if (!isDashboardDocumentVisible()) {
        clearCameraStatusPollTimer();
        return Promise.resolve();
    }
    if (cameraStatusRequest) {
        cameraStatusRefreshPending = true;
        return cameraStatusRequest;
    }

    clearCameraStatusPollTimer();
    const request = fetch('/api/stream_status')
        .then(response => response.json())
        .then(updateCameraStatus)
        .catch(() => showCameraDisconnected('서버와 연결이 끊겼습니다', true));

    cameraStatusRequest = request.finally(() => {
        cameraStatusRequest = null;
        const delay = cameraStatusRefreshPending
            ? 0
            : CAMERA_STATUS_POLL_INTERVAL_MS;
        cameraStatusRefreshPending = false;
        scheduleCameraStatusPoll(delay);
    });
    return cameraStatusRequest;
}

function pauseCameraStatusPolling() {
    cameraStatusRefreshPending = false;
    clearCameraStatusPollTimer();
}

function refreshCameraStatusPolling() {
    clearCameraStatusPollTimer();
    if (!isDashboardDocumentVisible()) return Promise.resolve();
    return fetchCameraStatus();
}

refreshCameraStatusPolling();

// ===================================================
// 카메라 에러 처리
// ===================================================
function handleCameraError() {
    showCameraDisconnected('영상 스트림을 불러올 수 없습니다', true);
}

const cameraStream = document.getElementById('camera-stream');
if (cameraStream) {
    cameraStream.addEventListener('error', handleCameraError);
}

// ===================================================
// LiDAR map 시각화
// ===================================================
const LIDAR_STALE_SECONDS = 3;
const LIDAR_OFFLINE_SECONDS = 8;
const SCAN_PULSE_DURATION_MS = 850;
const NAVIGATION_SNAPSHOT_VISIBLE_MS = 500;
const NAVIGATION_SNAPSHOT_HIDDEN_MS = 2000;
const NAVIGATION_SNAPSHOT_TIMEOUT_MS = 1000;

const lidarState = {
    status: null,
    statusObservedAtMs: 0,
    map: null,
    pose: null,
    scan: null,
    trajectory: { mode: null, point_count: 0, segments: [] },
    mapImage: null,
    mapImageKey: null,
    renderPending: false,
    lastScanKey: null,
    lastScanSeenAtMs: 0,
    lastScanPulseAtMs: 0,
    scanIntervalsMs: [],
};

function fetchOptionalJson(url, options = {}) {
    return fetch(url, options)
        .then(response => {
            if (!response.ok) return null;
            return response.json();
        })
        .catch(() => null);
}

function formatAgeSeconds(age) {
    return typeof age === 'number' && Number.isFinite(age) ? `${age.toFixed(1)}s` : '--';
}

function getScanKey(scan) {
    if (!scan || !Array.isArray(scan.ranges)) return null;
    const first = scan.ranges.length ? scan.ranges[0] : '';
    const last = scan.ranges.length ? scan.ranges[scan.ranges.length - 1] : '';
    return `${scan.timestamp || ''}:${scan.received_at || ''}:${scan.ranges.length}:${first}:${last}`;
}

function noteScanUpdate(scan) {
    const key = getScanKey(scan);
    if (!key || key === lidarState.lastScanKey) return;

    const now = performance.now();
    if (lidarState.lastScanSeenAtMs > 0) {
        const interval = now - lidarState.lastScanSeenAtMs;
        if (interval >= 80 && interval <= 10000) {
            lidarState.scanIntervalsMs.push(interval);
            if (lidarState.scanIntervalsMs.length > 8) lidarState.scanIntervalsMs.shift();
        }
    }

    lidarState.lastScanKey = key;
    lidarState.lastScanSeenAtMs = now;
    lidarState.lastScanPulseAtMs = now;
}

function getScanReceiveHz() {
    if (!lidarState.scanIntervalsMs.length) return null;
    const total = lidarState.scanIntervalsMs.reduce((sum, value) => sum + value, 0);
    const avg = total / lidarState.scanIntervalsMs.length;
    return avg > 0 ? 1000 / avg : null;
}

function getScanAgeSec() {
    if (lidarState.status?.has_scan === false) return null;
    const statusAge = lidarState.status?.last_update_age_sec;
    if (typeof statusAge === 'number' && Number.isFinite(statusAge)) {
        const observedAgoSec = lidarState.statusObservedAtMs > 0
            ? (performance.now() - lidarState.statusObservedAtMs) / 1000
            : 0;
        return statusAge + Math.max(0, observedAgoSec);
    }
    if (lidarState.lastScanSeenAtMs > 0) return (performance.now() - lidarState.lastScanSeenAtMs) / 1000;
    return null;
}

function getLidarLiveState() {
    const backendStatus = lidarState.status?.status;
    const hasScan = Boolean(lidarState.scan) && lidarState.status?.has_scan !== false;
    const age = getScanAgeSec();

    if (!hasScan) {
        return { level: 'offline', label: 'OFFLINE', title: 'LiDAR OFFLINE', detail: 'NO SCAN DATA', age };
    }
    if (backendStatus === 'offline') {
        return { level: 'offline', label: 'OFFLINE', title: 'LiDAR OFFLINE', detail: `LAST ${formatAgeSeconds(age)} AGO`, age };
    }
    if (typeof age === 'number' && age > LIDAR_OFFLINE_SECONDS) {
        return { level: 'offline', label: 'OFFLINE', title: 'LiDAR OFFLINE', detail: `LAST ${formatAgeSeconds(age)} AGO`, age };
    }
    if (backendStatus === 'stale' || (typeof age === 'number' && age > LIDAR_STALE_SECONDS)) {
        return { level: 'stale', label: 'STALE', title: 'LiDAR STALE', detail: `LAST ${formatAgeSeconds(age)} AGO`, age };
    }
    return { level: 'live', label: 'LIVE', title: 'LiDAR LIVE', detail: `AGE ${formatAgeSeconds(age)}`, age };
}

function decodeRleMap(runs, expectedLength) {
    const output = new Int16Array(expectedLength);
    let index = 0;
    if (!Array.isArray(runs)) return output;
    for (const run of runs) {
        if (!Array.isArray(run) || run.length < 2) continue;
        const value = Number(run[0]);
        const count = Number(run[1]);
        for (let i = 0; i < count && index < expectedLength; i += 1) {
            output[index] = value;
            index += 1;
        }
        if (index >= expectedLength) break;
    }
    return output;
}

function buildMapImage(map) {
    const width = Number(map?.width || 0);
    const height = Number(map?.height || 0);
    if (!width || !height || !map?.data) return null;

    const key = `${map.timestamp || ''}:${map.received_at || ''}:${width}x${height}`;
    if (lidarState.mapImage && lidarState.mapImageKey === key) return lidarState.mapImage;

    const cells = decodeRleMap(map.data, width * height);
    const offscreen = document.createElement('canvas');
    offscreen.width = width;
    offscreen.height = height;
    const ctx = offscreen.getContext('2d');
    const image = ctx.createImageData(width, height);

    for (let row = 0; row < height; row += 1) {
        for (let col = 0; col < width; col += 1) {
            const src = row * width + col;
            const dstRow = height - 1 - row;
            const dst = (dstRow * width + col) * 4;
            const value = cells[src];
            let r = 54, g = 65, b = 84;
            if (value === 0) {
                r = 230; g = 238; b = 246;
            } else if (value > 0) {
                const shade = Math.max(26, 92 - Math.round(value * 0.58));
                r = shade; g = shade + 6; b = shade + 16;
            }
            image.data[dst] = r;
            image.data[dst + 1] = g;
            image.data[dst + 2] = b;
            image.data[dst + 3] = 255;
        }
    }

    ctx.putImageData(image, 0, 0);
    lidarState.mapImage = offscreen;
    lidarState.mapImageKey = key;
    return offscreen;
}

function getCanvasLayout(canvas, map) {
    const padding = minimapExpanded ? 24 : 8;
    const width = canvas.width;
    const height = canvas.height;
    const mapWidth = Number(map?.width || 0);
    const mapHeight = Number(map?.height || 0);
    const resolution = Number(map?.resolution || 0.05);
    const worldWidth = mapWidth > 0 ? mapWidth * resolution : 8;
    const worldHeight = mapHeight > 0 ? mapHeight * resolution : 8;
    const scale = Math.min(
        (width - padding * 2) / Math.max(worldWidth, 0.1),
        (height - padding * 2) / Math.max(worldHeight, 0.1)
    );
    const drawWidth = worldWidth * scale;
    const drawHeight = worldHeight * scale;
    return {
        padding,
        scale,
        x: (width - drawWidth) / 2,
        y: (height - drawHeight) / 2,
        width: drawWidth,
        height: drawHeight,
        worldWidth,
        worldHeight,
        originX: Number(map?.origin?.x || 0),
        originY: Number(map?.origin?.y || 0),
        originYaw: Number(map?.origin?.yaw || 0),
    };
}

function worldToCanvas(x, y, layout) {
    const dx = x - layout.originX;
    const dy = y - layout.originY;
    const cosYaw = Math.cos(layout.originYaw);
    const sinYaw = Math.sin(layout.originYaw);
    const localX = cosYaw * dx + sinYaw * dy;
    const localY = -sinYaw * dx + cosYaw * dy;
    return {
        x: layout.x + localX * layout.scale,
        y: layout.y + layout.height - localY * layout.scale,
    };
}

function canvasToWorld(x, y, layout) {
    const localX = (x - layout.x) / layout.scale;
    const localY = (layout.y + layout.height - y) / layout.scale;
    if (localX < 0 || localY < 0 || localX >= layout.worldWidth || localY >= layout.worldHeight) {
        return null;
    }
    const cosYaw = Math.cos(layout.originYaw);
    const sinYaw = Math.sin(layout.originYaw);
    return {
        x: layout.originX + cosYaw * localX - sinYaw * localY,
        y: layout.originY + sinYaw * localX + cosYaw * localY,
    };
}

function drawTrajectory(ctx, trajectory, layout) {
    const segments = Array.isArray(trajectory?.segments) ? trajectory.segments : [];
    if (!segments.length) return;

    ctx.save();
    ctx.strokeStyle = '#166534';
    ctx.lineWidth = minimapExpanded ? 4 : 2.5;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.shadowColor = 'rgba(22, 101, 52, 0.45)';
    ctx.shadowBlur = minimapExpanded ? 5 : 2;

    for (const segment of segments) {
        if (!Array.isArray(segment) || segment.length < 2) continue;
        ctx.beginPath();
        let started = false;
        for (const item of segment) {
            const x = Number(item?.x);
            const y = Number(item?.y);
            if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
            const point = worldToCanvas(x, y, layout);
            if (!started) {
                ctx.moveTo(point.x, point.y);
                started = true;
            } else {
                ctx.lineTo(point.x, point.y);
            }
        }
        if (started) ctx.stroke();
    }
    ctx.restore();
}

function drawRobot(ctx, pose, layout) {
    if (!pose) return;
    const point = worldToCanvas(Number(pose.x || 0), Number(pose.y || 0), layout);
    const yaw = Number(pose.yaw || 0);
    const size = minimapExpanded ? 13 : 8;

    ctx.save();
    ctx.translate(point.x, point.y);
    ctx.rotate(-yaw);
    ctx.beginPath();
    ctx.moveTo(size, 0);
    ctx.lineTo(-size * 0.65, -size * 0.55);
    ctx.lineTo(-size * 0.35, 0);
    ctx.lineTo(-size * 0.65, size * 0.55);
    ctx.closePath();
    ctx.fillStyle = '#e11d48';
    ctx.strokeStyle = 'rgba(255,255,255,0.86)';
    ctx.lineWidth = minimapExpanded ? 2 : 1.2;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
}

function drawScan(ctx, scan, pose, layout) {
    if (!scan || !Array.isArray(scan.ranges) || scan.ranges.length === 0) return;
    const hasPose = Boolean(pose);
    const robotX = hasPose ? Number(pose.x || 0) : layout.originX + layout.worldWidth / 2;
    const robotY = hasPose ? Number(pose.y || 0) : layout.originY + layout.worldHeight / 2;
    const robotYaw = hasPose ? Number(pose.yaw || 0) : 0;
    const angleMin = Number(scan.angle_min || 0);
    const angleIncrement = Number(scan.angle_increment || 0);
    const rangeMin = Number(scan.range_min || 0);
    const rangeMax = Number(scan.range_max || 12);

    ctx.save();
    ctx.fillStyle = '#22d3ee';
    ctx.shadowColor = 'rgba(34, 211, 238, 0.5)';
    ctx.shadowBlur = minimapExpanded ? 5 : 2;
    const radius = minimapExpanded ? 2.2 : 1.4;
    for (let i = 0; i < scan.ranges.length; i += 1) {
        const range = scan.ranges[i];
        if (range === null || range === undefined) continue;
        const distance = Number(range);
        if (!Number.isFinite(distance) || distance < rangeMin || distance > rangeMax) continue;
        const angle = robotYaw + angleMin + angleIncrement * i;
        const x = robotX + Math.cos(angle) * distance;
        const y = robotY + Math.sin(angle) * distance;
        const point = worldToCanvas(x, y, layout);
        if (point.x < layout.x - 2 || point.x > layout.x + layout.width + 2) continue;
        if (point.y < layout.y - 2 || point.y > layout.y + layout.height + 2) continue;
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        ctx.fill();
    }
    ctx.restore();
}

function drawScanPulse(ctx, pose, layout) {
    if (!lidarState.lastScanPulseAtMs) return false;

    const elapsed = performance.now() - lidarState.lastScanPulseAtMs;
    if (elapsed < 0 || elapsed > SCAN_PULSE_DURATION_MS) return false;

    const progress = elapsed / SCAN_PULSE_DURATION_MS;
    const robotX = pose ? Number(pose.x || 0) : layout.originX + layout.worldWidth / 2;
    const robotY = pose ? Number(pose.y || 0) : layout.originY + layout.worldHeight / 2;
    const center = worldToCanvas(robotX, robotY, layout);
    const baseRadius = minimapExpanded ? 12 : 6;
    const spread = minimapExpanded ? 80 : 28;
    const radius = baseRadius + spread * progress;
    const alpha = Math.max(0, 0.78 * (1 - progress));

    ctx.save();
    ctx.beginPath();
    ctx.arc(center.x, center.y, radius, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(34, 211, 238, ${alpha})`;
    ctx.lineWidth = minimapExpanded ? 3 : 1.8;
    ctx.shadowColor = `rgba(34, 211, 238, ${alpha})`;
    ctx.shadowBlur = minimapExpanded ? 16 : 8;
    ctx.stroke();
    ctx.restore();
    return true;
}

function drawEmptyLidar(ctx, canvas) {
    ctx.fillStyle = '#101827';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = 'rgba(34, 211, 238, 0.14)';
    ctx.lineWidth = 1;
    const step = minimapExpanded ? 32 : 16;
    for (let x = 0; x < canvas.width; x += step) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, canvas.height);
        ctx.stroke();
    }
    for (let y = 0; y < canvas.height; y += step) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(canvas.width, y);
        ctx.stroke();
    }
}

function updateLidarLabels() {
    const statusEl = document.getElementById('lidar-map-status');
    const metaEl = document.getElementById('lidar-map-meta');
    const badgeEl = document.getElementById('lidar-live-badge');
    const overlayEl = document.getElementById('lidar-stale-overlay');
    const overlayTitleEl = document.getElementById('lidar-stale-title');
    const overlayDetailEl = document.getElementById('lidar-stale-detail');
    if (!statusEl || !metaEl) return;

    const liveState = getLidarLiveState();
    const hasData = Boolean(lidarState.map || lidarState.scan || lidarState.pose);
    const ageText = formatAgeSeconds(liveState.age);
    const map = lidarState.map;
    const pose = lidarState.pose;
    const scanHz = getScanReceiveHz();
    const scanText = scanHz ? `RX ${scanHz.toFixed(1)}Hz` : 'RX --';

    statusEl.classList.toggle('has-data', hasData);
    statusEl.classList.toggle('stale', liveState.level !== 'live');
    statusEl.textContent = hasData ? liveState.label : 'MAP AREA';

    if (badgeEl) {
        badgeEl.classList.remove('live', 'stale', 'offline');
        badgeEl.classList.add(liveState.level);
        badgeEl.textContent = liveState.level === 'live' ? `LiDAR ${liveState.label}` : liveState.label;
    }

    if (overlayEl) {
        overlayEl.classList.toggle('visible', liveState.level !== 'live');
        overlayEl.classList.remove('stale', 'offline');
        overlayEl.classList.add(liveState.level === 'stale' ? 'stale' : 'offline');
    }
    if (overlayTitleEl) overlayTitleEl.textContent = liveState.title;
    if (overlayDetailEl) overlayDetailEl.textContent = liveState.detail;

    if (map) {
        const x = pose ? Number(pose.x || 0).toFixed(2) : '--';
        const y = pose ? Number(pose.y || 0).toFixed(2) : '--';
        metaEl.textContent = `${liveState.label} / ${scanText} / ${map.width}x${map.height} / AGE ${ageText} / X ${x} Y ${y}`;
    } else if (lidarState.scan) {
        metaEl.textContent = `${liveState.label} / ${scanText} / AGE ${ageText}`;
    } else {
        metaEl.textContent = 'LiDAR OFFLINE';
    }
}

function renderLidarMap() {
    const canvas = document.getElementById('lidar-map-canvas');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const nextWidth = Math.max(1, Math.floor(rect.width * dpr));
    const nextHeight = Math.max(1, Math.floor(rect.height * dpr));
    if (canvas.width !== nextWidth || canvas.height !== nextHeight) {
        canvas.width = nextWidth;
        canvas.height = nextHeight;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const map = lidarState.map;
    const pose = lidarState.pose;
    const scan = lidarState.scan;
    const trajectory = lidarState.trajectory;
    const layout = getCanvasLayout(canvas, map);

    drawEmptyLidar(ctx, canvas);
    if (map) {
        const image = buildMapImage(map);
        if (image) {
            ctx.imageSmoothingEnabled = false;
            ctx.drawImage(image, layout.x, layout.y, layout.width, layout.height);
        }
        ctx.strokeStyle = 'rgba(34, 211, 238, 0.32)';
        ctx.lineWidth = Math.max(1, dpr);
        ctx.strokeRect(layout.x, layout.y, layout.width, layout.height);
    }
    drawTrajectory(ctx, trajectory, layout);
    drawScan(ctx, scan, pose, layout);
    const pulseActive = drawScanPulse(ctx, pose, layout);
    drawRobot(ctx, pose, layout);
    if (window.navigationControlOverlay?.draw) {
        window.navigationControlOverlay.draw(ctx, layout, worldToCanvas);
    }
    updateLidarLabels();
    if (pulseActive) requestLidarRender();
}

function requestLidarRender() {
    if (lidarState.renderPending) return;
    lidarState.renderPending = true;
    requestAnimationFrame(() => {
        lidarState.renderPending = false;
        renderLidarMap();
    });
}

document.addEventListener('dabom:navigation-control-state', event => {
    const mode = String(event?.detail?.navigation_mode || '').toUpperCase();
    const trajectoryMode = String(lidarState.trajectory?.mode || '').toUpperCase();
    if (
        (mode === 'MAPPING' || mode === 'DRIVING')
        && trajectoryMode
        && trajectoryMode !== mode
    ) {
        lidarState.trajectory = { mode, point_count: 0, segments: [] };
        requestLidarRender();
    }
});

function defaultNavigationMapName() {
    const now = new Date();
    const yyyy = now.getFullYear();
    const mm = String(now.getMonth() + 1).padStart(2, '0');
    const dd = String(now.getDate()).padStart(2, '0');
    const hh = String(now.getHours()).padStart(2, '0');
    const mi = String(now.getMinutes()).padStart(2, '0');
    const ss = String(now.getSeconds()).padStart(2, '0');
    return `patrol_area_${yyyy}${mm}${dd}_${hh}${mi}${ss}`;
}

function saveCurrentNavigationMap() {
    const button = document.getElementById('lidarMapSaveBtn');
    if (!lidarState.map) {
        alert('저장할 LiDAR map이 아직 없습니다. mapping 데이터 수신 후 다시 시도하세요.');
        return;
    }
    const mapName = prompt('저장할 map 이름을 입력하세요.', defaultNavigationMapName());
    if (mapName === null) return;
    const trimmedName = mapName.trim();
    if (!trimmedName) {
        alert('map 이름이 비어 있습니다.');
        return;
    }

    if (button) {
        button.disabled = true;
        button.textContent = '...';
    }

    fetch('/api/navigation/maps/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ map_name: trimmedName }),
    })
        .then(response => response.json().then(data => ({ ok: response.ok && data.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                alert(`map 저장 실패: ${data.error || 'unknown error'}`);
                return;
            }
            const savedName = data.map?.map_name || trimmedName;
            alert(`map 저장 완료: ${savedName}`);
        })
        .catch(error => {
            console.error('map 저장 오류:', error);
            alert('서버 통신 오류로 map을 저장하지 못했습니다.');
        })
        .finally(() => {
            if (button) {
                button.disabled = false;
                button.textContent = 'SAVE';
            }
        });
}

let navigationSnapshotTimer = null;
let navigationSnapshotInFlight = false;
let navigationSnapshotRefreshQueued = false;
let navigationMapRevision = null;

function navigationSnapshotDelayMs() {
    return document.hidden
        ? NAVIGATION_SNAPSHOT_HIDDEN_MS
        : NAVIGATION_SNAPSHOT_VISIBLE_MS;
}

function navigationSnapshotUrl() {
    if (navigationMapRevision === null || navigationMapRevision === undefined) {
        return '/api/navigation/snapshot';
    }
    return `/api/navigation/snapshot?map_revision=${encodeURIComponent(navigationMapRevision)}`;
}

function clearNavigationScan() {
    lidarState.scan = null;
    lidarState.lastScanKey = null;
    lidarState.lastScanSeenAtMs = 0;
    lidarState.lastScanPulseAtMs = 0;
    lidarState.scanIntervalsMs = [];
}

function applyNavigationSnapshot(data) {
    if (!data?.ok) return;

    if (data.status) {
        lidarState.status = data.status;
        lidarState.statusObservedAtMs = performance.now();
    }
    lidarState.pose = data.pose_available ? data.pose : null;
    lidarState.trajectory = (
        data.trajectory
        && Array.isArray(data.trajectory.segments)
    )
        ? data.trajectory
        : { mode: null, point_count: 0, segments: [] };
    if (data.scan_available && data.scan) {
        lidarState.scan = data.scan;
        noteScanUpdate(data.scan);
    } else {
        clearNavigationScan();
    }

    navigationMapRevision = data.map_revision ?? null;
    if (!data.map_available) {
        lidarState.map = null;
        lidarState.mapImage = null;
        lidarState.mapImageKey = null;
    } else if (data.map_changed && data.map) {
        lidarState.map = data.map;
    }
}

function scheduleNavigationSnapshot(delayMs = navigationSnapshotDelayMs()) {
    if (navigationSnapshotTimer !== null) window.clearTimeout(navigationSnapshotTimer);
    navigationSnapshotTimer = window.setTimeout(fetchNavigationSnapshot, delayMs);
}

async function fetchNavigationSnapshot() {
    if (navigationSnapshotInFlight) return;
    navigationSnapshotTimer = null;
    navigationSnapshotInFlight = true;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(
        () => controller.abort(),
        NAVIGATION_SNAPSHOT_TIMEOUT_MS,
    );
    try {
        const data = await fetchOptionalJson(
            navigationSnapshotUrl(),
            { signal: controller.signal },
        );
        applyNavigationSnapshot(data);
        requestLidarRender();
    } finally {
        window.clearTimeout(timeoutId);
        navigationSnapshotInFlight = false;
        const refreshImmediately = navigationSnapshotRefreshQueued;
        navigationSnapshotRefreshQueued = false;
        scheduleNavigationSnapshot(refreshImmediately ? 0 : navigationSnapshotDelayMs());
    }
}

document.addEventListener('visibilitychange', () => {
    if (navigationSnapshotTimer !== null) window.clearTimeout(navigationSnapshotTimer);
    navigationSnapshotTimer = null;
    if (navigationSnapshotInFlight) {
        navigationSnapshotRefreshQueued = !document.hidden;
    } else {
        scheduleNavigationSnapshot(document.hidden ? navigationSnapshotDelayMs() : 0);
    }
});
fetchNavigationSnapshot();
window.addEventListener('resize', requestLidarRender);
requestLidarRender();

// ===================================================
// 전체화면
// ===================================================
function toggleFullscreen(elementId) {
    const elem = document.getElementById(elementId);
    if (!document.fullscreenElement) {
        if (elem.requestFullscreen) elem.requestFullscreen();
        else if (elem.webkitRequestFullscreen) elem.webkitRequestFullscreen();
        else if (elem.msRequestFullscreen) elem.msRequestFullscreen();
    } else {
        if (document.exitFullscreen) document.exitFullscreen();
    }
}

// ===================================================
// 미니맵 확대 토글 (카메라 화면 크기만큼 확장)
// ===================================================
let minimapExpanded = false;
function toggleMinimapExpand() {
    const minimap = document.getElementById('minimap-overlay');
    const videoWrapper = document.getElementById('video-wrapper');
    const btn = document.getElementById('minimapExpandBtn');

    if (!minimapExpanded) {
        // 카메라 화면 크기 가져오기
        const wRect = videoWrapper.getBoundingClientRect();
        minimap.style.width = wRect.width + 'px';
        minimap.style.height = wRect.height + 'px';
        minimap.style.bottom = '0';
        minimap.style.right = '0';
        minimap.style.borderRadius = '6px';
        minimap.style.zIndex = '50';
        btn.textContent = '⊡';
        btn.title = '미니맵 축소';
        minimap.classList.add('expanded');
        minimapExpanded = true;
        requestLidarRender();
    } else {
        minimap.style.width = '';
        minimap.style.height = '';
        minimap.style.bottom = '';
        minimap.style.right = '';
        minimap.style.zIndex = '';
        btn.textContent = '⛶';
        btn.title = '미니맵 확대';
        minimap.classList.remove('expanded');
        minimapExpanded = false;
        requestLidarRender();
    }
}

window.navigationMapView = {
    canvasToWorld(event) {
        const canvas = document.getElementById('lidar-map-canvas');
        if (!canvas || !lidarState.map) return null;
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / Math.max(rect.width, 1);
        const scaleY = canvas.height / Math.max(rect.height, 1);
        const layout = getCanvasLayout(canvas, lidarState.map);
        return canvasToWorld(
            (event.clientX - rect.left) * scaleX,
            (event.clientY - rect.top) * scaleY,
            layout,
        );
    },
    snapshot() {
        return {
            map: lidarState.map,
            pose: lidarState.pose,
            expanded: minimapExpanded,
        };
    },
    requestRender: requestLidarRender,
};

// ===================================================
// 알림 지우기
// ===================================================
function clearAlerts() {
    if (confirm('현재 표시된 알림을 지우시겠습니까?')) {
        const clearedAt = Date.now();
        document.dispatchEvent(new CustomEvent(
            'dabom:alerts-cleared',
            { detail: { clearedAt } },
        ));
        document.getElementById('alertBox').innerHTML = `
            <div class="alert-entry alert-info">
                <span class="alert-time">${getCurrentTime()}</span>
                <span class="alert-message">현재 표시 알림을 지웠습니다. 새 알림 대기 중...</span>
            </div>`;
    }
}

// ===================================================
// 알림은 실제 서버/AI 감지 데이터만 표시
// (더미 자동 알림 생성 제거됨)
// ===================================================
// 실제 위험 감지 시 서버에서 push 또는 polling으로 받아올 것
// 예시: 서버에서 /get_alerts 엔드포인트 구현 후 아래처럼 연결
// function fetchAlerts() { ... }
// setInterval(fetchAlerts, 3000);

// ===================================================
// 텔레그램 신고
// ===================================================
let reportPending = false;

async function reportDanger(isAuto = false) {
    let confirmReport = true;
    if (!isAuto) confirmReport = confirm("신고? - Telegram");
    if (!confirmReport || reportPending) return false;
    const button = document.querySelector('.action-report');
    reportPending = true;
    if (button) button.disabled = true;
    try {
        const csrfResponse = await fetch('/api/auth/csrf', { credentials: 'same-origin' });
        const csrf = await csrfResponse.json().catch(() => ({}));
        if (!csrfResponse.ok || !csrf.csrf_token) throw new Error('보안 토큰을 확인하지 못했습니다.');
        const response = await fetch('/send_telegram', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrf.csrf_token,
            },
            body: JSON.stringify({}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.status !== 'success') {
            throw new Error(data.detail || data.error || '알림 전송에 실패했습니다.');
        }
        if (!isAuto) alert('긴급 알림이 전송되었습니다.');
        return true;
    } catch (error) {
        console.error('신고 요청 실패:', error);
        if (!isAuto) alert(error.message || '서버 통신 오류로 알림을 보내지 못했습니다.');
        return false;
    } finally {
        reportPending = false;
        if (button) button.disabled = false;
    }
}

function warnTrespasser() {
    if (window.navigationControl?.warning) {
        window.navigationControl.warning();
        return;
    }
    sendRobotCommand({ type: 'speak', text: '경고합니다. 즉시 물러나십시오.' })
        .then(ok => {
            if (ok) alert("⚠️ 경고 방송 명령을 전송했습니다.");
            else alert("경고 방송 명령 전송에 실패했습니다.");
        });
}

// ===================================================
// 수동/자동 순찰 모드 및 실제 주행 제어
// ===================================================
let currentPatrolMode = null;
let modeChangePending = false;
let currentRobotConnected = null;


function applyServerPatrolMode(mode) {
    const normalized = String(mode || '').trim().toLowerCase();
    currentPatrolMode = (
        currentRobotConnected !== false
        && (normalized === 'auto' || normalized === 'manual')
    ) ? normalized : null;
    setPatrolModeUi(currentPatrolMode);
}


function setDashboardRobotConnection(connected) {
    currentRobotConnected = typeof connected === 'boolean' ? connected : null;
    if (connected === false) {
        stopAllLocalInputs(false);
        applyServerPatrolMode(null);
    }
}


window.applyServerPatrolMode = applyServerPatrolMode;
window.setDashboardRobotConnection = setDashboardRobotConnection;

const ROBOT_ID = 'pi-01';
const MANUAL_SPEED = 0.35;
const COMMAND_REPEAT_MS = 120;

const DRIVE_KEYS = [
    'ArrowUp',
    'ArrowDown',
    'ArrowLeft',
    'ArrowRight',
];

let pointerMoveInterval = null;
let activePointerButton = null;

const pressedKeys = new Set();
let keyMoveInterval = null;
let robotCommandCsrfPromise = null;
let lastRobotCommandErrorAt = 0;


function robotCommandErrorMessage(data, status) {
    const code = String(data?.error_code || '').toUpperCase();
    if (['PI_OFFLINE', 'COMMAND_DELIVERY_FAILED', 'PI_COMMAND_FAILED'].includes(code) || status === 409) {
        return 'Pi가 연결되지 않아 명령을 전달하지 못했습니다.';
    }
    return data?.detail || data?.error || `로봇 명령 요청에 실패했습니다. (HTTP ${status})`;
}


function showRobotCommandFailure(data, status) {
    const feedback = document.getElementById('navigation-control-feedback');
    const message = robotCommandErrorMessage(data, status);
    if (feedback) {
        feedback.textContent = message;
        feedback.classList.add('error');
    }
    const now = Date.now();
    if (now - lastRobotCommandErrorAt >= 2000) {
        console.warn(message, data);
        lastRobotCommandErrorAt = now;
    }
}


function directionToCommand(direction) {
    const map = {
        '↑': 'forward',
        '↓': 'backward',
        '←': 'rotate_left',
        '→': 'rotate_right',
        '↖': 'forward_left',
        '↗': 'forward_right',
        '↙': 'backward_left',
        '↘': 'backward_right',
    };

    return map[direction] || direction;
}


function robotCommandCsrfToken() {
    if (!robotCommandCsrfPromise) {
        robotCommandCsrfPromise = fetch(
            '/api/auth/csrf',
            { credentials: 'same-origin' },
        )
            .then(async response => {
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.csrf_token) {
                    throw new Error('robot command CSRF token unavailable');
                }
                return data.csrf_token;
            })
            .catch(error => {
                robotCommandCsrfPromise = null;
                throw error;
            });
    }
    return robotCommandCsrfPromise;
}


async function sendRobotCommand(
    payload,
    keepalive = false,
) {
    try {
        const csrfToken = await robotCommandCsrfToken();
        const response = await fetch(
            `/api/robots/${ROBOT_ID}/command`,
            {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken,
                },
                body: JSON.stringify(payload),
                keepalive,
            },
        );
        const data = await response.json().catch(() => ({}));
        const ok = response.ok && data.ok;

        if (!ok) {
            showRobotCommandFailure(data, response.status);
        }

        return ok;
    } catch (error) {
        console.error('로봇 명령 통신 오류:', error);
        return false;
    }
}


function setPatrolModeUi(mode) {
    const switchUi = document.getElementById(
        'mode-switch-ui',
    );

    const dPadArea = document.getElementById(
        'd-pad-area',
    );

    const labelAuto = document.getElementById(
        'label-auto',
    );

    const labelManual = document.getElementById(
        'label-manual',
    );

    switchUi.classList.toggle('unknown', mode !== 'auto' && mode !== 'manual');

    if (mode === 'auto') {
        switchUi.classList.remove('manual');
        dPadArea.classList.add('disabled');

        labelAuto.classList.add('active');
        labelAuto.classList.remove('inactive');

        labelManual.classList.add('inactive');
        labelManual.classList.remove('active');

    } else if (mode === 'manual') {
        switchUi.classList.add('manual');
        dPadArea.classList.remove('disabled');

        labelAuto.classList.add('inactive');
        labelAuto.classList.remove('active');

        labelManual.classList.add('active');
        labelManual.classList.remove('inactive');
    } else {
        switchUi.classList.remove('manual');
        dPadArea.classList.add('disabled');

        labelAuto.classList.add('inactive');
        labelAuto.classList.remove('active');

        labelManual.classList.add('inactive');
        labelManual.classList.remove('active');
    }
}


async function togglePatrolMode() {
    if (modeChangePending) {
        return;
    }

    if (
        currentPatrolMode !== 'auto'
        && currentPatrolMode !== 'manual'
    ) {
        alert(
            '로봇의 현재 모드를 아직 '
            + '확인하지 못했습니다.',
        );

        return;
    }

    const targetMode = (
        currentPatrolMode === 'auto'
            ? 'manual'
            : 'auto'
    );

    const modeName = (
        targetMode === 'auto'
            ? '자동'
            : '수동'
    );

    if (
        !confirm(
            `${modeName} 순찰 모드로 변경하시겠습니까?`,
        )
    ) {
        return;
    }

    const requestDriveMode = window.navigationControl?.requestDriveMode;
    if (!requestDriveMode) {
        alert('Navigation Control 상태를 확인하지 못했습니다.');
        return;
    }

    stopAllLocalInputs(false);
    modeChangePending = true;

    try {
        const ok = await requestDriveMode(targetMode.toUpperCase());

        if (!ok) {
            alert(
                '로봇이 연결되지 않아 '
                + '모드를 변경하지 못했습니다.',
            );

            return;
        }

    } finally {
        modeChangePending = false;
    }
}


window.moveRobot = function moveRobot(direction) {
    if (currentPatrolMode !== 'manual') {
        console.warn(
            '수동 순찰 모드에서만 '
            + '로봇을 조작할 수 있습니다.',
        );

        return Promise.resolve(false);
    }

    return sendRobotCommand({
        type: 'move',
        direction: directionToCommand(direction),
        speed: MANUAL_SPEED,
    });
};


function stopRobot(
    reason = 'manual_stop',
    keepalive = false,
) {
    return sendRobotCommand(
        {
            type: 'stop',
            reason,
        },
        keepalive,
    );
}


function emergencyStopRobot(
    reason = 'dashboard_emergency_stop',
    keepalive = false,
) {
    stopAllLocalInputs(false);

    if (window.navigationControl?.emergencyStop) {
        return window.navigationControl.emergencyStop(reason, keepalive);
    }

    return sendRobotCommand(
        {
            type: 'emergency_stop',
            reason,
        },
        keepalive,
    );
}


function startButtonMove(
    direction,
    event,
) {
    if (
        event.isPrimary === false
        || event.button !== 0
    ) {
        return;
    }

    if (currentPatrolMode !== 'manual') {
        return;
    }

    event.preventDefault();

    stopPointerMove(false);

    activePointerButton = event.currentTarget;
    activePointerButton.classList.add(
        'active-key',
    );

    if (
        activePointerButton.setPointerCapture
        && event.pointerId !== undefined
    ) {
        try {
            activePointerButton.setPointerCapture(
                event.pointerId,
            );
        } catch (_) {
            // Pointer capture 미지원 브라우저
        }
    }

    moveRobot(direction);

    pointerMoveInterval = setInterval(
        () => moveRobot(direction),
        COMMAND_REPEAT_MS,
    );
}


document.querySelectorAll(
    '.d-pad .d-btn[data-drive-direction]',
).forEach(button => {
    button.addEventListener(
        'pointerdown',
        event => startButtonMove(
            button.dataset.driveDirection,
            event,
        ),
    );

    button.addEventListener(
        'lostpointercapture',
        () => stopPointerMove(
            true,
            'button_release',
        ),
    );
});


function stopPointerMove(
    sendStop = true,
    reason = 'button_release',
) {
    const wasActive = (
        pointerMoveInterval !== null
        || activePointerButton !== null
    );

    if (pointerMoveInterval !== null) {
        clearInterval(pointerMoveInterval);
        pointerMoveInterval = null;
    }

    if (activePointerButton) {
        activePointerButton.classList.remove(
            'active-key',
        );

        activePointerButton = null;
    }

    if (
        sendStop
        && wasActive
        && currentPatrolMode === 'manual'
    ) {
        stopRobot(reason);
    }
}


function normalizeDriveKey(key) {
    return DRIVE_KEYS.includes(key) ? key : '';
}


function getDirectionFromKeys() {
    const up = pressedKeys.has('ArrowUp');
    const down = pressedKeys.has('ArrowDown');
    const left = pressedKeys.has('ArrowLeft');
    const right = pressedKeys.has('ArrowRight');

    if (up && left) {
        return { direction: '↖' };
    }

    if (up && right) {
        return { direction: '↗' };
    }

    if (down && left) {
        return { direction: '↙' };
    }

    if (down && right) {
        return { direction: '↘' };
    }

    if (up) {
        return { direction: '↑' };
    }

    if (down) {
        return { direction: '↓' };
    }

    if (left) {
        return { direction: '←' };
    }

    if (right) {
        return { direction: '→' };
    }

    return null;
}


function highlightKeyboardButton(direction) {
    const buttons = document.querySelectorAll(
        '.d-pad .d-btn[data-drive-direction]',
    );

    buttons.forEach(button => {
        button.classList.toggle(
            'active-key',
            button.dataset.driveDirection
                === direction,
        );
    });
}


function isKeyboardDrivingBlocked(target = document.activeElement) {
    const element = target instanceof Element ? target : null;
    if (
        element?.matches('input, textarea, select')
        || element?.isContentEditable
        || element?.closest('[contenteditable]:not([contenteditable="false"])')
    ) return true;
    const modal = document.getElementById('commonModal');
    if (
        modal
        && (
            modal.dataset.modalView
            || modal.style.display === 'flex'
            || modal.classList.contains('open')
        )
    ) return true;
    return document.getElementById('sidebar')?.classList.contains('open') === true;
}


function sendCurrentKeyDirection() {
    if (currentPatrolMode !== 'manual') {
        return;
    }

    if (isKeyboardDrivingBlocked()) {
        stopKeyMove(true, 'input_blocked');
        return;
    }

    const result = getDirectionFromKeys();

    if (!result) {
        return;
    }

    moveRobot(result.direction);

    highlightKeyboardButton(
        result.direction,
    );
}


function startKeyMove() {
    sendCurrentKeyDirection();

    if (keyMoveInterval !== null) {
        return;
    }

    keyMoveInterval = setInterval(
        sendCurrentKeyDirection,
        COMMAND_REPEAT_MS,
    );
}


function stopKeyMove(
    sendStop = true,
    reason = 'key_release',
) {
    if (keyMoveInterval !== null) {
        clearInterval(keyMoveInterval);
        keyMoveInterval = null;
    }

    pressedKeys.clear();
    highlightKeyboardButton(null);

    if (
        sendStop
        && currentPatrolMode === 'manual'
    ) {
        stopRobot(reason);
    }
}


function stopAllLocalInputs(
    sendStop = true,
    reason = 'input_cancelled',
) {
    stopPointerMove(false);
    stopKeyMove(false);

    if (
        sendStop
        && currentPatrolMode === 'manual'
    ) {
        stopRobot(reason);
    }
}


document.addEventListener(
    'keydown',
    event => {
        const key = normalizeDriveKey(
            event.key,
        );

        if (!DRIVE_KEYS.includes(key)) {
            return;
        }

        if (isKeyboardDrivingBlocked(event.target)) {
            return;
        }

        if (currentPatrolMode !== 'manual') {
            return;
        }

        event.preventDefault();

        if (pressedKeys.has(key)) {
            return;
        }

        pressedKeys.add(key);
        startKeyMove();
    },
);


document.addEventListener(
    'keyup',
    event => {
        const key = normalizeDriveKey(
            event.key,
        );

        if (!DRIVE_KEYS.includes(key)) {
            return;
        }

        if (!pressedKeys.has(key)) {
            return;
        }

        pressedKeys.delete(key);

        if (!getDirectionFromKeys()) {
            stopKeyMove(
                true,
                'key_release',
            );
        } else {
            sendCurrentKeyDirection();
        }
    },
);


document.addEventListener(
    'pointerup',
    () => stopPointerMove(
        true,
        'button_release',
    ),
);


document.addEventListener(
    'pointercancel',
    () => stopPointerMove(
        true,
        'pointer_cancel',
    ),
);


window.addEventListener(
    'blur',
    () => {
        if (currentPatrolMode === 'manual') {
            emergencyStopRobot(
                'window_blur',
            );
        }
    },
);


document.addEventListener(
    'visibilitychange',
    () => {
        if (!isDashboardDocumentVisible()) {
            pauseTelemetryStatusPolling();
            pauseCameraStatusPolling();
        } else {
            refreshTelemetryStatusPolling();
            refreshCameraStatusPolling();
        }

        if (
            document.hidden
            && currentPatrolMode === 'manual'
        ) {
            emergencyStopRobot(
                'page_hidden',
                true,
            );
        }
    },
);

// ===================================================
// 사이드바 메뉴 (요구사항 7)
// ===================================================
function openSidebar() {
    document.getElementById('sidebar').classList.add('open');
    document.getElementById('sidebarOverlay').classList.add('open');
    document.dispatchEvent(new CustomEvent(
        'dabom:sidebar-visibility',
        { detail: { open: true } },
    ));
}
function closeSidebar() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebarOverlay').classList.remove('open');
    document.dispatchEvent(new CustomEvent(
        'dabom:sidebar-visibility',
        { detail: { open: false } },
    ));
}

// ===================================================
// 다크 모드 (요구사항 7)
// ===================================================
let darkMode = false;
function toggleDarkMode() {
    darkMode = !darkMode;
    document.body.classList.toggle('dark-mode', darkMode);
    document.getElementById('darkToggle').classList.toggle('active', darkMode);
    localStorage.setItem('darkMode', darkMode ? '1' : '0');
}

// 저장된 다크모드 설정 복원
window.addEventListener('DOMContentLoaded', () => {
    initializeDashboardComponentFoundation();

    const saved = localStorage.getItem('darkMode');
    if (saved === '1') toggleDarkMode();

    // Pi가 보고한 실제 모드로 초기 UI 동기화
    refreshTelemetryStatusPolling();
});

function initializeDashboardComponentFoundation() {
    const components = window.DabomDashboardComponents;
    if (!components) return;

    components.mounts = components.mounts || {};
    components.mounts.recordsToolbar = document.getElementById('records-toolbar-mount');
    components.mounts.dashboardModeControls = document.getElementById('dashboard-mode-controls-mount');
    components.mounts.dpadCenterAction = document.getElementById('dpad-center-action-mount');
    components.mounts.currentSituation = document.getElementById('current-situation-mount');

    components.modal?.mount({
        root: '#commonModal',
        title: '#modalTitle',
        body: '#modalBody',
    });
    components.records?.mount(components.mounts.recordsToolbar);
    components.controls?.mountDriveMode(components.mounts.dashboardModeControls);
    components.controls?.mountNavigationMode(document.getElementById('navigation-control-panel'));
    components.navigationMaps?.mount({
        trigger: document.getElementById('lidarMapSelectBtn'),
    });
    components.gallery?.mountImageDetail();
    components.currentSituation?.mount(components.mounts.currentSituation);
}

// ===================================================
// Session logout
// ===================================================
async function logout() {
    const button = document.getElementById('logoutBtn');
    if (button) button.disabled = true;
    try {
        const csrfResponse = await fetch('/api/auth/csrf', { credentials: 'same-origin' });
        const csrfData = await csrfResponse.json();
        if (!csrfResponse.ok || !csrfData.csrf_token) {
            throw new Error('\ubcf4\uc548 \ud1a0\ud070\uc744 \uc900\ube44\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.');
        }
        const response = await fetch('/api/auth/logout', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRF-Token': csrfData.csrf_token },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) {
            throw new Error(data.detail || '\ub85c\uadf8\uc544\uc6c3\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4.');
        }
        window.location.assign(data.redirect_url || '/login');
    } catch (error) {
        console.error('logout failed:', error);
        alert(error.message || '\ub85c\uadf8\uc544\uc6c3\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4.');
        if (button) button.disabled = false;
    }
}
