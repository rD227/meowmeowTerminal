"""
加喵文字转换模块
基于 main.java 的 getMsg() + modifyMessage() 逻辑移植
"""
import re

# 特殊符号手动列表（遇到时在其前面插入"喵"）
# 等同于 main.java 中的 specialSymbols 字段
_SPECIAL_SYMBOLS: set[str] = set('嗷呜？！?!。，,～~;；()（）：:."')


def _is_emoji(cp: int) -> bool:
    """判断 Unicode 码点是否为 emoji（对应 main.java isEmoji）"""
    return (
        0x2300 <= cp <= 0x27BF    # 杂项符号、符文、装饰符
        or 0x2900 <= cp <= 0x297F  # 补充箭头
        or 0x1F000 <= cp <= 0x1FFFF  # 主力 emoji 区（😀🎉🐱 等）
        or 0xFE00 <= cp <= 0xFE0F    # 变体选择符（emoji 修饰后缀）
    )


def _is_special_char(ch: str) -> bool:
    """
    判断单个字符是否为特殊符号（对应 main.java isSpecialChar）。
    Python 3 字符串天然按码点存储，无需处理代理对。
    """
    cp = ord(ch)
    return ch in _SPECIAL_SYMBOLS or _is_emoji(cp)


def _ends_with_miao(buf: list[str]) -> bool:
    """检查当前缓冲区末尾是否已经是'喵'"""
    for s in reversed(buf):
        if s:
            return s[-1] == '喵'
    return False


def process_message(msg: str) -> str:
    """
    完整处理流程，对应 main.java 的 getMsg()：
    1. 骂人词始终替换
    2. 词语喵化替换（什么→什喵 等）
    3. 行级加喵变换（modifyMessage）
    """
    # 骂人词替换（始终生效）
    for src in ("他妈", "踏马", "他么", "他马"):
        msg = msg.replace(src, "他喵")
    msg = re.sub(r'\btm\b', '他喵', msg, flags=re.ASCII | re.IGNORECASE)

    # 词语喵化
    msg = (msg
           .replace("什么", "什喵")
           .replace("怎么", "怎喵")
           .replace("在吗", "在嘛")
           .replace("不要啊", "不要呀"))

    return modify_message(msg)


def modify_message(msg: str) -> str:
    """
    行级加喵变换（对应 main.java modifyMessage），通常不直接调用，
    请使用 process_message() 获得完整处理。
    """
    lines = msg.split("\n")
    result_lines: list[str] = []

    for line in lines:
        # http / https / 标题行不处理
        if (line.startswith("http://")
                or line.startswith("https://")
                or line.startswith("#")):
            result_lines.append(line)
            continue

        buf: list[str] = []
        insert_symbol = False  # True = 上一个字符已经是特殊符号，避免连续重复加喵
        i = 0
        n = len(line)

        while i < n:
            ch = line[i]

            if ch == '.':
                # 统计连续点数
                dot_end = i
                while dot_end < n and line[dot_end] == '.':
                    dot_end += 1
                dot_count = dot_end - i

                if dot_count >= 3:
                    # 省略号 → 加喵
                    if not _ends_with_miao(buf):
                        buf.append('喵')
                    insert_symbol = True
                else:
                    # 普通点号（小数点、路径分隔符等）→ 不加喵
                    insert_symbol = False

                buf.append(line[i:dot_end])
                i = dot_end

            elif _is_special_char(ch):
                if not insert_symbol:
                    if not _ends_with_miao(buf):
                        buf.append('喵')
                    insert_symbol = True
                buf.append(ch)
                i += 1

            else:
                insert_symbol = False
                buf.append(ch)
                i += 1

        # 句尾补喵：末尾字符不是特殊符号且不以"喵"结尾
        if line and not _is_special_char(line[-1]) and not _ends_with_miao(buf):
            buf.append('喵')

        result_lines.append(''.join(buf))

    return "\n".join(result_lines)
