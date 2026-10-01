import math
import time
from datetime import datetime, timezone

import requests
import rclpy
from patrol_navigation.env_config import env_text
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.time import Time
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from tf2_ros import Buffer, TransformException, TransformListener


VALID_NAVIGATION_MODES = frozenset(
    {
        "mapping",
        "localization",
        "localization_nav2",
    }
)


def stamp_to_iso(stamp):
    seconds = stamp.sec + stamp.nanosec / 1_000_000_000
    return (
        datetime.fromtimestamp(seconds, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def now_iso():
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def yaw_from_quaternion(q):
    siny_cosp = 2.0 * (
        q.w * q.z
        + q.x * q.y
    )
    cosy_cosp = 1.0 - 2.0 * (
        q.y * q.y
        + q.z * q.z
    )

    return math.atan2(
        siny_cosp,
        cosy_cosp,
    )


def rle_encode(values):
    if not values:
        return []

    runs = []
    last = int(values[0])
    count = 1

    for value in values[1:]:
        value = int(value)

        if value == last:
            count += 1
        else:
            runs.append([last, count])
            last = value
            count = 1

    runs.append([last, count])

    return runs


class MapBridge(Node):
    def __init__(self):
        super().__init__("map_bridge")

        self.declare_parameter(
            "robot_id",
            "pi-01",
        )
        self.declare_parameter(
            "server_base_url",
            "http://127.0.0.1:21063",
        )

        # mapping.launch.py는 별도 값을 전달하지 않으므로
        # 기존 mapping 동작을 유지하기 위해 기본값은 mapping이다.
        self.declare_parameter(
            "navigation_mode",
            "mapping",
        )

        self.declare_parameter(
            "map_topic",
            "/map",
        )
        self.declare_parameter(
            "scan_topic",
            "/scan",
        )
        self.declare_parameter(
            "pose_parent_frame",
            "map",
        )
        self.declare_parameter(
            "pose_child_frame",
            "base_link",
        )

        self.declare_parameter(
            "map_publish_period_sec",
            1.0,
        )
        self.declare_parameter(
            "pose_publish_period_sec",
            0.2,
        )
        self.declare_parameter(
            "scan_publish_period_sec",
            0.0,
        )
        self.declare_parameter(
            "request_timeout_sec",
            1.0,
        )

        self.declare_parameter(
            "send_map",
            True,
        )
        self.declare_parameter(
            "send_pose",
            True,
        )
        self.declare_parameter(
            "send_scan",
            False,
        )
        self.declare_parameter(
            "max_scan_points",
            180,
        )

        self.robot_id = str(
            self.get_parameter(
                "robot_id"
            ).value
        )

        self.server_base_url = str(
            self.get_parameter(
                "server_base_url"
            ).value
        ).rstrip("/")
        self.control_token = env_text("ROBOT_CONTROL_TOKEN")

        navigation_mode = str(
            self.get_parameter(
                "navigation_mode"
            ).value
        ).strip().lower()

        if navigation_mode not in VALID_NAVIGATION_MODES:
            raise ValueError(
                "지원하지 않는 navigation_mode입니다: "
                f"{navigation_mode}. "
                "허용값: "
                f"{sorted(VALID_NAVIGATION_MODES)}"
            )

        self.navigation_mode = navigation_mode

        self.map_topic = str(
            self.get_parameter(
                "map_topic"
            ).value
        )
        self.scan_topic = str(
            self.get_parameter(
                "scan_topic"
            ).value
        )

        self.pose_parent_frame = str(
            self.get_parameter(
                "pose_parent_frame"
            ).value
        )
        self.pose_child_frame = str(
            self.get_parameter(
                "pose_child_frame"
            ).value
        )

        self.request_timeout_sec = float(
            self.get_parameter(
                "request_timeout_sec"
            ).value
        )

        self.send_map = bool(
            self.get_parameter(
                "send_map"
            ).value
        )
        self.send_pose = bool(
            self.get_parameter(
                "send_pose"
            ).value
        )
        self.send_scan = bool(
            self.get_parameter(
                "send_scan"
            ).value
        )

        self.max_scan_points = int(
            self.get_parameter(
                "max_scan_points"
            ).value
        )

        self.latest_map = None
        self.latest_scan = None
        self.last_warning_at = 0.0

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(
            self.tf_buffer,
            self,
        )

        self.map_subscription = (
            self.create_subscription(
                OccupancyGrid,
                self.map_topic,
                self.on_map,
                1,
            )
        )

        if self.send_scan:
            self.scan_subscription = (
                self.create_subscription(
                    LaserScan,
                    self.scan_topic,
                    self.on_scan,
                    qos_profile_sensor_data,
                )
            )
        else:
            self.scan_subscription = None

        map_period = float(
            self.get_parameter(
                "map_publish_period_sec"
            ).value
        )
        pose_period = float(
            self.get_parameter(
                "pose_publish_period_sec"
            ).value
        )
        scan_period = float(
            self.get_parameter(
                "scan_publish_period_sec"
            ).value
        )

        if self.send_map and map_period > 0:
            self.create_timer(
                map_period,
                self.publish_map,
            )

        if self.send_pose and pose_period > 0:
            self.create_timer(
                pose_period,
                self.publish_pose,
            )

        if self.send_scan and scan_period > 0:
            self.create_timer(
                scan_period,
                self.publish_scan,
            )

        self.get_logger().info(
            "map_bridge ready "
            f"robot_id={self.robot_id} "
            f"server={self.server_base_url} "
            f"navigation_mode={self.navigation_mode} "
            f"map_topic={self.map_topic} "
            "pose="
            f"{self.pose_parent_frame}"
            "->"
            f"{self.pose_child_frame}"
        )

    def on_map(self, msg):
        self.latest_map = msg
        # RViz2 Map display처럼 /map callback이 도착한 순간 dashboard로
        # 전달한다. 별도 timer polling으로 지연시키지 않는다.
        if self.send_map:
            self.publish_map(msg)

    def on_scan(self, msg):
        self.latest_scan = msg

    def post_json(
        self,
        path,
        payload,
    ):
        url = (
            f"{self.server_base_url}"
            f"{path}"
        )

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"X-Robot-Control-Token": self.control_token},
                timeout=self.request_timeout_sec,
            )

            if response.status_code >= 400:
                self.warn_throttled(
                    f"POST {path} failed "
                    f"status={response.status_code} "
                    f"body={response.text[:160]}"
                )
                return False

            return True

        except requests.RequestException as exc:
            self.warn_throttled(
                f"POST {path} failed: {exc}"
            )
            return False

    def warn_throttled(
        self,
        message,
    ):
        now = time.monotonic()

        if now - self.last_warning_at >= 5.0:
            self.get_logger().warning(
                message
            )
            self.last_warning_at = now

    def common_payload(self):
        return {
            "robot_id": self.robot_id,
            "navigation_mode": (
                self.navigation_mode
            ),
        }

    def publish_map(self, msg=None):
        msg = msg or self.latest_map

        if msg is None:
            return

        payload = {
            **self.common_payload(),
            "frame_id": (
                msg.header.frame_id
                or "map"
            ),
            "timestamp": stamp_to_iso(
                msg.header.stamp
            ),
            "bridge_timestamp": now_iso(),
            "resolution": msg.info.resolution,
            "width": msg.info.width,
            "height": msg.info.height,
            "origin": {
                "x": (
                    msg.info.origin
                    .position.x
                ),
                "y": (
                    msg.info.origin
                    .position.y
                ),
                "z": (
                    msg.info.origin
                    .position.z
                ),
                "yaw": yaw_from_quaternion(
                    msg.info.origin.orientation
                ),
            },
            "data_encoding": "rle",
            "data": rle_encode(
                list(msg.data)
            ),
        }

        self.post_json(
            "/navigation/map",
            payload,
        )

    def publish_pose(self):
        try:
            transform = (
                self.tf_buffer.lookup_transform(
                    self.pose_parent_frame,
                    self.pose_child_frame,
                    Time(),
                )
            )

        except TransformException as exc:
            self.warn_throttled(
                "TF lookup failed "
                f"{self.pose_parent_frame}"
                "->"
                f"{self.pose_child_frame}: "
                f"{exc}"
            )
            return

        translation = (
            transform.transform.translation
        )
        rotation = (
            transform.transform.rotation
        )

        payload = {
            **self.common_payload(),
            "frame_id": (
                self.pose_parent_frame
            ),
            "child_frame_id": (
                self.pose_child_frame
            ),
            "timestamp": stamp_to_iso(
                transform.header.stamp
            ),
            "bridge_timestamp": now_iso(),
            "x": translation.x,
            "y": translation.y,
            "z": translation.z,
            "yaw": yaw_from_quaternion(
                rotation
            ),
            "orientation": {
                "x": rotation.x,
                "y": rotation.y,
                "z": rotation.z,
                "w": rotation.w,
            },
        }

        self.post_json(
            "/navigation/pose",
            payload,
        )

    def publish_scan(self):
        msg = self.latest_scan

        if msg is None:
            return

        ranges = list(msg.ranges)

        if (
            self.max_scan_points > 0
            and len(ranges)
            > self.max_scan_points
        ):
            step = max(
                1,
                len(ranges)
                // self.max_scan_points,
            )

            ranges = ranges[::step]

            angle_increment = (
                msg.angle_increment
                * step
            )
        else:
            angle_increment = (
                msg.angle_increment
            )

        sanitized_ranges = []

        for value in ranges:
            if (
                math.isinf(value)
                or math.isnan(value)
            ):
                sanitized_ranges.append(
                    None
                )
            else:
                sanitized_ranges.append(
                    value
                )

        payload = {
            **self.common_payload(),
            "frame_id": msg.header.frame_id,
            "timestamp": stamp_to_iso(
                msg.header.stamp
            ),
            "bridge_timestamp": now_iso(),
            "angle_min": msg.angle_min,
            "angle_max": msg.angle_max,
            "angle_increment": (
                angle_increment
            ),
            "range_min": msg.range_min,
            "range_max": msg.range_max,
            "ranges": sanitized_ranges,
        }

        self.post_json(
            "/navigation/scan",
            payload,
        )


def main(args=None):
    rclpy.init(args=args)

    node = MapBridge()

    try:
        rclpy.spin(node)

    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
