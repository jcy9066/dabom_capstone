"""Host-only Pi session handshake against FakeSerial, never a device."""
from collections import deque
import queue
import threading

import pytest

from raspberry.controllers.motor_controller import MotorController, MotorControllerError
from raspberry.controllers.pico_uart_protocol_v2 import RequestSequencer
from raspberry.controllers.pico_uart_v2_router import V2AckRouter


class FakeSerial:
    def __init__(self, responder=None, threaded=False):
        self.is_open = True
        self.writes = []
        self.responder = responder
        self.threaded = threaded
        self.incoming = queue.Queue()
        self.controller = None

    def write(self, data):
        assert self.is_open
        self.writes.append(data)
        if self.responder:
            response = self.responder(data.decode('ascii').strip())
            if response is not None:
                if isinstance(response, str):
                    response = [response]
                for line in response:
                    if self.threaded:
                        self.incoming.put((line+'\n').encode('ascii'))
                    else:
                        self.controller._handle_uart_line(line)
        return len(data)

    def readline(self):
        try:
            return self.incoming.get(timeout=0.01)
        except queue.Empty:
            return b''

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class SimulatedPico:
    def __init__(self):
        self.boot = 'AA11223344556677'
        self.session = None
        self.last_id = 0
        self.inject = None

    def __call__(self, frame):
        if self.inject is not None:
            return self.inject(frame)
        parts = frame.split(',')
        assert parts[0]=='REQ'
        _, session, request_id, command, *args = parts
        rid = int(request_id)
        if command == 'DISCOVER':
            status, payload='OK','BOOT_'+self.boot
        elif command=='HELLO' and args==[self.boot] and rid==2:
            if self.session == session:
                status,payload='ERR','STALE_ID'
            else:
                self.session,last_id=session,rid
                self.last_id=rid
                status,payload='OK','HELLO'
        elif command in ('STOP','PING') and session==self.session and rid>self.last_id:
            self.last_id=rid
            status,payload='OK','STOP' if command=='STOP' else 'PONG'
        else:
            status,payload='ERR','NO_SESSION'
        return f'ACK,{session},{request_id},{status},{payload}'


def controller_with_fake(pico=None, threaded=False, allow=True):
    controller = MotorController.__new__(MotorController)
    controller._command_lock=threading.RLock()
    controller._write_lock=threading.Lock()
    controller._state_lock=threading.RLock()
    controller._response_condition=threading.Condition()
    controller._response_queue=deque()
    controller._pico_reboot_event=threading.Event()
    controller._reader_running=threaded
    controller._reader_thread=None
    serial=FakeSerial(pico,threaded=threaded)
    serial.controller=controller
    controller._serial=serial
    controller.serial_timeout_sec=0.09
    controller._v2_router=V2AckRouter()
    controller._v2_sequence=RequestSequencer(session='TESTSESSION')
    controller._v2_negotiated=False
    controller._v2_host_test_enabled=allow
    controller.current_motion='stop'
    controller.led_enabled=False
    if threaded:
        thread=threading.Thread(target=controller._reader_loop,args=(serial,),daemon=True)
        controller._reader_thread=thread
        thread.start()
    return controller,serial


def test_disabled_by_default_never_writes():
    c,s=controller_with_fake(SimulatedPico(),allow=False)
    with pytest.raises(MotorControllerError,match='disabled'):
        c.experimental_v2_negotiate()
    with pytest.raises(MotorControllerError,match='disabled'):
        c.experimental_v2_exchange('STOP')
    assert s.writes==[]


def test_session_required_before_stop():
    c,s=controller_with_fake(SimulatedPico())
    with pytest.raises(MotorControllerError,match='HELLO'):
        c.experimental_v2_exchange('STOP')
    assert s.writes==[]


def test_handshake_stop_ping_and_replay_id():
    pico=SimulatedPico()
    c,s=controller_with_fake(pico)
    assert c.experimental_v2_negotiate()=='TESTSESSION'
    assert c.experimental_v2_exchange('PING')=='PONG'
    assert c.experimental_v2_exchange('STOP')=='STOP'
    assert [w.decode().strip().split(',')[3] for w in s.writes] == ['DISCOVER','HELLO','PING','STOP']
    assert [w.decode().strip().split(',')[2] for w in s.writes] == ['1','2','3','4']
    with pytest.raises(MotorControllerError,match='already negotiated'):
        c.experimental_v2_negotiate()


def test_same_flow_with_single_reader_thread():
    c,s=controller_with_fake(SimulatedPico(),threaded=True)
    try:
        c.experimental_v2_negotiate()
        assert c.experimental_v2_exchange('STOP')=='STOP'
    finally:
        c._close_serial_locked()
    assert not s.is_open


@pytest.mark.parametrize('bad', [
    'ACK,OTHER,1,OK,BOOT_AA11223344556677',
    'ACK,TESTSESSION,2,OK,BOOT_AA11223344556677',
    'ACK,TESTSESSION,1,OK,BOOT_111111111111111Z',
    'ACK,TESTSESSION,1,ERR,NO_SESSION',
])
def test_discover_error_invalidates_link(bad):
    pico=SimulatedPico()
    pico.inject=lambda _:bad
    c,s=controller_with_fake(pico)
    with pytest.raises(MotorControllerError):
        c.experimental_v2_negotiate()
    assert not s.is_open
    assert not c._v2_negotiated


def test_hello_old_ack_detected():
    pico=SimulatedPico()
    def respond(frame):
        if ',HELLO,' in frame:
            return 'ACK,TESTSESSION,1,OK,HELLO'
        return pico(frame)
    c,s=controller_with_fake(respond)
    with pytest.raises(MotorControllerError):
        c.experimental_v2_negotiate()
    assert not s.is_open


def test_ready_mid_handshake_invalidates():
    pico=SimulatedPico()
    def respond(frame):
        if ',HELLO,' in frame:
            return 'READY,PICO_W_MOTOR_ENCODER'
        return pico(frame)
    c,s=controller_with_fake(respond)
    with pytest.raises(MotorControllerError):
        c.experimental_v2_negotiate()
    assert c._pico_reboot_event.is_set()
    assert not s.is_open


def test_timeout_during_hello_closes_uart():
    pico=SimulatedPico()
    c,s=controller_with_fake(lambda f:None if ',HELLO,' in f else pico(f))
    with pytest.raises(MotorControllerError,match='timeout|rejected'):
        c.experimental_v2_negotiate()
    assert not s.is_open


def test_old_ack_during_new_stop_is_rejected():
    pico=SimulatedPico()
    c,s=controller_with_fake(pico)
    c.experimental_v2_negotiate()
    assert c.experimental_v2_exchange('PING')=='PONG'
    pico.inject=lambda f:'ACK,TESTSESSION,3,OK,PONG'
    with pytest.raises(MotorControllerError):
        c.experimental_v2_exchange('STOP')
    assert not s.is_open


def test_no_move_drive_even_after_negotiation():
    c,s=controller_with_fake(SimulatedPico())
    c.experimental_v2_negotiate()
    count=len(s.writes)
    for cmd in ['MOVE','DRIVE','DRIVE,1,1','LED']:
        with pytest.raises(MotorControllerError,match='prohibited'):
            c.experimental_v2_exchange(cmd)
    assert len(s.writes)==count


def test_v1_ready_recovery_requires_fresh_connection():
    c,s=controller_with_fake(SimulatedPico())
    c._handle_uart_line('READY,PICO_W_MOTOR_ENCODER')
    assert c._pico_reboot_event.is_set()
    with pytest.raises(MotorControllerError,match='reboot'):
        c._exchange_locked('PING')
    assert s.writes==[]


def test_v1_ready_arriving_during_exchange_aborts_without_legacy_ack():
    c,s=controller_with_fake(lambda _:'READY,PICO_W_MOTOR_ENCODER')
    with pytest.raises(MotorControllerError,match='reboot'):
        c._exchange_locked('PING')
    assert s.writes == [b'PING\n']
    assert c._pico_reboot_event.is_set()


def test_v1_recovery_reopens_fresh_serial(monkeypatch):
    import types
    import raspberry.controllers.motor_controller as module
    c, old = controller_with_fake(lambda _:None)
    c._handle_uart_line('READY,PICO_W_MOTOR_ENCODER')
    assert c._pico_reboot_event.is_set()
    made=[]
    def new_port(**kwargs):
        def response(cmd):
            if cmd == 'PING': return 'OK,PONG'
            if cmd.startswith('STOP,'): return 'OK,STOP'
            if cmd == 'LED,0': return 'OK,LED,0'
            if cmd.startswith('ENC_STREAM,'): return 'OK,ENC_STREAM,1'
            raise AssertionError('Unexpected V1 recovery command: '+cmd)
        s=FakeSerial(response,threaded=True)
        s.controller=c
        s.reset_input_buffer=lambda:None
        made.append(s)
        return s
    monkeypatch.setattr(module,'serial',types.SimpleNamespace(Serial=new_port))
    # Keep actual short reader concurrency; no hardware is involved.
    c.serial_port='/dev/FAKE-NO-DEVICE'
    c.baudrate=115200
    c.recover_after_pico_reboot()
    assert not old.is_open
    assert not c._pico_reboot_event.is_set()
    assert c.connected
    assert c._v2_negotiated is False
    assert [x.decode().strip() for x in made[0].writes] == [
        'ENC_STREAM,0','PING','STOP,pi_connected',
        'LED,0','ENC_STREAM,1','STOP,pico_reboot_recovery',
        'LED,0','ENC_STREAM,1'
    ]
    c._close_serial_locked()


def test_default_pi_session_is_128_bit_random_hex():
    seq = RequestSequencer()
    assert len(seq.session) == 32
    assert all(ch in '0123456789ABCDEF' for ch in seq.session)
