import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped

from server.navigation_ros_control import (
    NavigationRosControl,
    NavigationRosError,
)


def make_nav(completed):
    nav = NavigationRosControl.__new__(NavigationRosControl)
    nav._lock = threading.RLock()
    nav._cancel_lock = threading.RLock()
    nav._navigation_state = "NAVIGATING"
    nav._navigation_error = None
    nav._require_started = lambda: None

    result_future = Future()
    if completed:
        result_future.set_result(
            SimpleNamespace(status=int(GoalStatus.STATUS_ABORTED))
        )

    cancel_future = Future()
    cancel_future.set_result(
        SimpleNamespace(goals_canceling=[])
    )

    handle = SimpleNamespace(
        cancel_goal_async=lambda: cancel_future
    )
    nav._navigate_goal_handle = handle
    nav._navigate_result_future = result_future

    return nav, handle, result_future


class NavigationGoalReconciliationTests(unittest.TestCase):

    def test_completed_goal_cancel_is_idempotent(self):
        nav, handle, future = make_nav(completed=True)

        with patch(
            "server.navigation_ros_control.env_float",
            return_value=0.1
        ):
            outcome = nav.cancel_navigation()

        self.assertTrue(outcome["confirmed"])
        self.assertEqual("FAILED", outcome["terminal"])
        self.assertIsNone(nav._navigate_goal_handle)

    def test_new_goal_after_delayed_callback(self):
        nav, old_handle, old_future = make_nav(completed=True)

        new_result = Future()
        new_handle = SimpleNamespace(
            accepted=True,
            get_result_async=lambda: new_result
        )

        accepted_future = Future()
        accepted_future.set_result(new_handle)

        nav._navigate_client = SimpleNamespace(
            wait_for_server=lambda timeout_sec: True,
            send_goal_async=lambda request: accepted_future
        )
        nav._pose_stamped = lambda goal: PoseStamped()

        result = nav.navigate_to_pose(
            {"x": 0.0, "y": 0.0, "yaw": 0.0}
        )

        self.assertTrue(result["accepted"])
        self.assertIs(nav._navigate_goal_handle, new_handle)

        nav._on_navigation_result(old_handle, old_future)
        self.assertIs(nav._navigate_goal_handle, new_handle)
        self.assertEqual("NAVIGATING", nav.navigation_status()["state"])

    def test_pending_goal_cancel_remains_protected(self):
        nav, handle, future = make_nav(completed=False)

        with patch(
            "server.navigation_ros_control.env_float",
            return_value=0.1
        ):
            with self.assertRaises(NavigationRosError) as caught:
                nav.cancel_navigation()

        self.assertEqual(
            "NAVIGATION_CANCEL_REJECTED",
            caught.exception.error_code
        )
        self.assertIs(nav._navigate_goal_handle, handle)

    def test_pending_goal_cannot_be_overwritten(self):
        nav, handle, future = make_nav(completed=False)

        with self.assertRaises(NavigationRosError) as caught:
            nav.navigate_to_pose(
                {"x": 0.0, "y": 0.0, "yaw": 0.0}
            )

        self.assertEqual(
            "NAVIGATION_ALREADY_ACTIVE",
            caught.exception.error_code
        )
        self.assertIs(nav._navigate_goal_handle, handle)


if __name__ == "__main__":
    unittest.main()
