/* Camera preview rotation is local to this browser view; video transport is unchanged. */
(() => {
    'use strict';

    const wrapper = document.getElementById('video-wrapper');
    const image = document.getElementById('camera-stream');
    const button = document.getElementById('camera-rotate-btn');
    if (!wrapper || !image || !button) return;

    const STORAGE_KEY = 'dabom.cameraRotationDegrees';
    let degrees = 0;
    try {
        const previous = window.localStorage.getItem(STORAGE_KEY);
        const value = Number(previous);
        if (previous !== null && [0, 90, 180, 270].includes(value)) degrees = value;
    } catch (_) {
        // Rotation still works when browser storage is unavailable.
    }

    function fitQuarterTurn() {
        if (degrees % 180 !== 90) return;
        const width = wrapper.clientWidth;
        const height = wrapper.clientHeight;
        if (width > 0 && height > 0) {
            // Swap the rendered IMG box dimensions before rotating it.
            // This prevents the portrait view from overflowing the preview.
            wrapper.style.setProperty('--camera-rotation-width', `${width}px`);
            wrapper.style.setProperty('--camera-rotation-height', `${height}px`);
        }
    }

    function render() {
        wrapper.dataset.cameraRotation = String(degrees);
        button.textContent = `↻ ${degrees}°`;
        const label = `카메라 화면을 시계 방향으로 90도 회전 (현재 ${degrees}도)`;
        button.setAttribute('aria-label', label);
        button.title = label;
        fitQuarterTurn();
    }

    button.addEventListener('click', () => {
        degrees = (degrees + 90) % 360;
        render();
        try {
            window.localStorage.setItem(STORAGE_KEY, String(degrees));
        } catch (_) {
            // Persistence is optional.
        }
    });

    if (typeof ResizeObserver === 'function') {
        new ResizeObserver(fitQuarterTurn).observe(wrapper);
    } else {
        window.addEventListener('resize', fitQuarterTurn);
    }
    document.addEventListener('fullscreenchange', fitQuarterTurn);
    document.addEventListener('webkitfullscreenchange', fitQuarterTurn);
    render();
})();
