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

    def test_five_number_buttons_with_centered_current_page(self):
        self.run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const assert = require('assert');
            const element = tag => ({
                tag, children: [], attributes: {}, listeners: {}, className: '', textContent: '',
                classList: { add(_) {} },
                setAttribute(key, value) { this.attributes[key] = value; },
                addEventListener(key, fn) { this.listeners[key] = fn; },
                append(...children) { this.children.push(...children); },
                replaceChildren(...children) { this.children = children; },
            });
            const context = { console, document: { createElement: element } };
            context.window = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync('frontend/components/records/pagination.js','utf8'), context);
            const render = context.DabomDashboardComponents.records.renderPagination;
            const root = element('nav'), clicked = [];
            function page(number,total) {
                render(root, { page: number, total, total_pages: Math.ceil(total/10) }, n => clicked.push(n));
                return root.children[1].children;
            }
            let buttons = page(1,123);
            assert.deepStrictEqual(buttons.map(x => x.textContent), ['', '', '1','2','3']);
            assert.strictEqual(buttons[2].attributes['aria-current'], 'page');
            assert.strictEqual(root.children[0].disabled, true);
            buttons[3].listeners.click();
            assert.strictEqual(clicked.pop(),2);
            buttons = page(5,123);
            assert.deepStrictEqual(buttons.map(x => x.textContent), ['3','4','5','6','7']);
            assert.strictEqual(buttons[2].attributes['aria-current'], 'page');
            buttons[4].listeners.click();
            assert.strictEqual(clicked.pop(),7);
            buttons = page(13,123);
            assert.deepStrictEqual(buttons.map(x => x.textContent), ['11','12','13','','']);
            assert.strictEqual(buttons[2].attributes['aria-current'], 'page');
            assert.strictEqual(root.children[2].disabled,true);
            render(root, {page:1,total:10,total_pages:1}, () => {});
            assert.strictEqual(root.hidden,true);
            assert.strictEqual(root.children.length,0);
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
