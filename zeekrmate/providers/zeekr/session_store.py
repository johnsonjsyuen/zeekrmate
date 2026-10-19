"""Persist the Zeekr login session between runs.

The file holds bearer/auth tokens, so it is written atomically (temp file in
the same directory + fsync + os.replace) with mode 0600, and the password
field from export_session() is stripped before writing.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("zeekrmate")

STRIPPED_KEYS = ("password",)


class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Optional[dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("Zeekr session file is unreadable; a fresh login will be used")
            return None
        if not isinstance(data, dict) or not data:
            return None
        for key in STRIPPED_KEYS:
            data.pop(key, None)
        return data

    def save(self, session: Optional[dict]) -> bool:
        if not isinstance(session, dict) or not session:
            return False
        data = {k: v for k, v in session.items() if k not in STRIPPED_KEYS}
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".zeekr_session.", suffix=".tmp", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, sort_keys=True, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        return True

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
