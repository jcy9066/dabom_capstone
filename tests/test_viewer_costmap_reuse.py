"""Exact costmap geometry reuse without altering Viewer semantics."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")
def test_costmap_duplicate_events_do_not_require_mesh_rebuild():
    script = r"""
const fs = require('fs');
const assert = require('assert');
const source = fs.readFileSync('frontend/services/static/lidar_3d_viewer.js', 'utf8');
const start = source.indexOf('    function costmapGeometryKey(');
const end = source.indexOf('    function rebuildCostmapCells(', start);
assert(start > 0 && end > start);
const f = new Function(source.slice(start, end) +
  '; return { costmapGeometryKey, sameCostmapIndices };')();
const a = {width:20, height:30, resolution:0.05, origin:{x:1,y:2,yaw:0.1}};
const b = {...a, received_at:42};
const c = {...b, origin:{...a.origin,yaw:0.2}};
assert.equal(f.costmapGeometryKey(a), f.costmapGeometryKey(b));
assert.notEqual(f.costmapGeometryKey(b), f.costmapGeometryKey(c));
assert(f.sameCostmapIndices([1, 2, 7], [1, 2, 7]));
assert(!f.sameCostmapIndices([1, 2, 7], [1, 3, 7]));
assert(!f.sameCostmapIndices([1, 2, 7], [2, 1, 7]));
assert(!f.sameCostmapIndices([1, 2], [1, 2, 7]));
assert(!f.sameCostmapIndices(null, [1, 2]));
assert(source.includes('if (forceInflation || geometryKey !== drawnInflationGeometryKey'));
assert(source.includes('renderOrder: 25') && source.includes('renderOrder: 10'));
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
