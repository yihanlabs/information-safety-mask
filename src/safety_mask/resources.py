"""The service owns one CPU budget; workers join without resetting it."""

from __future__ import annotations

import ctypes
import os
import secrets
from dataclasses import dataclass, field

THREADS = 2
CPU_BUDGET = 20


@dataclass(frozen=True)
class ResourceProfile:
    mode: str = "low_impact"
    threads: int = THREADS
    cpu_budget_percent: int = CPU_BUDGET
    ecoqos: bool = True

    @property
    def key(self):
        return f"{self.mode}:{self.threads}:{self.cpu_budget_percent}:{self.ecoqos}"


def resource_profile(mode="low_impact"):
    if mode == "high_performance":
        cores = os.cpu_count() or 2
        return ResourceProfile(mode, min(8, cores, max(2, cores - 2)), 80, False)
    if mode != "low_impact":
        raise ValueError("未知性能模式")
    return ResourceProfile()


def thread_environment(threads=THREADS):
    for name in ("MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(threads)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "TOKENIZERS_PARALLELISM"):
        os.environ[name] = "false" if name == "TOKENIZERS_PARALLELISM" else "1"


class Rate(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_ulong), ("rate", ctypes.c_ulong)]


class Power(ctypes.Structure):
    _fields_ = [("version", ctypes.c_ulong), ("control", ctypes.c_ulong), ("state", ctypes.c_ulong)]


def kernel_api():
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        ),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "SetPriorityClass": ([wintypes.HANDLE, wintypes.DWORD], wintypes.BOOL),
        "GetPriorityClass": ([wintypes.HANDLE], wintypes.DWORD),
        "SetProcessInformation": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        ),
        "GetProcessInformation": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        ),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def query_rate(kernel, handle):
    actual = Rate()
    if (
        handle
        and kernel.QueryInformationJobObject(handle, 15, ctypes.byref(actual), ctypes.sizeof(actual), None)
        and actual.flags & 0x5 == 0x5
    ):
        return actual.rate // 100
    return None


class CpuBudget:
    """Owns a job handle but never assigns the service process to the job."""

    def __init__(self):
        self.name = "Local\\SafetyMask-" + secrets.token_hex(16)
        self.kernel = kernel_api() if os.name == "nt" else None
        self.handle = self.kernel.CreateJobObjectW(None, self.name) if self.kernel else None
        self.mode = "low_impact"
        self.warning = None
        self.applied = False
        self.set_mode("low_impact")

    def _set(self, percent):
        rate = Rate(0x5, percent * 100)
        return bool(
            self.handle
            and self.kernel.SetInformationJobObject(self.handle, 15, ctypes.byref(rate), ctypes.sizeof(rate))
            and query_rate(self.kernel, self.handle) == percent
        )

    def set_mode(self, mode):
        self.applied = self._set(resource_profile(mode).cpu_budget_percent)
        if self.applied:
            self.mode, self.warning = mode, None
        else:
            self.mode = "low_impact"
            self.applied = self._set(CPU_BUDGET)
            self.warning = "高占用模式未生效，继续使用低占用设置" if mode != self.mode else None
            if not self.applied:
                self.warning = "系统未能启用 CPU 预算，后台计算将使用单线程"
        return self.mode

    def view(self):
        rate = query_rate(self.kernel, self.handle) if self.handle else None
        return {
            "cpu_budget_percent": rate,
            "cpu_cap_applied": rate == resource_profile(self.mode).cpu_budget_percent,
        }

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@dataclass
class ResourceLimits:
    profile: ResourceProfile = field(default_factory=ResourceProfile)
    threads: int = THREADS
    cpu_budget_percent: int | None = None
    cpu_cap_applied: bool = False
    below_normal: bool = False
    ecoqos: bool = False
    power_applied: bool = False
    _job: object = field(default=None, repr=False)
    _kernel: object = field(default=None, repr=False)
    _owner: object = field(default=None, repr=False)

    def view(self):
        warnings = []
        if not self.cpu_cap_applied:
            warnings.append("系统未能启用 CPU 预算，已使用单线程")
        if not self.below_normal or not self.power_applied:
            warnings.append("部分后台调度设置未生效")
        return {
            "mode": self.profile.mode,
            "threads": self.threads,
            "cpu_budget_percent": self.cpu_budget_percent,
            "cpu_cap_applied": self.cpu_cap_applied,
            "below_normal": self.below_normal,
            "ecoqos": self.ecoqos,
            "power_applied": self.power_applied,
            "warning": "；".join(warnings) or None,
        }

    def close(self):
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
        if self._owner:
            self._owner.close()
            self._owner = None


def apply_worker_limits(job_name=None, profile=None) -> ResourceLimits:
    profile = profile or resource_profile()
    limits = ResourceLimits(profile=profile, threads=profile.threads)
    if os.name == "nt":
        if job_name is None:
            # Standalone benchmark workers also own a private bounded job.
            limits._owner = CpuBudget()
            limits._owner.set_mode(profile.mode)
            job_name = limits._owner.name
        kernel = kernel_api()
        process = kernel.GetCurrentProcess()
        job = kernel.CreateJobObjectW(None, job_name)
        limits._job, limits._kernel = job, kernel
        # Joining an existing job must NEVER reset its shared budget.
        limits.cpu_budget_percent = query_rate(kernel, job)
        limits.cpu_cap_applied = bool(
            job and kernel.AssignProcessToJobObject(job, process) and limits.cpu_budget_percent is not None
        )
        limits.below_normal = bool(
            kernel.SetPriorityClass(process, 0x4000) and kernel.GetPriorityClass(process) == 0x4000
        )
        power, observed = Power(1, 1, int(profile.ecoqos)), Power(1, 0, 0)
        if kernel.SetProcessInformation(
            process, 4, ctypes.byref(power), ctypes.sizeof(power)
        ) and kernel.GetProcessInformation(process, 4, ctypes.byref(observed), ctypes.sizeof(observed)):
            limits.ecoqos = bool(observed.control & observed.state & 1)
            limits.power_applied = bool(observed.control & 1) and limits.ecoqos == profile.ecoqos
    if not limits.cpu_cap_applied:
        limits.threads = 1
    thread_environment(limits.threads)
    os.environ["VIPS_CONCURRENCY"] = str(limits.threads)
    return limits
