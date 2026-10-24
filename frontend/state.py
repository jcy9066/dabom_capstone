import time

# 영상 프레임 및 로봇 상태를 저장할 전역 변수
current_frame = None
last_frame_at = None
server_started_at = time.time()

status_frame_cache = {
    "key": None,
    "frame": None,
}

robot_status = {
    "cpu_usage": "0.0",
    "cpu_temp": "0.0",
    "ram_usage": "0.0",
    "internet": "원활",
    "updated_at": None,
}
