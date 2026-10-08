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
