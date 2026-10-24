import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "frontend" / "components" / "records"


class DashboardSoftDeleteTests(unittest.TestCase):
    def run_node(self, source: str) -> None:
        completed = subprocess.run(
            ["node", "-e", textwrap.dedent(source)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_selection_payload_current_page_and_page_fallback(self):
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
                'frontend/components/records/record_modal.js',
            ]) vm.runInContext(fs.readFileSync(source, 'utf8'), context, { filename: source });

            const records = context.DabomDashboardComponents.records;
            const plain = value => JSON.parse(JSON.stringify(value));
            const rows = [{ event_id: 11 }, { event_id: 12 }, { event_id: 13 }];
            const selection = records.createRowSelectionState(row => row.event_id);
            selection.toggle(rows[0], true);
            assert.deepStrictEqual(plain(selection.currentPageState(rows)), { checked: false, indeterminate: true });
            selection.setCurrentPage(rows, true);
            assert.deepStrictEqual(plain(selection.currentPageState(rows)), { checked: true, indeterminate: false });
            assert.deepStrictEqual(
                plain(records.buildLogDeletePayload('patrolModal', selection.selectedKeys)),
                { event_ids: [11, 12, 13], action_ids: [] },
            );
            assert.deepStrictEqual(
                plain(records.buildLogDeletePayload('actionsModal', ['21', '22'])),
                { event_ids: [], action_ids: [21, 22] },
            );
            selection.clear();
            assert.strictEqual(selection.count, 0);
            assert.strictEqual(records.resolvePostDeletePage(3, 3), 3);
            assert.strictEqual(records.resolvePostDeletePage(3, 2), 2);
            assert.strictEqual(records.resolvePostDeletePage(1, 0), 1);
            """
        )

    def test_preview_and_execute_use_csrf_json_contract_and_counts(self):
        self.run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const assert = require('assert');
            const calls = [];
            const responses = [
                { ok: true, status: 200, json: async () => ({ csrf_token: 'csrf-1' }) },
                { ok: true, status: 200, json: async () => ({ ok: true, counts: { events: 2, actions: 5, images: 4 } }) },
                { ok: true, status: 200, json: async () => ({ csrf_token: 'csrf-2' }) },
                { ok: true, status: 200, json: async () => ({ ok: true, counts: { events: 0, actions: 2, images: 1 } }) },
            ];
            const context = {
                console,
                fetch: async (endpoint, options = {}) => {
                    calls.push({ endpoint: String(endpoint), options });
                    return responses.shift();
                },
            };
            context.window = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync('frontend/components/records/record_modal.js', 'utf8'), context);
            const records = context.DabomDashboardComponents.records;

            (async () => {
                const payload = { event_ids: [7, 8], action_ids: [] };
                const preview = await records.previewLogDelete(payload);
                const deleted = await records.executeLogDelete({ event_ids: [], action_ids: [21, 22] });
                assert.deepStrictEqual(JSON.parse(JSON.stringify(preview.counts)), { events: 2, actions: 5, images: 4 });
                assert.deepStrictEqual(JSON.parse(JSON.stringify(deleted.counts)), { events: 0, actions: 2, images: 1 });
                assert.deepStrictEqual(calls.map(call => call.endpoint), [
                    '/api/auth/csrf', '/api/logs/delete/preview',
                    '/api/auth/csrf', '/api/logs/delete',
                ]);
                for (const call of [calls[1], calls[3]]) {
                    assert.strictEqual(call.options.method, 'POST');
                    assert.strictEqual(call.options.credentials, 'same-origin');
                    assert.strictEqual(call.options.headers['Content-Type'], 'application/json');
                    assert(call.options.headers['X-CSRF-Token'].startsWith('csrf-'));
                }
                assert.deepStrictEqual(JSON.parse(calls[1].options.body), payload);
            })().catch(error => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_impact_wording_reset_and_no_hard_delete_contract(self):
        self.run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const assert = require('assert');
            const context = { console };
            context.window = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync('frontend/components/records/record_modal.js', 'utf8'), context);
            const records = context.DabomDashboardComponents.records;
            const eventImpact = records.describeLogDeleteImpact('patrolModal', { events: 2, actions: 5, images: 4 });
            const actionImpact = records.describeLogDeleteImpact('actionsModal', { events: 0, actions: 2, images: 1 });
            assert(eventImpact.message.includes('연결된 관리자 조치 5건도 함께 삭제'));
            assert(eventImpact.message.includes('원본 JPEG 파일은 보존'));
            assert(actionImpact.message.includes('관련 이벤트 원본은 유지'));
            assert(actionImpact.message.includes('원본 JPEG 파일은 보존'));
            """
        )

        source = (RECORDS / "record_modal.js").read_text(encoding="utf-8")
        status_columns = source.split("if (type === 'statusModal') return [", 1)[1].split("];", 1)[0]
        self.assertNotIn("selectAll", status_columns)
        self.assertIn('data-action="delete-cancel"', source)
        self.assertIn("this.clearSelection()", source)
        self.assertIn("if (this.loadState !== 'ready') return;", source)
        self.assertNotIn("method: 'DELETE'", source)
        self.assertNotIn("/api/gallery/", source)
        self.assertNotIn("unlink", source)


if __name__ == "__main__":
    unittest.main()
