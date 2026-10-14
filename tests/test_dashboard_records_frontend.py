import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "frontend" / "components"


class DashboardRecordsFrontendTests(unittest.TestCase):
    def test_battery_status_is_absent_from_dashboard_frontend(self):
        sources = (
            ROOT / "frontend" / "templates" / "index.html",
            ROOT / "frontend" / "services" / "static" / "script.js",
            ROOT / "frontend" / "services" / "static" / "dashboard_state.js",
            COMPONENTS / "records" / "record_modal.js",
        )
        for source in sources:
            content = source.read_text(encoding="utf-8").lower()
            self.assertNotIn("battery", content, source)

    def test_records_component_owns_server_side_filter_sort_and_pagination(self):
        modal = (COMPONENTS / "records" / "record_modal.js").read_text(encoding="utf-8")
        state = (ROOT / "frontend" / "services" / "static" / "dashboard_state.js").read_text(encoding="utf-8")
        self.assertIn("pageSize: records.DEFAULT_PAGE_SIZE", modal)
        self.assertIn("this.pagination.reset();", modal)
        self.assertIn("direction: 'desc', touched: false", modal)
        self.assertNotIn("rows.slice().reverse()", modal)
        for query_name in (
            "page_size", "sort_by", "sort_direction", "event_type", "confidence_min",
            "is_resolved", "is_reported", "is_alerted", "is_false_alarm", "user_name", "action_type",
        ):
            self.assertIn(f"query.set('{query_name}'", state)

    def test_record_filters_auto_query_without_a_query_button(self):
        modal = (COMPONENTS / "records" / "record_modal.js").read_text(encoding="utf-8")
        self.assertNotIn('data-action="query"', modal)
        self.assertIn('const FILTER_QUERY_DEBOUNCE_MS = 300;', modal)
        self.assertIn('void this.query();', modal)
        self.assertIn("input.dataset.filter === 'startAt' || input.dataset.filter === 'endAt'", modal)
        self.assertIn('this.scheduleFilterQuery();', modal)
        self.assertIn('this.cancelScheduledFilterQuery();', modal)

    def test_patrol_and_action_columns_follow_display_contract(self):
        source = (COMPONENTS / "records" / "record_modal.js").read_text(encoding="utf-8")
        patrol_columns = source.split("if (type === 'patrolModal') return [", 1)[1].split("];", 1)[0]
        self.assertNotIn("출처", patrol_columns)
        self.assertIn("{ label: '관련 이벤트', sort: 'event_id' }", source)
        for value, label in (
            ("WARNING", "경고"),
            ("MANUAL_MOVING", "수동 주행"),
            ("REPORT", "신고"),
            ("COMMUNICATION", "직접 통신"),
            ("NOTE", "상황 기록"),
        ):
            self.assertIn(f"{value}: '{label}'", source)

    def test_patrol_and_action_type_badges_have_distinct_readable_classes(self):
        modal = (COMPONENTS / "records" / "record_modal.js").read_text(encoding="utf-8")
        style = (COMPONENTS / "records" / "records.css").read_text(encoding="utf-8")
        expected = {
            "INTRUSION": "record-event-intrusion",
            "ASSAULT": "record-event-assault",
            "SYSTEM_ERROR": "record-event-system-error",
            "NETWORK_LOSS": "record-event-network-loss",
            "SENSOR_ANOMALY": "record-event-sensor-anomaly",
            "WARNING": "record-action-warning",
            "MANUAL_MOVING": "record-action-manual-moving",
            "REPORT": "record-action-report",
            "COMMUNICATION": "record-action-communication",
            "NOTE": "record-action-note",
        }
        for value, class_name in expected.items():
            self.assertIn(f"{value}: '{class_name}'", modal)
            self.assertIn(f".{class_name}", style)
            self.assertIn(f"body.dark-mode .{class_name}", style)
        self.assertIn("records.escapeHtml(row.event_type)", modal)
        self.assertIn("ACTION_LABELS[row.action_type] || row.action_type", modal)

    def test_gallery_detail_back_restores_grid_scroll_position(self):
        source = (COMPONENTS / "gallery" / "image_detail.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("galleryScrollTop: grid?.scrollTop || 0", source)
        self.assertIn("function restoreGalleryScroll()", source)
        self.assertIn("global.requestAnimationFrame(apply)", source)

    def test_dashboard_state_builds_record_query_and_admin_label(self):
        script = textwrap.dedent(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const assert = require('assert');
            const requested = [];
            const context = {
                console,
                URLSearchParams,
                document: { getElementById: () => null, addEventListener: () => {} },
                fetch: async endpoint => {
                    requested.push(String(endpoint));
                    if (String(endpoint).includes('/api/logs/actions')) return {
                        ok: true, status: 200,
                        json: async () => ({
                            items: [{ action_id: 3, user_name: '홍길동', user_email: 'hong@example.com', action_type: 'WARNING' }],
                            page: 2, page_size: 10, total: 77, total_pages: 8,
                        }),
                    };
                    return { ok: false, status: 404, json: async () => ({}) };
                },
                setInterval: () => 0,
            };
            context.window = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync('frontend/services/static/dashboard_state.js', 'utf8'), context);
            (async () => {
                const result = await context.DabomDashboardState.fetchLogRows('actionsModal', {
                    page: 2, sortBy: 'user_name', sortDirection: 'asc', userName: '홍길', actionType: 'WARNING',
                });
                const url = requested.find(value => value.includes('/api/logs/actions'));
                assert(url.includes('page=2'));
                assert(url.includes('page_size=10'));
                assert(url.includes('sort_by=user_name'));
                assert(url.includes('sort_direction=asc'));
                assert(url.includes('user_name=%ED%99%8D%EA%B8%B8'));
                assert(url.includes('action_type=WARNING'));
                assert.strictEqual(result.total, 77);
                assert.strictEqual(result.rows[0].administrator_name, '홍길동 (hong@example.com)');
            })().catch(error => { console.error(error); process.exitCode = 1; });
            """
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
