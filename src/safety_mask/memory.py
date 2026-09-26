"""Content-free memory admission and diagnostics for local background work."""

from __future__ import annotations

import ctypes
import math
import os
from dataclasses import dataclass

import psutil

from .cache import ResourceUnavailable

GIB = 1024**3
RESERVE = GIB
SAVING_THRESHOLD = 8 * GIB
LABELS = {
    "loading": "加载文字模型",
    "loading_entities": "加载敏感内容模型",
    "preparing": "准备图片",
    "detecting": "寻找文字",
    "recognizing": "识别文字",
    "analyzing": "分析敏感内容",
    "preview": "生成预览",
    "export": "生成 PNG",
}


@dataclass(frozen=True)
class MemorySnapshot:
    available: int
    commit_available: int | None
    total: int

    @property
    def saving(self):
        return self.available < SAVING_THRESHOLD or (
            self.commit_available is not None and self.commit_available < SAVING_THRESHOLD
        )

    def view(self):
        return {
            "available_bytes": self.available,
            "commit_available_bytes": self.commit_available,
            "total_bytes": self.total,
        }


def snapshot():
    physical = psutil.virtual_memory()
    commit = None
    if os.name == "nt":
        # Commit and physical headroom are different budgets, never added together.
        class Performance(ctypes.Structure):
            _fields_ = (
                [("cb", ctypes.c_ulong)]
                + [
                    (name, ctypes.c_size_t)
                    for name in (
                        "CommitTotal",
                        "CommitLimit",
                        "CommitPeak",
                        "PhysicalTotal",
                        "PhysicalAvailable",
                        "SystemCache",
                        "KernelTotal",
                        "KernelPaged",
                        "KernelNonpaged",
                        "PageSize",
                    )
                ]
                + [(name, ctypes.c_ulong) for name in ("HandleCount", "ProcessCount", "ThreadCount")]
            )

        info = Performance()
        info.cb = ctypes.sizeof(info)
        if ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            commit = max(0, (info.CommitLimit - info.CommitTotal) * info.PageSize)
    return MemorySnapshot(physical.available, commit, physical.total)


def memory_issue(stage, *, code="memory_preflight", additional=0, measured=None):
    current = measured or snapshot()
    required = RESERVE + math.ceil(max(0, additional) * 1.25)
    physical_gap = max(0, required - current.available)
    commit_gap = max(0, required - current.commit_available) if current.commit_available is not None else 0
    return {
        "resource": "memory",
        "code": code,
        "stage": stage,
        **current.view(),
        "required_bytes": required,
        "release_bytes": max(physical_gap, commit_gap),
        "estimated": True,
    }


def issue_message(issue):
    label = LABELS.get(issue.get("stage"), "处理图片")
    available = issue["available_bytes"] / GIB
    required = issue["required_bytes"] / GIB
    gap = issue["release_bytes"] / GIB
    if issue["code"] == "memory_allocation":
        reason = f"{label}时内存不足（实际分配失败）"
    elif issue["code"] == "memory_pressure":
        reason = f"{label}时系统内存不足，已暂停"
    else:
        reason = f"{label}所需内存不足"
    message = f"{reason}；当前可用 {available:.1f} GiB"
    if issue["code"] == "memory_preflight":
        message += f"，预计本阶段需 {required:.1f} GiB"
    if (
        issue.get("commit_available_bytes") is not None
        and issue["commit_available_bytes"] < issue["required_bytes"]
    ):
        message += f"，系统剩余可分配额度 {issue['commit_available_bytes'] / GIB:.1f} GiB"
    if gap:
        message += f"；建议再释放约 {math.ceil(gap * 10) / 10:.1f} GiB 后重试"
    else:
        message += "；请释放内存后重试"
    return message


def memory_error(stage, *, code="memory_allocation", additional=0, measured=None):
    issue = memory_issue(stage, code=code, additional=additional, measured=measured)
    return ResourceUnavailable(issue_message(issue), issue=issue)


def require_memory(additional=0, stage="preparing"):
    """Only the next allocation/stage is charged, not already resident models."""
    current = snapshot()
    issue = memory_issue(stage, additional=additional, measured=current)
    if issue["release_bytes"]:
        raise ResourceUnavailable(issue_message(issue), issue=issue)
    return current


def is_memory_failure(exc):
    if isinstance(exc, ResourceUnavailable):
        return bool(exc.issue and exc.issue.get("resource") == "memory")
    return (
        isinstance(exc, MemoryError)
        or (isinstance(exc, OSError) and getattr(exc, "errno", None) == 12)
        or any(
            marker in str(exc).lower()
            for marker in (
                "out of memory",
                "bad_alloc",
                "cannot allocate memory",
                "not enough memory",
            )
        )
    )


def process_sample():
    info = psutil.Process().memory_info()
    return {
        "working_set_bytes": info.rss,
        "private_bytes": getattr(info, "private", info.vms),
        "peak_working_set_bytes": getattr(info, "peak_wset", info.rss),
    }
