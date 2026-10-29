"""加喵发送器 - 终端版入口"""
import ctypes
import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import queue
import sys
import threading
import time
import traceback

from config import CONFIGS
from core import ManosabaCore
from utils.keyboard_backend import create_backend, send_keys


# 两个实例会互相把对方注入的回车当成真回车，无限互相触发（顺带互抢剪贴板），
# 所以启动时先拿单实例锁，第二个实例在注册热键之前就被挡住。
_MUTEX_NAME = "Local\\meowmeowTerminal.main"
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5

# Windows 互斥体句柄必须一直持有到进程退出（不能 CloseHandle），退出时系统自动释放
_instance_mutex = None
# Linux 用 flock 锁文件，同样要保持文件对象存活
_instance_lock_file = None


def acquire_single_instance() -> bool:
    """拿到单实例锁返回 True，已有实例在跑返回 False。"""
    global _instance_mutex, _instance_lock_file

    if sys.platform.startswith("linux"):
        import fcntl
        import tempfile

        lock_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        lock_path = os.path.join(lock_dir, f"meowmeowTerminal-{os.getuid()}.lock")
        try:
            handle = open(lock_path, "a+", encoding="utf-8")
        except OSError as exc:
            print(f"[加喵] 无法创建单实例锁 {lock_path}: {exc}，跳过检查")
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        except OSError as exc:
            print(f"[加喵] 单实例锁不可用（{exc}），跳过检查继续启动")
            handle.close()
            return True
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass
        _instance_lock_file = handle
        return True

    if not sys.platform.startswith("win"):
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)

    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    err = ctypes.get_last_error()

    if not handle:
        # 打不开多半是已有一个提权实例占着：跨权限访问会直接报拒绝访问
        if err == _ERROR_ACCESS_DENIED:
            return False
        print(f"[加喵] 单实例检查失败（错误码 {err}），跳过检查继续启动")
        return True

    if err == _ERROR_ALREADY_EXISTS:
        return False

    _instance_mutex = handle
    return True


if not acquire_single_instance():
    print("=" * 40)
    print("  已经有一个加喵发送器在运行了，请先关掉它")
    print("  （两个实例会互相触发回车，还会互抢剪贴板）")
    if sys.platform.startswith("linux"):
        print("  查进程:  pgrep -af 'python.*main.py'")
        print("  结束它:  pkill -f 'python.*main.py'")
    else:
        print("  查进程:  tasklist | findstr python")
        print("  结束它:  Stop-Process -Id <PID> -Force")
    print("=" * 40)
    sys.exit(1)


_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_PATH = _LOG_DIR / "meowmeow.log"
_STACK_PATH = _LOG_DIR / "meowmeow-stacks.log"
_logger = logging.getLogger("meowmeow")
_logger.setLevel(logging.INFO)
_logger.propagate = False
try:
    _LOG_DIR.mkdir(exist_ok=True)
    _handler = RotatingFileHandler(
        _LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_handler)
except OSError as exc:
    print(f"[加喵] 无法写入诊断日志 {_LOG_PATH}: {exc}")

_diagnostic_events = queue.Queue(maxsize=512)
_key_event_count = 0
_last_key_event_at = None


def _diagnose(event, **details):
    """Keep disk I/O out of the Windows low-level keyboard hook callback."""
    try:
        _diagnostic_events.put_nowait((event, details))
    except queue.Full:
        pass


def _observe_key(_event):
    """Count keyboard events without recording keys or message content."""
    global _key_event_count, _last_key_event_at
    _key_event_count += 1
    _last_key_event_at = time.monotonic()


def _input_idle_seconds():
    if not sys.platform.startswith("win"):
        return None

    class LastInputInfo(ctypes.Structure):
        _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint))

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetTickCount.restype = ctypes.c_uint
    return round(((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000, 1)


def _diagnostic_loop():
    next_heartbeat = time.monotonic() + 60
    while True:
        try:
            event, details = _diagnostic_events.get(
                timeout=max(0, next_heartbeat - time.monotonic())
            )
            _logger.info("%s %s", event, json.dumps(details, ensure_ascii=False))
        except queue.Empty:
            pass

        if time.monotonic() >= next_heartbeat:
            last_key_age = (
                round(time.monotonic() - _last_key_event_at, 1)
                if _last_key_event_at is not None else None
            )
            backend_info = backend.diagnostics() if backend is not None else {}
            _logger.info(
                "heartbeat %s",
                json.dumps({
                    "pid": os.getpid(),
                    "enabled": _enabled,
                    "key_events": _key_event_count,
                    "last_key_age_s": last_key_age,
                    "system_input_idle_s": _input_idle_seconds(),
                    "processing_active": _lock.locked(),
                    **backend_info,
                }, ensure_ascii=False),
            )
            next_heartbeat = time.monotonic() + 60


core = ManosabaCore()
core.diagnostic = _diagnose
backend = None
_enabled = True
_lock = threading.Lock()
_active_run_started_at = None
_active_run_state_lock = threading.Lock()
_STUCK_RUN_SECONDS = 10


def _report_stuck_run(started_at):
    """Print a traceback if a native clipboard or keyboard call does not return."""
    with _active_run_state_lock:
        if _active_run_started_at != started_at:
            return
    _diagnose("processing_stuck", elapsed_s=_STUCK_RUN_SECONDS)
    print(f"[加喵] 本次处理已超过 {_STUCK_RUN_SECONDS} 秒，线程栈已写入诊断日志")
    try:
        with _STACK_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write("\n=== processing stuck: Python thread stacks ===\n")
            log_file.flush()
            faulthandler.dump_traceback(file=log_file, all_threads=True)
    except OSError:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def _run():
    global _active_run_started_at
    started_at = time.monotonic()
    with _active_run_state_lock:
        _active_run_started_at = started_at

    timer = threading.Timer(_STUCK_RUN_SECONDS, _report_stuck_run, args=(started_at,))
    timer.daemon = True
    timer.start()
    try:
        _diagnose("processing_started")
        print("[加喵] 正在处理…")
        result = core.send_text()
        _diagnose("processing_finished", result=result)
        print(f"[加喵] {result}")
    except Exception:
        _diagnose("processing_exception", traceback=traceback.format_exc())
        print("[加喵] 处理异常：")
        traceback.print_exc()
    finally:
        timer.cancel()
        with _active_run_state_lock:
            _active_run_started_at = None
        _lock.release()


def on_enter():
    """Run one conversion without ever blocking the global keyboard hook."""
    try:
        started = time.monotonic()
        allowed = core._active_process_allowed() if _enabled else False
        _diagnose(
            "enter_callback", enabled=_enabled, foreground_allowed=allowed,
            check_ms=round((time.monotonic() - started) * 1000, 1),
        )
        if not _enabled or not allowed:
            send_keys("enter")
            return

        # A hung native call used to keep this lock forever while every later
        # Enter created another blocked daemon thread.  Do not build that queue.
        if not _lock.acquire(blocking=False):
            _diagnose("enter_ignored", reason="previous_processing_active")
            print("[加喵] 上一次处理尚未结束，本次 Enter 已忽略")
            return
        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            _lock.release()
            raise
    except Exception:
        _diagnose("enter_exception", traceback=traceback.format_exc())
        print("[加喵] Enter 回调异常：")
        traceback.print_exc()


def toggle():
    """Ctrl+Alt+P 暂停/恢复"""
    global _enabled
    _enabled = not _enabled
    _diagnose("toggled", enabled=_enabled)
    print(f"[加喵] {'已恢复' if _enabled else '已暂停（Enter 恢复原始行为）'}")


try:
    backend = create_backend(on_enter, toggle, _observe_key)
    backend.start()
except Exception as exc:
    print(f"[加喵] 键盘监听启动失败：{exc}")
    traceback.print_exc()
    if sys.platform.startswith("linux"):
        print("  Linux 需要读取 /dev/input 并写入 /dev/uinput 的权限：")
        print("    临时方案: sudo -E python3 main.py   # -E 用于保留 Wayland/DISPLAY 环境变量")
        print("    长期方案: 把当前用户加入 input 组，并让 /dev/uinput 属于 input 组")
        print("              sudo usermod -aG input \"$USER\"   # 重新登录后生效")
    sys.exit(1)

threading.Thread(target=_diagnostic_loop, name="meow-diagnostics", daemon=True).start()
_diagnose("started", pid=os.getpid(), log_path=str(_LOG_PATH))
print(f"[加喵] 诊断日志目录: {_LOG_DIR}")
if sys.platform.startswith("linux"):
    from utils.clipboard_utils import clipboard_backend_name
    print(f"[加喵] 剪贴板后端: {clipboard_backend_name()}")

print("=" * 40)
print("  加喵发送器已启动")
print("  Enter       → 加喵处理后发送")
print("  Ctrl+Alt+P  → 暂停 / 恢复")
print("  Ctrl+C      → 退出")
print("=" * 40)

try:
    backend.wait()
except KeyboardInterrupt:
    print("\n[加喵] 已退出")
finally:
    backend.stop()
sys.exit(0)
