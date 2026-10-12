"""Regression tests for Map Bridge QoS and shutdown behavior."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SOURCE = (
    ROOT
    / "navigation"
    / "ros"
    / "patrol_navigation"
    / "patrol_navigation"
    / "map_bridge.py"
)


def _source():
    return SOURCE.read_text(encoding="utf-8")


def test_map_bridge_uses_transient_local_qos():
    source = _source()

    assert "DurabilityPolicy.TRANSIENT_LOCAL" in source
    assert "ReliabilityPolicy.RELIABLE" in source
    assert "HistoryPolicy.KEEP_LAST" in source

    tree = ast.parse(source)

    subscriptions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_subscription"
    ]

    map_subscriptions = [
        node
        for node in subscriptions
        if node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "OccupancyGrid"
    ]

    assert len(map_subscriptions) == 1

    qos = map_subscriptions[0].args[3]

    assert isinstance(qos, ast.Call)
    assert isinstance(qos.func, ast.Name)
    assert qos.func.id == "QoSProfile"

    kwargs = {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in qos.keywords
    }

    assert kwargs["durability"] == "DurabilityPolicy.TRANSIENT_LOCAL"
    assert kwargs["reliability"] == "ReliabilityPolicy.RELIABLE"
    assert kwargs["history"] == "HistoryPolicy.KEEP_LAST"
    assert kwargs["depth"] == "1"


def test_map_bridge_uses_safe_shutdown():
    source = _source()

    assert "rclpy.try_shutdown()" in source
    assert "rclpy.shutdown()" not in source
