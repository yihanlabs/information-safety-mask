from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
from ctypes import wintypes
from pathlib import Path

from .models import PerformanceSettings, RuleSet


class Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("持久化敏感字段需要 Windows 用户级加密")
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; data is bound to the signed-in Windows user.
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise RuntimeError("Windows 规则加密操作失败")
    try:
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return root / "InformationSafetyMask" / "settings.json"


class SettingsRepository:
    def __init__(self, path: Path | None = None, protector=dpapi):
        self.path = path or settings_path()
        self.protector = protector

    def load(self) -> RuleSet:
        if not self.path.exists():
            return RuleSet()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            plain = self.protector(base64.b64decode(document["encrypted_rules"]), decrypt=True)
            return RuleSet.model_validate_json(plain)
        except Exception as exc:
            raise RuntimeError(
                "无法读取已保存的规则。请使用原 Windows 用户启动，或在设置中重置规则。"
            ) from exc

    def save(self, rules: RuleSet):
        encrypted = self.protector(rules.model_dump_json().encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "encrypted_rules": base64.b64encode(encrypted).decode("ascii")}),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


class PerformanceRepository:
    """Non-sensitive preferences; an omitted path is isolated, in-memory test storage."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.mode = "low_impact"

    def load(self):
        if self.path is None or not self.path.exists():
            return self.mode
        try:
            return PerformanceSettings.model_validate_json(self.path.read_text(encoding="utf-8")).mode
        except Exception as exc:
            raise RuntimeError("性能设置无法读取，已使用低占用；可在设置中重新选择") from exc

    def save(self, mode):
        value = PerformanceSettings(mode=mode)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix("." + secrets.token_hex(8) + ".tmp")
            try:
                temporary.write_text(value.model_dump_json(), encoding="utf-8")
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        self.mode = mode
