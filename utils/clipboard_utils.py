"""剪贴板工具模块"""

import io
import os
import shutil
import tempfile
import subprocess
import re
import time
import threading
from contextlib import contextmanager
from sys import platform
import base64

from urllib.parse import urlparse, unquote
from urllib.request import url2pathname

from PIL import Image

from utils.session_env import desktop_env

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False


PLATFORM = platform.lower()

if PLATFORM.startswith("win"):
    try:
        import win32clipboard
    except ImportError:
        print("[red]请先安装 Windows 运行库: pip install pywin32[/red]")
        raise


# 剪贴板是全系统共用的独占资源：别的进程（聊天软件、输入法、剪贴板管理器、
# 云剪贴板）占用期间 OpenClipboard 会直接抛 ERROR_ACCESS_DENIED(5)。
# 之前是一失败就放弃，所以经常莫名其妙报"复制文本到剪贴板失败"，统一走这里退避重试。
_CLIPBOARD_RETRIES = 15
_CLIPBOARD_RETRY_DELAY = 0.02  # 最坏等约 300ms
_SUBPROCESS_TIMEOUT = 3
_linux_xclip_owners = []
_linux_xclip_owners_lock = threading.Lock()

# Linux 剪贴板后端：Wayland 下优先 wl-clipboard，X11 / XWayland 下用 xclip
_linux_clipboard_backend = None
# sudo 运行时补全原用户的 WAYLAND_DISPLAY / XDG_RUNTIME_DIR 等变量
_SESSION_ENV = desktop_env()


def _detect_linux_clipboard() -> str:
    """返回 "wayland" / "xclip" / ""（都不可用），结果缓存。"""
    global _linux_clipboard_backend
    if _linux_clipboard_backend is not None:
        return _linux_clipboard_backend

    if (_SESSION_ENV.get("WAYLAND_DISPLAY")
            and shutil.which("wl-copy") and shutil.which("wl-paste")):
        _linux_clipboard_backend = "wayland"
    elif shutil.which("xclip"):
        _linux_clipboard_backend = "xclip"
    else:
        _linux_clipboard_backend = ""
    return _linux_clipboard_backend


def clipboard_backend_name() -> str:
    """当前使用的剪贴板后端，用于启动提示和诊断。"""
    if PLATFORM.startswith("win"):
        return "win32"
    if PLATFORM == "darwin":
        return "pbcopy/pbpaste"
    return _detect_linux_clipboard() or "未找到（请安装 wl-clipboard 或 xclip）"


def _run_clipboard_command(args, data: bytes = b"", timeout: float = _SUBPROCESS_TIMEOUT) -> bool:
    """运行会自行退出的剪贴板命令（如默认 fork 到后台的 wl-copy）。

    不用 subprocess.run(capture_output=True)：wl-copy 的后台子进程会继承管道，
    communicate() 可能一直等不到 EOF 而超时，所以 stdout/stderr 一律丢给 DEVNULL。
    """
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_SESSION_ENV,
        )
    except OSError as e:
        print(f"Unable to run {args[0]}: {e}")
        return False

    try:
        proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print(f"{args[0]} timed out")
        return False
    return proc.returncode == 0


def _serve_xclip(args, data: bytes) -> bool:
    """启动 xclip 并让它常驻持有 selection，不等待它退出。"""
    try:
        proc = subprocess.Popen(
            ["xclip", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=_SESSION_ENV,
        )
        proc.stdin.write(data)
        proc.stdin.close()
    except (OSError, BrokenPipeError) as e:
        print(f"Unable to start xclip: {e}")
        return False

    # Surface immediate command failures without waiting for xclip to stop
    # owning the selection, which can legitimately take an unlimited time.
    time.sleep(0.02)
    if proc.poll() not in (None, 0):
        error = proc.stderr.read().decode("utf-8", errors="replace").strip()
        print(f"xclip failed: {error or proc.returncode}")
        return False

    with _linux_xclip_owners_lock:
        _linux_xclip_owners[:] = [p for p in _linux_xclip_owners if p.poll() is None]
        _linux_xclip_owners.append(proc)
    return True


@contextmanager
def _open_clipboard():
    """打开剪贴板，被占用时退避重试。

    yield 出 True 表示成功打开（离开 with 时自动 CloseClipboard），
    全部重试失败则 yield False。
    """
    opened = False
    for _ in range(_CLIPBOARD_RETRIES):
        try:
            win32clipboard.OpenClipboard()
            opened = True
            break
        except Exception:
            time.sleep(_CLIPBOARD_RETRY_DELAY)

    try:
        yield opened
    finally:
        if opened:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass


def _clipboard_write(write, retries=_CLIPBOARD_RETRIES) -> bool:
    """往剪贴板写数据，被占用时整套退避重试。

    注意不能只重试 OpenClipboard：打开成功之后，EmptyClipboard/SetClipboardData
    仍然可能被别的进程抢走（ERROR_CLIPBOARD_NOT_OPEN 1418），所以
    open → 写 → close 要一起重试。
    """
    last_error = None

    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
        except Exception as e:
            last_error = e
            time.sleep(_CLIPBOARD_RETRY_DELAY)
            continue

        try:
            write()
            return True
        except Exception as e:
            last_error = e
            time.sleep(_CLIPBOARD_RETRY_DELAY)
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

    print(f"写剪贴板失败（重试 {retries} 次）: {last_error}")
    return False


class ClipboardManager:
    """剪贴板管理器"""

    def __init__(self):
        self.platform = PLATFORM

    @staticmethod
    def _copy_text_linux(text: str) -> bool:
        backend = _detect_linux_clipboard()
        if backend == "wayland":
            return _run_clipboard_command(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                text.encode("utf-8"),
            )
        if backend == "xclip":
            return _serve_xclip(["-selection", "clipboard", "-in"], text.encode("utf-8"))
        print("未找到 wl-copy 或 xclip，无法写入剪贴板（请安装 wl-clipboard 或 xclip）")
        return False

    def copy_text_to_clipboard(self, text: str) -> bool:
        """Copy plain text with a bounded foreground operation on every platform."""
        try:
            if self.platform.startswith("win"):
                def _write():
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
                return _clipboard_write(_write)
            if self.platform == "darwin":
                result = subprocess.run(
                    ["pbcopy"], input=text.encode("utf-8"), capture_output=True,
                    check=False, timeout=_SUBPROCESS_TIMEOUT,
                )
                return result.returncode == 0
            return self._copy_text_linux(text)
        except subprocess.TimeoutExpired:
            print("Copying text to the clipboard timed out")
            return False
        except Exception as e:
            print(f"Copying text to the clipboard failed: {e}")
            return False

    def get_text(self) -> str:
        """Read text only; do not request image/HTML clipboard formats here."""
        try:
            if self.platform.startswith("win"):
                with _open_clipboard() as opened:
                    if not opened:
                        return ""
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                        return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                    return ""
            if self.platform == "darwin":
                result = subprocess.run(
                    ["pbpaste"], capture_output=True, check=False,
                    timeout=_SUBPROCESS_TIMEOUT,
                )
            elif _detect_linux_clipboard() == "wayland":
                result = subprocess.run(
                    ["wl-paste", "--no-newline", "--type", "text"],
                    capture_output=True, check=False, timeout=_SUBPROCESS_TIMEOUT,
                    env=_SESSION_ENV,
                )
            else:
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-out"],
                    capture_output=True, check=False, timeout=_SUBPROCESS_TIMEOUT,
                    env=_SESSION_ENV,
                )
            if result.returncode != 0:
                return ""
            return result.stdout.decode("utf-8", errors="replace")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"Reading the clipboard failed: {e}")
            return ""

    def describe_clipboard(self) -> str:
        """读取失败时记录剪贴板格式，用于区分"真的为空"和"类型不匹配"。"""
        try:
            if self.platform.startswith("win"):
                with _open_clipboard() as opened:
                    if not opened:
                        return "无法打开剪贴板"
                    formats = []
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                        formats.append("CF_UNICODETEXT")
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
                        formats.append("CF_DIB")
                    return ", ".join(formats) or "空"

            if self.platform == "darwin":
                return ""

            backend = _detect_linux_clipboard()
            if backend == "wayland":
                result = subprocess.run(
                    ["wl-paste", "--list-types"],
                    capture_output=True, text=True, check=False,
                    timeout=_SUBPROCESS_TIMEOUT, env=_SESSION_ENV,
                )
                types = result.stdout.split() if result.returncode == 0 else []
                return ", ".join(types) or "空"

            if backend == "xclip":
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"],
                    capture_output=True, check=False, timeout=_SUBPROCESS_TIMEOUT,
                    env=_SESSION_ENV,
                )
                if result.returncode != 0:
                    return "空"
                return ", ".join(
                    result.stdout.decode("utf-8", errors="replace").split()
                ) or "空"

            return ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _copy_text_to_clipboard_legacy(self, text: str) -> bool:
        """将文本复制到剪贴板"""
        try:
            if self.platform.startswith("win"):
                def _write():
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)

                return _clipboard_write(_write)
            if self.platform == "darwin":
                import subprocess
                result = subprocess.run(
                    ["pbcopy"],
                    input=text.encode("utf-8"),
                    capture_output=True,
                    check=False,
                )
                return result.returncode == 0
            # Linux
            return self._copy_text_linux(text)
        except Exception as e:
            print(f"复制文本到剪贴板失败: {e}")
            return False

    def copy_image_to_clipboard(self, bmp_bytes: bytes) -> bool:
        """将BMP字节数据复制到剪贴板"""
        try:
            if self.platform == "darwin":
                return self._copy_image_macos(bmp_bytes)
            if self.platform.startswith("win"):
                return self._copy_image_windows(bmp_bytes)
            return self._copy_image_linux(bmp_bytes)
        except Exception as e:
            print(f"复制图片到剪贴板失败: {e}")
            return False

    def _copy_image_macos(self, png_bytes: bytes) -> bool:
        """macOS 复制图片到剪贴板"""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png_bytes)
            tmp_path = tmp.name

        cmd = f"""osascript -e 'set the clipboard to (read (POSIX file "{tmp_path}") as «class PNGf»)'"""
        result = subprocess.run(cmd, shell=True, capture_output=True, check=False)

        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        return result.returncode == 0

    def _copy_image_windows(self, bmp_bytes: bytes) -> bool:
        """Windows 复制图片到剪贴板"""
        try:
            def _write():
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp_bytes)

            return _clipboard_write(_write)
        except Exception as e:
            print(f"Windows 复制图片失败: {e}")
            return False

    def _copy_image_linux(self, png_bytes: bytes) -> bool:
        """Linux 复制图片到剪贴板（PNG 字节）"""
        backend = _detect_linux_clipboard()
        if backend == "wayland":
            return _run_clipboard_command(["wl-copy", "--type", "image/png"], png_bytes)
        if backend == "xclip":
            return _serve_xclip(
                ["-selection", "clipboard", "-t", "image/png", "-in"], png_bytes
            )
        print("未找到 wl-copy 或 xclip，无法复制图片")
        return False

    def has_image_in_clipboard(self) -> bool:
        """检查剪贴板中是否有图片"""
        try:
            if platform.startswith("win"):
                with _open_clipboard() as opened:
                    if not opened:
                        return False
                    # 尝试获取剪贴板中的图片数据
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
                        return True
                    return False
            if platform == "darwin":
                # macOS 的实现
                from AppKit import NSPasteboard, NSPasteboardTypePNG

                pasteboard = NSPasteboard.generalPasteboard()
                return (
                    pasteboard.availableTypeFromArray_([NSPasteboardTypePNG])
                    is not None
                )
            # Linux 的实现
            backend = _detect_linux_clipboard()
            if backend == "wayland":
                result = subprocess.run(
                    ["wl-paste", "--list-types"],
                    capture_output=True, check=False, timeout=_SUBPROCESS_TIMEOUT,
                    env=_SESSION_ENV,
                )
                return result.returncode == 0 and any(
                    mime.startswith(b"image/") for mime in result.stdout.split()
                )
            if backend == "xclip":
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"],
                    capture_output=True, check=False, timeout=_SUBPROCESS_TIMEOUT,
                    env=_SESSION_ENV,
                )
                return result.returncode == 0 and b"image/" in result.stdout
            return False
        except Exception:
            return False
    
    def clear_clipboard(self) -> bool:
        """清空剪贴板，返回是否成功。

        失败必须让调用方知道：清不掉的话，后面读到的可能是剪贴板里的旧内容，
        会被当成用户输入框里的文字。
        """
        if self.platform.startswith("win"):
            try:
                return _clipboard_write(win32clipboard.EmptyClipboard)
            except Exception as e:
                print(f"清空剪贴板失败: {e}")
                return False

        if self.platform == "darwin":
            try:
                result = subprocess.run(
                    ["pbcopy"], input=b"", check=False, timeout=_SUBPROCESS_TIMEOUT,
                )
                return result.returncode == 0
            except (OSError, subprocess.TimeoutExpired) as e:
                print(f"清空剪贴板失败: {e}")
                return False

        backend = _detect_linux_clipboard()
        if backend == "wayland":
            return _run_clipboard_command(["wl-copy", "--clear"])
        if backend == "xclip":
            return _serve_xclip(["-selection", "clipboard", "-in"], b"")
        return True

    def get_clipboard_all(self):
        # get_clipboard_all 的图片/HTML 解析目前只实现了 Windows 版本
        if not self.platform.startswith("win"):
            return self.get_text(), None

        text = ""
        image = None

        with _open_clipboard() as opened:
            if not opened:
                return "", None

            try:
                # 1️⃣ 优先直接取位图（真正的图片）
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
                    data = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
                    header = (
                        b"BM"
                        + (len(data) + 14).to_bytes(4, "little")
                        + b"\x00\x00\x00\x00\x36\x00\x00\x00"
                    )
                    image = Image.open(io.BytesIO(header + data))
                    image.load()

                # 2️⃣ 取纯文本
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)

                # 3️⃣ 如果没有真正的位图，但有 HTML → 从 HTML 解析图片
                if image is None:
                    html = self._get_clipboard_html()
                    if html:
                        html_text, html_image = self.parse_html_clipboard(html)

                        if html_image is not None:
                            image = html_image
            except Exception:
                return "", None

        return text, image

    def _get_clipboard_html(self):
        html_format = win32clipboard.RegisterClipboardFormat("HTML Format")

        if not win32clipboard.IsClipboardFormatAvailable(html_format):
            return None

        html_bytes = win32clipboard.GetClipboardData(html_format)

        if isinstance(html_bytes, bytes):
            html = html_bytes.decode("utf-8", errors="ignore")
        else:
            html = html_bytes

        return html

    def _extract_img_src_from_html(self, html: str) -> str | None:
        if not _BS4_AVAILABLE:
            return None
        soup = BeautifulSoup(html, 'html.parser')
        img_tag = soup.find('img')
        return img_tag.get('src') if img_tag else None

    def _file_uri_to_path(self, uri: str) -> str | None:
        # 把 file:// URI 转成本地路径。
        # 兼容：
        # - file:///C:/path/to/x.jpg
        # - file:///E:\path\to\x.jpg
        # - file://E:/path...
        # - C:\\path\\to\\x.jpg (如果传入的是本地路径也直接返回)
        # 返回 None 表示转换失败

        if not uri:
            return None

        # 已经是本地路径（包含盘符或 UNC），直接返回规范化路径
        # 例如: C:\... 或 \\server\share\...
        if re.match(r'^[a-zA-Z]:[\\/]', uri) or uri.startswith('\\\\'):
            return os.path.normpath(uri)

        # 如果是 file URI
        parsed = urlparse(uri)
        if parsed.scheme and parsed.scheme.lower() == 'file':
            # parsed.path 在 windows 上通常是 '/C:/path...'，需要转换
            # 使用 url2pathname 处理转义和驱动器前导斜杠
            try:
                path = url2pathname(parsed.path)
                # url2pathname 在 windows 上通常返回 r'C:\...'
                return os.path.normpath(path)
            except Exception:
                # 兜底：手工 decode 并去掉可能的前导 '/'
                p = unquote(parsed.path)
                if p.startswith('/') and re.match(r'^/[A-Za-z]:', p):
                    p = p[1:]
                p = p.replace('/', os.sep)
                return os.path.normpath(p)

        # 有些实现会把 file URI 写成 file:///E:\...（反斜杠混合），尝试把 file:// 前缀去掉再处理
        if uri.startswith('file://'):
            rest = uri[7:]
            # 如果以 /E: 开头则去掉前导 /
            if rest.startswith('/') and re.match(r'^/[A-Za-z]:', rest):
                rest = rest[1:]
            rest = unquote(rest)
            return os.path.normpath(rest)

        # 如果包含 file:/// 但不合常规，尝试简单替换
        if uri.startswith('file:'):
            rest = uri[len('file:'):]
            rest = rest.lstrip('/')
            rest = unquote(rest)
            return os.path.normpath(rest)

        # 不是 file URI，也不是本地路径
        return None

    def parse_html_clipboard(self, html: str):
        """
        返回 (text, image)：
        - text: html 去标签的纯文本（简单清理）
        - image: PIL.Image or None（如果能从 img src 读取到文件）
        """
        text = None
        image = None

        # 提取 img src
        src = self._extract_img_src_from_html(html)
        if src:
            # src 可能是 file:// URI、绝对路径、也可能是 http(s)（少见）
            # 处理 file:// 或本地路径
            path = self._file_uri_to_path(src)
            if path:
                try:
                    # 尝试打开文件（注意微信临时文件可能会被占用/有权限问题）
                    # 先检查文件是否存在
                    if os.path.exists(path):
                        with Image.open(path) as im:
                            image = im.copy()  # 复制到内存，避免后续文件被删除导致问题
                    else:
                        print(f"HTML图片文件不存在: {path}")
                except Exception as e:
                    # 记录错误，继续尝试其它方式或返回 None
                    print(f"HTML图片加载失败（open file）: {e}, path: {path}")

            else:
                # src 不是本地 path（可能是 data: URI 或 http(s)）
                # 处理 data: URI 的情况
                if src.startswith('data:'):
                    # data:[<mediatype>][;base64],<data>
                    try:
                        # 找到 base64 数据的起始位置
                        if ';base64,' in src:
                            header, b64 = src.split(';base64,', 1)
                            raw = base64.b64decode(b64)
                            image = Image.open(io.BytesIO(raw)).copy()
                        else:
                            # 如果没有明确的 base64 标记，尝试直接解码
                            comma_idx = src.find(',')
                            if comma_idx != -1:
                                b64 = src[comma_idx+1:]
                                raw = base64.b64decode(b64)
                                image = Image.open(io.BytesIO(raw)).copy()
                    except Exception as e:
                        print(f"HTML图片加载失败（data URI）: {e}")
                elif src.startswith('http://') or src.startswith('https://'):
                    # 如果想支持远程抓取，可以在此处 requests.get(src) 然后 Image.open(BytesIO(...))
                    # 但注意网络依赖和超时策略
                    print(f"HTML 图片为远程 URL，未下载。URL: {src}")
                else:
                    # 尝试直接作为路径处理
                    try:
                        if os.path.exists(src):
                            with Image.open(src) as im:
                                image = im.copy()
                        else:
                            print(f"HTML 图片 src 无法识别或不可访问: {src}")
                    except Exception as e:
                        print(f"HTML图片加载失败（直接路径）: {e}")

        # 提取文本：移除注释和标签，解 html 实体等（这里使用简单方法）
        try:
            # 取 StartFragment-EndFragment 间的内容优先（许多程序会包含）
            # 使用非贪婪匹配，并正确处理换行
            frag_match = re.search(r'<!--StartFragment-->(.*?)<!--EndFragment-->', html, flags=re.DOTALL)
            if frag_match:
                fragment = frag_match.group(1)
            else:
                # 如果没有找到片段标记，使用整个HTML
                fragment = html
                
            # 去标签，但保留文本内容
            # 使用更稳健的方法：先移除脚本和样式标签
            fragment = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', fragment, flags=re.DOTALL)
            
            # 移除HTML标签
            text = re.sub(r'<[^>]+>', '', fragment)
            
            # 解常见 HTML 实体
            html_entities = {
                '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>',
                '&quot;': '"', '&#39;': "'", '&nbsp': ' '
            }
            
            for entity, char in html_entities.items():
                text = text.replace(entity, char)
            
            # 移除多余空白字符
            text = re.sub(r'\s+', ' ', text).strip()
            
            # 如果文本为空但HTML不为空，可能还有嵌套结构
            if not text and html:
                # 尝试更激进的方法：提取所有可见文本
                text_only = re.sub(r'<[^>]+>', ' ', html)
                text_only = re.sub(r'\s+', ' ', text_only).strip()
                if text_only:
                    text = text_only
                    
        except Exception as e:
            print(f"提取文本失败: {e}")
            text = html

        return text, image
