"""Seekable encrypted session data. Keys and original names never go to disk."""

from __future__ import annotations

import io
import os
import secrets
import shutil
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK = 1024 * 1024
MEMORY_BUDGET = 512 * 1024 * 1024
DISK_BUDGET = 20 * 1024**3
DISK_RESERVE = 5 * 1024**3
_quota_lock = threading.RLock()


class ResourceUnavailable(ValueError):
    def __init__(self, message, *, issue=None):
        super().__init__(message)
        self.issue = issue or {"resource": "disk", "code": "disk_capacity", "stage": "cache"}


@contextmanager
def file_lock(stream, blocking=True):
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass
class Blob:
    size: int
    data: bytes | None = field(default=None, repr=False)
    path: str | None = None
    key: bytes | None = field(default=None, repr=False)

    def open(self):
        return io.BytesIO(self.data) if self.data is not None else BlobReader(self)

    def remove(self):
        if self.path:
            Path(self.path).unlink(missing_ok=True)


class BlobReader(io.RawIOBase):
    def __init__(self, blob):
        self.blob, self.position = blob, 0
        self.stream = open(blob.path, "rb")  # noqa: SIM115 - owned by this seekable reader's close()
        self.aes = AESGCM(blob.key)
        self.blocks = OrderedDict()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        value = offset + (self.position if whence == 1 else self.blob.size if whence == 2 else 0)
        if value < 0 or whence not in (0, 1, 2):
            raise ValueError("invalid seek")
        self.position = value
        return value

    def read(self, size=-1):
        size = min(self.blob.size - self.position, size if size >= 0 else self.blob.size)
        if size <= 0:
            return b""
        parts = []
        while size:
            index, offset = divmod(self.position, CHUNK)
            if index not in self.blocks:
                self.stream.seek(index * (CHUNK + 28))
                length = min(CHUNK, self.blob.size - index * CHUNK)
                encoded = self.stream.read(length + 28)
                self.blocks[index] = self.aes.decrypt(encoded[:12], encoded[12:], index.to_bytes(8, "big"))
                if len(self.blocks) > 8:
                    self.blocks.popitem(last=False)
            self.blocks.move_to_end(index)
            block = self.blocks[index]
            count = min(size, len(block) - offset)
            parts.append(block[offset : offset + count])
            size -= count
            self.position += count
        return b"".join(parts)

    def readinto(self, target):
        data = self.read(len(target))
        target[: len(data)] = data
        return len(data)

    def close(self):
        self.blocks.clear()
        self.stream.close()
        super().close()


class CacheWriter:
    def __init__(self, cache, group, memory_limit=0):
        self.cache, self.group, self.memory_limit = cache, group, memory_limit
        self.pending = bytearray()
        self.size, self.index = 0, 0
        self.path = self.stream = None
        self.key = secrets.token_bytes(32)
        self.prefix = secrets.token_bytes(8)
        self.aes = AESGCM(self.key)
        self.finished = False

    def _write_block(self, value):
        nonce = self.prefix + self.index.to_bytes(4, "big")
        encoded = nonce + self.aes.encrypt(nonce, value, self.index.to_bytes(8, "big"))
        with self.cache.disk_allocation(len(encoded)):
            if self.stream is None:
                folder = self.cache.root / self.group
                folder.mkdir(exist_ok=True)
                self.path = folder / (secrets.token_hex(16) + ".bin")
                self.stream = self.path.open("xb", buffering=0)
            self.stream.write(encoded)
        self.index += 1

    def write(self, data):
        if self.finished:
            raise ValueError("cache writer closed")
        self.size += len(data)
        view = memoryview(data)
        while view:
            capacity = max(CHUNK, self.memory_limit + 1) if self.stream is None else CHUNK
            count = min(len(view), capacity - len(self.pending))
            self.pending.extend(view[:count])
            view = view[count:]
            if self.size > self.memory_limit:
                while len(self.pending) >= CHUNK:
                    self._write_block(bytes(self.pending[:CHUNK]))
                    del self.pending[:CHUNK]
        return len(data)

    def finish(self):
        if self.stream is None and self.size <= self.memory_limit:
            result = Blob(self.size, data=bytes(self.pending))
        else:
            if self.pending:
                self._write_block(bytes(self.pending))
            result = Blob(self.size, path=str(self.path), key=self.key)
        self.pending.clear()
        if self.stream:
            self.stream.close()
        self.finished = True
        return result

    def abort(self):
        self.pending.clear()
        if self.stream:
            self.stream.close()
        if self.path:
            self.path.unlink(missing_ok=True)
        self.finished = True


class SessionCache:
    def __init__(self, root=None, *, existing=False, disk_budget=DISK_BUDGET, reserve=DISK_RESERVE):
        self.disk_budget, self.reserve = disk_budget, reserve
        self.owner = not existing
        self.lock_stream = self.ownership = None
        if existing:
            self.root = Path(root)
            return
        base = (
            Path(root)
            if root
            else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SafetyMask" / "sessions"
        )
        base.mkdir(parents=True, exist_ok=True)
        with (base / "startup.lock").open("a+b") as startup, file_lock(startup):
            # Packaged Windows launchers can virtualize LocalAppData files while
            # resolving the parent directory to its unvirtualized spelling.
            # Anchor every containment check to the actual opened lock file.
            self.base = Path(startup.name).resolve().parent
            self._start(self.base)

    def _start(self, base):
        # The live owner holds an OS lock; a PID alone is not safe against PID reuse.
        for folder in base.iterdir():
            if (
                not folder.is_dir()
                or not folder.name.startswith("session-")
                or folder.resolve().parent != self.base
            ):
                continue
            try:
                with (folder / "owner.lock").open("a+b") as stream, file_lock(stream, blocking=False):
                    pass
                shutil.rmtree(folder)
            except OSError:
                pass
        self.root = base / ("session-" + secrets.token_hex(16))
        self.root.mkdir()
        self.lock_stream = (self.root / "owner.lock").open("a+b")
        self.lock_stream.write(b"0")
        self.lock_stream.flush()
        self.ownership = file_lock(self.lock_stream, blocking=False)
        self.ownership.__enter__()

    def writer(self, group, memory_limit=0):
        # Group identifiers are generated in the application, never filenames supplied by users.
        if not group.isalnum():
            raise ValueError("invalid cache group")
        return CacheWriter(self, group, memory_limit)

    @contextmanager
    def disk_allocation(self, count):
        with _quota_lock, (self.root / "quota.lock").open("a+b") as lock, file_lock(lock):
            used = 0
            for path in self.root.glob("*/*.bin"):
                try:
                    used += path.stat().st_size
                except FileNotFoundError:
                    pass  # Another image can be removed while this upload is being admitted.
            if used + count > self.disk_budget:
                raise ResourceUnavailable("会话加密缓存已达到 20 GiB，请导出并移除部分图片后重试")
            if shutil.disk_usage(self.root).free - count < self.reserve:
                raise ResourceUnavailable("磁盘可用空间不足，需要保留 5 GiB；请释放空间后重试")
            yield

    def clear_group(self, group):
        target = (self.root / group).resolve()
        if target.parent != self.root.resolve():
            raise ValueError("invalid cache group")
        if target.exists():
            shutil.rmtree(target)

    def close(self):
        if self.owner and self.ownership:
            self.ownership.__exit__(None, None, None)
            self.ownership = None
            self.lock_stream.close()
            if self.root.resolve().parent == self.base:
                shutil.rmtree(self.root, ignore_errors=True)
