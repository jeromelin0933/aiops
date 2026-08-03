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
        self._started = False

    def read_new_lines_once(self) -> list[str]:
        """Return currently available appended lines without blocking or sleeping."""
        if not self._started:
            self._started = True
            try:
                initial_stat = self.path.stat()
            except FileNotFoundError:
                return []
            self._offset = initial_stat.st_size
            self._inode = initial_stat.st_ino
            return []

        try:
            current_stat = self.path.stat()
        except FileNotFoundError:
            return []

        if current_stat.st_ino != self._inode or current_stat.st_size < self._offset:
            self._offset = 0
            self._inode = current_stat.st_ino

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as log_file:
                log_file.seek(self._offset)
                lines = [raw_line.strip() for raw_line in log_file if raw_line.strip()]
                self._offset = log_file.tell()
        except FileNotFoundError:
            return []

        return lines

    def tail(self):
        """Yield nonblank lines appended after tailing starts."""
        while True:
            yield from self.read_new_lines_once()
            time.sleep(self.poll_interval)

    def read_all(self) -> list:
        """Return every nonblank line without changing tail state."""
        if not self.path.exists():
            return []
        contents = self.path.read_text(encoding="utf-8", errors="replace")
        return [line.strip() for line in contents.splitlines() if line.strip()]
