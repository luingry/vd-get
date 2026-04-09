#!/usr/bin/env python3
"""
Foca uma janela do navegador já aberta com a interface VDGET, se existir;
caso contrário abre index.html no app padrão (Windows: os.startfile).
"""
from __future__ import annotations

import os
import sys


def _titulo_eh_interface_vdget(title: str) -> bool:
    if "VDGET" not in title or "Gerenciador" not in title:
        return False
    # index.html usa EM DASH (U+2014); o iniciar.bat usa hífen no title do CMD
    if "\u2014" in title:
        return True
    navegador = (
        "Chrome",
        "Edge",
        "Firefox",
        "Brave",
        "Opera",
        "Vivaldi",
        "Arc",
        "Chromium",
    )
    return any(b in title for b in navegador)


def _focar_janela_vdget_windows() -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    encontrados: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd) + 1
        if n <= 1:
            return True
        buf = ctypes.create_unicode_buffer(n)
        user32.GetWindowTextW(hwnd, buf, n)
        if _titulo_eh_interface_vdget(buf.value):
            encontrados.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not encontrados:
        return False
    hwnd = encontrados[-1]
    # SW_RESTORE em janela maximizada a desmaximiza; só restaurar se minimizada.
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


def main() -> int:
    base = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(base, "index.html")
    if not os.path.isfile(index_path):
        print(f"[abrir_interface] Arquivo nao encontrado: {index_path}", file=sys.stderr)
        return 1

    if sys.platform == "win32" and _focar_janela_vdget_windows():
        return 0

    if sys.platform == "win32":
        os.startfile(index_path)  # type: ignore[attr-defined]
    else:
        import subprocess

        subprocess.run(["xdg-open", index_path], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
