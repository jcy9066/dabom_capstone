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

    function ripple(event) {
        const button = event.target?.closest?.('.action-beep, .action-warning');
        if (!button || !controls.contains(button) || button.disabled) return;
        if (global.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) return;

        const bounds = button.getBoundingClientRect();
        const pointer = event.detail !== 0 && Number.isFinite(event.clientX) && Number.isFinite(event.clientY);
        const x = pointer ? event.clientX - bounds.left : bounds.width / 2;
        const y = pointer ? event.clientY - bounds.top : bounds.height / 2;
        button.style.setProperty('--action-ripple-x', x + 'px');
        button.style.setProperty('--action-ripple-y', y + 'px');
        button.classList.remove('action-ripple-running');
        void button.offsetWidth;
        button.classList.add('action-ripple-running');
    }

    // Capture precedes action handlers that can synchronously disable a pending button.
    controls.addEventListener('click', ripple, { capture: true });
    controls.addEventListener('animationend', event => {
        if (event.animationName === 'dashboard-action-ripple') {
            event.target.classList.remove('action-ripple-running');
        }
    });

    global.DabomActionFeedback = { setReportState };
    setReportState('idle');
})(window);
