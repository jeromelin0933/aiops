"""Read complete log files or follow newly appended lines."""

import time
from pathlib import Path


class LogReader:
    """A non-duplicating file reader with tail and rotation support."""

    def __init__(self, log_file_path: str, poll_interval_seconds: int = 5):
        self.path = Path(log_file_path)
        self.poll_interval = poll_interval_seconds
        self._offset = 0
        self._inode = -1

    def tail(self):
        """Yield nonblank lines appended after tailing starts."""
        if self.path.exists():
            stat = self.path.stat()
            self._offset = stat.st_size
            self._inode = stat.st_ino

        while True:
            if not self.path.exists():
                time.sleep(self.poll_interval)
                continue

            current_stat = self.path.stat()
            if current_stat.st_ino != self._inode or current_stat.st_size < self._offset:
                self._offset = 0
                self._inode = current_stat.st_ino

            with self.path.open("r", encoding="utf-8", errors="replace") as log_file:
                log_file.seek(self._offset)
                for raw_line in log_file:
                    stripped = raw_line.strip()
                    if stripped:
                        yield stripped
                self._offset = log_file.tell()

            time.sleep(self.poll_interval)

    def read_all(self) -> list:
        """Return every nonblank line without changing tail state."""
        if not self.path.exists():
            return []
        contents = self.path.read_text(encoding="utf-8", errors="replace")
        return [line.strip() for line in contents.splitlines() if line.strip()]
