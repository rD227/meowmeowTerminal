"""跨平台键盘后端工厂。

- Windows / macOS：utils/legacy_input.py（keyboard 库）
- Linux：utils/linux_input.py（evdev + uinput，因为 keyboard 库在 Linux 上不能拦截按键）
"""
import sys

_IS_LINUX = sys.platform.startswith("linux")

_active_backend = None


def create_backend(on_enter, on_toggle, observer=None):
    """创建并登记当前平台的键盘后端（尚未 start）。"""
    global _active_backend
    if _IS_LINUX:
        from utils.linux_input import LinuxKeyboardBackend as Backend
    else:
        from utils.legacy_input import LegacyKeyboardBackend as Backend

    _active_backend = Backend(on_enter, on_toggle, observer)
    return _active_backend


def send_keys(combo: str):
    """向前台窗口发送按键组合，供 core.send_text() 使用。"""
    if _active_backend is not None:
        _active_backend.send(combo)
        return

    # 理论上 send_text 只会在后端启动后调用；这里保留一个兜底
    if _IS_LINUX:
        raise RuntimeError("键盘后端尚未启动")
    import keyboard
    keyboard.send(combo)
