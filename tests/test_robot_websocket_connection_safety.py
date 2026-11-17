"""Regression tests for robot WebSocket connection identity and pending ACKs."""

import asyncio
from types import SimpleNamespace

from server.app import RobotConnectionManager


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.accepted = False
        self.closed = False
        self.sent = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def close(self):
        self.closed = True

    async def send_json(self, message):
        self.messages.append(dict(message))
        self.sent.set()


def test_replaced_connection_cannot_ack_new_command():
    async def scenario():
        manager = RobotConnectionManager()
        old = FakeWebSocket()
        new = FakeWebSocket()
        await manager.connect("pi-01", old)
        await manager.connect("pi-01", new)
        assert old.closed
        assert not await manager.is_current("pi-01", old)
        task = asyncio.create_task(manager.send_command_wait_ack(
            "pi-01", {"type": "move", "command_id": "new-command"}
        ))
        await asyncio.wait_for(new.sent.wait(), 1)
        assert not await manager.receive_ack(
            "pi-01", old, {"command_id": "new-command", "ok": True}
        )
        assert not task.done()
        assert await manager.receive_ack(
            "pi-01", new, {"command_id": "new-command", "ok": True}
        )
        assert await task
    asyncio.run(scenario())


def test_reconnection_fails_old_pending_ack():
    async def scenario():
        manager = RobotConnectionManager()
        old = FakeWebSocket()
        new = FakeWebSocket()
        await manager.connect("pi-01", old)
        task = asyncio.create_task(manager.send_command_wait_ack(
            "pi-01", {"type": "move", "command_id": "old-command"}
        ))
        await asyncio.wait_for(old.sent.wait(), 1)
        await manager.connect("pi-01", new)
        assert await task is False
        assert not await manager.receive_ack(
            "pi-01", old, {"command_id": "old-command", "ok": True}
        )
    asyncio.run(scenario())


def test_disconnecting_old_socket_does_not_drop_new_socket():
    async def scenario():
        manager = RobotConnectionManager()
        old = FakeWebSocket()
        new = FakeWebSocket()
        await manager.connect("pi-01", old)
        await manager.connect("pi-01", new)
        assert not await manager.disconnect("pi-01", old)
        assert await manager.is_current("pi-01", new)
        assert await manager.is_connected("pi-01")
    asyncio.run(scenario())


def test_blocked_socket_send_has_deadline_and_does_not_hold_global_lock():
    async def scenario():
        manager = RobotConnectionManager()
        manager.ack_timeout_sec = 0.06
        manager.send_timeout_sec = 0.06
        class BlockedSocket(FakeWebSocket):
            async def send_json(self, message):
                await asyncio.Event().wait()
        blocked = BlockedSocket()
        other = FakeWebSocket()
        await manager.connect("pi-01", blocked)
        await manager.connect("pi-02", other)
        blocked_task = asyncio.create_task(
            manager.send_command_wait_ack("pi-01", {"type": "emergency_stop"})
        )
        await asyncio.sleep(0.01)
        assert await asyncio.wait_for(manager.is_current("pi-01", blocked), 0.05)
        assert await asyncio.wait_for(
            manager.send_command("pi-02", {"type": "beep"}), 0.05
        )
        assert await asyncio.wait_for(blocked_task, 0.20) is False
        assert manager.pending_acks == {}
    asyncio.run(scenario())


def test_replaced_socket_cannot_report_a_late_send_as_success():
    async def scenario():
        manager = RobotConnectionManager()
        manager.send_timeout_sec = 0.10
        first = FakeWebSocket()
        latest = FakeWebSocket()
        gate = asyncio.Event()
        first.send_json = lambda message: gate.wait()
        await manager.connect("pi-01", first)
        send = asyncio.create_task(manager.send_command("pi-01", {"type": "led"}))
        await asyncio.sleep(0.01)
        await manager.connect("pi-01", latest)
        gate.set()
        assert await send is False
        assert await manager.send_command("pi-01", {"type": "beep"})
        assert len(latest.messages) == 1
    asyncio.run(scenario())


def test_same_robot_sends_are_serialized():
    async def scenario():
        manager = RobotConnectionManager()
        manager.send_timeout_sec = 0.15
        gate = asyncio.Event()
        in_flight = 0
        peak = 0
        class TrackingSocket(FakeWebSocket):
            async def send_json(self, message):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    if message["type"] == "move":
                        await gate.wait()
                    await super().send_json(message)
                finally:
                    in_flight -= 1
        ws = TrackingSocket()
        await manager.connect("pi-01", ws)
        first = asyncio.create_task(manager.send_command("pi-01", {"type":"move"}))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(manager.send_command("pi-01", {"type":"stop"}))
        await asyncio.sleep(0.01)
        assert not second.done()
        gate.set()
        assert await first and await second
        assert peak == 1
        assert [msg["type"] for msg in ws.messages] == ["move", "stop"]
    asyncio.run(scenario())
