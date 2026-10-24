import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardFilterResultTests(unittest.TestCase):
    def run_node(self, source: str) -> None:
        completed = subprocess.run(
            ["node", "-e", textwrap.dedent(source)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_record_queries_change_returned_rows_with_and_filters(self):
        self.run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const assert = require('assert');

            const datasets = {
                '/api/logs/system-status': [
                    { status_id: 1, recorded_at: '2026-08-01T09:00:00Z', cpu_usage: 10 },
                    { status_id: 2, recorded_at: '2026-08-02T09:00:00Z', cpu_usage: 20 },
                    { status_id: 3, recorded_at: '2026-08-03T09:00:00Z', cpu_usage: 30 },
                ],
                '/api/logs/events': [
                    { event_id: 11, detected_at: '2026-08-01T10:00:00Z', event_type: 'INTRUSION', confidence: 0.91, is_resolved: true, is_reported: true, is_alerted: true, is_false_alarm: false },
                    { event_id: 12, detected_at: '2026-08-02T10:00:00Z', event_type: 'INTRUSION', confidence: 0.72, is_resolved: true, is_reported: false, is_alerted: true, is_false_alarm: false },
                    { event_id: 13, detected_at: '2026-08-03T10:00:00Z', event_type: 'ASSAULT', confidence: 0.95, is_resolved: true, is_reported: true, is_alerted: true, is_false_alarm: false },
                    { event_id: 14, detected_at: '2026-08-04T10:00:00Z', event_type: 'INTRUSION', confidence: 0.93, is_resolved: true, is_reported: true, is_alerted: true, is_false_alarm: true },
                ],
                '/api/logs/actions': [
                    { action_id: 21, created_at: '2026-08-01T11:00:00Z', user_name: '홍길동', user_email: 'hong@example.com', action_type: 'WARNING', description_content: 'first' },
                    { action_id: 22, created_at: '2026-08-02T11:00:00Z', user_name: '김관리', user_email: 'kim@example.com', action_type: 'WARNING', description_content: 'second' },
                    { action_id: 23, created_at: '2026-08-03T11:00:00Z', user_name: '홍길순', user_email: 'soon@example.com', action_type: 'REPORT', description_content: 'third' },
                ],
            };

            const asBool = value => value === 'true';
            const filterRows = (pathname, params) => {
                let rows = datasets[pathname].slice();
                const timestampKey = pathname.endsWith('system-status') ? 'recorded_at' : pathname.endsWith('events') ? 'detected_at' : 'created_at';
                if (params.has('start_at')) rows = rows.filter(row => new Date(row[timestampKey]) >= new Date(params.get('start_at')));
                if (params.has('end_at')) rows = rows.filter(row => new Date(row[timestampKey]) <= new Date(params.get('end_at')));
                if (params.has('event_type')) rows = rows.filter(row => row.event_type === params.get('event_type'));
                if (params.has('confidence_min')) rows = rows.filter(row => row.confidence >= Number(params.get('confidence_min')));
                if (params.has('confidence_max')) rows = rows.filter(row => row.confidence <= Number(params.get('confidence_max')));
                for (const key of ['is_resolved', 'is_reported', 'is_alerted', 'is_false_alarm']) {
                    if (params.has(key)) rows = rows.filter(row => row[key] === asBool(params.get(key)));
                }
                if (params.has('user_name')) rows = rows.filter(row => row.user_name.includes(params.get('user_name')));
                if (params.has('action_type')) rows = rows.filter(row => row.action_type === params.get('action_type'));
                const sortBy = params.get('sort_by') || timestampKey;
                const direction = params.get('sort_direction') === 'asc' ? 1 : -1;
                rows.sort((left, right) => String(left[sortBy]).localeCompare(String(right[sortBy])) * direction);
                return rows;
            };

            const requested = [];
            const context = {
                console,
                URL,
                URLSearchParams,
                document: { getElementById: () => null, addEventListener: () => {} },
                fetch: async endpoint => {
                    const url = new URL(String(endpoint), 'http://dashboard.test');
                    requested.push(url);
                    if (!datasets[url.pathname]) return { ok: false, status: 404, json: async () => ({}) };
                    const rows = filterRows(url.pathname, url.searchParams);
                    return {
                        ok: true,
                        status: 200,
                        json: async () => ({ items: rows, page: 1, page_size: 10, total: rows.length, total_pages: rows.length ? 1 : 0 }),
                    };
                },
                setInterval: () => 0,
            };
            context.window = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync('frontend/services/static/dashboard_state.js', 'utf8'), context);

            (async () => {
                const state = context.DabomDashboardState;

                const statusAll = await state.fetchLogRows('statusModal', { page: 1, sortBy: 'recorded_at', sortDirection: 'desc' });
                assert.strictEqual(statusAll.total, 3);
                assert.deepStrictEqual(statusAll.rows.map(row => row.status_id), [3, 2, 1]);
                assert.strictEqual(statusAll.rows[0].cpu_usage, 30);
                const statusFiltered = await state.fetchLogRows('statusModal', {
                    startAt: '2026-08-02T00:00:00Z', endAt: '2026-08-02T23:59:59Z', page: 1,
                    sortBy: 'recorded_at', sortDirection: 'desc',
                });
                assert.strictEqual(statusFiltered.total, 1);
                assert.strictEqual(statusFiltered.rows[0].status_id, 2);
                assert.strictEqual(statusFiltered.rows[0].cpu_usage, 20);

                const eventAll = await state.fetchLogRows('patrolModal', { page: 1, sortBy: 'detected_at', sortDirection: 'desc' });
                assert.strictEqual(eventAll.total, 4);
                assert.deepStrictEqual(eventAll.rows.map(row => row.event_id), [14, 13, 12, 11]);
                assert.strictEqual(eventAll.rows[0].event_type, 'INTRUSION');
                const eventFiltered = await state.fetchLogRows('patrolModal', {
                    startAt: '2026-08-01T00:00:00Z', endAt: '2026-08-03T23:59:59Z', eventType: 'INTRUSION',
                    confidenceMin: 0.8, confidenceMax: 0.94, isResolved: 'true', isReported: 'true',
                    isAlerted: 'true', isFalseAlarm: 'false', page: 1, sortBy: 'detected_at', sortDirection: 'desc',
                });
                assert.strictEqual(eventFiltered.total, 1);
                assert.deepStrictEqual(eventFiltered.rows.map(row => row.event_id), [11]);
                assert.strictEqual(eventFiltered.rows[0].event_type, 'INTRUSION');

                const actionAll = await state.fetchLogRows('actionsModal', { page: 1, sortBy: 'created_at', sortDirection: 'desc' });
                assert.strictEqual(actionAll.total, 3);
                assert.deepStrictEqual(actionAll.rows.map(row => row.action_id), [23, 22, 21]);
                assert.strictEqual(actionAll.rows[0].description_content, 'third');
                const actionFiltered = await state.fetchLogRows('actionsModal', {
                    startAt: '2026-08-01T00:00:00Z', endAt: '2026-08-02T23:59:59Z', userName: '  홍길  ',
                    actionType: 'WARNING', page: 1, sortBy: 'created_at', sortDirection: 'desc',
                });
                assert.strictEqual(actionFiltered.total, 1);
                assert.strictEqual(actionFiltered.rows[0].action_id, 21);
                assert.strictEqual(actionFiltered.rows[0].administrator_name, '홍길동 (hong@example.com)');
                assert.strictEqual(actionFiltered.rows[0].description_content, 'first');

                const actionUrl = requested.find(url => url.pathname.endsWith('/actions') && url.searchParams.has('user_name'));
                assert.strictEqual(actionUrl.searchParams.get('user_name'), '홍길');
            })().catch(error => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_three_state_sort_returns_to_unmarked_default_and_page_one(self):
        self.run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const assert = require('assert');
            const context = { console };
            context.window = context;
            vm.createContext(context);
            for (const source of [
                'frontend/components/records/record_filters.js',
                'frontend/components/records/pagination.js',
                'frontend/components/records/record_table.js',
            ]) vm.runInContext(fs.readFileSync(source, 'utf8'), context, { filename: source });

            const records = context.DabomDashboardComponents.records;
            const plain = value => JSON.parse(JSON.stringify(value));
            const defaults = { statusModal: 'recorded_at', patrolModal: 'detected_at', actionsModal: 'created_at' };
            for (const defaultBy of Object.values(defaults)) {
                const pagination = records.createPaginationState({ page: 4, total: 200 });
                let sort = { by: defaultBy, direction: 'desc', touched: false };
                const columns = [{ label: '기록 시간', sort: defaultBy }, { label: '테스트', sort: 'test_field' }];
                assert(!/[↓↑]/.test(records.tableHeaderHtml(columns, sort)));
                sort = records.nextSortState(sort, defaultBy, 'test_field');
                pagination.reset();
                assert.deepStrictEqual(plain(sort), { by: 'test_field', direction: 'desc', touched: true });
                assert(records.tableHeaderHtml(columns, sort).includes('↓'));
                assert.strictEqual(pagination.page, 1);
                sort = records.nextSortState(sort, defaultBy, 'test_field');
                assert(records.tableHeaderHtml(columns, sort).includes('↑'));
                sort = records.nextSortState(sort, defaultBy, 'test_field');
                assert.deepStrictEqual(plain(sort), { by: defaultBy, direction: 'desc', touched: false });
                assert(!/[↓↑]/.test(records.tableHeaderHtml(columns, sort)));
            }
            """
        )


if __name__ == "__main__":
    unittest.main()
