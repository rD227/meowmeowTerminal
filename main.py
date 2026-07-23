"""加喵发送器 - 终端版入口"""
import sys
import threading
import keyboard

from config import CONFIGS
from core import ManosabaCore

core = ManosabaCore()
_enabled = True
_lock = threading.Lock()


def on_enter():
    """Enter 热键回调：加喵处理后发送"""
    if not _enabled:
        # 未启用时放行这次 Enter（keyboard suppress 已拦截，需手动补发）
        keyboard.send('enter')
        return
    # 在独立线程中执行，避免阻塞热键监听
    threading.Thread(target=_run, daemon=True).start()


def _run():
    with _lock:
        result = core.send_text()
        print(f"[加喵] {result}")


def toggle():
    """Ctrl+Alt+P 暂停/恢复"""
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
