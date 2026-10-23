from raspberry.lidar_recovery_policy import restart_backoff


def test_lidar_recovery_backoff_is_bounded():
    assert restart_backoff(1) == 1.0
    assert restart_backoff(2) == 2.0
    assert restart_backoff(3) == 4.0
    assert restart_backoff(100) == 10.0


def test_lidar_recovery_backoff_handles_custom_limits():
    assert restart_backoff(2, base_delay=2.0, max_delay=3.0) == 3.0
