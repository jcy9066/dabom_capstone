import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch


RASPBERRY_DIR = Path(__file__).resolve().parents[1] / "raspberry"
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))

import robot_command_client as client_module  # noqa: E402
from robot_command_client import RobotCommandClient  # noqa: E402


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class RobotCommandClientTests(unittest.IsolatedAsyncioTestCase):
    def configured_client(self):
        args = Namespace(
            server_base_url="https://server.example",
            robot_id="robot test",
            control_token="token with spaces",
            serial_port="test-serial",
            serial_baudrate=115200,
            serial_timeout_sec=0.25,
            command_timeout_sec=0.45,
            max_wheel_mps=0.5,
            status_interval_sec=0.001,
            status_request_timeout_sec=0.75,
            ws_reconnect_delay_sec=1.0,
            encoder_interval_sec=0.05,
        )
        motor = Mock()
        motor.connected = True
        motor.current_motion = "stop"
        with patch.object(client_module, "MotorController", return_value=motor), patch.object(
            client_module, "SpeakerController", return_value=Mock()
        ):
            return RobotCommandClient(args)

    def make_client(self):
        client = RobotCommandClient.__new__(RobotCommandClient)
        client.current_mode = "manual"
        client.navigation_mode = "mapping"
        client.emergency_stop_latched = False
        client.led_enabled = False
        client._led_task = None
        client.motor = Mock()
        client.motor.current_motion = "stop"
        client.motor.connected = True
        client.speaker = Mock()
        return client

    async def test_emergency_stop_is_latched_until_resume(self):
        client = self.make_client()
        socket = FakeWebSocket()

        await client.handle_command(socket, {"type": "emergency_stop", "command_id": "stop"})
        self.assertTrue(client.emergency_stop_latched)
        self.assertTrue(socket.messages[-1]["ok"])

        client.current_mode = "auto"
        await client.handle_command(
            socket,
            {"type": "auto_drive", "left_mps": 0.1, "right_mps": 0.1, "command_id": "drive"},
        )
        self.assertFalse(socket.messages[-1]["ok"])
        client.motor.drive.assert_not_called()

        await client.handle_command(socket, {"type": "resume_navigation", "command_id": "resume"})
        self.assertFalse(client.emergency_stop_latched)
        self.assertTrue(socket.messages[-1]["ok"])

    async def test_navigation_mode_does_not_change_auto_manual_mode(self):
        client = self.make_client()
        socket = FakeWebSocket()
        await client.handle_command(
            socket,
            {"type": "navigation_mode", "mode": "driving", "command_id": "mode"},
        )
        self.assertEqual("driving", client.navigation_mode)
        self.assertEqual("manual", client.current_mode)
        self.assertTrue(socket.messages[-1]["ok"])

    async def test_removed_camera_config_command_is_rejected(self):
        client = self.make_client()
        socket = FakeWebSocket()
        await client.handle_command(
            socket,
            {"type": "camera_config", "command_id": "legacy-camera"},
        )
        self.assertFalse(socket.messages[-1]["ok"])
        self.assertIn("unknown command type", socket.messages[-1]["error"])

    async def test_beep_led_and_warning_use_pico_contract_and_existing_speaker(self):
        client = self.make_client()
        socket = FakeWebSocket()

        await client.handle_command(
            socket,
            {"type": "beep", "duration_ms": 350, "command_id": "beep"},
        )
        client.motor.beep.assert_called_once_with(350)
        self.assertTrue(socket.messages[-1]["ok"])

        await client.handle_command(
            socket,
            {"type": "led", "enabled": True, "duration_ms": 0, "command_id": "led"},
        )
        client.motor.set_led.assert_called_with(True)
        self.assertTrue(client.led_enabled)

        await client.handle_command(
            socket,
            {"type": "warning", "text": "warning", "led_duration_ms": 0, "command_id": "warning"},
        )
        client.motor.set_led.assert_called_with(True)
        client.speaker.speak.assert_called_with("warning")
        self.assertTrue(socket.messages[-1]["ok"])

    def test_duration_rejects_fractional_values(self):
        with self.assertRaisesRegex(RuntimeError, "integer"):
            RobotCommandClient._duration_ms(1.5)

    def test_command_websocket_is_derived_without_token_in_url(self):
        client = self.configured_client()

        self.assertEqual(
            "wss://server.example/ws/robot/robot%20test",
            client.ws_url,
        )
        source = Path(client_module.__file__).read_text(encoding="utf-8")
        self.assertIn(
            'extra_headers={"X-Robot-Control-Token": self.control_token}',
            source,
        )

    def test_status_payload_has_no_battery_field(self):
        payload = self.configured_client().status_payload()
        self.assertNotIn("battery", payload)
        self.assertNotIn("battery_level", payload)

    def test_status_payload_uses_measured_system_metrics(self):
        client = self.configured_client()
        client._server_reachable = True
        client._last_status_latency_ms = 12.4
        with patch.object(client, "_cpu_usage_percent", return_value=23.5), patch.object(
            client, "_cpu_temp_c", return_value=48.2
        ), patch.object(client, "_ram_usage_percent", return_value=41.7):
            payload = client.status_payload()

        self.assertEqual(23.5, payload["cpu_usage"])
        self.assertEqual(48.2, payload["cpu_temp"])
        self.assertEqual(41.7, payload["ram_usage"])
        self.assertEqual("ok", payload["internet"])
        self.assertEqual(12.4, payload["ping"])

    def test_status_request_uses_robot_control_token_header(self):
        client = self.configured_client()

        def stop_after_post(*args, **kwargs):
            client.running = False
            return Mock(status_code=200)

        with patch.object(client_module.requests, "post", side_effect=stop_after_post) as post:
            client.status_loop()

        self.assertEqual(
            {"X-Robot-Control-Token": "token with spaces"},
            post.call_args.kwargs["headers"],
        )
        self.assertEqual(0.75, post.call_args.kwargs["timeout"])
        self.assertTrue(client._server_reachable)
        self.assertIsNotNone(client._last_status_latency_ms)


if __name__ == "__main__":
    unittest.main()
