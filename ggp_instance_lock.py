"""Cross-platform OS-backed single-instance lock for the GGP trading runtime."""
from __future__ import annotations

import os
from typing import Optional, BinaryIO


def acquire_instance_lock(path: str) -> Optional[BinaryIO]:
    """Return a held file handle, or None when another process owns the lock.

    The OS releases the lock automatically on process termination, so a stale
    lock file never blocks a later start.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return None
        return handle
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        raise
