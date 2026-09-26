"""Image codecs run outside the web server under the same CPU job as OCR."""

from __future__ import annotations

import multiprocessing
import threading
import time

from .memory import RESERVE, is_memory_failure, memory_error, snapshot
from .resources import apply_worker_limits


def _main(connection, root, job_name):
    limits = apply_worker_limits(job_name)
    import cv2

    from .cache import ResourceUnavailable, SessionCache
    from .network import install_local_only_guard
    from .raster import export_png, inspect_image, prepare_image, preview_png

    cv2.setNumThreads(1)

    install_local_only_guard()
    cache = SessionCache(root, existing=True)
    try:
        connection.send(("runtime", limits.view()))
        while True:
            operation, args = connection.recv()
            result = None
            try:
                report = lambda event: connection.send(("progress", event))
                if operation == "info":
                    result = inspect_image(args[0])
                elif operation == "prepare":
                    result = prepare_image(args[0], cache, args[1], report, args[2] if len(args) > 2 else 0)
                elif operation == "preview":
                    result = preview_png(*args)
                elif operation == "export":
                    result = export_png(args[0], args[1], cache, args[2], report)
                else:
                    raise ValueError("未知图片操作")
                connection.send(("result", result))
            except Exception as exc:  # noqa: BLE001 - third-party exceptions may contain input data
                if isinstance(exc, ResourceUnavailable):
                    connection.send(("resource_error", {"message": str(exc), "issue": exc.issue}))
                    return
                elif is_memory_failure(exc):
                    error = memory_error("preparing" if operation == "prepare" else operation)
                    connection.send(("resource_error", {"message": str(error), "issue": error.issue}))
                    return
                else:
                    message = "图片处理未完成，文件可能损坏；请重试或重新导入"
                connection.send(("error", message))
            finally:
                args = result = None
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()
        limits.close()


class ImageWorker:
    def __init__(self, root, job_name):
        self.root, self.job_name = str(root), job_name
        self.serial = threading.Lock()
        self.lifecycle = threading.Lock()
        self.process = self.connection = None
        self.closed = False
        self.runtime = {}

    def call(self, operation, *args, report=None):
        from .cache import ResourceUnavailable

        with self.serial:
            with self.lifecycle:
                if self.closed:
                    raise RuntimeError("图片会话已结束")
                if self.process is None:
                    context = multiprocessing.get_context("spawn")
                    parent, child = context.Pipe()
                    process = context.Process(
                        target=_main, args=(child, self.root, self.job_name), daemon=True, name="local-image"
                    )
                    process.start()
                    child.close()
                    self.process, self.connection = process, parent
                process, connection = self.process, self.connection
            try:
                connection.send((operation, args))
                checked_at = 0.0
                while True:
                    if operation != "info" and time.monotonic() - checked_at >= 0.5:
                        checked_at = time.monotonic()
                        current = snapshot()
                        if current.available < RESERVE or (
                            current.commit_available is not None and current.commit_available < RESERVE
                        ):
                            self.cancel()
                            raise memory_error(
                                "preparing" if operation == "prepare" else operation,
                                code="memory_pressure",
                                measured=current,
                            )
                    if not connection.poll(0.5):
                        if not process.is_alive():
                            self.cancel()
                            raise RuntimeError("图片处理进程已退出，请重试")
                        continue
                    kind, value = connection.recv()
                    if kind == "runtime":
                        self.runtime = value
                    elif kind == "progress":
                        if report:
                            report(value)
                    elif kind == "error":
                        raise ValueError(value)
                    elif kind == "resource_error":
                        self.cancel()
                        raise ResourceUnavailable(value["message"], issue=value["issue"])
                    else:
                        return value
            except (EOFError, OSError):
                self.cancel()
                raise RuntimeError("图片处理已取消或进程退出，请重试") from None

    def cancel(self):
        from .worker import ProcessEngine

        with self.lifecycle:
            process, connection = self.process, self.connection
            self.process = self.connection = None
        ProcessEngine._stop(process, connection)

    def close(self):
        with self.lifecycle:
            self.closed = True
        self.cancel()
