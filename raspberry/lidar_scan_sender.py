#!/usr/bin/env python3

import asyncio
import json
import math
import queue
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
import websockets

try:
    from raspberry.env_config import env_float, env_text
except ModuleNotFoundError:  # Direct script execution from raspberry/.
    from env_config import env_float, env_text


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def finite_or_none(value: float):
    value = float(value)
    return value if math.isfinite(value) else None


class LidarScanSender(Node):
    """
    Raspberry Pi 역할:
      - 로컬 ROS2 /scan 구독
      - LaserScan 원본 데이터를 GPU 서버로 전송

    SLAM, 위치 추정, 장애물 판단 등의 연산은 수행하지 않는다.
    """

    def __init__(self):
        super().__init__("lidar_scan_sender")

        self.robot_id = env_text("ROBOT_ID")
        self.control_token = env_text("ROBOT_CONTROL_TOKEN")
        self.scan_topic = env_text("LIDAR_SCAN_TOPIC")
        server_base_url = env_text("SERVER_BASE_URL").rstrip("/")
        ws_base_url = server_base_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        self.ws_url = (
            f"{ws_base_url}/ws/sensors/{quote(self.robot_id, safe='')}/lidar"
        )
        self.reconnect_sec = env_float("LIDAR_WS_RECONNECT_SEC", minimum=0.01)

        self.message_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()

        self.scan_count = 0
        self.sent_count = 0
        self.dropped_count = 0
        self.last_warning_at = 0.0

        self.subscription = self.create_subscription(
            LaserScan,
            self.scan_topic,
            self.on_scan,
            qos_profile_sensor_data,
        )

        self.websocket_thread = threading.Thread(
            target=self.run_websocket_thread,
            name="lidar-websocket-sender",
            daemon=True,
        )
        self.websocket_thread.start()

        self.get_logger().info(
            f"LiDAR sender ready "
            f"robot_id={self.robot_id} "
            f"topic={self.scan_topic}"
        )

    def on_scan(self, msg: LaserScan) -> None:
        self.scan_count += 1

        payload = {
            "type": "laser_scan",
            "robot_id": self.robot_id,
            "sequence": self.scan_count,
            "sent_at": utc_now_iso(),
            "header": {
                "stamp_sec": int(msg.header.stamp.sec),
                "stamp_nanosec": int(msg.header.stamp.nanosec),
                "frame_id": msg.header.frame_id or "laser",
            },
            "scan_time": float(msg.scan_time),
            "time_increment": float(msg.time_increment),
            "angle_min": float(msg.angle_min),
            "angle_max": float(msg.angle_max),
            "angle_increment": float(msg.angle_increment),
            "range_min": float(msg.range_min),
            "range_max": float(msg.range_max),

            # SLAM 입력이므로 포인트를 줄이지 않는다.
            "ranges": [
                finite_or_none(value)
                for value in msg.ranges
            ],
            "intensities": [
                finite_or_none(value)
                for value in msg.intensities
            ],
        }

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

        # 네트워크가 느려질 경우 오래된 scan을 쌓지 않고
        # 가장 최근 scan 하나만 유지한다.
        if self.message_queue.full():
            try:
                self.message_queue.get_nowait()
                self.dropped_count += 1
            except queue.Empty:
                pass

        try:
            self.message_queue.put_nowait(encoded)
        except queue.Full:
            self.dropped_count += 1

        if self.scan_count % 100 == 0:
            self.get_logger().info(
                f"scan received={self.scan_count} "
                f"sent={self.sent_count} "
                f"dropped={self.dropped_count} "
                f"points={len(msg.ranges)}"
            )

    def warn_throttled(self, message: str) -> None:
        now = time.monotonic()

        if now - self.last_warning_at >= 5.0:
            self.get_logger().warning(message)
            self.last_warning_at = now

    def run_websocket_thread(self) -> None:
        try:
            asyncio.run(self.websocket_loop())
        except Exception as exc:
            self.warn_throttled(
                f"WebSocket thread terminated: {type(exc).__name__}"
            )

    async def get_next_message(self):
        try:
            return await asyncio.to_thread(
                self.message_queue.get,
                True,
                0.5,
            )
        except queue.Empty:
            return None

    async def websocket_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    extra_headers={"X-Robot-Control-Token": self.control_token},
                    ping_interval=10,
                    ping_timeout=5,
                    close_timeout=2,
                    max_size=None,
                    compression="deflate",
                ) as websocket:
                    hello = {
                        "type": "sensor_hello",
                        "sensor": "lidar",
                        "robot_id": self.robot_id,
                        "frame_id": "laser",
                        "timestamp": utc_now_iso(),
                    }

                    await websocket.send(
                        json.dumps(
                            hello,
                            separators=(",", ":"),
                        )
                    )

                    self.get_logger().info(
                        f"LiDAR WebSocket connected robot_id={self.robot_id}"
                    )

                    while not self.stop_event.is_set():
                        message = await self.get_next_message()

                        if message is None:
                            continue

                        await websocket.send(message)
                        self.sent_count += 1

            except asyncio.CancelledError:
                return

            except Exception as exc:
                self.warn_throttled(
                    f"LiDAR WebSocket disconnected: {type(exc).__name__}"
                )

                await asyncio.sleep(self.reconnect_sec)

    def destroy_node(self):
        self.stop_event.set()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = LidarScanSender()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
