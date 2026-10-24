(function initializeRecordFilters(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const records = components.records = components.records || {};

    const EMPTY_RANGE = Object.freeze({ startAt: '', endAt: '' });
    const RECORD_FILTER_DEFAULTS = Object.freeze({
        statusModal: Object.freeze({ startAt: '', endAt: '' }),
        patrolModal: Object.freeze({
            startAt: '', endAt: '', eventType: '', confidenceMin: '', confidenceMax: '',
            isResolved: '', isReported: '', isAlerted: '', isFalseAlarm: '',
        }),
        actionsModal: Object.freeze({ startAt: '', endAt: '', userName: '', actionType: '' }),
    });

    records.RECORD_FILTER_DEFAULTS = RECORD_FILTER_DEFAULTS;

    records.createFilterState = function createFilterState(initialValues = {}) {
        const initial = { ...initialValues };
        const allowedKeys = new Set(Object.keys(initial));
        let values = { ...initial };
        const normalize = nextValues => Object.fromEntries(
            Object.entries(nextValues || {}).filter(([key]) => allowedKeys.has(key)),
        );
        return {
            get values() { return { ...values }; },
            update(nextValues = {}) {
                values = { ...values, ...normalize(nextValues) };
                return this.values;
            },
            replace(nextValues = {}) {
                values = { ...initial, ...normalize(nextValues) };
                return this.values;
            },
            reset() {
                values = { ...initial };
                return this.values;
            },
        };
    };

    records.createRecordFilterState = function createRecordFilterState(type) {
        return records.createFilterState(RECORD_FILTER_DEFAULTS[type] || EMPTY_RANGE);
    };

    records.createRowSelectionState = function createRowSelectionState(keyForRow) {
        const selected = new Set();
        const keyOf = row => {
            const value = keyForRow?.(row);
            return value === undefined || value === null || value === '' ? '' : String(value);
        };
        return {
            get count() { return selected.size; },
            get selectedKeys() { return Array.from(selected); },
            has(row) {
                const key = keyOf(row);
                return Boolean(key && selected.has(key));
            },
            toggle(row, checked) {
                const key = keyOf(row);
                if (!key) return false;
                if (checked) selected.add(key);
                else selected.delete(key);
                return selected.has(key);
            },
            setCurrentPage(rows, checked) {
                (rows || []).forEach(row => this.toggle(row, checked));
                return this.currentPageState(rows);
            },
            currentPageState(rows) {
                const keys = (rows || []).map(keyOf).filter(Boolean);
                const selectedCount = keys.filter(key => selected.has(key)).length;
                return {
                    checked: keys.length > 0 && selectedCount === keys.length,
                    indeterminate: selectedCount > 0 && selectedCount < keys.length,
                };
            },
            replace(keys = []) {
                selected.clear();
                keys.forEach(key => {
                    if (key !== undefined && key !== null && key !== '') selected.add(String(key));
                });
                return this.selectedKeys;
            },
            clear() { selected.clear(); },
        };
    };

    records.readDateTimeRange = function readDateTimeRange(root) {
        if (!root) return { ...EMPTY_RANGE };
        const startAt = root.querySelector('[data-filter="startAt"]')?.value || '';
        const endAt = root.querySelector('[data-filter="endAt"]')?.value || '';
        if (startAt && endAt && new Date(startAt) > new Date(endAt)) {
            throw new Error('종료 일시는 시작 일시보다 빠를 수 없습니다.');
        }
        return { startAt, endAt };
    };

    records.CYCLE_FILTER_OPTIONS = Object.freeze({
        eventType: [
            { value: '', label: '유형: 전체' },
            { value: 'INTRUSION', label: '유형: 침입' },
            { value: 'ASSAULT', label: '유형: 폭행' },
            { value: 'SYSTEM_ERROR', label: '유형: 시스템 오류' },
            { value: 'NETWORK_LOSS', label: '유형: 네트워크 손실' },
            { value: 'SENSOR_ANOMALY', label: '유형: 센서 이상' },
        ],
        resolved: [
            { value: '', label: '조치: 전체' },
            { value: 'true', label: '조치: 완료' },
            { value: 'false', label: '조치: 미조치' },
        ],
        reported: [
            { value: '', label: '신고: 전체' },
            { value: 'true', label: '신고: 완료' },
            { value: 'false', label: '신고: 미신고' },
        ],
        alerted: [
            { value: '', label: '경고: 전체' },
            { value: 'true', label: '경고: 완료' },
            { value: 'false', label: '경고: 미경고' },
        ],
        falseAlarm: [
            { value: '', label: '오탐: 전체' },
            { value: 'true', label: '오탐: 오탐' },
            { value: 'false', label: '오탐: 정상' },
        ],
        actionType: [
            { value: '', label: '조치 유형: 전체' },
            { value: 'WARNING', label: '조치 유형: 경고' },
            { value: 'MANUAL_MOVING', label: '조치 유형: 수동 주행' },
            { value: 'REPORT', label: '조치 유형: 신고' },
            { value: 'COMMUNICATION', label: '조치 유형: 직접 통신' },
            { value: 'NOTE', label: '조치 유형: 상황 기록' },
        ],
    });
})(window);
