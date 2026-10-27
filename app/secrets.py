"""Windows DPAPI or environment credentials; never store API keys as plaintext."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class Secrets:
    def __init__(self, folder):
        self.folder = Path(folder)

    # ------------------------------------------------------------ 存储路径
    def _path(self, ref: str) -> Path:
        digest = hashlib.sha256(str(ref).encode()).hexdigest()
        return self.folder / (digest + '.bin')

    def has(self, ref: str) -> bool:
        return self._path(ref).exists()

    # ------------------------------------------------------------ 读写
    def set(self, ref: str, value: str) -> None:
        value = (value or '').strip()
        if not value:
            self.clear(ref)
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(ref).write_bytes(self._protect(value.encode('utf-8')))

    def clear(self, ref: str) -> None:
        p = self._path(ref)
        if p.exists():
            p.unlink()

    def get(self, ref: str, env: str = '') -> str:
        """Environment wins, so CI/CLI can inject without touching disk."""
        if env:
            value = os.getenv(env, '').strip()
            if value:
                return value
        p = self._path(ref)
        if not p.exists():
            return ''
        try:
            return self._protect(p.read_bytes(), decrypt=True).decode('utf-8')
        except Exception:
            return ''

    def source(self, ref: str, env: str = '') -> str:
        """Where the effective key came from, for display only."""
        if env and os.getenv(env, '').strip():
            return f'环境变量 {env}'
        return '系统凭据管理' if self._path(ref).exists() else '未配置'

    # ------------------------------------------------------------ DPAPI
    @staticmethod
    def _protect(data: bytes, decrypt: bool = False) -> bytes:
        if os.name != 'nt':
            raise ValueError('此平台请通过环境变量提供密钥')
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD),
                        ('data', ctypes.POINTER(ctypes.c_ubyte))]

        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        src = Blob(len(data), buf)
        dst = Blob()
        fn = (ctypes.windll.crypt32.CryptUnprotectData if decrypt
              else ctypes.windll.crypt32.CryptProtectData)
        if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
            raise ValueError('本机凭据保护失败')
        try:
            return ctypes.string_at(dst.data, dst.size)
        finally:
            ctypes.windll.kernel32.LocalFree(dst.data)
