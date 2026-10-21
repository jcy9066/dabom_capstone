/* Uiverse volume slider: authoritative Pico value is reported via Navigation state. */
(() => {
    'use strict';

    const root = document.getElementById('speaker-volume-control');
    const slider = root?.querySelector('#speaker-volume-slider');
    const readout = root?.querySelector('#speaker-volume-value');
    if (!root || !slider || !readout) return;

    const clamp = value => Math.max(0, Math.min(100, Number(value) || 0));
    let connected = false;
    let confirmed = null;
    let editing = false;
    let pending = false;
    let queued = null;
    let error = '';
    let csrf = null;

    function paint() {
        const value = clamp(slider.value);
        readout.textContent = value + '%';
        slider.setAttribute('aria-valuetext', value + '%');
        root.dataset.volumeState = !connected ? 'unavailable' : error ? 'error' : pending ? 'pending' : 'ready';
        root.title = !connected
            ? 'Pi/Pico W 연결 또는 음량 상태 확인이 필요합니다.'
            : error || ('Beep 및 경고음 음량: ' + value + '%');
        slider.disabled = !connected;
        slider.setAttribute('aria-busy', String(pending));
    }

    function applyState(payload) {
        const volume = payload?.speaker_volume_percent;
        const ready = payload?.connected === true && Number.isInteger(volume)
            && volume >= 0 && volume <= 100;
        connected = ready;
        if (!ready) {
            confirmed = null;
            if (!editing && !pending) slider.value = 20;
        } else if (!pending && !editing) {
            confirmed = volume;
            slider.value = volume;
            error = '';
        } else if (!pending) {
            confirmed = volume;
        }
        paint();
    }

    async function getCsrf() {
        if (!csrf) {
            csrf = fetch('/api/auth/csrf', { credentials: 'same-origin' })
                .then(async response => {
                    const data = await response.json();
                    if (!response.ok || !data.csrf_token) throw Error('CSRF token unavailable');
                    return data.csrf_token;
                })
                .catch(e => { csrf = null; throw e; });
        }
        return csrf;
    }

    async function writeVolume(value) {
        const token = await getCsrf();
        const response = await fetch('/api/navigation/control/speaker-volume', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
            body: JSON.stringify({ volume_percent: value }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.ok !== true || result.volume_percent !== value) {
            throw Error(result.error || '음량 설정 ACK를 확인하지 못했습니다.');
        }
    }

    async function commit() {
        if (!connected) return;
        queued = clamp(slider.value);
        if (pending) return;
        pending = true;
        paint();
        while (queued !== null && connected) {
            const target = queued;
            queued = null;
            if (target === confirmed) continue;
            try {
                await writeVolume(target);
                confirmed = target;
                error = '';
            } catch (cause) {
                queued = null;
                error = cause.message || '음량 설정에 실패했습니다.';
                confirmed = null; // The Pico may have applied a command whose ACK was lost.
                try {
                    const response = await fetch('/api/navigation/control/state', { credentials: 'same-origin' });
                    const state = await response.json();
                    if (response.ok && state.ok !== false &&
                        Number.isInteger(state.speaker_volume_percent)) {
                        confirmed = state.speaker_volume_percent;
                    }
                } catch (_) { /* Show unavailable rather than claiming an unconfirmed value. */ }
                break;
            }
        }
        pending = false;
        if (!editing) {
            if (confirmed !== null) slider.value = confirmed;
            else connected = false;
        }
        paint();
    }

    function preview() {
        if (!connected) return;
        editing = true;
        error = '';
        paint();
    }

    slider.addEventListener('input', preview);
    slider.addEventListener('change', () => {
        editing = false;
        void commit();
    });
    slider.addEventListener('pointerdown', () => { editing = true; });
    slider.addEventListener('pointerup', () => {
        editing = false;
        void commit();
    });
    slider.addEventListener('pointercancel', () => {
        editing = false;
        void commit();
    });
    slider.addEventListener('keydown', event => {
        if (!connected || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        editing = true;
        slider.value = clamp(Number(slider.value) + (event.key === 'ArrowRight' ? 5 : -5));
        preview();
    });
    slider.addEventListener('keyup', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        editing = false;
        void commit();
    });
    slider.addEventListener('blur', () => {
        if (editing) {
            editing = false;
            void commit();
        }
    });
    document.addEventListener('dabom:navigation-control-state', event => applyState(event.detail));
    if (window.dabomNavigationControlState) applyState(window.dabomNavigationControlState);
    paint();
})();
