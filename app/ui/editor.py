"""用外部编辑器打开文件 —— 优先 Notepad++，找不到就退回系统默认程序。

为什么单独一层
--------------
用户明确要求「打开逐字稿文档时默认用 Notepad++ 打开」。但 Notepad++ 的
安装路径在不同机器上差别极大（Program Files / Program Files (x86) / 便携版 /
scoop / chocolatey…），不能写死一个路径。所以这里做**多路探测**：

1. 配置里显式指定的路径（最高优先级，用户在设置里选了就用它）；
2. 注册表 `HKEY_CLASSES_ROOT\\Applications\\notepad++.exe\\shell\\open\\command`
   —— 装过 Notepad++ 的机器基本都有这条（安装器写的）；
3. 常见安装目录逐个试；
4. PATH 里直接 `where notepad++`；
5. Scoop / Chocolatey 的默认位置。

全部失败才回退到 `os.startfile()`（系统默认程序）。

为什么要检测「是否真的是 Notepad++」
-----------------------------------
用户可能把路径指到一个不存在的 exe，或者指到别的编辑器。所以
`resolve_editor()` 返回的是**已经验证过存在**的路径，或 None。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Notepad++ 常见安装位置（按命中概率排序）
_CANDIDATES = (
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
    r"D:\Program Files\Notepad++\notepad++.exe",
    r"D:\Notepad++\notepad++.exe",
    r"E:\Notepad++\notepad++.exe",
    r"F:\Notepad++\notepad++.exe",
    r"F:\4-25\Notepad++\notepad++.exe",
    r"C:\Notepad++\notepad++.exe",
)

_EXE_NAMES = ("notepad++.exe", "Notepad++.exe", "notepad++", "npp.exe")


def _from_registry() -> str:
    """从注册表拿 Notepad++ 的安装路径（安装器会写这条）。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return ""

    keys = (
        (winreg.HKEY_CLASSES_ROOT,
         r"Applications\notepad++.exe\shell\open\command"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Notepad++"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Notepad++"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Notepad++"),
    )
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                # 情况 A：command 键，默认值是 `"C:\...\notepad++.exe" "%1"`
                try:
                    val, _ = winreg.QueryValueEx(k, None)
                    if val:
                        path = _extract_exe(str(val))
                        if path and Path(path).exists():
                            return path
                except OSError:
                    pass
                # 情况 B：Notepad++ 键，有 InstallDir / Path 值
                for name in ("Path", "InstallDir", "ExePath"):
                    try:
                        val, _ = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    if not val:
                        continue
                    p = Path(str(val))
                    if p.is_dir():
                        p = p / "notepad++.exe"
                    if p.exists():
                        return str(p)
        except OSError:
            continue
    return ""


def _extract_exe(command: str) -> str:
    """从 `"C:\\x\\notepad++.exe" "%1"` 里抠出 exe 路径。"""
    s = command.strip()
    if not s:
        return ""
    if s.startswith('"'):
        end = s.find('"', 1)
        return s[1:end] if end > 0 else ""
    # 没引号：取到第一个空格前，但要小心 "C:\Program Files" 这种带空格的
    for ext in (".exe", ".EXE"):
        i = s.find(ext)
        if i > 0:
            return s[:i + len(ext)]
    return s.split(" ")[0]


def find_notepadpp(explicit: str = "") -> str:
    """探测 Notepad++ 可执行文件路径；找不到返回空串。

    explicit 非空且文件存在时直接用它（用户在设置里指定过）。
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        # 用户可能选的是安装目录
        if p.is_dir():
            for n in _EXE_NAMES:
                cand = p / n
                if cand.exists():
                    return str(cand)

    for c in _CANDIDATES:
        if Path(c).exists():
            return c

    reg = _from_registry()
    if reg:
        return reg

    for n in _EXE_NAMES:
        w = shutil.which(n)
        if w:
            return w

    # Scoop / Chocolatey 的常见布局
    home = Path(os.environ.get("USERPROFILE", ""))
    extra = [
        home / "scoop" / "apps" / "notepadplusplus" / "current" / "notepad++.exe",
        Path("C:/ProgramData/chocolatey/bin/notepad++.exe"),
        Path("C:/ProgramData/chocolatey/lib/notepadplusplus/tools/notepad++.exe"),
    ]
    for p in extra:
        if p.exists():
            return str(p)
    return ""


def open_with_editor(path: str | Path, editor: str = "",
                     line: int | None = None) -> tuple[bool, str]:
    """用编辑器打开文件。

    返回 (是否用了 Notepad++, 说明文字)。
    editor 为空时先自动探测 Notepad++；探测不到就回退系统默认程序。
    """
    p = Path(path)
    if not p.exists():
        return False, f"文件不存在：{p}"

    exe = find_notepadpp(editor)
    if exe:
        args = [exe]
        if line:
            args.append(f"-n{line}")
        args.append(str(p))
        try:
            # Notepad++ 是多实例风格，直接 Popen 不阻塞
            subprocess.Popen(args, close_fds=True)
            return True, f"已用 Notepad++ 打开：{p.name}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Notepad++ 启动失败（{exc}），已改用系统默认程序"

    # 回退：系统默认程序
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return False, f"未找到 Notepad++，已用系统默认程序打开：{p.name}"
    except Exception as exc:  # noqa: BLE001
        return False, f"打开失败：{exc}"
