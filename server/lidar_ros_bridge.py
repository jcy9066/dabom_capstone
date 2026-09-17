#!/usr/bin/env python3

import math
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

import rclpy
from geometry_msgs.msg import TransformStamped
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from tf2_ros import StaticTransformBroadcaster


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def require_float(packet: dict, name: str) -> float:
    value = packet.get(name)

    if value is None:
        raise ValueError(f"missing field: {name}")

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid float field: {name}") from exc

    if not math.isfinite(result):
        raise ValueError(f"non-finite field: {name}")

    return result


def ros_range(value: Any) -> float:
    if value is None:
        return float("inf")

    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("inf")

    if not math.isfinite(result):
        return float("inf")

    return result


def dashboard_range(value: Any):
    if value is None:
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(result):
        return None

    return result


def quaternion_from_rpy(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
    half_roll = roll / 2.0
    half_pitch = pitch / 2.0
    half_yaw = yaw / 2.0

    cr, sr = math.cos(half_roll), math.sin(half_roll)
    cp, sp = math.cos(half_pitch), math.sin(half_pitch)
    cy, sy = math.cos(half_yaw), math.sin(half_yaw)

    return (
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    )


class LidarPublisherNode(Node):
    def __init__(self, bridge: "LidarRosBridge"):
        super().__init__("lidar_websocket_bridge")

        self.bridge = bridge

        self.publisher = self.create_publisher(
            LaserScan,
            bridge.ros_topic,
            qos_profile_sensor_data,
        )

        self.static_tf_broadcaster = StaticTransformBroadcaster(self)

        self.create_timer(0.005, self.publish_latest_scan)
        self.publish_static_transform()

    def publish_static_transform(self) -> None:
        transform = TransformStamped()

        transform.header.stamp = self.get_clock().now().to_msg()
        transform.header.frame_id = self.bridge.base_frame
        transform.child_frame_id = self.bridge.lidar_frame

        transform.transform.translation.x = self.bridge.lidar_x
        transform.transform.translation.y = self.bridge.lidar_y
        transform.transform.translation.z = self.bridge.lidar_z

        qx, qy, qz, qw = quaternion_from_rpy(
            self.bridge.lidar_roll,
            self.bridge.lidar_pitch,
            self.bridge.lidar_yaw,
        )
        transform.transform.rotation.x = qx
        transform.transform.rotation.y = qy
        transform.transform.rotation.z = qz
        transform.transform.rotation.w = qw

        self.static_tf_broadcaster.sendTransform(transform)

        self.get_logger().info(
            f"static TF published "
            f"{self.bridge.base_frame}->{self.bridge.lidar_frame} "
            f"x={self.bridge.lidar_x} "
            f"y={self.bridge.lidar_y} "
            f"z={self.bridge.lidar_z} "
            f"roll={self.bridge.lidar_roll} "
            f"pitch={self.bridge.lidar_pitch} "
            f"yaw={self.bridge.lidar_yaw}"
        )

    def publish_latest_scan(self) -> None:
        latest = None

        while True:
            try:
                latest = self.bridge.scan_queue.get_nowait()
            except queue.Empty:
                break

        if latest is None:
            return

        self.publisher.publish(latest)
        self.bridge.mark_published()


class LidarRosBridge:
    def __init__(
        self,
        ros_topic: str = "/scan",
        base_frame: str = "base_link",
        lidar_frame: str = "laser",
        lidar_x: float = 0.0,
        lidar_y: float = 0.0,
        lidar_z: float = 0.12,
        lidar_roll: float | None = None,
        lidar_pitch: float | None = None,
        lidar_yaw: float = 0.0,
        dashboard_max_points: int = 360,
        use_source_timestamp: bool = True,
    ):
        self.ros_topic = ros_topic
        self.base_frame = base_frame
        self.lidar_frame = lidar_frame

        self.lidar_x = float(lidar_x)
        self.lidar_y = float(lidar_y)
        self.lidar_z = float(lidar_z)
        self.lidar_roll = float(
            os.getenv("LIDAR_ROLL", "0") if lidar_roll is None else lidar_roll
        )
        self.lidar_pitch = float(
            os.getenv("LIDAR_PITCH", "0") if lidar_pitch is None else lidar_pitch
        )
        self.lidar_yaw = float(lidar_yaw)

        self.dashboard_max_points = max(0, int(dashboard_max_points))
        self.use_source_timestamp = bool(use_source_timestamp)

        # 오래된 scan이 밀리지 않도록 최신 scan만 유지한다.
        self.scan_queue: queue.Queue[LaserScan] = queue.Queue(maxsize=1)

        self.node = None
        self.executor = None
        self.thread = None
        self.started = False

        self.stats_lock = threading.Lock()
        self.bridge_stats = {
            "connected": False,
            "robot_id": None,
            "connected_at": None,
            "disconnected_at": None,
            "received": 0,
            "published": 0,
            "dropped": 0,
            "last_received_at": None,
            "last_published_at": None,
            "last_sequence": None,
            "last_points": 0,
        }

    def start(self) -> None:
        if self.started:
            return

        if not rclpy.ok():
            rclpy.init(args=None)

        self.node = LidarPublisherNode(self)
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)

        self.thread = threading.Thread(
            target=self.executor.spin,
            name="lidar-ros-executor",
            daemon=True,
        )
        self.thread.start()

        self.started = True

        print(
            f"[lidar-ros] started "
            f"topic={self.ros_topic} "
            f"frame={self.lidar_frame} "
            f"base={self.base_frame}"
        )

    def close(self) -> None:
        if not self.started:
            return

        if self.executor is not None:
            try:
                self.executor.shutdown(timeout_sec=2.0)
            except Exception:
                pass

        if self.thread is not None:
            self.thread.join(timeout=2.0)

        if self.node is not None:
            try:
                self.node.destroy_node()
            except Exception:
                pass

        # Do not call rclpy.shutdown() here. The FastAPI process can share
        # this global context with encoder and navigation ROS nodes.
        self.node = None
        self.executor = None
        self.thread = None
        self.started = False

    def mark_connected(self, robot_id: str) -> None:
        with self.stats_lock:
            self.bridge_stats["connected"] = True
            self.bridge_stats["robot_id"] = robot_id
            self.bridge_stats["connected_at"] = time.time()
            self.bridge_stats["disconnected_at"] = None

    def mark_disconnected(self, robot_id: str) -> None:
        with self.stats_lock:
            if self.bridge_stats.get("robot_id") == robot_id:
                self.bridge_stats["connected"] = False
                self.bridge_stats["disconnected_at"] = time.time()

    def mark_published(self) -> None:
        with self.stats_lock:
            self.bridge_stats["published"] += 1
            self.bridge_stats["last_published_at"] = time.time()

    def stats(self) -> dict:
        with self.stats_lock:
            return dict(self.bridge_stats)

    def submit(self, packet: dict) -> dict:
        if not self.started:
            raise RuntimeError("LiDAR ROS bridge is not started")

        message, dashboard_payload = self.packet_to_scan(packet)

        if self.scan_queue.full():
            try:
                self.scan_queue.get_nowait()

                with self.stats_lock:
                    self.bridge_stats["dropped"] += 1
            except queue.Empty:
                pass

        try:
            self.scan_queue.put_nowait(message)
        except queue.Full:
            with self.stats_lock:
                self.bridge_stats["dropped"] += 1

        with self.stats_lock:
            self.bridge_stats["received"] += 1
            self.bridge_stats["last_received_at"] = time.time()
            self.bridge_stats["last_sequence"] = packet.get("sequence")
            self.bridge_stats["last_points"] = len(message.ranges)

        return dashboard_payload

    def packet_to_scan(self, packet: dict):
        if not isinstance(packet, dict):
            raise ValueError("scan packet must be an object")

        if packet.get("type") != "laser_scan":
            raise ValueError("unsupported packet type")

        raw_ranges = packet.get("ranges")

        if not isinstance(raw_ranges, list) or not raw_ranges:
            raise ValueError("ranges must be a non-empty list")

        if len(raw_ranges) > 10000:
            raise ValueError("too many scan points")

        header = packet.get("header") or {}

        if not isinstance(header, dict):
            raise ValueError("header must be an object")

        angle_min = require_float(packet, "angle_min")
        angle_max = require_float(packet, "angle_max")
        angle_increment = require_float(packet, "angle_increment")
        range_min = require_float(packet, "range_min")
        range_max = require_float(packet, "range_max")

        if angle_increment <= 0.0:
            raise ValueError("angle_increment must be positive")

        if range_min < 0.0 or range_max <= range_min:
            raise ValueError("invalid LiDAR range limits")

        message = LaserScan()
        message.header.frame_id = str(
            header.get("frame_id") or self.lidar_frame
        )

        if self.use_source_timestamp:
            stamp_sec = int(header.get("stamp_sec") or 0)
            stamp_nanosec = int(header.get("stamp_nanosec") or 0)

            if stamp_sec > 0 and 0 <= stamp_nanosec < 1_000_000_000:
                message.header.stamp.sec = stamp_sec
                message.header.stamp.nanosec = stamp_nanosec
            else:
                message.header.stamp = self.node.get_clock().now().to_msg()
        else:
            message.header.stamp = self.node.get_clock().now().to_msg()

        message.angle_min = angle_min
        message.angle_max = angle_max
        message.angle_increment = angle_increment

        message.time_increment = float(
            packet.get("time_increment") or 0.0
        )
        message.scan_time = float(
            packet.get("scan_time") or 0.0
        )

        message.range_min = range_min
        message.range_max = range_max
        message.ranges = [
            ros_range(value)
            for value in raw_ranges
        ]

        raw_intensities = packet.get("intensities") or []

        if isinstance(raw_intensities, list):
            message.intensities = [
                0.0 if value is None else ros_range(value)
                for value in raw_intensities
            ]
        else:
            message.intensities = []

        dashboard_ranges = [
            dashboard_range(value)
            for value in raw_ranges
        ]

        step = 1

        if (
            self.dashboard_max_points > 0
            and len(dashboard_ranges) > self.dashboard_max_points
        ):
            step = max(
                1,
                math.ceil(
                    len(dashboard_ranges)
                    / self.dashboard_max_points
                ),
            )
            dashboard_ranges = dashboard_ranges[::step]

        dashboard_angle_increment = angle_increment * step
        dashboard_angle_max = (
            angle_min
            + dashboard_angle_increment
            * max(0, len(dashboard_ranges) - 1)
        )

        dashboard_payload = {
            "robot_id": packet.get("robot_id"),
            "frame_id": message.header.frame_id,
            "timestamp": packet.get("sent_at") or utc_now_iso(),
            "bridge_timestamp": utc_now_iso(),
            "sequence": packet.get("sequence"),
            "angle_min": angle_min,
            "angle_max": dashboard_angle_max,
            "angle_increment": dashboard_angle_increment,
            "range_min": range_min,
            "range_max": range_max,
            "ranges": dashboard_ranges,
            "source_points": len(raw_ranges),
            "dashboard_points": len(dashboard_ranges),
        }

        return message, dashboard_payload
