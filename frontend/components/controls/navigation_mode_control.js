(function initializeNavigationModeControl(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};
    const instances = controls.navigationModes = controls.navigationModes || [];

    function canRender(root) {
        return Boolean(root?.ownerDocument?.createElement && root.replaceChildren);
    }

    function buildToggle(root, controller) {
        if (!canRender(root)) return;

        const document = root.ownerDocument;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'navigation-mode-toggle';
        button.setAttribute('aria-label', 'Navigation mode');
        button.setAttribute('aria-pressed', 'false');

        const mapping = document.createElement('span');
        mapping.className = 'navigation-mode-toggle-label';
        mapping.dataset.navigationModeLabel = 'MAPPING';
        mapping.textContent = 'Mapping';

        const track = document.createElement('span');
        track.className = 'navigation-mode-toggle-track';
        track.setAttribute('aria-hidden', 'true');

        const thumb = document.createElement('span');
        thumb.className = 'navigation-mode-toggle-thumb';
        track.append(thumb);

        const driving = document.createElement('span');
        driving.className = 'navigation-mode-toggle-label';
        driving.dataset.navigationModeLabel = 'DRIVING';
        driving.textContent = 'Driving';

        button.append(mapping, track, driving);
        root.replaceChildren(button);

        button.addEventListener('click', event => {
            event.preventDefault();
            event.stopPropagation();
            if (controller.pending) return;
            const target = controller.mode === 'DRIVING' ? 'MAPPING' : 'DRIVING';
            controller.request(target);
        });

        controller.button = button;
        controller.mappingLabel = mapping;
        controller.drivingLabel = driving;
        controller.track = track;
        controller.render();
    }

    controls.mountNavigationMode = function mountNavigationMode(root, handlers = {}) {
        if (!root) return null;
        root.dataset.dashboardComponent = 'navigation-mode-control';

        const existing = root._dabomNavigationModeController;
        if (existing) {
            existing.handlers = { ...existing.handlers, ...handlers };
            existing.render();
            return existing;
        }

        const controller = {
            root,
            handlers,
            mode: '',
            pending: false,
            button: null,
            mappingLabel: null,
            drivingLabel: null,
            track: null,
            request(mode) {
                const normalized = String(mode || '').toUpperCase();
                if (!['MAPPING', 'DRIVING'].includes(normalized)) return false;
                const request = this.handlers.request || global.navigationControl?.requestNavigationMode;
                return request?.(normalized);
            },
            sync(mode, options = {}) {
                this.mode = String(mode || '').toUpperCase();
                this.pending = Boolean(options.pending);
                root.dataset.navigationMode = this.mode || 'UNKNOWN';
                this.render();
                this.handlers.sync?.(this.mode, options);
            },
            render() {
                if (!this.button) return;
                const known = this.mode === 'MAPPING' || this.mode === 'DRIVING';
                const driving = this.mode === 'DRIVING';

                this.button.disabled = this.pending || !known;
                this.button.setAttribute('aria-busy', String(this.pending));
                this.button.setAttribute('aria-pressed', String(driving));
                this.button.classList.toggle('driving', driving);
                this.button.classList.toggle('mapping', this.mode === 'MAPPING');
                this.button.classList.toggle('unknown', !known);

                this.mappingLabel?.classList.toggle('active', this.mode === 'MAPPING');
                this.mappingLabel?.classList.toggle('inactive', this.mode !== 'MAPPING');
                this.drivingLabel?.classList.toggle('active', driving);
                this.drivingLabel?.classList.toggle('inactive', !driving);

                if (this.track) {
                    this.track.dataset.mode = known ? this.mode : 'UNKNOWN';
                }
            },
        };

        root._dabomNavigationModeController = controller;
        instances.push(controller);
        controls.navigationMode = controller;
        buildToggle(root, controller);
        return controller;
    };

    controls.syncNavigationMode = function syncNavigationMode(mode, options = {}) {
        instances.forEach(controller => controller.sync(mode, options));
    };
})(window);
