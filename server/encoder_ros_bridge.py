#!/usr/bin/env python3

import queue
import threading
import time

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Int64MultiArray
from std_msgs.msg import MultiArrayDimension


class EncoderPublisherNode(Node):
    def __init__(self, bridge: "EncoderRosBridge") -> None:
        super().__init__("encoder_websocket_bridge")

        self.bridge = bridge

        self.publisher = self.create_publisher(
            Int64MultiArray,
            bridge.ros_topic,
            10,
        )

        self.create_timer(
            0.005,
            self.publish_latest,
        )

    def publish_latest(self) -> None:
        latest = None

        while True:
            try:
                latest = self.bridge.message_queue.get_nowait()
            except queue.Empty:
                break

        if latest is None:
            return

        self.publisher.publish(latest)
        self.bridge.mark_published()


class EncoderRosBridge:
    """
    WebSocket 엔코더 데이터를 ROS2로 전달한다.

    /wheel_ticks:
      data[0] = left_front
      data[1] = right_front
      data[2] = left_rear
      data[3] = right_rear

    FastAPI 프로세스의 rclpy context는 LiDAR, encoder, navigation이
    공유할 수 있으므로 개별 bridge close()에서 전역 rclpy.shutdown()을
    호출하지 않는다. 각 bridge는 자신이 만든 executor/node만 정리한다.
    """

    def __init__(
        self,
        ros_topic: str = "/wheel_ticks",
    ) -> None:
        self.ros_topic = ros_topic

        self.message_queue = queue.Queue(maxsize=1)

        self.node = None
        self.executor = None
        self.thread = None
        self.started = False

        self.stats_lock = threading.Lock()

        self.bridge_stats = {
            "received": 0,
            "published": 0,
            "dropped": 0,
            "last_received_at": None,
            "last_published_at": None,
            "last_sequence": None,
            "last_ticks": None,
        }

    def start(self) -> None:
        if self.started:
            return

        if not rclpy.ok():
            rclpy.init(args=None)

        self.node = EncoderPublisherNode(self)

        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)

        self.thread = threading.Thread(
            target=self.executor.spin,
            name="encoder-ros-executor",
            daemon=True,
        )

        self.thread.start()
        self.started = True

        print(
            f"[encoder-ros] started "
            f"topic={self.ros_topic}"
        )

    def close(self) -> None:
        if not self.started:
            return

        if self.executor is not None:
            try:
                self.executor.shutdown(
                    timeout_sec=2.0
                )
            except Exception:
                pass

        if self.thread is not None:
            self.thread.join(timeout=2.0)

        if self.node is not None:
            try:
                self.node.destroy_node()
            except Exception:
                pass

        # Do not call rclpy.shutdown() here. The global context can be shared
        # by LiDAR and navigation nodes in the same FastAPI process.
        self.node = None
        self.executor = None
        self.thread = None
        self.started = False

    def mark_published(self) -> None:
        with self.stats_lock:
            self.bridge_stats["published"] += 1
            self.bridge_stats[
                "last_published_at"
            ] = time.time()

    def stats(self) -> dict:
        with self.stats_lock:
            return dict(self.bridge_stats)

    def submit(self, packet: dict) -> None:
        if not self.started:
            raise RuntimeError(
                "Encoder ROS bridge is not started"
            )

        if not isinstance(packet, dict):
            raise ValueError(
                "encoder packet must be an object"
            )

        ticks = [
            int(packet["left_front_ticks"]),
            int(packet["right_front_ticks"]),
            int(packet["left_rear_ticks"]),
            int(packet["right_rear_ticks"]),
        ]

        message = Int64MultiArray()

        dimension = MultiArrayDimension()
        dimension.label = "LF_RF_LR_RR"
        dimension.size = 4
        dimension.stride = 4

        message.layout.dim = [dimension]
        message.layout.data_offset = 0
        message.data = ticks

        if self.message_queue.full():
            try:
                self.message_queue.get_nowait()

                with self.stats_lock:
                    self.bridge_stats["dropped"] += 1

            except queue.Empty:
                pass

        try:
            self.message_queue.put_nowait(message)

        except queue.Full:
            with self.stats_lock:
                self.bridge_stats["dropped"] += 1

        with self.stats_lock:
            self.bridge_stats["received"] += 1
            self.bridge_stats[
                "last_received_at"
            ] = time.time()
            self.bridge_stats[
                "last_sequence"
            ] = packet.get("sequence")
            self.bridge_stats[
                "last_ticks"
            ] = ticks
