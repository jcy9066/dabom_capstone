import threading
import time

import cv2
import requests
from perception.config import settings
try:
    from perception.env_config import env_float
except ModuleNotFoundError:  # Direct perception script execution.
    from env_config import env_float


class TelegramNotifier:
    def __init__(
        self,
        token=None,
        chat_id=None,
        cooldown_sec=None,
        sender=None,
        clock=None,
    ):
        self.token = settings.TELEGRAM_TOKEN if token is None else token
        self.chat_id = settings.TELEGRAM_CHAT_ID if chat_id is None else chat_id
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.cooldown_sec = max(
            0.0,
            env_float("TELEGRAM_ALERT_COOLDOWN_SEC", minimum=0.0)
            if cooldown_sec is None
            else float(cooldown_sec),
        )
        self.sender = sender or requests.post
        self.clock = clock or time.monotonic
        self._cooldowns = {}
        self._cooldown_lock = threading.Lock()

    def send_alert_async(self, message, frame=None):
        """Send a legacy, non-event notification without an automatic cooldown."""
        return self._start_send(message, frame)

    def send_event_alert_async(self, message, *, robot_id, event_type):
        """Send an automatic text alert with per-robot/event cooldown."""
        key = (str(robot_id), str(event_type))
        now = self.clock()
        with self._cooldown_lock:
            previous = self._cooldowns.get(key)
            if previous is not None and now - previous < self.cooldown_sec:
                return False
            self._cooldowns[key] = now

        # Raw camera frames are never attached to automatic alerts. A visual
        # attachment must first pass through the persistence privacy processor.
        self._start_send(message, None)
        return True

    def _start_send(self, message, frame):
        thread = threading.Thread(target=self._send_process, args=(message, frame))
        thread.daemon = True
        thread.start()
        return thread

    def _send_process(self, message, frame):
        try:
            if not self.token or not self.chat_id:
                raise RuntimeError("Telegram configuration is incomplete.")
            if frame is not None:
                ok, buffer = cv2.imencode(".jpg", frame)
                if not ok:
                    raise RuntimeError("Telegram image encoding failed.")
                response = self.sender(
                    f"{self.base_url}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": message},
                    files={"photo": buffer.tobytes()},
                    timeout=5,
                )
            else:
                response = self.sender(
                    f"{self.base_url}/sendMessage",
                    data={"chat_id": self.chat_id, "text": message},
                    timeout=5,
                )
            if getattr(response, "status_code", 200) != 200:
                raise RuntimeError(
                    f"Telegram returned HTTP {getattr(response, 'status_code', 'unknown')}"
                )
        except Exception as exc:
            print(f"Telegram send failed: {exc}")
