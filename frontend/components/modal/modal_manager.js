(function initializeModalComponent(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const modal = components.modal = components.modal || {};

    function resolveElement(value) {
        return typeof value === 'string' ? document.querySelector(value) : value;
    }

    class ModalManager {
        constructor(options = {}) {
            this.root = resolveElement(options.root || '#commonModal');
            this.title = resolveElement(options.title || '#modalTitle');
            this.body = resolveElement(options.body || '#modalBody');
            this.closeControl = resolveElement(options.close || this.root?.querySelector?.('[data-modal-close]'));
            this.views = new Map();
            this.stack = [];
            this.activeView = null;
            this.originalParent = this.root?.parentNode || null;
            this.originalNextSibling = this.root?.nextSibling || null;
            this.bindDismissEvents();
            this.bindFullscreenEvents();
        }

        bindDismissEvents() {
            this.closeControl?.addEventListener?.('click', event => {
                event.preventDefault();
                this.close();
            });
            this.root?.addEventListener?.('click', event => {
                if (event.target === this.root) this.close();
            });
        }

        bindFullscreenEvents() {
            document.addEventListener?.('fullscreenchange', () => this.syncFullscreenHost());
            document.addEventListener?.('webkitfullscreenchange', () => this.syncFullscreenHost());
        }

        currentFullscreenElement() {
            return document.fullscreenElement || document.webkitFullscreenElement || null;
        }

        restoreRootParent() {
            if (!this.root || !this.originalParent || this.root.parentNode === this.originalParent) return;
            if (this.originalNextSibling && this.originalNextSibling.parentNode === this.originalParent) {
                this.originalParent.insertBefore(this.root, this.originalNextSibling);
            } else {
                this.originalParent.appendChild(this.root);
            }
        }

        syncFullscreenHost() {
            if (!this.root) return;
            const fullscreenElement = this.currentFullscreenElement();
            const visible = this.root.style.display !== 'none';

            if (fullscreenElement && visible) {
                if (this.root.parentNode !== fullscreenElement) fullscreenElement.appendChild(this.root);
                return;
            }

            this.restoreRootParent();
        }

        register(name, descriptor) {
            if (!name || !descriptor) throw new Error('A modal view name and descriptor are required.');
            this.views.set(name, descriptor);
            return this;
        }

        setTitle(value) {
            if (this.title) this.title.textContent = value == null ? '' : String(value);
        }

        setBody(content) {
            if (!this.body) return;
            if (content instanceof Node) this.body.replaceChildren(content);
            else this.body.innerHTML = content == null ? '' : String(content);
        }

        show() {
            if (this.root) this.root.style.display = 'flex';
            this.syncFullscreenHost();
        }

        hide() {
            if (this.root) this.root.style.display = 'none';
            this.restoreRootParent();
        }

        snapshot() {
            if (!this.activeView) return null;
            const descriptor = this.views.get(this.activeView.name);
            return {
                ...this.activeView,
                state: descriptor?.captureState?.() ?? this.activeView.state ?? null,
                scrollTop: this.body?.scrollTop || 0,
            };
        }

        async open(name, context = {}, options = {}) {
            const descriptor = this.views.get(name);
            if (!descriptor) throw new Error(`Unknown modal view: ${name}`);
            const previous = this.snapshot();
            if (previous && options.replace !== true) this.stack.push(previous);
            this.activeView = { name, context, state: options.state || null };
            if (this.root?.dataset) this.root.dataset.modalView = name;
            this.setTitle(typeof descriptor.title === 'function' ? descriptor.title(context) : descriptor.title || name);
            if (descriptor.render) this.setBody(await descriptor.render(context, options.state || null));
            this.show();
            await descriptor.onOpen?.(context, options.state || null);
            if (this.body) this.body.scrollTop = options.scrollTop || 0;
            return this.activeView;
        }

        async back() {
            const previous = this.stack.pop();
            if (!previous) return false;
            const descriptor = this.views.get(previous.name);
            this.activeView = previous;
            if (this.root?.dataset) this.root.dataset.modalView = previous.name;
            this.setTitle(typeof descriptor?.title === 'function'
                ? descriptor.title(previous.context)
                : descriptor?.title || previous.name);
            if (descriptor?.render) this.setBody(await descriptor.render(previous.context, previous.state));
            this.show();
            await descriptor?.restoreState?.(previous.state, previous.context);
            if (this.body) this.body.scrollTop = previous.scrollTop || 0;
            return true;
        }

        close() {
            const descriptor = this.activeView && this.views.get(this.activeView.name);
            descriptor?.onClose?.(this.activeView?.context);
            this.activeView = null;
            this.stack.length = 0;
            if (this.root?.dataset) delete this.root.dataset.modalView;
            this.hide();
        }
    }

    let defaultManager = null;
    modal.ModalManager = ModalManager;
    modal.mount = function mountModal(options = {}) {
        defaultManager = new ModalManager(options);
        return defaultManager;
    };
    modal.getDefault = function getDefaultModalManager() {
        return defaultManager;
    };
})(window);
