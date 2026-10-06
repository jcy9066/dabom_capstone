import importlib
import os
import subprocess
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

os.environ["INFERENCE_ENABLED"] = "false"
os.environ["VISUALIZATION_ENABLED"] = "false"
os.environ["MODEL_REQUIRED"] = "false"
os.environ["NAV_DRY_RUN_ENABLED"] = "true"
os.environ["ROBOT_CONTROL_TOKEN"] = "test-robot-token"
os.environ["ROBOT_ID"] = "pi-01"
os.environ["DASHBOARD_ESTOP_COOLDOWN_SEC"] = "1.5"
os.environ["DASHBOARD_GOAL_REACHED_TOLERANCE_M"] = "0.25"


class NavigationSnapshotApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError as exc:
            raise unittest.SkipTest(
                f"FastAPI test dependencies are unavailable: {exc}"
            )
        cls.server = importlib.import_module("server.app")
        cls.client = TestClient(cls.server.app)

    def setUp(self):
        with self.server.state_lock:
            self.server.navigation_state.update(
                {
                    "robot_id": "pi-01",
                    "mode": "mapping",
                    "mode_updated_at": None,
                    "map": None,
                    "pose": None,
                    "scan": None,
                    "decision": None,
                    "map_updated_at": None,
                    "map_revision": None,
                    "pose_updated_at": None,
                    "scan_updated_at": None,
                }
            )

    def test_snapshot_combines_status_pose_scan_and_initial_map(self):
        observed_at = time.time()
        revision = "revision-a"
        current_map = {
            "width": 2,
            "height": 1,
            "resolution": 0.05,
            "data": [[0, 2]],
        }
        pose = {"x": 1.0, "y": 2.0, "yaw": 0.5}
        scan = {"ranges": [0.4, 0.8], "received_at": observed_at}
        with self.server.state_lock:
            self.server.navigation_state.update(
                {
                    "map": current_map,
                    "pose": pose,
                    "scan": scan,
                    "map_updated_at": observed_at,
                    "map_revision": revision,
                    "pose_updated_at": observed_at,
                    "scan_updated_at": observed_at,
                }
            )

        body = self.client.get("/api/navigation/snapshot").json()

        self.assertTrue(body["ok"])
        self.assertEqual("mapping", body["status"]["status"])
        self.assertEqual(pose, body["pose"])
        self.assertEqual(scan, body["scan"])
        self.assertTrue(body["map_changed"])
        self.assertEqual(revision, body["map_revision"])
        self.assertEqual(current_map, body["map"])

    def test_snapshot_omits_unchanged_map_payload(self):
        map_payload = {
            "robot_id": "pi-01",
            "navigation_mode": "mapping",
            "frame_id": "map",
            "timestamp": "2026-08-30T00:00:00Z",
            "bridge_timestamp": "2026-08-30T00:00:01Z",
            "resolution": 0.05,
            "width": 1,
            "height": 1,
            "origin": {"x": 0, "y": 0, "yaw": 0},
            "data_encoding": "rle",
            "data": [[0, 1]],
        }
        first = self.client.post(
            "/navigation/map",
            json=map_payload,
            headers={"X-Robot-Control-Token": "test-robot-token"},
        )
        self.assertEqual(200, first.status_code)
        revision = self.client.get("/api/navigation/snapshot").json()[
            "map_revision"
        ]

        map_payload["bridge_timestamp"] = "2026-08-30T00:00:03Z"
        repeated = self.client.post(
            "/navigation/map",
            json=map_payload,
            headers={"X-Robot-Control-Token": "test-robot-token"},
        )
        self.assertEqual(200, repeated.status_code)

        body = self.client.get(
            "/api/navigation/snapshot",
            params={"map_revision": revision},
        ).json()

        self.assertTrue(body["map_available"])
        self.assertFalse(body["map_changed"])
        self.assertNotIn("map", body)

    def test_snapshot_includes_map_again_after_revision_changes(self):
        previous_revision = "revision-a"
        next_revision = "revision-b"
        next_map = {"width": 2, "height": 1, "data": [[0, 2]]}
        with self.server.state_lock:
            self.server.navigation_state.update(
                {
                    "map": next_map,
                    "map_updated_at": time.time(),
                    "map_revision": next_revision,
                }
            )

        body = self.client.get(
            "/api/navigation/snapshot",
            params={"map_revision": previous_revision},
        ).json()

        self.assertTrue(body["map_changed"])
        self.assertEqual(next_revision, body["map_revision"])
        self.assertEqual(next_map, body["map"])

    def test_snapshot_status_uses_scan_freshness_when_map_and_pose_are_fresh(self):
        now = time.time()
        stale_scan_at = now - self.server.NAVIGATION_TIMEOUT_SEC - 1
        with self.server.state_lock:
            self.server.navigation_state.update(
                {
                    "map": {"width": 1, "height": 1, "data": [[0, 1]]},
                    "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
                    "scan": {"ranges": [0.4], "received_at": stale_scan_at},
                    "map_updated_at": now,
                    "pose_updated_at": now,
                    "scan_updated_at": stale_scan_at,
                }
            )

        status = self.client.get("/api/navigation/snapshot").json()["status"]

        self.assertEqual("stale", status["status"])
        self.assertEqual(stale_scan_at, status["last_update_at"])
        self.assertGreater(status["last_update_age_sec"], self.server.NAVIGATION_TIMEOUT_SEC)

    def test_snapshot_status_is_offline_without_scan_despite_fresh_map_and_pose(self):
        now = time.time()
        with self.server.state_lock:
            self.server.navigation_state.update(
                {
                    "map": {"width": 1, "height": 1, "data": [[0, 1]]},
                    "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
                    "scan": None,
                    "map_updated_at": now,
                    "pose_updated_at": now,
                    "scan_updated_at": None,
                }
            )

        status = self.client.get("/api/navigation/snapshot").json()["status"]

        self.assertEqual("offline", status["status"])
        self.assertIsNone(status["last_update_at"])
        self.assertIsNone(status["last_update_age_sec"])
        self.assertTrue(status["has_map"])
        self.assertTrue(status["has_pose"])
        self.assertFalse(status["has_scan"])


class NavigationPollingEfficiencyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = (
            ROOT / "frontend/services/static/script.js"
        ).read_text(encoding="utf-8")
        cls.control = (
            ROOT / "frontend/services/static/navigation_control.js"
        ).read_text(encoding="utf-8")

    def test_visual_polling_uses_one_non_overlapping_snapshot_loop(self):
        self.assertIn("const NAVIGATION_SNAPSHOT_VISIBLE_MS = 1000", self.dashboard)
        self.assertIn("const NAVIGATION_SNAPSHOT_HIDDEN_MS = 2000", self.dashboard)
        self.assertIn("const NAVIGATION_SNAPSHOT_TIMEOUT_MS = 1000", self.dashboard)
        self.assertIn("/api/navigation/snapshot?map_revision=", self.dashboard)
        self.assertIn("navigationSnapshotInFlight", self.dashboard)
        self.assertIn("document.hidden", self.dashboard)
        for obsolete in (
            "fetchOptionalJson('/api/navigation/status')",
            "fetchOptionalJson('/api/navigation/map')",
            "fetchOptionalJson('/api/navigation/pose')",
            "fetchOptionalJson('/api/navigation/scan')",
        ):
            self.assertNotIn(obsolete, self.dashboard)

    def test_authoritative_control_poll_remains_750ms_and_non_overlapping(self):
        self.assertIn("const POLL_MS = 750", self.control)
        self.assertIn("const HIDDEN_POLL_MS = 2000", self.control)
        self.assertIn("const CONTROL_REFRESH_TIMEOUT_MS = 700", self.control)
        self.assertIn("'/api/navigation/control/state',", self.control)
        self.assertIn("{ signal: controller.signal }", self.control)
        self.assertIn("() => controller.abort()", self.control)
        self.assertIn("if (state.controlRefreshPromise)", self.control)
        self.assertIn("document.addEventListener('visibilitychange'", self.control)
        self.assertIn("else pollControlState()", self.control)
        self.assertNotIn("window.setInterval(refreshState, POLL_MS)", self.control)

    def test_visible_and_hidden_request_rates_match_contract(self):
        control_rate = 1000 / 750
        visible_rate = control_rate + 1000 / 1000
        hidden_rate = 1000 / 2000 + 1000 / 2000
        self.assertAlmostEqual(2.333, visible_rate, places=3)
        self.assertAlmostEqual(1.000, hidden_rate, places=3)

    def test_lidar_live_state_requires_an_actual_fresh_scan(self):
        result = subprocess.run(
            ["node", "-e", r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync('frontend/services/static/script.js', 'utf8');
const liveStateLogic = source.slice(
    source.indexOf('function formatAgeSeconds'),
    source.indexOf('function updateLidarLabels'),
);
const context = {
    performance,
    lidarState: {
        status: null,
        statusObservedAtMs: performance.now(),
        map: {},
        pose: {},
        scan: null,
        lastScanSeenAtMs: 0,
    },
};
vm.createContext(context);
vm.runInContext(
    `const LIDAR_STALE_SECONDS = 3;
     const LIDAR_OFFLINE_SECONDS = 8;
     ${liveStateLogic}`,
    context,
);

context.lidarState.status = {
    status: 'mapping',
    has_scan: false,
    last_update_age_sec: 0,
};
assert.strictEqual(context.getLidarLiveState().level, 'offline');

context.lidarState.scan = { ranges: [0.4] };
context.lidarState.status = {
    status: 'mapping',
    has_scan: true,
    last_update_age_sec: 4,
};
assert.strictEqual(context.getLidarLiveState().level, 'stale');
"""],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_foreground_resume_queues_one_immediate_snapshot_without_overlap(self):
        result = subprocess.run(
            ["node", "-e", r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync('frontend/services/static/script.js', 'utf8');
const snapshotLogic = source.slice(
    source.indexOf('let navigationSnapshotTimer'),
    source.indexOf('function currentFullscreenElement()'),
);

async function verifyForegroundResume() {
    let requests = 0;
    let activeRequests = 0;
    let maxActiveRequests = 0;
    let nextTimerId = 1;
    const timers = new Map();
    const visibilityListeners = [];
    const requestResolvers = [];
    const context = {
        AbortController,
        encodeURIComponent,
        performance,
        lidarState: {},
        noteScanUpdate: () => {},
        requestLidarRender: () => {},
        WebSocket: class FakeWebSocket {
            static OPEN = 1;
            static CONNECTING = 0;
            constructor() { this.readyState = 0; }
            addEventListener() {}
            close() {}
        },
        fetchOptionalJson: () => new Promise(resolve => {
            requests += 1;
            activeRequests += 1;
            maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
            requestResolvers.push(() => {
                activeRequests -= 1;
                resolve(null);
            });
        }),
        document: {
            hidden: true,
            addEventListener: (name, callback) => {
                if (name === 'visibilitychange') visibilityListeners.push(callback);
            },
        },
    };
    context.window = {
        setTimeout: (callback, delay) => {
            const id = nextTimerId++;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout: id => timers.delete(id),
        location: { protocol: 'http:', host: 'localhost' },
    };
    vm.createContext(context);
    vm.runInContext(
        `const NAVIGATION_SNAPSHOT_VISIBLE_MS = 500;
         const NAVIGATION_SNAPSHOT_HIDDEN_MS = 2000;
         const NAVIGATION_SNAPSHOT_TIMEOUT_MS = 1000;
         ${snapshotLogic}`,
        context,
    );

    assert.strictEqual(requests, 1);
    context.document.hidden = false;
    visibilityListeners[0]();
    visibilityListeners[0]();
    assert.strictEqual(requests, 1);
    assert.strictEqual(maxActiveRequests, 1);

    requestResolvers.shift()();
    await new Promise(resolve => setImmediate(resolve));
    const immediateTimers = [...timers.entries()].filter(([, timer]) => timer.delay === 0);
    assert.strictEqual(immediateTimers.length, 1);

    const [immediateId, immediateTimer] = immediateTimers[0];
    timers.delete(immediateId);
    immediateTimer.callback();
    assert.strictEqual(requests, 2);
    assert.strictEqual(maxActiveRequests, 1);

    requestResolvers.shift()();
    await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual([...timers.values()].filter(timer => timer.delay === 0).length, 0);
}

verifyForegroundResume().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_hung_requests_abort_without_overlap_and_polling_can_resume(self):
        result = subprocess.run(
            ["node", "-e", r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const controlSource = fs.readFileSync(
    'frontend/services/static/navigation_control.js',
    'utf8',
);
const refreshLogic = controlSource.slice(
    controlSource.indexOf('function refreshState()'),
    controlSource.indexOf('function canSetGoal'),
);

async function verifyControlRecovery() {
    let requests = 0;
    const state = { controlRefreshPromise: null };
    const context = {
        AbortController,
        state,
        setFeedback: () => {},
        applyControlState: () => {},
        requestJson: (_url, { signal }) => {
            requests += 1;
            return new Promise((resolve, reject) => {
                signal.addEventListener('abort', () => reject(new Error('aborted')));
            });
        },
    };
    context.window = {
        setTimeout: (callback) => setTimeout(callback, 5),
        clearTimeout,
    };
    vm.createContext(context);
    vm.runInContext(
        `const CONTROL_REFRESH_TIMEOUT_MS = 5; ${refreshLogic}`,
        context,
    );
    const first = context.refreshState();
    const overlapping = context.refreshState();
    assert.strictEqual(first, overlapping);
    await first;
    assert.strictEqual(requests, 1);
    await context.refreshState();
    assert.strictEqual(requests, 2);
}

const dashboardSource = fs.readFileSync(
    'frontend/services/static/script.js',
    'utf8',
);
const snapshotLogic = dashboardSource.slice(
    dashboardSource.indexOf('let navigationSnapshotTimer'),
    dashboardSource.indexOf('function currentFullscreenElement()'),
);

async function verifySnapshotRecovery() {
    let aborts = 0;
    let renders = 0;
    let schedules = 0;
    const context = {
        AbortController,
        encodeURIComponent,
        performance,
        lidarState: {},
        noteScanUpdate: () => {},
        requestLidarRender: () => { renders += 1; },
        WebSocket: class FakeWebSocket {
            static OPEN = 1;
            static CONNECTING = 0;
            constructor() { this.readyState = 0; }
            addEventListener() {}
            close() {}
        },
        fetchOptionalJson: (_url, { signal }) => new Promise(resolve => {
            signal.addEventListener('abort', () => {
                aborts += 1;
                resolve(null);
            });
        }),
        document: {
            hidden: false,
            addEventListener: () => {},
        },
    };
    context.window = {
        setTimeout: (callback, delay) => {
            if (delay === 5) return setTimeout(callback, delay);
            schedules += 1;
            return schedules;
        },
        clearTimeout,
        location: { protocol: 'http:', host: 'localhost' },
    };
    vm.createContext(context);
    vm.runInContext(
        `const NAVIGATION_SNAPSHOT_VISIBLE_MS = 50;
         const NAVIGATION_SNAPSHOT_HIDDEN_MS = 200;
         const NAVIGATION_SNAPSHOT_TIMEOUT_MS = 5;
         ${snapshotLogic}`,
        context,
    );
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.strictEqual(aborts, 1);
    assert.strictEqual(renders, 2);
    assert.strictEqual(schedules, 1);
    await context.fetchNavigationSnapshot();
    assert.strictEqual(aborts, 2);
    assert.strictEqual(renders, 3);
}

(async () => {
    await verifyControlRecovery();
    await verifySnapshotRecovery();
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
