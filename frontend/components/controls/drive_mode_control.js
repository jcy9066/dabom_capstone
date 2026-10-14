(function initializeDriveModeControl(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};

    function canRender(root) {
        return Boolean(root?.ownerDocument?.createElement && root.querySelector && root.append);
    }

    function createButton(document, label, mode, className) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = className;
        button.dataset.mode = mode;
        button.textContent = label;
        return button;
    }

    function ensureNavigationMount(root) {
        let mount = root.querySelector('#dashboard-navigation-mode-controls-mount');
        if (!mount) {
            mount = root.ownerDocument.createElement('div');
            mount.id = 'dashboard-navigation-mode-controls-mount';
            mount.dataset.dashboardMount = 'navigation-mode-controls';
            root.append(mount);
        }
        mount.classList?.add('dashboard-navigation-mode-group');
        return mount;
    }

    function render(root, controller) {
        if (!canRender(root)) return;
        root.querySelector('.dashboard-drive-mode-group')?.remove();
        const lever = root.querySelector('[data-dashboard-control="drive-mode"]');
        const navigationMount = ensureNavigationMount(root);
        [lever, navigationMount].filter(Boolean).forEach(child => {
            child.hidden = false;
            child.removeAttribute?.('aria-hidden');
        });
        root.classList?.add('dashboard-mode-controls-upgraded');
        controller.lever = lever;
        controls.mountNavigationMode?.(navigationMount);
    }

    controls.mountDriveMode = function mountDriveMode(root, handlers = {}) {
        if (!root) return null;
        root.dataset.dashboardComponent = 'drive-mode-control';
        const existing = root._dabomDriveModeController;
        if (existing) {
            existing.handlers = { ...existing.handlers, ...handlers };
            render(root, existing);
            return existing;
        }

        const controller = {
            root,
            handlers,
            mode: '',
            connected: null,
            pending: false,
            lever: null,
            request(mode) {
                const normalized = String(mode || '').toUpperCase();
                const request = this.handlers.request || global.navigationControl?.requestDriveMode;
                return request?.(normalized);
            },
            sync(mode, options = {}) {
                this.mode = String(mode || '').toUpperCase();
                if (Object.prototype.hasOwnProperty.call(options, 'connected')) this.connected = options.connected;
                if (Object.prototype.hasOwnProperty.call(options, 'pending')) this.pending = Boolean(options.pending);
                root.dataset.driveMode = this.mode;
                root.dataset.robotConnected = String(this.connected === true);
                if (this.lever) {
                    this.lever.disabled = this.connected !== true || this.pending;
                    this.lever.setAttribute('aria-busy', String(this.pending));
                }
                this.handlers.sync?.(this.mode, options);
            },
        };
        root._dabomDriveModeController = controller;
        controls.driveMode = controller;
        render(root, controller);
        return controller;
    };

    controls.mountEmergencyStop = function mountEmergencyStop(root, handlers = {}) {
        if (!root) return null;
        const existing = root._dabomEmergencyStopController;
        if (existing) {
            existing.handlers = { ...existing.handlers, ...handlers };
            return existing;
        }
        const controller = {
            root,
            handlers,
            active: false,
            connected: null,
            pending: false,
            cooldownUntil: 0,
            timer: null,
            request() {
                if (this.connected !== true || this.pending || Date.now() < this.cooldownUntil) return;
                return this.handlers.request?.(this.active ? 'RESUME' : 'STOP');
            },
            sync(options = {}) {
                this.active = Boolean(options.active);
                this.connected = options.connected;
                this.pending = Boolean(options.pending);
                if (Number.isFinite(options.cooldownUntil)) this.cooldownUntil = options.cooldownUntil;
                this.render();
            },
            render() {
                if (!this.button) return;
                const remainingMs = Math.max(0, this.cooldownUntil - Date.now());
                const remainingSec = Math.ceil(remainingMs / 1000);
                this.button.textContent = this.active ? 'PLAY' : 'STOP';
                if (remainingSec) this.button.textContent += ` (${remainingSec})`;
                this.button.classList.toggle('active', this.active);
                this.button.disabled = this.connected !== true || this.pending || remainingMs > 0;
                this.button.setAttribute('aria-pressed', String(this.active));
                root.dataset.emergencyStop = this.active ? 'active' : 'clear';
                if (remainingMs > 0 && !this.timer) {
                    this.timer = global.setInterval(() => {
                        if (Date.now() >= this.cooldownUntil) {
                            global.clearInterval(this.timer);
                            this.timer = null;
                        }
                        this.render();
                    }, 250);
                }
            },
        };
        if (canRender(root)) {
            root.replaceChildren();
            root.removeAttribute?.('aria-hidden');
            const button = createButton(root.ownerDocument, 'STOP', 'ESTOP', 'dpad-estop-button');
            button.setAttribute('aria-label', '긴급 정지');
            button.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                controller.request();
            });
            root.append(button);
            controller.button = button;
        }
        root._dabomEmergencyStopController = controller;
        controls.emergencyStop = controller;
        controller.render();
        return controller;
    };
})(window);
