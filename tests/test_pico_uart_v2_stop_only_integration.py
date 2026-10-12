"""Compatibility regression: new negotiated C tests supersede legacy unbound STOP."""
from pathlib import Path

def test_stop_only_host_c_contract():
    from test_pico_uart_v2_session_c import test_state_machine_c_contract
    test_state_machine_c_contract()

def test_firmware_integration_disabled_by_default():
    main = (Path(__file__).resolve().parents[1] / 'raspberry/pico_w_sdk/main.c').read_text()
    assert '#ifdef DABOM_V2_STOP_ONLY_EXPERIMENTAL' in main
    assert 'uart_v2_stop_only(line, response, sizeof(response), stop_motors)' in main
    assert 'ERR,V2_LEGACY_DISABLED' in main
