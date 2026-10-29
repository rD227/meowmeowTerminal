"""Linux 全局键盘后端：evdev 独占抓取 + uinput 虚拟键盘回放。

`keyboard` 库在 Linux 上不支持按键拦截（其 README 写明 Key suppression/blocking
only available on Windows），而本程序必须在 Enter 到达聊天窗口之前把它吞掉，
因此 Linux 下直接在 evdev 层实现：

1. 打开 /dev/input/event* 中所有键盘类设备并 EVIOCGRAB 独占；
2. 自己读取事件，非触发键原样写回 /dev/uinput 创建的虚拟键盘；
3. 触发键（Enter、Ctrl+Alt+P）只做处理、不回放，处理完由发送流程补发。

权限要求：能读 /dev/input/event*，能写 /dev/uinput。
- 临时方案：`sudo -E python3 main.py`（-E 保留 Wayland/DISPLAY 环境变量）
- 长期方案：把用户加入 input 组，并让 /dev/uinput 也属于 input 组（见 README）
"""
from __future__ import annotations

import errno
import logging
import os
import threading

from evdev import InputDevice, UInput, UInputError, ecodes, list_devices

_log = logging.getLogger("meowmeow.linux_input")

_VIRTUAL_NAME = "meowmeow-virtual-keyboard"
_GRAB_RESCAN_SECONDS = 5.0

_VALUE_RELEASE = 0
_VALUE_PRESS = 1
_VALUE_REPEAT = 2

_CTRL_KEYS = frozenset({ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL})
_ALT_KEYS = frozenset({ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT})
_SHIFT_KEYS = frozenset({ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT})
_META_KEYS = frozenset({ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA})
_MODIFIER_KEYS = _CTRL_KEYS | _ALT_KEYS | _SHIFT_KEYS | _META_KEYS

_ENTER_KEYS = frozenset({ecodes.KEY_ENTER, ecodes.KEY_KPENTER})
_PAUSE_KEY = ecodes.KEY_P

# 用来区分"键盘"和电源键、鼠标之类：这些键基本只出现在键盘上
_KEYBOARD_PROBE_KEYS = frozenset({
    ecodes.KEY_A,
    ecodes.KEY_Z,
    ecodes.KEY_ENTER,
    ecodes.KEY_SPACE,
    ecodes.KEY_LEFTSHIFT,
})

_KEY_NAME_ALIASES = {
    "ctrl": ecodes.KEY_LEFTCTRL,
    "control": ecodes.KEY_LEFTCTRL,
    "alt": ecodes.KEY_LEFTALT,
    "shift": ecodes.KEY_LEFTSHIFT,
    "meta": ecodes.KEY_LEFTMETA,
    "super": ecodes.KEY_LEFTMETA,
    "win": ecodes.KEY_LEFTMETA,
    "enter": ecodes.KEY_ENTER,
    "return": ecodes.KEY_ENTER,
    "tab": ecodes.KEY_TAB,
    "esc": ecodes.KEY_ESC,
    "escape": ecodes.KEY_ESC,
    "space": ecodes.KEY_SPACE,
    "backspace": ecodes.KEY_BACKSPACE,
    "delete": ecodes.KEY_DELETE,
}


def resolve_key(name: str) -> int:
    """把 'ctrl'、'a'、'enter' 之类的名字解析成 evdev key code。"""
    key = name.strip().lower()
    if not key:
        raise ValueError("按键名不能为空")
    if key in _KEY_NAME_ALIASES:
        return _KEY_NAME_ALIASES[key]

    code = ecodes.ecodes.get("KEY_" + key.upper())
    if code is not None:
        return code
    raise ValueError(f"未知按键: {name!r}")


class LinuxKeyboardBackend:
    """用 evdev 抓取物理键盘，再通过 uinput 虚拟键盘回放。"""

    def __init__(self, on_enter, on_toggle, observer=None):
        self._on_enter = on_enter
        self._on_toggle = on_toggle
        self._observer = observer

        self._ui: UInput | None = None
        self._ui_lock = threading.Lock()

        self._devices: dict[str, InputDevice] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        # 权限不足 / 被占用只提示一次，避免热插拔轮询反复刷日志
        self._skipped_logged: set[str] = set()

        # 物理键盘当前按下的修饰键，以及每个设备上被吞掉、还没等到 key-up 的键
        self._state_lock = threading.Lock()
        self._pressed: set[int] = set()
        self._swallowed: dict[str, set[int]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        try:
            self._ui = UInput(
                {ecodes.EV_KEY: list(range(ecodes.KEY_MAX + 1))},
                name=_VIRTUAL_NAME,
            )
        except (UInputError, PermissionError, OSError) as exc:
            raise RuntimeError(
                "无法创建虚拟键盘 /dev/uinput，请用 sudo 运行，"
                "或把当前用户加入 input 组并配置 /dev/uinput 权限（见 README）"
            ) from exc

        self._grab_existing()
        if not self._devices:
            print("[加喵] 警告：没有抓取到任何键盘设备，请检查 input 组权限，或改用 sudo -E 运行")
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="meow-input-monitor", daemon=True
        )
        self._monitor.start()

    def wait(self) -> None:
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()

        for path, dev in list(self._devices.items()):
            self._devices.pop(path, None)
            try:
                dev.ungrab()
            except OSError:
                pass
            try:
                dev.close()
            except OSError:
                pass

        with self._ui_lock:
            ui, self._ui = self._ui, None
        if ui is not None:
            try:
                ui.close()
            except OSError:
                pass

    def diagnostics(self) -> dict:
        with self._state_lock:
            swallowed = sum(len(codes) for codes in self._swallowed.values())
            pressed = len(self._pressed)
        return {
            "input_backend": "evdev",
            "grabbed_devices": len(self._devices),
            "pressed_keys": pressed,
            "swallowed_keys": swallowed,
        }

    # ------------------------------------------------------------------
    # 合成按键（等价于 Windows 下的 keyboard.send）
    # ------------------------------------------------------------------
    def send(self, combo: str) -> None:
        codes = [resolve_key(part) for part in combo.split("+")]
        if not codes:
            raise ValueError("空按键组合")

        with self._ui_lock:
            ui = self._ui
            if ui is None:
                raise RuntimeError("虚拟键盘尚未启动")
            for code in codes:
                ui.write(ecodes.EV_KEY, code, _VALUE_PRESS)
                ui.syn()
            for code in reversed(codes):
                ui.write(ecodes.EV_KEY, code, _VALUE_RELEASE)
                ui.syn()

    # ------------------------------------------------------------------
    # 设备抓取
    # ------------------------------------------------------------------
    def _grab_existing(self) -> None:
        try:
            paths = sorted(set(list_devices()))
        except OSError as exc:
            _log.warning("枚举输入设备失败: %s", exc)
            return
        for path in paths:
            if path not in self._devices:
                self._grab_device(path)

    def _grab_device(self, path: str) -> None:
        dev = None
        try:
            dev = InputDevice(path)
            if dev.name == _VIRTUAL_NAME:
                dev.close()
                return
            keys = set(dev.capabilities().get(ecodes.EV_KEY, ()))
            if not _KEYBOARD_PROBE_KEYS.issubset(keys):
                dev.close()
                return
            dev.grab()
        except PermissionError:
            if path not in self._skipped_logged:
                self._skipped_logged.add(path)
                _log.warning("没有权限读取 %s，跳过（把用户加入 input 组或用 sudo）", path)
            if dev is not None:
                dev.close()
            return
        except OSError as exc:
            if path not in self._skipped_logged:
                self._skipped_logged.add(path)
                if exc.errno == errno.EBUSY:
                    _log.info("%s 已被其它程序独占，跳过", path)
                else:
                    _log.info("无法抓取 %s: %s", path, exc)
            if dev is not None:
                dev.close()
            return

        self._devices[path] = dev
        thread = threading.Thread(
            target=self._read_loop,
            args=(dev,),
            name=f"meow-input-{os.path.basename(path)}",
            daemon=True,
        )
        self._threads[path] = thread
        thread.start()
        _log.info("已抓取键盘设备 %s (%s)", path, dev.name)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(_GRAB_RESCAN_SECONDS):
            # 只负责发现新插入的键盘；设备断开会由读线程自己清理
            self._grab_existing()

    def _read_loop(self, dev: InputDevice) -> None:
        path = dev.path
        try:
            for event in dev.read_loop():
                if self._stop.is_set():
                    break
                if self._observer is not None:
                    try:
                        self._observer(event)
                    except Exception:
                        pass
                if event.type == ecodes.EV_KEY:
                    self._handle_key(path, event)
                else:
                    self._forward(event)
        except OSError as exc:
            if not self._stop.is_set():
                _log.info("输入设备 %s 断开: %s", path, exc)
        finally:
            self._release_device(path, dev)

    def _release_device(self, path: str, dev: InputDevice) -> None:
        if self._devices.get(path) is dev:
            self._devices.pop(path, None)
        self._threads.pop(path, None)
        try:
            dev.ungrab()
        except OSError:
            pass
        try:
            dev.close()
        except OSError:
            pass
        with self._state_lock:
            self._swallowed.pop(path, None)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _handle_key(self, path: str, event) -> None:
        code = event.code
        value = event.value

        if code in _MODIFIER_KEYS:
            with self._state_lock:
                if value == _VALUE_RELEASE:
                    self._pressed.discard(code)
                else:
                    self._pressed.add(code)
            self._forward(event)
            return

        if value == _VALUE_RELEASE:
            if self._release_swallowed(path, code):
                return
            self._forward(event)
            return

        # 自动连发由 X server / Wayland compositor 根据按下状态自行生成，
        # 这里再回放一遍会变成重复输入。
        if value == _VALUE_REPEAT:
            return

        with self._state_lock:
            ctrl = bool(self._pressed & _CTRL_KEYS)
            alt = bool(self._pressed & _ALT_KEYS)
            shift = bool(self._pressed & _SHIFT_KEYS)
            meta = bool(self._pressed & _META_KEYS)

        if code in _ENTER_KEYS:
            # 与 Windows 版一致：带修饰键的 Enter（如 Shift+Enter 换行）放行
            if ctrl or alt or shift or meta:
                self._forward(event)
                return
            self._swallow(path, code)
            self._trigger(self._on_enter)
            return

        if code == _PAUSE_KEY and ctrl and alt:
            self._swallow(path, code)
            self._trigger(self._on_toggle)
            return

        self._forward(event)

    def _forward(self, event) -> None:
        # EV_MSC 等事件虚拟设备没有声明，丢弃即可；EV_SYN 必须转发用于提交按键。
        if event.type not in (ecodes.EV_KEY, ecodes.EV_SYN):
            return
        with self._ui_lock:
            ui = self._ui
            if ui is None:
                return
            try:
                ui.write(event.type, event.code, event.value)
            except (OSError, ValueError):
                pass

    def _trigger(self, callback) -> None:
        if callback is None:
            return

        def run():
            try:
                callback()
            except Exception:
                _log.exception("键盘回调异常")

        threading.Thread(target=run, daemon=True).start()

    def _swallow(self, path: str, code: int) -> None:
        with self._state_lock:
            self._swallowed.setdefault(path, set()).add(code)

    def _release_swallowed(self, path: str, code: int) -> bool:
        with self._state_lock:
            held = self._swallowed.get(path)
            if not held or code not in held:
                return False
            held.discard(code)
            if not held:
                self._swallowed.pop(path, None)
            return True
