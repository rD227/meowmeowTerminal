"""桌面会话环境恢复。

用 `sudo`（没有 `-E`）运行本程序时，Wayland / niri 相关的环境变量会被清掉，
导致剪贴板和前台窗口查询失效。这里根据 `SUDO_UID` 找回原用户的运行时目录。
"""
import glob
import os


def desktop_env() -> dict:
    """返回补全了 Wayland / niri 环境变量的环境副本。"""
    env = os.environ.copy()

    uid = env.get("SUDO_UID")
    if not uid:
        return env

    runtime = f"/run/user/{uid}"
    if not os.path.isdir(runtime):
        return env

    # 覆盖掉 sudo 可能设置成的 /root，改用原用户的会话目录
    env["XDG_RUNTIME_DIR"] = runtime

    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(
            path for path in glob.glob(os.path.join(runtime, "wayland-*"))
            if not path.endswith(".lock")
        )
        if sockets:
            env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])

    if not env.get("NIRI_SOCKET"):
        sockets = sorted(glob.glob(os.path.join(runtime, "niri.*.sock")))
        if sockets:
            env["NIRI_SOCKET"] = sockets[0]

    # XWayland / X11 的显示号通常就是 :0；xdotool 等工具在非 niri 会话下还会用到
    if not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"

    return env
