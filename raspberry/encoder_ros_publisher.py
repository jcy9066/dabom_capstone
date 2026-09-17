from __future__ import annotations


class EncoderRosPublisher:
    """
    Pico W에서 받은 4개 바퀴 encoder tick을
    Raspberry Pi의 ROS2 /wheel_ticks topic으로 발행한다.
    """

    def __init__(self, topic: str = "/wheel_ticks") -> None:
        self.topic = topic

        self.rclpy = None
        self.node = None
        self.publisher = None

        self.Int64MultiArray = None
        self.MultiArrayDimension = None

        self.owns_rclpy = False
        self.started = False

    def start(self) -> None:
        if self.started:
            return

        try:
            import rclpy
            from std_msgs.msg import Int64MultiArray
            from std_msgs.msg import MultiArrayDimension

        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ROS2 Python 환경을 찾을 수 없습니다. "
                "/opt/ros/humble/setup.bash를 source한 뒤 실행하세요."
            ) from exc

        self.rclpy = rclpy
        self.Int64MultiArray = Int64MultiArray
        self.MultiArrayDimension = MultiArrayDimension

        if not rclpy.ok():
            rclpy.init(args=None)
            self.owns_rclpy = True

        self.node = rclpy.create_node(
            "pi_encoder_publisher"
        )

        self.publisher = self.node.create_publisher(
            Int64MultiArray,
            self.topic,
            10,
        )

        self.started = True

        print(
            f"[encoder-ros] started topic={self.topic}"
        )

    def publish(self, snapshot: dict) -> None:
        if not self.started:
            return

        message = self.Int64MultiArray()

        dimension = self.MultiArrayDimension()
        dimension.label = "LF_RF_LR_RR"
        dimension.size = 4
        dimension.stride = 4

        message.layout.dim = [dimension]
        message.layout.data_offset = 0

        message.data = [
            int(snapshot["left_front_ticks"]),
            int(snapshot["right_front_ticks"]),
            int(snapshot["left_rear_ticks"]),
            int(snapshot["right_rear_ticks"]),
        ]

        self.publisher.publish(message)

    def close(self) -> None:
        if not self.started:
            return

        if self.node is not None:
            try:
                self.node.destroy_node()
            except Exception:
                pass

        if (
            self.owns_rclpy
            and self.rclpy is not None
            and self.rclpy.ok()
        ):
            try:
                self.rclpy.shutdown()
            except Exception:
                pass

        self.started = False
