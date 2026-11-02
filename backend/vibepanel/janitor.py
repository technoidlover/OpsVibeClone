"""Dọn rác Docker định kỳ để ổ đĩa VPS không đầy dần.

Xóa service/database thì panel gỡ ngay container, image, volume của nó. Còn những
thứ tích lại theo thời gian thì dọn ở đây, mỗi ngày một lần:
- build cache của Nixpacks/BuildKit cũ hơn 7 ngày (build sau vẫn dùng cache mới);
- image treo (không còn tag, sinh ra khi build lại cùng tên);
- container do panel tạo đã dừng.
Không bao giờ xóa volume (dữ liệu) hay thứ panel không tạo ra.
"""

import logging
import threading

logger = logging.getLogger(__name__)

FIRST_RUN_DELAY = 15 * 60
INTERVAL = 24 * 3600
BUILD_CACHE_UNTIL = "168h"


class Janitor:
    def __init__(self, docker):
        self.docker = docker
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[str]:
        results = self.docker.prune(BUILD_CACHE_UNTIL)
        for line in results:
            logger.info("Dọn rác: %s", line)
        return results

    def _loop(self) -> None:
        delay = FIRST_RUN_DELAY
        while not self._stop.wait(delay):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - dọn rác lỗi không được làm sập panel
                logger.exception("Dọn rác Docker thất bại")
            delay = INTERVAL

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="opsvibes-janitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
