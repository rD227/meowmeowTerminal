"""魔裁文本框核心逻辑"""
from config import CONFIGS
from utils.clipboard_utils import ClipboardManager
from utils.miao_transform import process_message

import keyboard
import time
import psutil
import threading
from sys import platform

if platform.startswith("win"):
    try:
        import win32gui
        import win32process
    except ImportError:
        print("[red]请先安装 Windows 运行库: pip install pywin32[/red]")
        raise


class ManosabaCore:
    """魔裁文本框核心类"""

    def __init__(self):
        self.clipboard_manager = ClipboardManager()

    def update_status(self, message: str):
        """更新状态"""
        print(f"[状态] {message}")

    def _active_process_allowed(self) -> bool:
        """校验当前前台进程是否在白名单"""
        if not CONFIGS.process_whitelist:
            return True

        wl = {name.lower() for name in CONFIGS.process_whitelist}

        if platform.startswith("win"):
            try:
                hwnd = win32gui.GetForegroundWindow()
                if not hwnd:
                    return False
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                name = psutil.Process(pid).name().lower()
                return name in wl
            except (psutil.Error, OSError):
                return False

        elif platform == "darwin":
            try:
                import subprocess
                result = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of first process whose frontmost is true'],
                    capture_output=True, text=True, check=True,
                )
                return result.stdout.strip().lower() in wl
            except subprocess.SubprocessError:
                return False

        else:
            return True

    def send_text(self) -> str:
        """
        发送加喵文本。
        流程：拦截 Enter → 全选复制 → 加喵变换 → 粘贴（覆盖选区）→ Enter 发送

        用 ctrl+c 而不是 ctrl+x：剪切一旦后面任何一步失败，输入框里的原文就没了；
        复制的话失败时原文还在（粘贴会覆盖选区，成功路径的表现完全一样）。
        """
        if not self._active_process_allowed():
            return "前台应用不在白名单内"

        start_time = time.time()

        # 清空剪贴板，避免读到旧数据。清不掉就直接放弃本轮：
        # 否则可能把剪贴板里的旧内容当成用户输入框里的文字粘回去
        if not self.clipboard_manager.clear_clipboard():
            return "错误: 剪贴板被占用，已放弃本轮"
        time.sleep(0.01)

        # 全选 + 复制
        keyboard.send('ctrl+a')
        time.sleep(0.01)
        keyboard.send('ctrl+c')

        # 等待剪贴板写入（最多 2.5 秒）
        deadline = time.time() + 2.5
        text = ""
        while time.time() < deadline:
            text, _ = self.clipboard_manager.get_clipboard_all()
            if text and text.strip():
                break
            time.sleep(0.005)

        if not text or not text.strip():
            return "错误: 输入框为空"

        # 加喵变换
        transformed = process_message(text)
        print(f"[加喵] {text!r}")
        print(f"  → {transformed!r}")

        # 写回剪贴板（文本格式）
        if not self.clipboard_manager.copy_text_to_clipboard(transformed):
            return "复制文本到剪贴板失败"

        time.sleep(0.05)

        # 粘贴
        keyboard.send('ctrl+v')

        # 发送
        if CONFIGS.AUTO_SEND_IMAGE:
            time.sleep(0.3)
            keyboard.send('enter')

        return f"完成，用时 {int((time.time() - start_time) * 1000)}ms"
