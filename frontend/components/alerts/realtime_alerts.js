(function (global) {
    'use strict';

    const POLL_MS = 750;
    let timer = null;
    let lastDangerSignature = '';
    let lastPowerSignature = '';
    let clearedAt = 0;

    function alertBox() { return document.getElementById('alertBox'); }

    function timestamp() {
        return new Date().toLocaleTimeString('ko-KR', { hour12: false });
    }

    function appendAlert(message, danger = false) {
        const box = alertBox();
        if (!box) return;
        const entry = document.createElement('div');
        entry.className = `alert-entry ${danger ? 'alert-danger' : 'alert-info'}`;
        const time = document.createElement('span');
        time.className = 'alert-time';
        time.textContent = timestamp();
        const text = document.createElement('span');
        text.className = 'alert-message';
        text.textContent = message;
        entry.append(time, text);
        box.prepend(entry);
        while (box.children.length > 20) box.lastElementChild?.remove();
    }

    async function poll() {
        if (document.hidden) {
            timer = global.setTimeout(poll, 2500);
            return;
        }
        try {
            const [resultResponse, statusResponse] = await Promise.all([
                fetch('/api/latest_result', { credentials: 'same-origin' }),
                fetch('/get_status', { credentials: 'same-origin' }),
            ]);
            const result = resultResponse.ok ? await resultResponse.json() : {};
            const status = statusResponse.ok ? await statusResponse.json() : {};

            const dangerous = Array.isArray(result.detections)
                ? result.detections.filter(item => item?.danger === true)
                : [];
            const signature = dangerous
                .map(item => `${item.id}:${item.label}:${Number(item.score || 0).toFixed(3)}`)
                .sort()
                .join('|');

            if (result.danger === true && signature && signature !== lastDangerSignature) {
                const labels = [...new Set(dangerous.map(item => item.label || 'DANGER'))].join(', ');
                appendAlert(`AI 위험 감지: ${labels}`, true);
            }
            lastDangerSignature = signature;

            if (status.battery_low === true) {
                const lowSignature = `battery:${status.battery_percent ?? 'unknown'}:${status.power_undervoltage === true}`;
                if (lowSignature !== lastPowerSignature) {
                    appendAlert(
                        status.power_undervoltage === true
                            ? '전원 저전압 감지: 안전 정지가 요청되었습니다.'
                            : `배터리 부족 감지: ${status.battery_percent ?? '--'}%`,
                        true,
                    );
                    lastPowerSignature = lowSignature;
                }
            } else {
                lastPowerSignature = '';
            }
        } catch (_) {
            // Existing dashboard status indicators handle connectivity failures.
        } finally {
            timer = global.setTimeout(poll, POLL_MS);
        }
    }

    document.addEventListener('dabom:alerts-cleared', event => {
        clearedAt = Number(event.detail?.clearedAt || Date.now());
        lastDangerSignature = `cleared:${clearedAt}`;
        lastPowerSignature = `cleared:${clearedAt}`;
    });

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', poll, { once: true });
    else poll();
})(window);
