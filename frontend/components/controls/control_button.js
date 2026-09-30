(function initializeDashboardControlButton(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};

    const VARIANTS = new Set([
        'mode',
        'accent',
        'secondary',
        'danger',
        'success',
        'view',
        'goal',
    ]);

    function normalizeVariant(value) {
        const variant = String(value || 'secondary').trim().toLowerCase();
        return VARIANTS.has(variant) ? variant : 'secondary';
    }

    function enhance(button) {
        if (!button || button.tagName !== 'BUTTON') return button;
        const variant = normalizeVariant(button.dataset.controlButton);
        button.classList.add('dashboard-control-button');
        for (const name of VARIANTS) {
            button.classList.toggle(
                `dashboard-control-button--${name}`,
                name === variant,
            );
        }
        return button;
    }

    function enhanceAll(root = global.document) {
        if (!root?.querySelectorAll) return [];
        const buttons = Array.from(root.querySelectorAll('button[data-control-button]'));
        buttons.forEach(enhance);
        return buttons;
    }

    controls.controlButton = {
        enhance,
        enhanceAll,
    };

    enhanceAll();
})(window);
