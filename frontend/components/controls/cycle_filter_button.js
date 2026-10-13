(function initializeCycleFilterButton(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};

    class CycleFilterButton {
        constructor(element, options = {}) {
            this.element = element;
            this.options = Array.isArray(options.options) ? options.options : [];
            this.index = Math.max(0, Number(options.index) || 0);
            this.onChange = options.onChange;
            this.handleClick = this.next.bind(this);
            this.element?.addEventListener('click', this.handleClick);
            this.render();
        }

        get value() {
            return this.options[this.index]?.value;
        }

        next() {
            if (!this.options.length) return;
            this.index = (this.index + 1) % this.options.length;
            this.render();
            this.onChange?.(this.value, this.index);
        }

        reset() {
            this.index = 0;
            this.render();
        }

        render() {
            const option = this.options[this.index];
            if (!this.element || !option) return;
            this.element.textContent = option.label;
            this.element.dataset.value = option.value == null ? '' : String(option.value);
            // Styling only: the existing cycle index determines whether the filter is active.
            this.element.dataset.uiFilterSelected = String(this.index !== 0);
        }

        destroy() {
            this.element?.removeEventListener('click', this.handleClick);
        }
    }

    controls.CycleFilterButton = CycleFilterButton;
})(window);
