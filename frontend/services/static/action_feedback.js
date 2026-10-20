/* Visual BEEP/warning ripple and Telegram report status. No control/API side effects. */
(function dashboardActionFeedback(global) {
    'use strict';

    const controls = global.document?.querySelector('.dashboard-action-controls');
    if (!controls) return;

    const report = controls.querySelector('.action-report');
    const reportLabel = report?.querySelector('.action-report-label');
    const captions = Object.freeze({
        idle: '신고', sending: '전송 중', success: '신고 완료', error: '전송 실패'
    });
    let resetTimer = null;
    const pressTimers = new WeakMap();
    const pendingPointerClicks = new WeakSet();

    function setReportState(state, message = '') {
        if (!report || !reportLabel) return;
        const next = Object.prototype.hasOwnProperty.call(captions, state) ? state : 'idle';
        if (resetTimer != null) global.clearTimeout(resetTimer);
        resetTimer = null;
        report.dataset.reportState = next;
        report.setAttribute('aria-busy', String(next === 'sending'));
        reportLabel.textContent = captions[next];
        if (next === 'error' && message) report.title = message;
        else if (next === 'success') report.title = 'Telegram 신고가 전송되었습니다.';
        else report.removeAttribute('title');

        if (next === 'success' || next === 'error') {
            resetTimer = global.setTimeout(() => setReportState('idle'), 1800);
        }
    }

    function actionButton(event) {
        const button = event.target?.closest?.('.action-beep, .action-warning');
        if (!button || !controls.contains(button) || button.disabled) return null;
        return button;
    }

    function animatePress(event, button) {
        // Even with reduced-motion enabled, expose a visible static pressed
        // state; CSS suppresses the animated motion in that preference.

        // CSS :active vanishes on release. A full keyframe cycle is visible
        // for both mouse/touch presses and keyboard-generated clicks.
        const previous = pressTimers.get(button);
        if (previous != null) global.clearTimeout(previous);
        button.classList.remove('uiverse-action-clicked', 'action-ripple-running');
        void button.offsetWidth; // restart consecutive presses
        const bounds = button.getBoundingClientRect();
        const pointer = event.detail !== 0 && Number.isFinite(event.clientX) && Number.isFinite(event.clientY);
        button.style.setProperty('--action-ripple-x', (pointer ? event.clientX - bounds.left : bounds.width / 2) + 'px');
        button.style.setProperty('--action-ripple-y', (pointer ? event.clientY - bounds.top : bounds.height / 2) + 'px');
        button.classList.add('uiverse-action-clicked', 'action-ripple-running');
        pressTimers.set(button, global.setTimeout(() => {
            button.classList.remove('uiverse-action-clicked');
            pressTimers.delete(button);
        }, 680));
    }

    // Start at pointerdown to show a press even when the control handler
    // synchronously disables its button on click.
    controls.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        const button = actionButton(event);
        if (!button) return;
        pendingPointerClicks.add(button);
        animatePress(event, button);
    }, { capture: true });

    // Click also covers keyboard activation and programmatic accessible clicks.
    controls.addEventListener('click', event => {
        const button = actionButton(event);
        if (!button) return;
        if (pendingPointerClicks.has(button)) {
            pendingPointerClicks.delete(button);
            return;
        }
        animatePress(event, button);
    }, { capture: true });

    controls.addEventListener('pointercancel', event => {
        const button = event.target?.closest?.('.action-beep, .action-warning');
        if (button) pendingPointerClicks.delete(button);
    }, { capture: true });

    // The feedback animations have no effect on the existing command callbacks.
    controls.addEventListener('animationend', event => {
        if (event.animationName === 'dashboard-action-ripple') {
            event.target.classList.remove('action-ripple-running');
        }
    });

    global.DabomActionFeedback = { setReportState };
    setReportState('idle');
})(window);
