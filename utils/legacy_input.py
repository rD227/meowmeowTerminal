"""Windows / macOS 键盘后端（基于 keyboard 库）。

Windows 下 keyboard 库支持按键拦截（suppress），Linux 不支持，
所以这个模块只在非 Linux 平台使用，Linux 走 utils/linux_input.py。
"""
import ctypes

import keyboard

# 由 LegacyKeyboardBackend.start() 注入
on_enter = lambda: None
toggle = lambda: None

_intercepted_enters = set()


def _handle_enter(event):
    # Match the physical key directly: a missed key-up in keyboard's cached
    # pressed-key set must not disable Enter interception.
    if event.event_type == keyboard.KEY_DOWN:
        if any(
            ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
            for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C)
        ):
            _intercepted_enters.discard(event.scan_code)
            return True
        _intercepted_enters.add(event.scan_code)
        on_enter()
        return False
    if event.scan_code in _intercepted_enters:
        _intercepted_enters.remove(event.scan_code)
        return False
    return True


def _handle_pause(event):
    # Use Windows' current modifier state, not keyboard's cached combinations.
    if (event.event_type == keyboard.KEY_DOWN
            and ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000
            and ctypes.windll.user32.GetAsyncKeyState(0x12) & 0x8000):
        toggle()
    return True


class LegacyKeyboardBackend:
    """用 keyboard 库注册全局钩子的后端（Windows / macOS）。"""

    def __init__(self, on_enter_cb, on_toggle_cb, observer=None):
        self._on_enter = on_enter_cb
        self._on_toggle = on_toggle_cb
        self._observer = observer

    def start(self):
        global on_enter, toggle
        on_enter = self._on_enter
        toggle = self._on_toggle

        if self._observer is not None:
            keyboard.hook(self._observer)
        keyboard.hook_key('enter', _handle_enter, suppress=True)
        keyboard.hook_key('p', _handle_pause, suppress=True)

    def wait(self):
        keyboard.wait()

    def stop(self):
        # keyboard 库没有提供反注册入口，进程退出时随之结束
        pass

    def send(self, combo):
        keyboard.send(combo)

    def diagnostics(self):
        listener = getattr(keyboard, "_listener", None)
        listening_thread = getattr(listener, "listening_thread", None)
        processing_thread = getattr(listener, "processing_thread", None)
        keyboard_queue = getattr(listener, "queue", None)
        return {
            "input_backend": "keyboard",
            "listener_alive": listening_thread.is_alive() if listening_thread else None,
            "processor_alive": processing_thread.is_alive() if processing_thread else None,
            "keyboard_queue_size": keyboard_queue.qsize() if keyboard_queue else None,
        }
