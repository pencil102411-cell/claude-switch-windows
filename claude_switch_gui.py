"""Claude 桌面端一键切换账号 —— 图形界面版。

单击账号卡片就切换；卡片上显示每个账号的 5 小时额度和周额度（来自桌面端自己的用量记录，
见 plan_usage.py），可以改名。切换、添加、撤销的具体做法都在 claude_switch.py 里，这里只管界面。

桌面图标用 pythonw.exe 打开它，不弹黑窗口。测试时可以加 --data-dir / --store / --no-app，
含义和 claude_switch.py 一样。
"""

import argparse
import ctypes
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import claude_switch as cs
import plan_usage
import sync_sessions as sync

TITLE = "CLAUDE//SWITCH"
BG = "#030805"
PANEL = "#06110a"
PANEL_HOT = "#0b2215"
CONSOLE = "#020604"
LINE = "#124a26"
EMPTY = "#0c2616"
DIM = "#2f7a4a"
SOFT = "#19c463"
GREEN = "#00ff7b"
AMBER = "#ffb000"
RED = "#ff3b5c"
CYAN = "#00e5ff"
RAIN = ["#0a2e17", "#0d3b1e", "#114a26", "#0f5a2c"]
MONO = "Consolas"
CJK = "Microsoft YaHei UI"
HEX ="0123456789ABCDEF"
SEGMENTS = 30
RELOAD_MS = 15000  # 多久重新读一次账号和用量文件
UNSAVED = "\0unsaved"  # 当前登录但还没保存的账号，卡片用这个键
ICON = Path(__file__).resolve().parent / "assets" / "claude_switch.ico"


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
        image.put(GREEN, to=(x, 5 + i * 2, x + 4, 9 + i * 2))
        image.put(GREEN, to=(x, 23 - i * 2, x + 4, 27 - i * 2))
    image.put(GREEN, to=(18, 22, 28, 26))
    for edge in ((0, 0, 32, 1), (0, 31, 32, 32), (0, 0, 1, 32), (31, 0, 32, 32)):
        image.put(LINE, to=edge)
    root.iconphoto(True, image)
    return image


def font(text: str, size: int, bold: bool = False) -> tuple:
    """含中文的用雅黑（Consolas 里没有中文，系统替补的字发糊），其余用等宽的 Consolas。"""
    family = CJK if any(ord(ch) >= 0x2E80 for ch in text) else MONO
    return (family, size, "bold") if bold else (family, size)


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
    return GREEN if remaining >= 50 else AMBER if remaining >= 20 else RED


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


class Meter:
    """一行额度：名称 + 分段进度条 + 剩余百分比 + 恢复倒计时。"""

    def __init__(self, app, parent, name: str):
        self.app = app
        self.frame = tk.Frame(parent, bg=PANEL)
        self.name = tk.Label(self.frame, text=name, font=(MONO, 10, "bold"), fg=SOFT, bg=PANEL, width=4, anchor="w")
        self.bar = tk.Canvas(self.frame, width=app.px(300), height=app.px(11), bg=PANEL, highlightthickness=0)
        self.percent = tk.Label(self.frame, font=(MONO, 10, "bold"), bg=PANEL, width=9, anchor="w")
        self.reset = tk.Label(self.frame, fg=DIM, bg=PANEL, anchor="w")
        self.name.pack(side="left")
        self.bar.pack(side="left", padx=(0, app.px(10)))
        self.percent.pack(side="left")
        self.reset.pack(side="left", fill="x", expand=True)
        self.widgets = [self.frame, self.name, self.bar, self.percent, self.reset]

    def show(self, window, now: int) -> None:
        bar = self.bar
        bar.delete("all")
        width, height = int(bar["width"]), int(bar["height"])
        gap = max(1, self.app.px(2))
        segment = (width - gap * (SEGMENTS - 1)) / SEGMENTS
        if window is None:
            filled, color = 0, DIM
            self.percent.configure(text="NO DATA", fg=DIM)
            text, tint = "用这个账号登录后自动记录", DIM
        else:
            filled, color = round(window.remaining / 100 * SEGMENTS), level(window.remaining)
            self.percent.configure(text=f"{window.remaining:>3}% LEFT", fg=color)
            if window.note:
                text, tint = f"● {window.note}", SOFT
            elif window.reset_by is None:
                text, tint = "恢复时间 --（记录太少）", DIM
            else:
                text, tint = f"恢复 ≤ T-{countdown(window.reset_by - now)}", CYAN
        self.reset.configure(text=text, fg=tint, font=font(text, 9))
        for i in range(SEGMENTS):
            x = i * (segment + gap)
            bar.create_rectangle(x, 0, x + segment, height, fill=color if i < filled else EMPTY, outline="")


class Card:
    """一个账号一张卡片：单击切换，「改名」单独处理。"""

    def __init__(self, app, parent, key: str):
        self.app, self.key = app, key
        self.hover = False
        px = app.px
        self.frame = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=LINE,
                              padx=px(14), pady=px(8), cursor="hand2")
        top = tk.Frame(self.frame, bg=PANEL)
        self.index = tk.Label(top, font=(MONO, 10), fg=DIM, bg=PANEL)
        self.name = tk.Label(top, font=(MONO, 15, "bold"), fg=GREEN, bg=PANEL)
        self.rename = tk.Label(top, text="✎ 改名", font=font("改名", 9), fg=DIM, bg=PANEL, cursor="hand2", padx=px(6))
        self.state = tk.Label(top, font=(MONO, 9, "bold"), padx=px(8), pady=px(1))
        self.index.pack(side="left", padx=(0, px(8)))
        self.name.pack(side="left")
        self.rename.pack(side="right")
        self.state.pack(side="right", padx=(0, px(6)))
        top.pack(fill="x")
        self.meta = tk.Label(self.frame, font=(MONO, 9), fg=DIM, bg=PANEL, anchor="w")
        self.meta.pack(fill="x", pady=(px(2), px(5)))
        self.five = Meter(app, self.frame, "5H")
        self.week = Meter(app, self.frame, "7D")
        self.five.frame.pack(fill="x", pady=(0, px(3)))
        self.week.frame.pack(fill="x")
        self.painted = [self.frame, top, self.index, self.name, self.rename, self.meta] \
            + self.five.widgets + self.week.widgets
        for widget in self.painted:
            widget.bind("<Enter>", self.on_enter)
            widget.bind("<Leave>", self.on_leave)
            if widget is not self.rename:
                widget.bind("<Button-1>", lambda _: app.on_card(self.key))
        self.state.bind("<Button-1>", lambda _: app.on_card(self.key))
        self.rename.bind("<Button-1>", lambda _: app.on_rename(self.key))
        self.rename.bind("<Enter>", lambda _: self.rename.configure(fg=CYAN), add="+")
        self.rename.bind("<Leave>", lambda _: self.rename.configure(fg=DIM), add="+")

    def on_enter(self, _event) -> None:
        if not self.hover:
            self.hover = True
            self.app.render()

    def on_leave(self, _event) -> None:
        self.app.root.after(15, self.check_leave)

    def check_leave(self) -> None:
        widget = self.app.root.winfo_containing(*self.app.root.winfo_pointerxy())
        while widget is not None and widget is not self.frame:
            widget = widget.master
        if widget is None and self.hover:
            self.hover = False
            self.app.render()

    def show(self, number: int, label: str, account: str, saved_at: str, active: bool,
             switching: bool, usage, now: int, busy: bool) -> None:
        hot = self.hover and not busy and not active
        background = PANEL_HOT if hot else PANEL
        for widget in self.painted:
            widget.configure(bg=background)
        border = GREEN if active else AMBER if switching else CYAN if hot else LINE
        self.frame.configure(highlightbackground=border, highlightcolor=border,
                             cursor="arrow" if active or busy else "hand2")
        self.index.configure(text=f"[{number:02d}]")
        unsaved = self.key == UNSAVED
        name = "未保存的当前账号" if unsaved else label
        self.name.configure(text=name, fg=AMBER if unsaved else GREEN, font=font(name, 15, True))
        if unsaved:
            self.rename.pack_forget()
        if switching:
            dots = "." * (int(time.time() * 2) % 4)
            state = (f"⟳ LINKING{dots:<3}", AMBER, PANEL)
        elif active and unsaved:
            state = ("◉ ACTIVE · 单击保存", BG, AMBER)
        elif active:
            state = ("◉ ACTIVE", BG, GREEN)
        elif hot:
            state = ("▶ 单击切换到此账号", BG, CYAN)
        else:
            state = ("○ STANDBY", DIM, background)
        self.state.configure(text=state[0], fg=state[1], bg=state[2], font=font(state[0], 9, True))
        saved = "UNSAVED" if unsaved else f"SAVED {saved_at}"
        data = f"DATA {ago(now - usage.sampled_at)}" if usage else "DATA --"
        self.meta.configure(text=f"UID {account[:8]}  ·  {saved}  ·  {data}")
        self.five.show(usage.five_hour if usage else None, now)
        self.week.show(usage.weekly if usage else None, now)


class App:
    def __init__(self, root: tk.Tk, env: cs.Env):
        self.root, self.env = root, env
        self.scale = root.winfo_fpixels("1i") / 96
        self.events = queue.Queue()
        self.pending = ""  # 后台 print 还没凑满一行的部分
        self.busy = None  # 正在进行的操作名称
        self.switching = None  # 正在切换到的账号名
        self.profiles, self.current = [], None
        self.snapshot = plan_usage.Snapshot(env.data_dir)
        self.cards, self.card_keys = [], None
        self.blink = False
        self.last_reload = 0.0

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
        self.tick_rain()
        root.after(1800, self.tick_glitch)

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    # ---------- 搭界面 ----------

    def build(self) -> None:
        px = self.px
        outer = tk.Frame(self.root, bg=BG, padx=px(16), pady=px(14))
        outer.pack(fill="both", expand=True)
        self.build_header(outer)
        self.build_status(outer)
        self.section(outer, "ACCOUNTS", "单击卡片 = 立即切换")
        self.list = tk.Frame(outer, bg=BG)
        self.list.pack(fill="x")
        self.empty = tk.Label(self.list, text="还没有保存任何账号。点下面的 [ + 添加账号 ] 开始。",
                              font=font("中", 10), fg=DIM, bg=PANEL, pady=px(18),
                              highlightthickness=1, highlightbackground=LINE)
        self.build_actions(outer)
        self.section(outer, "CONSOLE", "")
        self.console = tk.Text(outer, height=8, bg=CONSOLE, fg=SOFT, font=(MONO, 9), relief="flat",
                               highlightthickness=1, highlightbackground=LINE, padx=px(10), pady=px(6),
                               wrap="word", state="disabled", cursor="arrow",
                               selectbackground=LINE, selectforeground=GREEN)
        self.console.pack(fill="x")
        for tag, color in (("time", DIM), ("ok", GREEN), ("warn", AMBER), ("err", RED), ("dim", DIM), ("cyan", CYAN)):
            self.console.tag_configure(tag, foreground=color)
        self.console.tag_configure("cjk", font=font("中", 9))
        tip = "切换会重启 Claude 桌面端，进行中的对话会中断 · 额度来自桌面端本地记录，恢复时间为估算"
        tk.Label(outer, text=tip, font=font(tip, 8), fg=DIM, bg=BG, anchor="w").pack(fill="x", pady=(px(6), 0))

    def build_header(self, parent) -> None:
        px = self.px
        width, height = px(780), px(92)
        canvas = tk.Canvas(parent, width=width, height=height, bg=BG, highlightthickness=0)
        canvas.pack(fill="x")
        self.header, self.header_h = canvas, height
        self.rain = []
        for x in range(px(8), width, px(14)):
            item = canvas.create_text(x, random.uniform(-height, height), text=self.rain_text(),
                                      fill=random.choice(RAIN), font=(MONO, 8), anchor="s")
            self.rain.append([item, canvas.coords(item)[1], random.uniform(0.8, 3.2) * self.scale])
        for y in range(0, height, 3):  # 扫描线
            canvas.create_line(0, y, width, y, fill="#010302")
        self.title_at = (px(22), px(38))
        font = (MONO, 26, "bold")
        self.glitch = [canvas.create_text(*self.title_at, text=TITLE, fill=color, font=font, anchor="w", state="hidden")
                       for color in (CYAN, RED)]
        canvas.create_text(*self.title_at, text=TITLE, fill=GREEN, font=font, anchor="w")
        canvas.create_text(px(24), px(70), text="> MULTI-ACCOUNT CONTROL NODE  //  v2.0  //  LOCAL TELEMETRY ONLY",
                           fill=SOFT, font=(MONO, 9), anchor="w")
        host = f"HOST {os.environ.get('COMPUTERNAME', '?')}  ::  USER {os.environ.get('USERNAME', '?')}"
        canvas.create_text(width - px(18), px(18), text=host, fill=DIM, font=(MONO, 8), anchor="e")
        canvas.create_rectangle(0, 0, width - 1, height - 1, outline=LINE)
        arm = px(14)
        for x, y, dx, dy in ((0, 0, 1, 1), (width - 1, 0, -1, 1), (0, height - 1, 1, -1), (width - 1, height - 1, -1, -1)):
            canvas.create_line(x, y, x + dx * arm, y, fill=GREEN, width=2)
            canvas.create_line(x, y, x, y + dy * arm, fill=GREEN, width=2)

    def build_status(self, parent) -> None:
        px = self.px
        bar = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=LINE, padx=px(10), pady=px(4))
        bar.pack(fill="x", pady=(px(8), 0))
        font = (MONO, 9, "bold")
        self.dot = tk.Label(bar, text="●", font=font, fg=GREEN, bg=PANEL)
        self.state = tk.Label(bar, text="IDLE", font=font, fg=GREEN, bg=PANEL)
        self.active = tk.Label(bar, font=font, fg=SOFT, bg=PANEL)
        self.count = tk.Label(bar, font=font, fg=DIM, bg=PANEL)
        self.cursor = tk.Label(bar, text="█", font=font, fg=GREEN, bg=PANEL)
        self.clock = tk.Label(bar, font=font, fg=SOFT, bg=PANEL)
        self.dot.pack(side="left")
        self.state.pack(side="left", padx=(px(4), px(18)))
        self.active.pack(side="left", padx=(0, px(18)))
        self.count.pack(side="left")
        self.cursor.pack(side="right")
        self.clock.pack(side="right", padx=(0, px(4)))

    def section(self, parent, title: str, hint: str) -> None:
        px = self.px
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(px(12), px(5)))
        tk.Label(row, text=f"▌{title}", font=(MONO, 10, "bold"), fg=GREEN, bg=BG).pack(side="left")
        tk.Frame(row, bg=LINE, height=1).pack(side="left", fill="x", expand=True, padx=px(10))
        if hint:
            tk.Label(row, text=hint, font=font(hint, 9), fg=DIM, bg=BG).pack(side="right")

    def build_actions(self, parent) -> None:
        px = self.px
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(px(10), 0))
        self.buttons = []
        for text, command in (("+ 添加账号", self.on_add), ("⇄ 同步会话", self.on_sync),
                              ("↺ 撤销上次", self.on_undo), ("⟳ 刷新", self.on_refresh)):
            self.buttons.append(self.button(row, text, command, GREEN))
            self.buttons[-1].pack(side="left", padx=(0, px(8)))
        self.cancel = self.button(row, "✕ 取消等待登录", self.on_cancel, RED)

    def button(self, parent, text: str, command, color: str) -> tk.Label:
        widget = tk.Label(parent, text=f"[ {text} ]", font=font(text, 10, True), fg=color, bg=BG,
                          padx=self.px(6), pady=self.px(4), cursor="hand2",
                          highlightthickness=1, highlightbackground=LINE)
        widget.color = color

        def enter(_):
            if widget.cget("fg") != DIM:
                widget.configure(bg=widget.color, fg=BG)

        def leave(_):
            if widget.cget("fg") != DIM:
                widget.configure(bg=BG, fg=widget.color)

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
        widget.bind("<Button-1>", lambda _: command())
        return widget

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
                card.frame.destroy()
            self.cards = [Card(self, self.list, key) for key in keys]
            for card in self.cards:
                card.frame.pack(fill="x", pady=(0, self.px(8)))
            self.card_keys = keys
            if keys:
                self.empty.pack_forget()
            else:
                self.empty.pack(fill="x")
        for number, card in enumerate(self.cards, 1):
            if card.key == UNSAVED:
                label, account, saved_at = "", self.current, ""
            else:
                profile = cs.find(self.profiles, label=card.key)
                label, account, saved_at = profile.label, profile.account, profile.saved_at
            card.show(number, label, account, saved_at, account == self.current,
                      self.switching == card.key, self.snapshot.usage(account, now), now, bool(self.busy))
        current = cs.find(self.profiles, account=self.current) if self.current else None
        text = f"ACTIVE ▸ {current.label if current else 'UNSAVED' if self.current else 'NONE'}"
        self.active.configure(text=text, font=font(text, 9, True))
        self.count.configure(text=f"PROFILES {len(self.profiles)}")

    # ---------- 定时 ----------

    def tick_clock(self) -> None:
        self.blink = not self.blink
        self.clock.configure(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.cursor.configure(fg=GREEN if self.blink else PANEL)
        if self.busy:
            self.dot.configure(fg=AMBER if self.blink else PANEL)
            self.state.configure(text=f"BUSY ▸ {self.busy}", fg=AMBER)
        else:
            self.dot.configure(fg=GREEN if self.blink else DIM)
            self.state.configure(text="IDLE", fg=GREEN)
        if not self.busy and time.time() - self.last_reload > RELOAD_MS / 1000:
            self.reload()
        self.render()
        self.root.after(500, self.tick_clock)

    @staticmethod
    def rain_text() -> str:
        return "\n".join(random.choice(HEX) for _ in range(random.randint(3, 7)))

    def tick_rain(self) -> None:
        canvas = self.header
        for drop in self.rain:
            item, y, speed = drop
            y += speed
            if y > self.header_h + self.px(80):
                y = random.uniform(-self.px(10), 0)
                canvas.itemconfigure(item, text=self.rain_text(), fill=random.choice(RAIN))
            elif random.random() < 0.05:
                canvas.itemconfigure(item, text=self.rain_text())
            drop[1] = y
            canvas.coords(item, canvas.coords(item)[0], y)
        self.root.after(70, self.tick_rain)

    def tick_glitch(self) -> None:
        x, y = self.title_at
        for item in self.glitch:
            self.header.coords(item, x + random.choice((-3, -2, 2, 3)) * self.scale, y + random.choice((-1, 0, 1)))
            self.header.itemconfigure(item, state="normal")
        self.root.after(random.randint(60, 140),
                        lambda: [self.header.itemconfigure(item, state="hidden") for item in self.glitch])
        self.root.after(random.randint(2500, 7000), self.tick_glitch)

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
        console = self.console
        console.configure(state="normal")
        console.insert("end", datetime.now().strftime("[%H:%M:%S] "), "time")
        console.insert("end", "> ", "dim")
        tags = tuple(t for t in (tag, "cjk" if font(text, 9)[0] == CJK else None) if t)
        console.insert("end", text + "\n", tags)
        lines = int(console.index("end-1c").split(".")[0])
        if lines > 400:
            console.delete("1.0", f"{lines - 400}.0")
        console.see("end")
        console.configure(state="disabled")

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
        self.cancel.pack_forget()
        self.set_buttons(True)
        self.reload()
        self.render()

    def set_buttons(self, enabled: bool) -> None:
        for widget in self.buttons:
            widget.configure(fg=widget.color if enabled else DIM, bg=BG, cursor="hand2" if enabled else "arrow")

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
            self.cancel.pack(side="right")
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
        top = tk.Toplevel(self.root, bg=BG, highlightthickness=1, highlightbackground=GREEN)
        top.overrideredirect(True)
        top.transient(self.root)
        top.attributes("-topmost", True)
        tk.Label(top, text=f"▌{title}", font=font(title, 11, True), fg=GREEN, bg=BG, anchor="w") \
            .pack(fill="x", padx=px(16), pady=(px(12), px(6)))
        tk.Label(top, text=message, font=font(message, 10), fg=SOFT, bg=BG, anchor="w", justify="left",
                 wraplength=px(420)).pack(fill="x", padx=px(16))
        result = {"value": None}
        entry = None
        if entry_text is not None:
            entry = tk.Entry(top, font=font("中", 12), bg=CONSOLE, fg=GREEN, insertbackground=GREEN, relief="flat",
                             highlightthickness=1, highlightbackground=LINE, highlightcolor=GREEN,
                             selectbackground=LINE, selectforeground=GREEN, width=32)
            entry.insert(0, entry_text)
            entry.select_range(0, "end")
            entry.pack(fill="x", padx=px(16), pady=(px(10), 0), ipady=px(4))

        def ok(_=None):
            result["value"] = entry.get() if entry is not None else True
            top.destroy()

        row = tk.Frame(top, bg=BG)
        row.pack(fill="x", padx=px(16), pady=px(12))
        self.button(row, "取消 Esc", top.destroy, SOFT).pack(side="right")
        self.button(row, "确认 Enter", ok, GREEN).pack(side="right", padx=(0, px(8)))
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


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Claude 桌面端一键切换账号（图形界面）")
    # 下面几项只在测试时用
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--projects", type=Path, default=None)
    parser.add_argument("--sessions-backup", type=Path, default=here / "backups")
    parser.add_argument("--no-app", action="store_true")
    parser.add_argument("--settle", type=int, default=60)
    parser.add_argument("--login-timeout", type=int, default=600)
    args = parser.parse_args()

    env = cs.Env(
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
    setup_windows()
    root = tk.Tk()
    App(root, env)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
