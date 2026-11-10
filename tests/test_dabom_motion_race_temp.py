import asyncio
import importlib.util
import time
from pathlib import Path

test_file = Path.cwd() / "tests/test_navigation_control_api.py"
spec = importlib.util.spec_from_file_location("nav_fixture", test_file)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_encoder_changes_between_permission_and_ack():
    async def scenario():
        fixture = module.NavigationControlTests()
        await fixture.asyncSetUp()

        try:
            api = fixture.api
            now = time.time()

            # Pi가 연결돼 있고, 정지 확인이 완료된 MANUAL 상태를 구성.
            api.note_pi_status({
                "mode": "manual",
                "navigation_mode": "driving",
                "emergency_stop": False,
                "safety_session": "test-pi",
                "safety_epoch": 0,
            }, now=now)

            with api._lock:
                api._state["navigation_state"] = "READY"
                api._state["emergency_stop"] = False
                api._stop_commanded_at = now
                api._stop_encoder_ticks = (0, 0, 0, 0)
                api._encoder_sequence = 10
                api._stop_confirmation_after_sequence = 10
                api._stop_confirmation_ticks = (0, 0, 0, 0)
                api._stop_confirmation_started_at = now - 0.2
                api._stop_confirmation_last_at = now
                api._stop_confirmation_samples = 5

            context = api.motion_safety_context()
            assert context is not None, "MOVE 전 안전 승인 자체에 실패했습니다."

            # MOVE ACK 전에 실제 바퀴가 1틱 움직였다고 가정.
            api.note_encoder({
                "sequence": 11,
                "left_front_ticks": 1,
                "right_front_ticks": 1,
                "left_rear_ticks": 1,
                "right_rear_ticks": 1,
            })

            with api._lock:
                stop_confirmed = api._stop_confirmed_locked(time.time())
                violation = api._encoder_stop_violation

            accepted = api.note_authorized_motion(context)

            print(
                f"\nMOVE 전 승인: 성공"
                f"\nEncoder 위반: {violation}"
                f"\nACK 시점 STOP 안정 판정: {stop_confirmed}"
                f"\nACK 후 주행 승인: {accepted}"
            )

            # 현재 코드에서 의심되는 경합이 재현되는지 확인.
            assert violation is False
            assert stop_confirmed is False
            assert accepted is False
        finally:
            fixture.doCleanups()

    asyncio.run(scenario())
