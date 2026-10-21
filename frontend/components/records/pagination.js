(function initializeRecordPagination(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const records = components.records = components.records || {};

    records.DEFAULT_PAGE_SIZE = 10;
    records.createPaginationState = function createPaginationState(options = {}) {
        let page = Math.max(1, Number(options.page) || 1);
        let total = Math.max(0, Number(options.total) || 0);
        let serverTotalPages = Math.max(0, Number(options.totalPages) || 0);
        const pageSize = records.DEFAULT_PAGE_SIZE;
        return {
            get page() { return page; },
            get pageSize() { return pageSize; },
            get total() { return total; },
            get totalPages() { return serverTotalPages || Math.max(1, Math.ceil(total / pageSize)); },
            setPage(nextPage) {
                page = Math.min(Math.max(1, Number(nextPage) || 1), this.totalPages);
                return page;
            },
            update(metadata = {}) {
                total = Math.max(0, Number(metadata.total) || 0);
                serverTotalPages = Math.max(0, Number(metadata.total_pages ?? metadata.totalPages) || 0);
                page = Math.max(1, Number(metadata.page) || page);
                if (serverTotalPages) page = Math.min(page, serverTotalPages);
                return this.snapshot();
            },
            setTotal(nextTotal) {
                total = Math.max(0, Number(nextTotal) || 0);
                serverTotalPages = 0;
                page = Math.min(page, this.totalPages);
                return total;
            },
            reset() { page = 1; },
            snapshot() { return { page, page_size: pageSize, total, total_pages: this.totalPages }; },
        };
    };

    // Five fixed number slots: the current page is always the center item.
    records.renderPagination = function renderPagination(target, metadata, onPage) {
        if (!target) return;
        const total = Math.max(0, Number(metadata?.total) || 0);
        target.hidden = total <= records.DEFAULT_PAGE_SIZE;
        if (target.hidden) {
            target.replaceChildren();
            return;
        }
        const totalPages = Math.max(1, Number(metadata?.total_pages) || Math.ceil(total / records.DEFAULT_PAGE_SIZE));
        const page = Math.min(totalPages, Math.max(1, Number(metadata?.page) || 1));
        const previous = document.createElement('button');
        previous.type = 'button';
        previous.className = 'records-page-arrow';
        previous.textContent = '‹';
        previous.setAttribute('aria-label', '이전 페이지');
        previous.disabled = page <= 1;
        previous.addEventListener('click', () => onPage?.(page - 1));

        const numbers = document.createElement('div');
        numbers.className = 'records-page-numbers';
        numbers.setAttribute('role', 'group');
        numbers.setAttribute('aria-label', '페이지 번호 선택');
        for (let offset = -2; offset <= 2; offset++) {
            const number = page + offset;
            if (number < 1 || number > totalPages) {
                const placeholder = document.createElement('span');
                placeholder.className = 'records-page-empty';
                placeholder.setAttribute('aria-hidden', 'true');
                numbers.append(placeholder);
                continue;
            }
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'records-page-number';
            button.textContent = String(number);
            button.setAttribute('aria-label', `${number}페이지`);
            if (number === page) {
                button.classList.add('is-current');
                button.setAttribute('aria-current', 'page');
            } else {
                button.addEventListener('click', () => onPage?.(number));
            }
            numbers.append(button);
        }

        const next = document.createElement('button');
        next.type = 'button';
        next.className = 'records-page-arrow';
        next.textContent = '›';
        next.setAttribute('aria-label', '다음 페이지');
        next.disabled = page >= totalPages;
        next.addEventListener('click', () => onPage?.(page + 1));
        target.replaceChildren(previous, numbers, next);
    };
})(window);
