(function initializeRecordTable(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const records = components.records = components.records || {};

    records.escapeHtml = function escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;');
    };

    records.formatBoolean = function formatBoolean(value, yesLabel, noLabel) {
        if (value === true || value === 1 || value === '1') return yesLabel;
        if (value === false || value === 0 || value === '0') return noLabel;
        return '-';
    };

    records.formatValue = function formatValue(value, suffix = '') {
        if (value === undefined || value === null || value === '') return '-';
        return `${records.escapeHtml(value)}${suffix}`;
    };

    records.nextSortState = function nextSortState(current, defaultBy, nextBy) {
        if (!current?.touched || current.by !== nextBy) {
            return { by: nextBy, direction: 'desc', touched: true };
        }
        if (current.direction === 'desc') {
            return { by: nextBy, direction: 'asc', touched: true };
        }
        return { by: defaultBy, direction: 'desc', touched: false };
    };

    records.tableHeaderHtml = function tableHeaderHtml(columns, sort) {
        return columns.map(column => {
            if (column.selectAll) {
                return '<th class="record-selection-column"><input type="checkbox" data-record-select-all aria-label="현재 페이지 전체 선택"></th>';
            }
            const active = Boolean(column.sort && sort?.touched && sort.by === column.sort);
            const direction = active ? sort.direction : '';
            const indicator = active ? (direction === 'desc' ? ' ▼' : ' ▲') : '';
            const attributes = column.sort
                ? ` data-sort-by="${records.escapeHtml(column.sort)}" aria-sort="${active ? (direction === 'desc' ? 'descending' : 'ascending') : 'none'}"`
                : '';
            const content = column.sort
                ? `<button type="button" class="record-sort-button" data-sort-by="${records.escapeHtml(column.sort)}">${records.escapeHtml(column.label)}<span aria-hidden="true">${indicator}</span></button>`
                : records.escapeHtml(column.label);
            return `<th${attributes}>${content}</th>`;
        }).join('');
    };

    records.renderTableState = function renderTableState(target, options = {}) {
        if (!target) return false;
        const state = options.state || 'ready';
        if (state === 'ready') return false;
        const messages = {
            loading: '서버 데이터를 불러오는 중입니다.',
            empty: '조회된 데이터가 없습니다.',
            unavailable: '연결된 조회 API가 없습니다.',
            error: '서버 데이터를 불러오지 못했습니다.',
        };
        const message = options.message || messages[state] || messages.error;
        target.innerHTML = `<tr><td colspan="${Number(options.colspan) || 1}"><div class="table-empty is-${records.escapeHtml(state)}">${records.escapeHtml(message)}</div></td></tr>`;
        return true;
    };

    records.bindSortHeaders = function bindSortHeaders(root, onSort) {
        root?.querySelectorAll('[data-sort-by]').forEach(button => {
            if (!button.matches('button')) return;
            button.addEventListener('click', () => onSort?.(button.dataset.sortBy));
        });
    };
})(window);
