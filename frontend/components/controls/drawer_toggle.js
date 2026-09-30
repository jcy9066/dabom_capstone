(function initializeDashboardDrawerToggle(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};

    function resolveTarget(button) {
        const id = button?.dataset?.drawerTarget;
        return id ? global.document.getElementById(id) : null;
    }

    function setOpen(button, open, options = {}) {
        const target = resolveTarget(button);
        if (!button || !target) return false;

        const isOpen = Boolean(open);
        target.dataset.open = isOpen ? 'true' : 'false';
        button.setAttribute('aria-expanded', isOpen ? 'true' : 'false');

        const label = isOpen
            ? (button.dataset.drawerOpenLabel || '패널 접기')
            : (button.dataset.drawerClosedLabel || '패널 펼치기');

        button.setAttribute('aria-label', label);
        button.title = label;

        if (options.emit !== false) {
            global.document.dispatchEvent(new CustomEvent('dabom:drawer-toggle', {
                detail: {
                    targetId: target.id,
                    buttonId: button.id || null,
                    open: isOpen,
                },
            }));
        }

        return isOpen;
    }

    function enhance(button) {
        if (!button || button.dataset.drawerToggleReady === 'true') return button;
        const target = resolveTarget(button);
        if (!target) return button;

        button.dataset.drawerToggleReady = 'true';
        const initialOpen = target.dataset.open !== 'false';
        setOpen(button, initialOpen, { emit: false });

        button.addEventListener('click', event => {
            event.preventDefault();
            const open = target.dataset.open !== 'true';
            setOpen(button, open);
        });

        return button;
    }

    function enhanceAll(root = global.document) {
        if (!root?.querySelectorAll) return [];
        const buttons = Array.from(root.querySelectorAll('[data-drawer-toggle]'));
        buttons.forEach(enhance);
        return buttons;
    }

    function findButton(targetOrId) {
        const targetId = typeof targetOrId === 'string'
            ? targetOrId
            : targetOrId?.id;
        if (!targetId) return null;
        return global.document.querySelector(
            `[data-drawer-toggle][data-drawer-target="${CSS.escape(targetId)}"]`,
        );
    }

    controls.drawerToggle = {
        enhance,
        enhanceAll,
        setOpen(targetOrId, open) {
            const button = findButton(targetOrId);
            return button ? setOpen(button, open) : false;
        },
    };

    enhanceAll();
})(window);
