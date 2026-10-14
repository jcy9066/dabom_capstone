import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "frontend" / "components" / "records"


class DashboardRecordsCoreTests(unittest.TestCase):
    def run_node(self, source: str) -> None:
        completed = subprocess.run(
            ["node", "-e", textwrap.dedent(source)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_view_filters_pagination_sort_and_selection_state(self):
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
            const statusFilters = records.createRecordFilterState('statusModal');
            assert.deepStrictEqual(Object.keys(statusFilters.values), ['startAt', 'endAt']);
            statusFilters.update({ startAt: '2026-08-01T00:00', eventType: 'INTRUSION' });
            assert.strictEqual(statusFilters.values.startAt, '2026-08-01T00:00');
            assert.strictEqual(statusFilters.values.eventType, undefined);

            const patrolFilters = records.createRecordFilterState('patrolModal');
            patrolFilters.update({ eventType: 'INTRUSION', isReported: 'true', userName: 'ignored' });
            assert.strictEqual(patrolFilters.values.eventType, 'INTRUSION');
            assert.strictEqual(patrolFilters.values.isReported, 'true');
            assert.strictEqual(patrolFilters.values.userName, undefined);
            assert.strictEqual(patrolFilters.reset().eventType, '');

            const pagination = records.createPaginationState({ page: 2, pageSize: 10, total: 101 });
            assert.strictEqual(pagination.pageSize, 10);
            assert.strictEqual(pagination.totalPages, 11);
            pagination.reset();
            assert.strictEqual(pagination.page, 1);

            const columns = [
                { selectAll: true },
                { label: '기록 시간', sort: 'detected_at' },
                { label: '신뢰도', sort: 'confidence' },
            ];
            const defaultSort = { by: 'detected_at', direction: 'desc', touched: false };
            let html = records.tableHeaderHtml(columns, defaultSort);
            assert(!html.includes('↓'));
            assert(!html.includes('↑'));
            assert(html.includes('aria-sort="none"'));
            assert(html.includes('data-record-select-all'));

            let sort = records.nextSortState(defaultSort, 'detected_at', 'confidence');
            assert.deepStrictEqual(plain(sort), { by: 'confidence', direction: 'desc', touched: true });
            html = records.tableHeaderHtml(columns, sort);
            assert(html.includes('↓'));
            assert.strictEqual((html.match(/aria-sort="descending"/g) || []).length, 1);
            sort = records.nextSortState(sort, 'detected_at', 'confidence');
            assert.deepStrictEqual(plain(sort), { by: 'confidence', direction: 'asc', touched: true });
            assert(records.tableHeaderHtml(columns, sort).includes('↑'));
            sort = records.nextSortState(sort, 'detected_at', 'confidence');
            assert.deepStrictEqual(plain(sort), defaultSort);
            html = records.tableHeaderHtml(columns, sort);
            assert(!html.includes('↓'));
            assert(!html.includes('↑'));

            const pageRows = [{ event_id: 11 }, { event_id: 12 }];
            const selection = records.createRowSelectionState(row => row.event_id);
            selection.toggle(pageRows[0], true);
            assert.deepStrictEqual(plain(selection.currentPageState(pageRows)), { checked: false, indeterminate: true });
            selection.setCurrentPage(pageRows, true);
            assert.deepStrictEqual(plain(selection.selectedKeys), ['11', '12']);
            assert.deepStrictEqual(plain(selection.currentPageState(pageRows)), { checked: true, indeterminate: false });
            selection.setCurrentPage(pageRows, false);
            assert.strictEqual(selection.count, 0);
            selection.replace([21, 22]);
            selection.clear();
            assert.deepStrictEqual(plain(selection.selectedKeys), []);
            """
        )

    def test_record_controller_wires_state_reset_and_mutation_hooks(self):
        source = (RECORDS / "record_modal.js").read_text(encoding="utf-8")
        for contract in (
            "records.createRecordFilterState(type)",
            "records.nextSortState(this.sort, DEFAULT_SORT[this.type], sortBy)",
            "data-record-select-row",
            "data-record-select-all",
            "selectedRecordIds()",
            "reloadAfterMutation()",
            "this.selection.setCurrentPage(this.rows, event.currentTarget.checked)",
        ):
            self.assertIn(contract, source)
        self.assertGreaterEqual(source.count("this.clearSelection();"), 6)


if __name__ == "__main__":
    unittest.main()
