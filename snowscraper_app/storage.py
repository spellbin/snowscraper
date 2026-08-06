"""Small, shared persistence helpers for Snow Scraper configuration files.

Configuration is written with a temporary file followed by ``os.replace``.
That replacement is atomic on the target filesystem, so a power loss is much
less likely to leave a partially written JSON or text file on the Raspberry
Pi's SD card.  Callers remain responsible for choosing the file format and for
handling any exception raised by a failed write.
"""

import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(content: str, path: str) -> None:
    """Replace ``path`` atomically with UTF-8 text.

    The temporary file is created beside the destination.  This is important:
    ``os.replace`` is only guaranteed to be atomic when both paths are on the
    same filesystem.  ``fsync`` asks the operating system to flush the file
    contents before the final rename.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f"{target.name}.", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target)
    finally:
        # ``os.replace`` removes the temporary pathname on success.  On an
        # earlier failure, best-effort cleanup prevents stale files building up.
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def atomic_write_json(payload, path: str, *, indent=None) -> None:
    """Serialize ``payload`` as JSON and atomically replace ``path``."""
    atomic_write_text(json.dumps(payload, indent=indent), path)
