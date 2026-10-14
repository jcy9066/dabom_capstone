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

    records.renderPagination = function renderPagination(target, metadata, onPage) {
        if (!target) return;
        const page = Math.max(1, Number(metadata?.page) || 1);
        const total = Math.max(0, Number(metadata?.total) || 0);
        const totalPages = Math.max(0, Number(metadata?.total_pages) || 0);
        target.hidden = total <= records.DEFAULT_PAGE_SIZE;
        if (target.hidden) {
            target.replaceChildren();
            return;
        }
        const previous = document.createElement('button');
        const summary = document.createElement('span');
        const next = document.createElement('button');
        previous.type = next.type = 'button';
        previous.textContent = '이전';
        next.textContent = '다음';
        previous.disabled = page <= 1;
        next.disabled = page >= totalPages;
        summary.textContent = `${page} / ${totalPages}`;
        previous.addEventListener('click', () => onPage?.(page - 1));
        next.addEventListener('click', () => onPage?.(page + 1));
        target.replaceChildren(previous, summary, next);
    };
})(window);
