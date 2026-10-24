from server.env_config import env_float, env_int, env_text

CAMERA_TIMEOUT_SEC = env_float("CAMERA_TIMEOUT_SEC", minimum=0.1)
ROBOT_STATUS_TIMEOUT_SEC = env_float("ROBOT_STATUS_TIMEOUT_SEC", minimum=0.1)

STATUS_FRAME_WIDTH = env_int("STREAM_WIDTH", minimum=1)
STATUS_FRAME_HEIGHT = env_int("STREAM_HEIGHT", minimum=1)

TELEGRAM_TOKEN = env_text("TELEGRAM_TOKEN", allow_empty=True)
TELEGRAM_CHAT_ID = env_text("TELEGRAM_CHAT_ID", allow_empty=True)
