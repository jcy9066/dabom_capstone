#!/usr/bin/env python3

from __future__ import annotations

import math
import threading
import time

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Int64MultiArray
from tf2_ros import TransformBroadcaster

try:
    from server.env_config import env_float, env_text
except ModuleNotFoundError:  # Direct script execution from server/.
    from env_config import env_float, env_text


def normalize_angle(angle: float) -> float:
    return math.atan2(
        math.sin(angle),
        math.cos(angle),
    )


class WheelOdometryNode(Node):
    """
    /wheel_ticks:
      data[0] = left_front
      data[1] = right_front
      data[2] = left_rear
      data[3] = right_rear

    출력:
      /odom
      TF: odom -> base_link
    """

    def __init__(
        self,
        wheel_diameter_m: float,
        wheel_track_m: float,
        ticks_per_revolution: float,
        wheel_ticks_topic: str,
        odom_topic: str,
        odom_frame: str,
        base_frame: str,
    ) -> None:
        super().__init__("wheel_odometry")

        self.wheel_diameter_m = float(
            wheel_diameter_m
        )
        self.wheel_track_m = float(
            wheel_track_m
        )
        self.ticks_per_revolution = float(
            ticks_per_revolution
        )

        self.meters_per_tick = (
            math.pi * self.wheel_diameter_m
            / self.ticks_per_revolution
        )

        self.odom_frame = odom_frame
        self.base_frame = base_frame

        self.odom_publisher = self.create_publisher(
            Odometry,
            odom_topic,
            10,
        )

        self.tf_broadcaster = TransformBroadcaster(
            self
        )

        self.subscription = self.create_subscription(
            Int64MultiArray,
            wheel_ticks_topic,
            self.encoder_callback,
            10,
        )

        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0

        self.previous_ticks = None
        self.previous_time_sec = None

        # 엔코더 callback에서 계산한 최신 속도다.
        self.latest_linear_velocity = 0.0
        self.latest_angular_velocity = 0.0
        self.last_encoder_time_sec = None
        self.has_encoder_data = False

        # Fresh encoder input is required before publishing /odom and TF.
        self.publish_rate_hz = 20.0
        self.velocity_timeout_sec = env_float(
            "ODOMETRY_ENCODER_TIMEOUT_SEC", minimum=0.05
        )

        self.received = 0
        self.published = 0
        self.last_ticks = None
        self.last_update_at = None

        self.publish_timer = self.create_timer(
            1.0 / self.publish_rate_hz,
            self.publish_timer_callback,
        )

        self.get_logger().info(
            "wheel odometry started "
            f"diameter={self.wheel_diameter_m:.4f}m "
            f"track={self.wheel_track_m:.4f}m "
            f"ticks_per_rev="
            f"{self.ticks_per_revolution:.3f} "
            f"meters_per_tick="
            f"{self.meters_per_tick:.10f}"
        )

    def encoder_callback(
        self,
        message: Int64MultiArray,
    ) -> None:
        if len(message.data) < 4:
            self.get_logger().warning(
                "wheel_ticks requires four values"
            )
            return

        ticks = [
            int(message.data[0]),
            int(message.data[1]),
            int(message.data[2]),
            int(message.data[3]),
        ]

        now = self.get_clock().now()
        now_sec = now.nanoseconds / 1_000_000_000.0

        self.received += 1
        self.last_ticks = ticks
        self.last_update_at = time.time()

        self.last_encoder_time_sec = now_sec
        self.has_encoder_data = True

        if self.previous_ticks is None:
            self.previous_ticks = ticks
            self.previous_time_sec = now_sec
            self.latest_linear_velocity = 0.0
            self.latest_angular_velocity = 0.0
            return

        delta_ticks = [
            current - previous
            for current, previous in zip(
                ticks,
                self.previous_ticks,
            )
        ]

        # 앞·뒤 바퀴 tick 평균을 좌우 대표값으로 사용한다.
        left_delta_ticks = (
            delta_ticks[0] + delta_ticks[2]
        ) / 2.0

        right_delta_ticks = (
            delta_ticks[1] + delta_ticks[3]
        ) / 2.0

        left_distance = (
            left_delta_ticks
            * self.meters_per_tick
        )

        right_distance = (
            right_delta_ticks
            * self.meters_per_tick
        )

        center_distance = (
            left_distance + right_distance
        ) / 2.0

        delta_yaw = (
            right_distance - left_distance
        ) / self.wheel_track_m

        midpoint_yaw = (
            self.yaw + delta_yaw / 2.0
        )

        self.x += (
            center_distance
            * math.cos(midpoint_yaw)
        )

        self.y += (
            center_distance
            * math.sin(midpoint_yaw)
        )

        self.yaw = normalize_angle(
            self.yaw + delta_yaw
        )

        dt = max(
            0.001,
            now_sec - self.previous_time_sec,
        )

        linear_velocity = center_distance / dt
        angular_velocity = delta_yaw / dt

        self.previous_ticks = ticks
        self.previous_time_sec = now_sec

        self.latest_linear_velocity = linear_velocity
        self.latest_angular_velocity = angular_velocity

    def publish_timer_callback(self) -> None:
        """최신 위치를 20Hz로 계속 발행한다."""
        if not self.has_encoder_data:
            return

        now = self.get_clock().now()
        now_sec = now.nanoseconds / 1_000_000_000.0

        encoder_is_stale = (
            self.last_encoder_time_sec is None
            or (
                now_sec - self.last_encoder_time_sec
                > self.velocity_timeout_sec
            )
        )

        if encoder_is_stale:
            return

        linear_velocity = self.latest_linear_velocity
        angular_velocity = self.latest_angular_velocity

        self.publish_odometry(
            now=now,
            linear_velocity=linear_velocity,
            angular_velocity=angular_velocity,
        )

    def publish_odometry(
        self,
        now,
        linear_velocity: float,
        angular_velocity: float,
    ) -> None:
        quaternion_z = math.sin(
            self.yaw / 2.0
        )
        quaternion_w = math.cos(
            self.yaw / 2.0
        )

        odometry = Odometry()

        odometry.header.stamp = now.to_msg()
        odometry.header.frame_id = self.odom_frame
        odometry.child_frame_id = self.base_frame

        odometry.pose.pose.position.x = self.x
        odometry.pose.pose.position.y = self.y
        odometry.pose.pose.position.z = 0.0

        odometry.pose.pose.orientation.x = 0.0
        odometry.pose.pose.orientation.y = 0.0
        odometry.pose.pose.orientation.z = quaternion_z
        odometry.pose.pose.orientation.w = quaternion_w

        odometry.twist.twist.linear.x = (
            linear_velocity
        )
        odometry.twist.twist.angular.z = (
            angular_velocity
        )

        # 평면 주행용 초기 공분산.
        odometry.pose.covariance[0] = 0.02
        odometry.pose.covariance[7] = 0.02
        odometry.pose.covariance[14] = 99999.0
        odometry.pose.covariance[21] = 99999.0
        odometry.pose.covariance[28] = 99999.0
        odometry.pose.covariance[35] = 0.05

        odometry.twist.covariance[0] = 0.03
        odometry.twist.covariance[7] = 0.03
        odometry.twist.covariance[14] = 99999.0
        odometry.twist.covariance[21] = 99999.0
        odometry.twist.covariance[28] = 99999.0
        odometry.twist.covariance[35] = 0.08

        self.odom_publisher.publish(odometry)

        transform = TransformStamped()

        transform.header.stamp = now.to_msg()
        transform.header.frame_id = self.odom_frame
        transform.child_frame_id = self.base_frame

        transform.transform.translation.x = self.x
        transform.transform.translation.y = self.y
        transform.transform.translation.z = 0.0

        transform.transform.rotation.x = 0.0
        transform.transform.rotation.y = 0.0
        transform.transform.rotation.z = quaternion_z
        transform.transform.rotation.w = quaternion_w

        self.tf_broadcaster.sendTransform(transform)

        self.published += 1


class WheelOdometryRunner:
    def __init__(self) -> None:
        self.node = None
        self.executor = None
        self.thread = None

    def run(self) -> None:
        rclpy.init(args=None)

        self.node = WheelOdometryNode(
            wheel_diameter_m=env_float("WHEEL_DIAMETER_M", minimum=0.001),
            wheel_track_m=env_float("WHEEL_TRACK_M", minimum=0.001),
            ticks_per_revolution=env_float("ENCODER_TICKS_PER_REV", minimum=1.0),
            wheel_ticks_topic=env_text("WHEEL_TICKS_TOPIC"),
            odom_topic=env_text("ODOM_TOPIC"),
            odom_frame=env_text("ODOM_FRAME"),
            base_frame=env_text("BASE_FRAME"),
        )

        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)

        try:
            self.executor.spin()

        except (KeyboardInterrupt, ExternalShutdownException):
            pass

        finally:
            self.executor.shutdown()
            self.node.destroy_node()

            if rclpy.ok():
                rclpy.shutdown()


if __name__ == "__main__":
    WheelOdometryRunner().run()
