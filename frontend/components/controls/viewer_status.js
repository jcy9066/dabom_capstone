(function initializeViewerStatus(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};
    const DEFAULT_DURATION_MS = 3000;

    function formatNumber(value, digits) {
        const number = Number(value);
        return Number.isFinite(number) ? number.toFixed(digits) : '--';
    }

    function mount(options = {}) {
        const messageElement = options.messageElement
            || global.document.getElementById('lidar-control-message');
        const gpsElement = options.gpsElement
            || global.document.getElementById('lidar-gps-meta');
        const durationMs = Number.isFinite(Number(options.durationMs))
            ? Math.max(0, Number(options.durationMs))
            : DEFAULT_DURATION_MS;

        if (!messageElement || !gpsElement) return null;
        if (messageElement._dabomViewerStatusController) {
            return messageElement._dabomViewerStatusController;
        }

        let hideTimer = null;
        let lastMessage = '';

        function clearTimer() {
            if (hideTimer !== null) {
                global.clearTimeout(hideTimer);
                hideTimer = null;
            }
        }

        function hideMessage() {
            clearTimer();
            messageElement.hidden = true;
            messageElement.textContent = '';
            messageElement.classList.remove('error');
            lastMessage = '';
        }

        function show(message, options = {}) {
            const text = String(message || '').trim();
            if (!text) {
                hideMessage();
                return;
            }

            const error = Boolean(options.error);
            const nextDuration = Number.isFinite(Number(options.durationMs))
                ? Math.max(0, Number(options.durationMs))
                : durationMs;

            if (text !== lastMessage || messageElement.hidden) {
                messageElement.textContent = text;
                lastMessage = text;
            }
            messageElement.classList.toggle('error', error);
            messageElement.hidden = false;

            clearTimer();
            if (nextDuration > 0) {
                hideTimer = global.setTimeout(hideMessage, nextDuration);
            }
        }

        function updateGps(payload = null, available = true) {
            if (!available || !payload) {
                gpsElement.textContent = 'GPS LAT -- / LNG -- / ALT --';
                return;
            }

            const lat = formatNumber(payload.gps_lat, 6);
            const lng = formatNumber(payload.gps_lng, 6);
            const alt = formatNumber(payload.gps_alt, 1);
            gpsElement.textContent = `GPS LAT ${lat} / LNG ${lng} / ALT ${alt}`;
        }

        const telemetryHandler = event => {
            const detail = event?.detail || {};
            updateGps(detail.payload || null, detail.available === true);
        };
        global.document.addEventListener('dabom:telemetry-status', telemetryHandler);

        const controller = {
            show,
            hide: hideMessage,
            updateGps,
            durationMs,
        };
        messageElement._dabomViewerStatusController = controller;
        controls.viewerStatus = controller;
        return controller;
    }

    controls.mountViewerStatus = mount;
})(window);
