"""Linux 前台窗口所属进程查询。

Wayland 下 xdotool 只能看到 X11 / XWayland 窗口，原生 Wayland 窗口会直接查询失败
（niri 甚至不会设置 _NET_ACTIVE_WINDOW）。这里按合成器逐级回退：

- niri:      niri msg --json windows
- Hyprland:  hyprctl activewindow -j
- sway/i3:   swaymsg / i3-msg get_tree
- X11:       xdotool

返回小写的候选名字集合（窗口 app_id / WM_CLASS / 进程名）；
完全查不到时返回空集合，由调用方决定放行还是拦截。
"""
from __future__ import annotations

import json
import shutil
import subprocess

import psutil

from utils.session_env import desktop_env

_TIMEOUT = 1.0


def _run(args, env=None):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, check=False,
            timeout=_TIMEOUT, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _process_name(pid) -> str:
    try:
        return psutil.Process(int(pid)).name().lower()
    except (psutil.Error, TypeError, ValueError):
        return ""


def _names(app_id=None, pid=None) -> set[str]:
    names = {
        (app_id or "").strip().lower(),
        _process_name(pid) if pid is not None else "",
    }
    return {name for name in names if name}


def _niri(env) -> set[str]:
    if not shutil.which("niri") or not env.get("NIRI_SOCKET"):
        return set()

    result = _run(["niri", "msg", "--json", "windows"], env=env)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return set()
    try:
        windows = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return set()

    for window in windows:
        if window.get("is_focused"):
            return _names(window.get("app_id"), window.get("pid"))
    return set()


def _hyprland(env) -> set[str]:
    if not shutil.which("hyprctl") or not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return set()

    result = _run(["hyprctl", "activewindow", "-j"], env=env)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return set()
    try:
        window = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return set()

    names = _names(window.get("class"), window.get("pid"))
    initial = (window.get("initialClass") or "").strip().lower()
    if initial:
        names.add(initial)
    return names


def _find_focused(node):
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = _find_focused(child)
        if found is not None:
            return found
    return None


def _sway(env) -> set[str]:
    commands = (
        ("swaymsg", ["swaymsg", "-t", "get_tree", "-r"]),
        ("i3-msg", ["i3-msg", "-t", "get_tree"]),
    )
    for executable, args in commands:
        if not shutil.which(executable):
            continue
        result = _run(args, env=env)
        if not result or result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            node = _find_focused(json.loads(result.stdout))
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue
        if not node:
            continue

        properties = node.get("window_properties") or {}
        names = _names(node.get("app_id"), node.get("pid"))
        window_class = (properties.get("class") or "").strip().lower()
        if window_class:
            names.add(window_class)
        return names
    return set()


def _xdotool(env) -> set[str]:
    if not shutil.which("xdotool") or not env.get("DISPLAY"):
        return set()

    names: set[str] = set()
    result = _run(["xdotool", "getactivewindow", "getwindowclassname"], env=env)
    if result and result.returncode == 0 and result.stdout.strip():
        names.add(result.stdout.strip().lower())

    result = _run(["xdotool", "getactivewindow", "getwindowpid"], env=env)
    if result and result.returncode == 0 and result.stdout.strip():
        names.add(_process_name(result.stdout.strip()))

    return {name for name in names if name}


def get_foreground_names() -> set[str]:
    """返回前台窗口对应的候选名字（小写）；查不到返回空集合。"""
    env = desktop_env()

    for getter in (_niri, _hyprland, _sway):
        names = getter(env)
        if names:
            return names

    return _xdotool(env)
