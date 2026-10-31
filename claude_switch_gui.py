"""Claude 桌面端一键切换账号 —— 图形界面版。

单击账号卡片就切换；卡片上显示每个账号的 5 小时额度和周额度（来自桌面端自己的用量记录，
见 plan_usage.py），可以改名。切换、添加、撤销的具体做法都在 claude_switch.py 里，这里只管界面。

桌面图标用 pythonw.exe 打开它，不弹黑窗口。测试时可以加 --data-dir / --store / --no-app，
含义和 claude_switch.py 一样。
"""

import argparse
import ctypes
import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from datetime import datetime
from pathlib import Path

import claude_switch as cs
import plan_usage
import sync_sessions as sync

TITLE = "CLAUDE//SWITCH"
# 战术终端配色：中性的黑灰打底，白色是主色（当前账号、余量充足）；
# 红色只做点缀（雷达扫描线、鼠标瞄准、余量快没了），橙色表示进行中 / 余量一般
BG = "#0a0909"
PANEL = "#121011"
PANEL_HOT = "#1c1314"  # 鼠标瞄准时卡片的底色，带一点点红
CONSOLE = "#0c0b0b"
LINE = "#353031"
FAINT = "#1d1a1b"
TICK = "#4a4445"
DIM = "#6d6869"
SOFT = "#a39d9e"
BRIGHT = "#ece8e8"
AMBER = "#ff9a3c"
RED = "#ff3b4a"
MONO = "Consolas"
CJK = "Microsoft YaHei UI"
HUD = "Bahnschrift SemiBold Condensed"  # 窄体，像科幻界面的标签字；系统没有就退回 Consolas
HUD_LIGHT = "Bahnschrift Light Condensed"  # 大时钟用的细体
HEX = "0123456789ABCDEF"
KANA = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"
SCRAMBLE = HEX + KANA + "#$%&*+=?@<>/"
RELOAD_MS = 15000  # 多久重新读一次账号和用量文件
UNSAVED = "\0unsaved"  # 当前登录但还没保存的账号，卡片用这个键
ICON = Path(__file__).resolve().parent / "assets" / "claude_switch.ico"
SIDEBAR = 230  # 左侧栏宽度（96 DPI 下的像素，高分屏自动放大）
MAIN = 580  # 右侧账号卡片的宽度
RADAR_Y, RADAR_R = 160, 80  # 雷达圆心高度和半径


def setup_windows() -> None:
    """高分屏不发虚；任务栏上用自己的图标，不跟 Python 混在一起。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("claude-switch.gui")
    except (AttributeError, OSError):
        pass


def dark_titlebar(root: tk.Tk) -> None:
    """Windows 标题栏也换成深色（窗口显示出来之后调用）。"""
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attribute in (20, 19):  # Win10 20H1 之后是 20，更早的是 19
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED：让标题栏马上重画
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x1 | 0x2 | 0x4 | 0x20)
    except (AttributeError, OSError):
        pass


def make_icon(root: tk.Tk):
    """窗口和任务栏图标：用 assets 里和桌面图标同一个 .ico；文件不在时退回 32×32 的「>_」像素图标。"""
    try:
        root.iconbitmap(default=str(ICON))
        return None
    except tk.TclError:
        pass
    image = tk.PhotoImage(width=32, height=32)
    image.put(BG, to=(0, 0, 32, 32))
    for i in range(6):
        x = 5 + i * 2
        image.put(BRIGHT, to=(x, 5 + i * 2, x + 4, 9 + i * 2))
        image.put(BRIGHT, to=(x, 23 - i * 2, x + 4, 27 - i * 2))
    image.put(BRIGHT, to=(18, 22, 28, 26))
    for edge in ((0, 0, 32, 1), (0, 31, 32, 32), (0, 0, 1, 32), (31, 0, 32, 32)):
        image.put(LINE, to=edge)
    root.iconphoto(True, image)
    return image


def font(text: str, size: int, bold: bool = False, hud: bool = False) -> tuple:
    """含中文的用雅黑（Consolas 里没有中文，系统替补的字发糊）；其余用等宽的 Consolas，
    标签类文字（hud=True）用窄体 HUD 字体。"""
    if any(ord(ch) >= 0x2E80 for ch in text):
        family = CJK
    elif hud and HUD != MONO:
        return (HUD, size + 1)  # 窄体本身就是半粗，不再加粗；字面偏小，放大一号
    else:
        family = MONO
    return (family, size, "bold") if bold else (family, size)


def check_fonts(root: tk.Tk) -> None:
    """老版本 Windows 没有 Bahnschrift，标签字退回 Consolas。"""
    global HUD, HUD_LIGHT
    families = set(tkfont.families(root))
    if HUD not in families:
        HUD = MONO
    if HUD_LIGHT not in families:
        HUD_LIGHT = HUD


def countdown(ms: int) -> str:
    seconds = max(0, int(ms // 1000))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{days}d{hours:02d}h{minutes:02d}m" if days else f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def ago(ms: int) -> str:
    seconds = max(0, int(ms // 1000))
    if seconds < 60:
        return "JUST NOW"
    if seconds < 3600:
        return f"{seconds // 60}m AGO"
    if seconds < 86400:
        return f"{seconds // 3600}h AGO"
    return f"{seconds // 86400}d AGO"


def level(remaining: int) -> str:
    return BRIGHT if remaining >= 50 else AMBER if remaining >= 20 else RED


def mix(a: str, b: str, t: float) -> str:
    """两个 #rrggbb 颜色按比例混合。"""
    pa, pb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


class QueueWriter:
    """后台操作里 print 的内容转进队列，由界面线程显示到控制台。"""

    def __init__(self, target: queue.Queue):
        self.target = target

    def write(self, text: str) -> int:
        if text:
            self.target.put(("out", text))
        return len(text)

    def flush(self) -> None:
        pass


class Card:
    """一个账号一张卡片，整张画在画布上：四角瞄准框、两条带刻度的额度条。单击切换，「改名」单独处理。"""

    HEIGHT = 128

    def __init__(self, app, parent, key: str):
        self.app, self.key = app, key
        self.hover = False
        self.canvas = tk.Canvas(parent, width=app.px(MAIN), height=app.px(self.HEIGHT), bg=PANEL,
                                highlightthickness=0, cursor="hand2")
        self.canvas.bind("<Enter>", lambda _: self.set_hover(True))
        self.canvas.bind("<Leave>", lambda _: self.set_hover(False))
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.tag_bind("rename", "<Enter>", lambda _: self.canvas.itemconfigure("rename", fill=RED))
        self.canvas.tag_bind("rename", "<Leave>", lambda _: self.canvas.itemconfigure("rename", fill=DIM))

    def set_hover(self, hover: bool) -> None:
        if hover != self.hover:
            self.hover = hover
            self.app.render()

    def on_click(self, _event) -> None:
        current = self.canvas.find_withtag("current")
        if current and "rename" in self.canvas.gettags(current[0]):
            self.app.on_rename(self.key)
        else:
            self.app.on_card(self.key)

    def show(self, number: int, label: str, account: str, saved_at: str, active: bool,
             switching: bool, usage, now: int, busy: bool) -> None:
        c, px = self.canvas, self.app.px
        w, h = px(MAIN), px(self.HEIGHT)
        unsaved = self.key == UNSAVED
        hot = self.hover and not busy and (unsaved or not active)
        c.delete("all")
        c.configure(bg=PANEL_HOT if hot else PANEL, cursor="hand2" if hot else "arrow")
        blink = int(time.time() * 4) % 2  # 切换中瞄准框闪烁
        corner = ((AMBER if blink else LINE) if switching else AMBER if unsaved else BRIGHT if active
                  else RED if hot else LINE)
        arm = px(22 if hot else 14)
        for x, y, dx, dy in ((1, 1, 1, 1), (w - 2, 1, -1, 1), (1, h - 2, 1, -1), (w - 2, h - 2, -1, -1)):
            c.create_line(x, y + dy * arm, x, y, x + dx * arm, y, fill=corner, width=2)

        left, right, top = px(24), w - px(24), px(24)
        index = c.create_text(left, top, text=f"TGT-{number:02d}", font=font("T", 10, hud=True), fill=SOFT, anchor="w")
        name = "未保存的当前账号" if unsaved else label
        item = c.create_text(c.bbox(index)[2] + px(10), top, text=name, font=font(name, 15, True),
                             fill=AMBER if unsaved else BRIGHT, anchor="w")
        if hot and not unsaved:
            c.create_text(c.bbox(item)[2] + px(12), top + px(1), text="✎ 改名", font=font("改名", 9), fill=DIM,
                          anchor="w", tags="rename")
        if switching:
            self.tag(right, top, f"» LINKING{'.' * (int(time.time() * 2) % 4):<3}", AMBER)
        elif unsaved:
            self.tag(right, top, "◉ 未保存 · 单击保存", BG, fill=AMBER)
        elif active:
            self.tag(right, top, "● LOCKED", BG, fill=BRIGHT)
        elif hot:
            self.tag(right, top, "◎ ACQUIRE ▸ 切换", RED)
        else:
            self.tag(right, top, "STANDBY", DIM)
        saved = "UNSAVED" if unsaved else f"SAVED {saved_at}"
        data = f"DATA {ago(now - usage.sampled_at)}" if usage else "DATA --"
        c.create_text(left, px(48), text=f"UID {account[:8]}  ·  {saved}  ·  {data}", font=(MONO, 9), fill=DIM, anchor="w")
        self.meter(px(78), "5H", usage.five_hour if usage else None, now)
        self.meter(px(106), "7D", usage.weekly if usage else None, now)

    def tag(self, x, y, text, color, fill=None) -> None:
        """右上角的状态字；给了 fill 就是实心色块。"""
        c, px = self.canvas, self.app.px
        pad = px(8) if fill else 0
        item = c.create_text(x - pad, y, text=text, font=font(text, 9, True, hud=True), fill=color, anchor="e")
        if fill:
            x0, y0, x1, y1 = c.bbox(item)
            c.tag_lower(c.create_rectangle(x0 - pad, y0 - px(2), x1 + pad, y1 + px(2), fill=fill, outline=""), item)

    def meter(self, y, key, window, now) -> None:
        """一条额度：名称 + 细条（下面带 10 格刻度）+ 剩余百分比 + 恢复时间。"""
        c, px = self.canvas, self.app.px
        w = px(MAIN)
        x0, x1 = px(62), w - px(214)
        c.create_text(px(24), y, text=key, font=font(key, 10, hud=True), fill=SOFT, anchor="w")
        c.create_line(x0, y, x1, y, fill=FAINT, width=px(4))
        for i in range(11):
            tick = x0 + (x1 - x0) * i / 10
            c.create_line(tick, y + px(5), tick, y + px(8), fill=TICK)
        if window is None:
            value, color, note, note_color = "--", DIM, "用这个账号登录后自动记录", DIM
        else:
            color = level(window.remaining)
            if window.remaining > 0:
                c.create_line(x0, y, x0 + (x1 - x0) * window.remaining / 100, y, fill=color, width=px(4))
            value = f"{window.remaining}%"
            if window.note:
                note, note_color = window.note, SOFT
            elif window.reset_by is None:
                note, note_color = "恢复时间 --", DIM
            else:
                note, note_color = f"恢复 ≤ T-{countdown(window.reset_by - now)}", SOFT
        c.create_text(w - px(166), y, text=value, font=(MONO, 10, "bold"), fill=color, anchor="e")
        c.create_text(w - px(156), y, text=note, font=font(note, 9), fill=note_color, anchor="w")


class App:
    BUTTONS = (("add", "添加账号", "ADD"), ("sync", "同步会话", "SYNC"), ("undo", "撤销上次", "UNDO"),
               ("refresh", "刷新", "REFRESH"))

    def __init__(self, root: tk.Tk, env: cs.Env):
        self.root, self.env = root, env
        self.init_state()
        check_fonts(root)
        self.cards, self.card_keys = [], None
        self.blink = False
        self.started = time.time()
        self.typing, self.typing_active, self.typed = deque(), False, 0  # 控制台逐字「解密」的队列
        self.sweep = 0.0  # 雷达扫描线的角度
        self.blips = []  # 雷达上的账号光点
        self.waiting_login = False  # 等新账号登录时，侧栏多一个「取消等待登录」
        self.buttons_enabled = True
        self.button_hover = None
        self.button_boxes = []

        root.title(TITLE)
        root.configure(bg=BG)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self.on_error
        self.icon = make_icon(root)
        # 先在屏幕外显示一下拿到窗口句柄，把标题栏设成深色；搭好、摆正再正式显示
        root.geometry("+-4000+-4000")
        root.update()
        dark_titlebar(root)
        root.withdraw()
        self.build()
        self.reload()
        self.render()
        self.center()
        root.deiconify()
        self.boot()
        self.tick_events()
        self.tick_clock()
        self.tick_radar()

    def init_state(self) -> None:
        """和界面样式无关的状态（后台操作、当前账号、用量记录）。"""
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.events = queue.Queue()
        self.pending = ""  # 后台 print 还没凑满一行的部分
        self.busy = None  # 正在进行的操作名称
        self.switching = None  # 正在切换到的账号名
        self.profiles, self.current = [], None
        self.snapshot = plan_usage.Snapshot(self.env.data_dir)
        self.last_reload = 0.0

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    # ---------- 搭界面 ----------

    def build(self) -> None:
        """左边侧栏（标志、雷达、时钟、读数、按钮），右边账号列表和控制台。"""
        px = self.px
        self.side = tk.Canvas(self.root, width=px(SIDEBAR), height=px(700), bg=BG, highlightthickness=0)
        self.side.pack(side="left", fill="y")
        self.side.bind("<Motion>", self.on_side_motion)
        self.side.bind("<Leave>", lambda _: self.set_button_hover(None))
        self.side.bind("<Button-1>", self.on_side_click)
        tk.Frame(self.root, width=1, bg=LINE).pack(side="left", fill="y")
        main = tk.Frame(self.root, bg=BG, padx=px(18), pady=px(16))
        main.pack(side="left", fill="both", expand=True)
        self.section(main, "TARGETS", "单击锁定 = 立即切换")
        self.list = tk.Frame(main, bg=BG)
        self.list.pack(fill="x")
        self.empty = tk.Label(self.list, text="还没有保存任何账号。点左边的「添加账号」开始。",
                              font=font("中", 10), fg=DIM, bg=PANEL, pady=px(18))
        self.section(main, "CONSOLE", "")
        self.console = tk.Text(main, height=9, width=1, bg=CONSOLE, fg=SOFT, font=(MONO, 9), relief="flat",
                               highlightthickness=1, highlightbackground=LINE, padx=px(12), pady=px(8),
                               wrap="word", state="disabled", cursor="arrow",
                               selectbackground=LINE, selectforeground=BRIGHT)
        self.console.pack(fill="x")
        for tag, color in (("time", DIM), ("ok", BRIGHT), ("warn", AMBER), ("err", RED), ("dim", DIM), ("cyan", SOFT),
                           ("prompt", RED), ("scramble", DIM)):
            self.console.tag_configure(tag, foreground=color)
        self.console.tag_configure("cjk", font=font("中", 9))
        tip = "切换会重启 Claude 桌面端，进行中的对话会中断 · 额度来自桌面端本地记录，恢复时间为估算"
        tk.Label(main, text=tip, font=font(tip, 8), fg=DIM, bg=BG, anchor="w").pack(fill="x", pady=(px(8), 0))

    def section(self, parent, title: str, hint: str) -> None:
        px = self.px
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(px(4), px(8)))
        tk.Label(row, text=title, font=font(title, 10, True, hud=True), fg=BRIGHT, bg=BG).pack(side="left")
        tk.Frame(row, bg=LINE, height=1).pack(side="left", fill="x", expand=True, padx=px(10))
        if hint:
            tk.Label(row, text=hint, font=font(hint, 9), fg=SOFT, bg=BG).pack(side="right")

    def button(self, parent, text: str, command, color: str) -> tk.Label:
        """对话框里的按钮：细边框，鼠标移上去边框变红。"""
        widget = tk.Label(parent, text=text, font=font(text, 10, True), fg=color, bg=BG, padx=self.px(14),
                          pady=self.px(5), cursor="hand2", highlightthickness=1, highlightbackground=LINE)
        widget.bind("<Enter>", lambda _: widget.configure(highlightbackground=RED, bg=PANEL_HOT))
        widget.bind("<Leave>", lambda _: widget.configure(highlightbackground=LINE, bg=BG))
        widget.bind("<Button-1>", lambda _: command())
        return widget

    # ---------- 侧栏 ----------

    def draw_side(self) -> None:
        """侧栏重画一遍（雷达扫描线和光点由 tick_radar 每帧单独画）。"""
        c, px = self.side, self.px
        c.delete("side")
        x0, x1, cx = px(18), px(SIDEBAR - 18), px(SIDEBAR) / 2
        x = x0
        for part, color in (("CLAUDE", BRIGHT), ("//", RED), ("SWITCH", BRIGHT)):
            item = c.create_text(x, px(30), text=part, font=(HUD, 19), fill=color, anchor="w", tags="side")
            x = c.bbox(item)[2] + px(1)
        c.create_text(x0, px(54), text="CONTROL NODE  ·  V2.1", font=(HUD, 8), fill=SOFT, anchor="w", tags="side")

        cy, r = px(RADAR_Y), px(RADAR_R)
        for k in (1, 0.7, 0.4):
            c.create_oval(cx - r * k, cy - r * k, cx + r * k, cy + r * k, outline=LINE, tags="side")
        c.create_line(cx - r, cy, cx + r, cy, fill=FAINT, tags="side")
        c.create_line(cx, cy - r, cx, cy + r, fill=FAINT, tags="side")
        for i in range(72):
            a, inner = i / 72 * 2 * math.pi, r - px(10 if i % 6 == 0 else 5)
            c.create_line(cx + math.sin(a) * r, cy - math.cos(a) * r, cx + math.sin(a) * inner,
                          cy - math.cos(a) * inner, fill=TICK, tags="side")

        now = datetime.now()
        c.create_text(cx, px(284), text=now.strftime("%H:%M:%S"), font=(HUD_LIGHT, 34), fill=BRIGHT, tags="side")
        c.create_text(cx, px(314), text=now.strftime("%Y  ·  %m  ·  %d"), font=(HUD, 9), fill=SOFT, tags="side")

        current = cs.find(self.profiles, account=self.current) if self.current else None
        up = int(time.time() - self.started)
        stats = (("STATE", "BUSY" if self.busy else "IDLE", AMBER if self.busy else BRIGHT),
                 ("ACTIVE", current.label if current else "UNSAVED" if self.current else "NONE", BRIGHT),
                 ("PROFILES", f"{len(self.profiles):02d}", BRIGHT),
                 ("UPTIME", f"{up // 3600:02d}:{up // 60 % 60:02d}:{up % 60:02d}", BRIGHT),
                 ("NETWORK", "LOCAL", BRIGHT))
        y = px(340)
        c.create_line(x0, y, x1, y, fill=LINE, tags="side")
        for name, value, color in stats:
            c.create_text(x0, y + px(16), text=name, font=(HUD, 10), fill=SOFT, anchor="w", tags="side")
            c.create_text(x1, y + px(16), text=value, font=font(value, 10, True, hud=True), fill=color, anchor="e", tags="side")
            y += px(32)
            c.create_line(x0, y, x1, y, fill=FAINT, tags="side")

        y += px(18)
        self.button_boxes = []
        buttons = self.BUTTONS + ((("stop", "取消等待登录", "STOP"),) if self.waiting_login else ())
        for i, (kind, label, english) in enumerate(buttons):
            self.draw_button(kind, label, english, x0, y, x1, y + px(36), primary=i == 0)
            y += px(44)
        height = y + px(8)
        if int(c["height"]) != height:
            c.configure(height=height)

    def draw_button(self, kind, label, english, x0, y0, x1, y1, primary) -> None:
        c, px = self.side, self.px
        enabled = self.buttons_enabled or kind == "stop"
        hot = enabled and self.button_hover == kind
        if kind == "stop":
            fill, outline, text, sub = (RED, RED, BG, BG) if hot else (BG, RED, RED, RED)
        elif primary:
            fill, outline, text, sub = ((RED, RED, "#ffffff", "#ffd9dc") if hot else
                                        (BRIGHT, BRIGHT, BG, "#5a5556") if enabled else (LINE, LINE, DIM, DIM))
        else:
            fill, outline, text, sub = ((PANEL_HOT, RED, BRIGHT, SOFT) if hot else
                                        (BG, LINE, BRIGHT, SOFT) if enabled else (BG, FAINT, DIM, DIM))
        c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, tags="side")
        c.create_text(x0 + px(14), (y0 + y1) / 2, text=label, font=font(label, 10, True), fill=text, anchor="w", tags="side")
        c.create_text(x1 - px(12), (y0 + y1) / 2, text=english, font=(HUD, 8), fill=sub, anchor="e", tags="side")
        self.button_boxes.append((x0, y0, x1, y1, kind, enabled))

    def side_button_at(self, x, y):
        for x0, y0, x1, y1, kind, enabled in self.button_boxes:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return kind, enabled
        return None, False

    def on_side_motion(self, event) -> None:
        kind, enabled = self.side_button_at(event.x, event.y)
        self.side.configure(cursor="hand2" if kind and enabled else "")
        self.set_button_hover(kind)

    def set_button_hover(self, kind) -> None:
        if kind != self.button_hover:
            self.button_hover = kind
            self.draw_side()

    def on_side_click(self, event) -> None:
        kind, enabled = self.side_button_at(event.x, event.y)
        if kind is None:
            return
        if not enabled:
            self.refuse_if_busy()
            return
        {"add": self.on_add, "sync": self.on_sync, "undo": self.on_undo, "refresh": self.on_refresh,
         "stop": self.on_cancel}[kind]()

    def center(self) -> None:
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = max(0, (self.root.winfo_screenheight() - height) // 2 - self.px(30))
        self.root.geometry(f"+{x}+{y}")


    # ---------- 数据 ----------

    def reload(self) -> None:
        """重新读账号、当前登录和用量；后台操作进行中时不读，免得读到改了一半的文件。"""
        if self.busy:
            return
        self.last_reload = time.time()
        try:
            self.profiles = cs.load_profiles(self.env)
            self.current, _ = cs.login_of(cs.read_config(self.env))
        except (cs.Stop, OSError) as error:
            self.log(str(error), "warn")
        self.snapshot = plan_usage.Snapshot(self.env.data_dir)

    def render(self) -> None:
        now = int(time.time() * 1000)
        unsaved = bool(self.current) and cs.find(self.profiles, account=self.current) is None
        keys = [p.label for p in self.profiles] + ([UNSAVED] if unsaved else [])
        if keys != self.card_keys:
            for card in self.cards:
                card.canvas.destroy()
            self.cards = [Card(self, self.list, key) for key in keys]
            for card in self.cards:
                card.canvas.pack(pady=(0, self.px(10)))
            self.card_keys = keys
            if keys:
                self.empty.pack_forget()
            else:
                self.empty.pack(fill="x")
        self.blips = []
        for number, card in enumerate(self.cards, 1):
            if card.key == UNSAVED:
                label, account, saved_at = "", self.current, ""
            else:
                profile = cs.find(self.profiles, label=card.key)
                label, account, saved_at = profile.label, profile.account, profile.saved_at
            usage = self.snapshot.usage(account, now)
            active = account == self.current
            card.show(number, label, account, saved_at, active, self.switching == card.key, usage, now, bool(self.busy))
            # 雷达光点：按顺序错开角度；本周额度剩得越多，离圆心越近
            weekly = usage.weekly.remaining if usage else 50
            kind = "active" if active else "hot" if card.hover or self.switching == card.key else "idle"
            self.blips.append((f"{number:02d}", (number * 137.5 + 30) % 360, 0.22 + 0.68 * (1 - weekly / 100), kind))
        self.draw_side()

    # ---------- 定时 ----------

    def tick_clock(self) -> None:
        self.blink = not self.blink
        if not self.busy and time.time() - self.last_reload > RELOAD_MS / 1000:
            self.reload()
        self.render()
        self.root.after(500, self.tick_clock)

    def tick_radar(self) -> None:
        """雷达扫描线（带渐隐余晖）和账号光点；扫描线扫过光点时光点会亮一下。"""
        c, px = self.side, self.px
        c.delete("sweep")
        cx, cy, r = px(SIDEBAR) / 2, px(RADAR_Y), px(RADAR_R)
        self.sweep = (self.sweep + (9 if self.busy else 4)) % 360  # 切换进行中扫得更快
        for k in reversed(range(14)):
            a = math.radians(self.sweep - k * 3)
            c.create_line(cx, cy, cx + math.sin(a) * (r - 1), cy - math.cos(a) * (r - 1),
                          fill=mix(RED, BG, k / 14), width=2 if k == 0 else 1, tags="sweep")
        for name, angle, distance, kind in self.blips:
            a = math.radians(angle)
            x, y = cx + math.sin(a) * r * distance, cy - math.cos(a) * r * distance
            glow = max(0.0, 1 - ((self.sweep - angle) % 360) / 140)
            base = {"active": BRIGHT, "hot": RED}.get(kind, SOFT)
            color = mix(mix(base, BG, 0.45), base, glow)
            size = px(4 if kind == "active" else 3)
            c.create_oval(x - size, y - size, x + size, y + size, fill=color, outline="", tags="sweep")
            c.create_text(x + size + px(4), y, text=name, font=(HUD, 8), fill=mix(DIM, SOFT, glow), anchor="w", tags="sweep")
        self.root.after(40, self.tick_radar)


    def tick_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "out":
                    self.pending += value
                    *lines, self.pending = self.pending.split("\n")
                    for line in lines:
                        line = line.strip().replace("按 Ctrl+C 取消", "点 [ ✕ 取消等待登录 ] 可取消")
                        if line:
                            self.log(line, "err" if line.startswith(("出错", "没能")) else None)
                elif kind == "fail":
                    self.log(value, "err")
                elif kind == "done":
                    if self.pending.strip():
                        self.log(self.pending.strip())
                    self.pending = ""
                    self.log(f"{value} —— 完成", "ok")
                    self.finish()
                elif kind == "end":
                    self.finish()
        except queue.Empty:
            pass
        self.root.after(60, self.tick_events)

    # ---------- 控制台 ----------

    def log(self, text: str, tag: str = None) -> None:
        """控制台加一行：先冒出一串乱码，再逐字「解密」成真正的内容。"""
        self.typing.append((datetime.now().strftime("[%H:%M:%S] "), text, tag))
        if not self.typing_active:
            self.type_line()

    def type_line(self) -> None:
        if not self.typing:
            self.typing_active = False
            return
        self.typing_active = True
        stamp = self.typing[0][0]
        console = self.console
        console.configure(state="normal")
        console.insert("end", stamp, "time")
        console.insert("end", ":: ", "prompt")
        console.mark_set("typing", "end-1c")
        console.mark_gravity("typing", "left")
        console.configure(state="disabled")
        self.typed = 0
        self.type_step()

    def type_step(self) -> None:
        _, text, tag = self.typing[0]
        # 一次揭开 5 个字；排队的行多了（比如一口气来好几条）就直接整行显示
        self.typed = len(text) if len(self.typing) > 3 else min(len(text), self.typed + 5)
        rest = len(text) - self.typed
        tags = tuple(t for t in (tag, "cjk" if font(text, 9)[0] == CJK else None) if t)
        console = self.console
        console.configure(state="normal")
        console.delete("typing", "end-1c")
        console.insert("end", text[:self.typed], tags)
        if rest:
            console.insert("end", "".join(random.choice(SCRAMBLE) for _ in range(min(rest, 6))), ("scramble", "cjk"))
        else:
            console.insert("end", "\n")
        lines = int(console.index("end-1c").split(".")[0])
        if lines > 400:
            console.delete("1.0", f"{lines - 400}.0")
        console.see("end")
        console.configure(state="disabled")
        if rest:
            self.root.after(16, self.type_step)
        else:
            self.typing.popleft()
            self.type_line()

    def boot(self) -> None:
        names = "、".join(p.label for p in self.profiles) or "无"
        lines = [
            (f"{TITLE} v2.0 —— 初始化", "ok"),
            (f"桌面端数据：{self.env.data_dir}", "dim"),
            (f"已载入 {len(self.profiles)} 个账号：{names}", None),
            (f"用量记录：{len(self.snapshot.samples)} 个组织", None),
            ("就绪。单击账号卡片即可切换。", "ok"),
        ]
        if not self.env.data_dir.is_dir():
            lines.append(("没找到 Claude 桌面端的数据文件夹，这台电脑上可能没装桌面端。", "err"))
        if not self.env.use_app:
            lines.append(("测试模式：不会关闭或打开真实的桌面端。", "warn"))
        for i, (text, tag) in enumerate(lines):
            self.root.after(120 * i, lambda t=text, g=tag: self.log(t, g))

    # ---------- 后台操作 ----------

    def run(self, title: str, job) -> None:
        """在后台线程里跑 claude_switch 的操作，输出显示到控制台。"""
        self.busy = title
        self.set_buttons(False)
        self.log(f"{title} ……", "cyan")

        def work():
            old = sys.stdout
            sys.stdout = QueueWriter(self.events)
            try:
                job()
                self.events.put(("done", title))
            except cs.Stop as reason:
                self.events.put(("fail", str(reason).strip()))
                self.events.put(("end", None))
            except Exception as error:  # 至少让人看到出了什么错
                self.events.put(("fail", f"出错了，操作没有完成：{error!r}。登录状态不对的话，可以点 [ ↺ 撤销上次 ]。"))
                self.events.put(("end", None))
            finally:
                sys.stdout = old

        threading.Thread(target=work, daemon=True).start()

    def finish(self) -> None:
        self.busy = self.switching = None
        self.show_cancel(False)
        self.set_buttons(True)
        self.reload()
        self.render()

    def show_cancel(self, show: bool) -> None:
        """等新账号登录时，侧栏多一个「取消等待登录」。"""
        self.waiting_login = show
        self.draw_side()

    def set_buttons(self, enabled: bool) -> None:
        self.buttons_enabled = enabled
        self.draw_side()


    def refuse_if_busy(self) -> bool:
        if self.busy:
            self.log(f"正在{self.busy}，请等它完成。", "warn")
        return bool(self.busy)

    def name_for_current(self):
        """当前登录的账号没保存时，切换或添加前先让用户给它起名；取消返回 False。"""
        if not self.current or cs.find(self.profiles, account=self.current):
            return None
        label = self.ask("保存当前账号", "当前登录的账号还没保存，换下来就丢了。先给它起个名字：")
        if not label:
            return False
        return cs.check_new_label(self.env, label, self.profiles)

    def guarded(self, action) -> None:
        try:
            action()
        except cs.Stop as reason:
            self.log(str(reason).strip(), "err")

    # ---------- 按钮和卡片 ----------

    def on_card(self, key: str) -> None:
        if self.refuse_if_busy():
            return
        if key == UNSAVED:
            self.guarded(self.save_current)
            return
        profile = cs.find(self.profiles, label=key)
        if profile is None:
            return
        if profile.account == self.current:
            self.log(f"「{key}」就是当前账号，不用切换。", "dim")
            return

        def go():
            save_as = self.name_for_current()
            if save_as is False:
                self.log("已取消。", "dim")
                return
            self.switching = key
            self.run(f"切换到「{key}」", lambda: cs.cmd_switch(self.env, key, save_as))

        self.guarded(go)

    def save_current(self) -> None:
        label = self.ask("保存当前账号", "给当前登录的账号起个名字（比如「工作号」）：")
        if not label:
            return
        label = cs.check_new_label(self.env, label, self.profiles)
        self.run(f"保存「{label}」", lambda: cs.cmd_save(self.env, label))

    def on_rename(self, key: str) -> None:
        if self.refuse_if_busy():
            return
        new = self.ask("改名", f"给「{key}」起个新名字：", key)
        if new is None or new.strip() == key:
            return

        def go():
            old_stdout = sys.stdout
            sys.stdout = QueueWriter(self.events)
            try:
                cs.cmd_rename(self.env, key, new)
            finally:
                sys.stdout = old_stdout
            self.reload()
            self.render()

        self.guarded(go)

    def on_add(self) -> None:
        if self.refuse_if_busy():
            return

        def go():
            save_as = self.name_for_current()
            if save_as is False:
                return
            label = self.ask("添加账号", "给要添加的新账号起个名字（比如「个人号」）：")
            if not label:
                return
            label = cs.check_new_label(self.env, label, self.profiles)
            if label == save_as:
                raise cs.Stop("两个账号不能用同一个名字。")
            self.env.cancel.clear()
            self.show_cancel(True)
            self.run(f"添加「{label}」", lambda: cs.cmd_add(self.env, label, save_as))

        self.guarded(go)

    def on_cancel(self) -> None:
        self.env.cancel.set()
        self.log("正在取消，恢复原来的账号……", "warn")

    def on_sync(self) -> None:
        if self.refuse_if_busy():
            return
        if not self.confirm("同步会话列表", "会关闭 Claude 桌面端（进行中的对话会中断），同步完自动重新打开。"):
            return

        def job():
            was_running = cs.stop_app(self.env)
            try:
                cs.sync_sessions(self.env)
            finally:
                if was_running:
                    cs.start_app(self.env)

        self.run("同步会话列表", job)

    def on_undo(self) -> None:
        if self.refuse_if_busy():
            return
        folder = cs.latest_backup(self.env)
        if folder is None:
            self.log("没有可撤销的备份。", "warn")
            return
        if not self.confirm("撤销", f"回到 {folder.name} 那时的登录状态（会关闭再重新打开桌面端）。"):
            return
        self.run("撤销上一次切换或添加", lambda: cs.cmd_undo(self.env))

    def on_refresh(self) -> None:
        if self.refuse_if_busy():
            return
        self.reload()
        self.render()
        self.log("已刷新账号和用量。", "dim")

    def on_close(self) -> None:
        if self.busy:
            self.log(f"正在{self.busy}，完成后再关闭，不然登录状态可能只换了一半。", "warn")
            return
        self.root.destroy()

    def on_error(self, *info) -> None:
        self.log(f"界面出错：{info[1]!r}", "err")

    # ---------- 对话框 ----------

    def dialog(self, title: str, message: str, entry_text=None):
        """统一风格的小对话框；entry_text 不是 None 时带输入框。确认返回输入的文字（或 True），取消返回 None。"""
        px = self.px
        top = tk.Toplevel(self.root, bg=BG, highlightthickness=1, highlightbackground=BRIGHT)
        top.overrideredirect(True)
        top.transient(self.root)
        top.attributes("-topmost", True)
        tk.Label(top, text=f"▌{title}", font=font(title, 11, True), fg=BRIGHT, bg=BG, anchor="w") \
            .pack(fill="x", padx=px(16), pady=(px(12), px(6)))
        tk.Label(top, text=message, font=font(message, 10), fg=SOFT, bg=BG, anchor="w", justify="left",
                 wraplength=px(420)).pack(fill="x", padx=px(16))
        result = {"value": None}
        entry = None
        if entry_text is not None:
            entry = tk.Entry(top, font=font("中", 12), bg=CONSOLE, fg=BRIGHT, insertbackground=BRIGHT, relief="flat",
                             highlightthickness=1, highlightbackground=LINE, highlightcolor=BRIGHT,
                             selectbackground=LINE, selectforeground=BRIGHT, width=32)
            entry.insert(0, entry_text)
            entry.select_range(0, "end")
            entry.pack(fill="x", padx=px(16), pady=(px(10), 0), ipady=px(4))

        def ok(_=None):
            result["value"] = entry.get() if entry is not None else True
            top.destroy()

        row = tk.Frame(top, bg=BG)
        row.pack(fill="x", padx=px(16), pady=px(12))
        self.button(row, "取消 Esc", top.destroy, SOFT).pack(side="right")
        self.button(row, "确认 Enter", ok, BRIGHT).pack(side="right", padx=(0, px(8)))
        top.bind("<Return>", ok)
        top.bind("<Escape>", lambda _: top.destroy())
        top.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - top.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - top.winfo_reqheight()) // 3
        top.geometry(f"+{x}+{y}")
        top.wait_visibility()
        top.attributes("-topmost", True)  # 显示后再设一次，不然可能压在主窗口下面
        top.geometry(f"+{x}+{y}")  # 上一步会把无边框窗口挪到左上角，摆回来
        top.lift()
        top.grab_set()
        (entry or top).focus_force()
        self.root.wait_window(top)
        return result["value"]

    def ask(self, title: str, message: str, initial: str = ""):
        value = self.dialog(title, message, initial)
        return value.strip() if value else None

    def confirm(self, title: str, message: str) -> bool:
        return bool(self.dialog(title, message))


def build_parser(description: str) -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=description)
    # 下面几项只在测试时用
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--projects", type=Path, default=None)
    parser.add_argument("--sessions-backup", type=Path, default=here / "backups")
    parser.add_argument("--no-app", action="store_true")
    parser.add_argument("--settle", type=int, default=60)
    parser.add_argument("--login-timeout", type=int, default=600)
    return parser


def make_env(args) -> cs.Env:
    return cs.Env(
        data_dir=args.data_dir or sync.default_data_dir(),
        store=args.store or Path(os.environ["LOCALAPPDATA"]) / "claude-switch",
        projects=args.projects or sync.default_projects(),
        sessions_backup=args.sessions_backup,
        use_app=not args.no_app,
        settle=args.settle,
        login_timeout=args.login_timeout,
        yes=True,  # 单击即切换，不再逐项确认
        cancel=threading.Event(),
    )


def main() -> int:
    args = build_parser("Claude 桌面端一键切换账号（图形界面）").parse_args()
    env = make_env(args)
    setup_windows()
    root = tk.Tk()
    App(root, env)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
