/* Elastic overscroll for genuine vertical scroll boundaries only.
 * Never wrap the DOM or change the native scrollTop. */
(function initializeElasticScroll(global) {
    'use strict';
    const doc = global.document;
    if (!doc?.addEventListener || typeof global.getComputedStyle !== 'function') return;
    if (global.CSS?.supports && !global.CSS.supports('translate', '0 1px')) return;

    const MAX_PX = 24;
    const STIFFNESS = 48;
    const RETURN_MS = 250;
    const IDLE_MS = 90;
    const EPS = 1;
    const EXCLUDED = [
        '.lidar-3d-viewer', '#lidar-3d-canvas', '.gallery-image-viewport',
        '[data-lidar-interaction]', '[data-elastic-ignore]', '[draggable="true"]',
        'canvas', 'input', 'textarea', 'select', 'option',
        '[contenteditable]:not([contenteditable="false"])',
        '.saved-map-location-choice', '.saved-map-location-options',
    ].join(', ');
    const nativePageBounce = /iP(?:ad|hone|od)/i.test(global.navigator?.userAgent || '');
    const preference = global.matchMedia?.('(prefers-reduced-motion: reduce)');
    let active = null;
    let gesture = null;
    const returnTimers = new WeakMap();

    const reduced = () => Boolean(preference?.matches);
    function ignored(target) {
        const element = target?.nodeType === 1 ? target : target?.parentElement;
        return !element || Boolean(element.closest?.(EXCLUDED));
    }

    function pageScroll() {
        const root = doc.scrollingElement || doc.documentElement;
        const viewport = doc.documentElement?.clientHeight || global.innerHeight || 0;
        const height = Math.max(root?.scrollHeight || 0, doc.body?.scrollHeight || 0);
        return { element: doc.body, position: root?.scrollTop || 0,
            max: Math.max(0, height - viewport) };
    }

    function scrollChain(target) {
        const chain = [];
        let node = target?.nodeType === 1 ? target : target?.parentElement;
        while (node && node !== doc.body && node !== doc.documentElement) {
            const style = global.getComputedStyle(node);
            if (/^(auto|scroll|overlay)$/.test(style?.overflowY || '')) {
                const max = Math.max(0, node.scrollHeight - node.clientHeight);
                if (max > EPS) {
                    chain.push({ element: node, position: node.scrollTop, max,
                        contain: /^(contain|none)$/.test(style.overscrollBehaviorY || '') });
                }
            }
            node = node.parentElement;
        }
        // Fixed desktop dashboards can have scrollHeight > clientHeight while
        // body overflow:hidden prevents native scrolling. Do not mistake that
        // inaccessible overflow for a valid scroll-chain ancestor.
        const bodyOverflow = global.getComputedStyle(doc.body)?.overflowY || '';
        const pageIsLocked = /^(hidden|clip)$/.test(bodyOverflow);
        // Modals, drawers and dialogs must not scroll the page behind them.
        if (!target?.closest?.('#commonModal, .sidebar, .signup-modal, .ui-confirm-overlay')
            && !nativePageBounce && !pageIsLocked) {
            const page = pageScroll();
            if (page.element && page.max > EPS) chain.push(page);
        }
        return chain;
    }

    function atEdge(item, direction) {
        return direction < 0 ? item.position <= EPS : item.position >= item.max - EPS;
    }

    function visualBoundary(target) {
        // The desktop dashboard has a locked body rather than a page scroller.
        // Keep native inner scroll containers independent and bounce only at edges.
        if (nativePageBounce) return null;
        const element = target?.nodeType === 1 ? target : target?.parentElement;
        const scoped = element?.closest?.('.modal-body, .sidebar-body, .signup-dialog');
        if (scoped) return scoped;
        const locked = /^(hidden|clip)$/.test(global.getComputedStyle(doc.body)?.overflowY || '');
        if (!locked) return null;
        const dashboard = element?.closest?.('.dashboard-container, .right-panel')
            || (element?.closest?.('header, #topSystemStatusBar')
                && doc.querySelector?.('.dashboard-container'));
        return dashboard || null;
    }

    function pickBoundary(target, direction) {
        const chain = scrollChain(target);
        if (!chain.length) return visualBoundary(target);
        for (const item of chain) {
            if (!atEdge(item, direction)) return null;
            if (item.contain) return item.element;
        }
        // At a common boundary, bounce the deepest actual scroll container.
        return chain[0].element;
    }

    function clearEntryTimers(entry) {
        if (entry?.idle != null) global.clearTimeout(entry.idle);
        if (entry?.reset != null) global.clearTimeout(entry.reset);
        if (entry) { entry.idle = null; entry.reset = null; }
    }

    function cancelReturnTimer(element) {
        const timer = returnTimers.get(element);
        if (timer != null) global.clearTimeout(timer);
        returnTimers.delete(element);
    }

    function release(immediate = false) {
        if (!active) return;
        const entry = active;
        active = null;
        clearEntryTimers(entry);
        const element = entry.element;
        cancelReturnTimer(element);
        element.classList.remove('elastic-scroll-active');
        element.style.removeProperty('--elastic-scroll-offset');
        if (immediate || reduced() || !element.isConnected) {
            element.classList.remove('elastic-scroll-returning');
            return;
        }
        element.classList.add('elastic-scroll-returning');
        const timer = global.setTimeout(() => {
            element.classList.remove('elastic-scroll-returning');
            returnTimers.delete(element);
        }, RETURN_MS);
        returnTimers.set(element, timer);
    }

    function stretch(element, change, kind) {
        if (active?.element !== element) release(true);
        if (!active) {
            cancelReturnTimer(element);
            element.classList.remove('elastic-scroll-returning');
            active = { element, distance: 0, idle: null, reset: null };
        }
        clearEntryTimers(active);
        active.distance = Math.max(-420, Math.min(420, active.distance + change));
        const value = Math.sign(active.distance) * MAX_PX
            * (1 - Math.exp(-Math.abs(active.distance) / STIFFNESS));
        element.classList.add('elastic-scroll-active');
        element.style.setProperty('--elastic-scroll-offset', value.toFixed(2) + 'px');
        if (kind === 'wheel') active.idle = global.setTimeout(() => release(), IDLE_MS);
    }

    function handleWheel(event) {
        if (event.defaultPrevented || !event.cancelable || reduced() ||
            event.ctrlKey || event.metaKey || event.altKey || ignored(event.target)) return;
        const scale = event.deltaMode === 1 ? 16
            : event.deltaMode === 2 ? (global.innerHeight || 600) : 1;
        const dy = event.deltaY * scale;
        if (!Number.isFinite(dy) || Math.abs(dy) < 0.1 ||
            Math.abs(event.deltaX || 0) > Math.abs(event.deltaY) * 1.2) return;
        const element = pickBoundary(event.target, Math.sign(dy));
        if (!element) { release(true); return; }
        event.preventDefault();
        stretch(element, -Math.max(-80, Math.min(80, dy)), 'wheel');
    }

    function onlyTouch(event) {
        return event.touches?.length === 1 ? event.touches[0] : null;
    }

    function handleTouchStart(event) {
        release();
        const point = onlyTouch(event);
        gesture = point && !reduced() && !ignored(event.target)
            ? { id: point.identifier, startX: point.clientX, startY: point.clientY,
                lastY: point.clientY, target: event.target }
            : null;
    }

    function handleTouchMove(event) {
        if (!gesture || reduced() || !event.cancelable) return;
        const point = onlyTouch(event);
        if (!point || point.identifier !== gesture.id) {
            gesture = null; release(); return;
        }
        const totalX = point.clientX - gesture.startX;
        const totalY = point.clientY - gesture.startY;
        if (Math.abs(totalX) > Math.abs(totalY) * 1.2) return;
        const delta = point.clientY - gesture.lastY;
        gesture.lastY = point.clientY;
        if (Math.abs(delta) < .25) return;
        const element = pickBoundary(gesture.target, delta < 0 ? 1 : -1);
        if (!element) { release(true); return; }
        event.preventDefault();
        stretch(element, Math.max(-80, Math.min(80, delta)), 'touch');
    }

    function finishTouch() {
        gesture = null;
        release();
    }

    doc.addEventListener('wheel', handleWheel, { capture: true, passive: false });
    doc.addEventListener('touchstart', handleTouchStart, { capture: true, passive: true });
    doc.addEventListener('touchmove', handleTouchMove, { capture: true, passive: false });
    doc.addEventListener('touchend', finishTouch, { capture: true, passive: true });
    doc.addEventListener('touchcancel', finishTouch, { capture: true, passive: true });
    doc.addEventListener('visibilitychange', () => { if (doc.hidden) finishTouch(); });
    preference?.addEventListener?.('change', () => { if (reduced()) finishTouch(); });
    global.addEventListener?.('pagehide', () => release(true));
})(window);
