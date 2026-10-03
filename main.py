"""加喵发送器 - 终端版入口"""
import ctypes
import faulthandler
import sys
import threading
import time
import traceback
import keyboard

from config import CONFIGS
from core import ManosabaCore


# 两个实例会互相把对方注入的回车当成真回车，无限互相触发（顺带互抢剪贴板），
# 所以启动时用命名互斥体把第二个实例挡在注册热键之前。
_MUTEX_NAME = "Local\\meowmeowTerminal.main"
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5

# 句柄必须一直持有到进程退出（不能 CloseHandle），退出时系统自动释放
_instance_mutex = None


def acquire_single_instance() -> bool:
    """拿到单实例锁返回 True，已有实例在跑返回 False。"""
    global _instance_mutex

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
    print("  查进程:  tasklist | findstr python")
    print("  结束它:  Stop-Process -Id <PID> -Force")
    print("=" * 40)
    sys.exit(1)


core = ManosabaCore()
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
    print(f"[meow] Processing has exceeded {_STUCK_RUN_SECONDS} seconds; traceback follows:")
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
        print("[meow] Processing...")
        result = core.send_text()
        print(f"[meow] {result}")
    except Exception:
        print("[meow] Processing failed with an exception:")
        traceback.print_exc()
    finally:
        timer.cancel()
        with _active_run_state_lock:
            _active_run_started_at = None
        _lock.release()


def on_enter():
    """Run one conversion without ever blocking the global keyboard hook."""
    try:
        if not _enabled or not core._active_process_allowed():
            keyboard.send("enter")
            return

        # A hung native call used to keep this lock forever while every later
        # Enter created another blocked daemon thread.  Do not build that queue.
        if not _lock.acquire(blocking=False):
            print("[meow] Previous processing is still running; ignored this Enter")
            return
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        print("[meow] Enter hotkey callback failed:")
        traceback.print_exc()


def _on_enter_legacy():
    """Enter 热键回调：加喵处理后发送"""
    if not _enabled or not core._active_process_allowed():
        # 暂停 或 前台窗口不在白名单：放行这次 Enter
        # （keyboard suppress 已拦截，需手动补发，否则会吞掉回车/中文候选确定）
        keyboard.send('enter')
        return
    # 在独立线程中执行，避免阻塞热键监听
    threading.Thread(target=_run, daemon=True).start()


def _run_legacy():
    with _lock:
        result = core.send_text()
        print(f"[加喵] {result}")


def toggle():
    """Ctrl+Alt+X 暂停/恢复"""
    global _enabled
    _enabled = not _enabled
    print(f"[加喵] {'已恢复' if _enabled else '已暂停（Enter 恢复原始行为）'}")


keyboard.add_hotkey('enter', on_enter, suppress=True)
keyboard.add_hotkey('ctrl+alt+p', toggle)

print("=" * 40)
print("  加喵发送器已启动")
print("  Enter       → 加喵处理后发送")
print("  Ctrl+Alt+P  → 暂停 / 恢复")
print("  Ctrl+C      → 退出")
print("=" * 40)

try:
    keyboard.wait()
except KeyboardInterrupt:
    print("\n[加喵] 已退出")
    sys.exit(0)
