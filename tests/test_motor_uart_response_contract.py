"""Pico UART replies must prove the requested motor command executed."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "raspberry/controllers/motor_controller.py"


def _validator():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "MotorController")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_valid_uart_ack")
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ClassDef(name="OfflineMotor", bases=[], keywords=[],
                     body=[method], decorator_list=[]),
    ], type_ignores=[]))
    env = {}
    exec(compile(module, str(SRC), "exec"), env)
    return env["OfflineMotor"]._valid_uart_ack


def test_stop_requires_exact_pico_stop_ack():
    valid = _validator()
    assert valid("STOP,timeout", "OK,STOP")
    for answer in ("OK", "OK,PONG", "OK,DRIVE", "OK,STOP,1",
                   "OK,STOP\nOK,DRIVE", "ERR,STOP", ""):
        assert not valid("STOP,timeout", answer), answer


def test_move_drive_and_reconfiguration_ack_cannot_cross_match():
    valid = _validator()
    cases = [
        ("PING", "OK,PONG"),
        ("FW_INFO", "OK,FW,MOTOR_SAFE_V1"),
        ("DRIVE,0.1,-0.1", "OK,DRIVE"),
        ("MOVE,forward,0.200", "OK,MOVE,forward"),
        ("ENC_STREAM,1", "OK,ENC_STREAM,1"),
        ("ENC_RESET", "OK,ENC_RESET"),
        ("LED,0", "OK,LED,0"),
        ("BEEP,350", "OK,BEEP,350"),
        ("ENC_GET", "OK,ENC,1,-2,3,4,500"),
    ]
    for cmd, ack in cases:
        assert valid(cmd, ack)
        assert not valid(cmd, "OK,STOP") if not cmd.startswith("STOP") else True
    assert not valid("MOVE,forward,0.200", "OK,MOVE,backward")
    assert not valid("LED,1", "OK,LED,0")
    assert not valid("ENC_GET", "OK,ENC,one,2,3,4,5")


def test_exchange_fails_closed_on_mismatched_response():
    source = SRC.read_text(encoding="utf-8")
    part = source.split("    def _exchange_locked(", 1)[1].split(
        "    def _ensure_connected_locked(", 1
    )[0]
    assert "if not self._valid_uart_ack(command, response):" in part
    assert "self._close_serial_locked()" in part
    assert "raise MotorControllerError(" in part
