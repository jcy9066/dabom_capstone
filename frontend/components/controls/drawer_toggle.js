(function initializeDashboardDrawerToggle(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};

    function resolveTarget(button) {
        const id = button?.dataset?.drawerTarget;
        return id ? global.document.getElementById(id) : null;
    }

    function isOpen(button, target) {
        const collapseClass = button?.dataset?.drawerCollapseClass;
        return collapseClass
            ? !target.classList.contains(collapseClass)
            : target.dataset.open !== 'false';
    }

    function setOpen(button, open, options = {}) {
        const target = resolveTarget(button);
        if (!button || !target) return false;

        const expanded = Boolean(open);
        const collapseClass = button.dataset.drawerCollapseClass;
        if (collapseClass) {
            target.classList.toggle(collapseClass, !expanded);
            const panelId = button.dataset.drawerPanel;
            const panel = panelId && global.document.getElementById(panelId);
            panel?.setAttribute('aria-hidden', String(!expanded));
        } else {
            target.dataset.open = expanded ? 'true' : 'false';
        }
        button.setAttribute('aria-expanded', String(expanded));

        const label = expanded
            ? (button.dataset.drawerOpenLabel || '패널 접기')
            : (button.dataset.drawerClosedLabel || '패널 펼치기');

        button.setAttribute('aria-label', label);
        button.title = label;

        if (options.emit !== false) {
            global.document.dispatchEvent(new CustomEvent('dabom:drawer-toggle', {
                detail: {
                    targetId: target.id,
                    buttonId: button.id || null,
                    open: expanded,
                },
            }));
        }

        return expanded;
    }

    function enhance(button) {
        if (!button || button.dataset.drawerToggleReady === 'true') return button;
        const target = resolveTarget(button);
        if (!target) return button;

        button.dataset.drawerToggleReady = 'true';
        const initialOpen = isOpen(button, target);
        setOpen(button, initialOpen, { emit: false });

        button.addEventListener('click', event => {
            event.preventDefault();
            const open = !isOpen(button, target);
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
