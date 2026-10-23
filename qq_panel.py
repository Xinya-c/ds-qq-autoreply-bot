#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 自动回复 · 控制面板（NapCat WebUI 风格）
=====================================================
界面复刻 NapCat WebUI 的设计语言（HeroUI 浅色主题）：
  · 浅灰页面底 + 白色卡片 + 12px 圆角
  · NapCat 标志性粉色 #F31260（HeroUI primary）
  · 左侧窄导航（选中项圆角底+粉字）+ 顶栏大标题 + 状态胶囊
  · 自绘开关 / 圆角输入框 / 圆角按钮 / 胶囊徽章

功能与旧版完全一致：63 个配置字段、启停守护进程、实时日志、
模型链、人设、记忆、媒体、原始 JSON 编辑。

打包：python -m PyInstaller --noconsole --onefile --name QQBotPanel qq_panel.py
"""
import os
import sys
import json
import time
import base64
import socket
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

APP_TITLE = "QQ 自动回复控制台"

# ================= NapCat WebUI 色板（HeroUI light）=================
BG        = "#F6F6F7"   # background 浅灰（衬托白卡）
CARD      = "#FFFFFF"   # content1 卡片底
CARD2     = "#F4F4F5"   # content2 输入框/悬浮底
CARD3     = "#E4E4E7"   # content3 滚动条 thumb
LINE      = "#E4E4E7"   # divider
BORDER2   = "#D4D4D8"   # 输入框描边
FG        = "#18181B"   # foreground 主文字
FG2       = "#52525B"   # foreground-500 次要
FG3       = "#8E8E96"   # 弱文字
PINK      = "#F31260"   # primary  NapCat 粉
PINK_H    = "#F771A0"   # primary-600 hover
PINK_SOFT = "#FDE8EF"   # primary-100 浅粉底
GREEN     = "#0E9F4E"   # success（浅底可读）
GREEN_SOFT= "#E7F8EF"
YELLOW    = "#F5A524"   # warning
RED       = "#DB3656"   # danger
RED_SOFT  = "#FDE8EC"   # danger-100 浅红底
SWITCH_OFF= "#A1A1AA"   # 开关关闭态
LOG_BG    = "#F8F8F9"   # 日志/代码区
LOG_FG    = "#3F3F46"
RADIUS    = 12          # HeroUI radius-medium


# ---------------- 字体 ----------------
def _font_family():
    tmp = None
    try:
        import tkinter as _tk
        try:
            _tk._get_default_root("use font.families()")
        except RuntimeError:
            tmp = _tk.Tk()
            tmp.withdraw()
        import tkinter.font as tkfont
        fams = set(tkfont.families())
        for f in ("Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei",
                  "DengXian", "SimHei"):
            if f in fams:
                return f
    except Exception:
        pass
    finally:
        if tmp is not None:
            try:
                tmp.destroy()
            except Exception:
                pass
    return "TkDefaultFont"


_FAM = _font_family()
FONT     = (_FAM, 10)
FONT_B   = (_FAM, 10, "bold")
FONT_SM  = (_FAM, 9)
FONT_TITLE = (_FAM, 15, "bold")     # 顶栏页面大标题
FONT_CARD = (_FAM, 12, "bold")      # 卡片标题
FONT_LOGO = (_FAM, 13, "bold")
MONO     = ("Consolas", 9)


# ================= PIL 圆角工具 =================
_HAVE_PIL = True
try:
    import io
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    _HAVE_PIL = False

_IMG_CACHE = {}


def rrect_photo(w, h, r, fill, outline=None, ow=1, cache_key=None):
    """PIL 画圆角矩形 → PhotoImage（Tk8.6 直接吃 base64 PNG）。"""
    if not _HAVE_PIL:
        return None
    w, h = max(int(w), 2), max(int(h), 2)
    key = cache_key or ("rrect", w, h, r, fill, outline, ow)
    hit = _IMG_CACHE.get(key)
    if hit is not None:
        return hit
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if fill:
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=fill)
    if outline:
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, outline=outline,
                            width=ow)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
    _IMG_CACHE[key] = photo
    return photo


def logo_photo(size=56, text="Bot", fg="#FFFFFF", bg=PINK):
    """侧栏/概览用的圆角logo块：粉底白字。"""
    if not _HAVE_PIL:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=14, fill=bg)
    try:
        font = ImageFont.truetype("msyh.ttc", int(size * 0.5))
    except Exception:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", int(size * 0.5))
        except Exception:
            font = ImageFont.load_default()
    bb = d.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((size - tw) / 2 - bb[0], (size - th) / 2 - bb[1]), text,
           font=font, fill=fg)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


def switch_photo(w, h, on, knob_r=9, pad=3):
    """HeroUI 开关：圆角底 + 白色圆点。"""
    if not _HAVE_PIL:
        return None
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2,
                        fill=PINK if on else SWITCH_OFF)
    r = knob_r
    cx = (w - pad - r) if on else (pad + r)
    cy = h // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


# ================= 浅色标题栏（Win10/11 DWM）=================
def light_titlebar(hwnd):
    if os.name != "nt":
        return
    try:
        import ctypes
        # DWMWA_USE_DARK_MODE = 20（Win11 与 Win10 20H2+）；0 = 浅色
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(ctypes.c_int(0)), ctypes.sizeof(ctypes.c_int))
    except Exception:
        pass


# ================= 路径 =================
def find_base():
    if getattr(sys, "frozen", False):
        d = os.path.dirname(os.path.abspath(sys.executable))
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    for cand in (d, os.path.dirname(d)):
        if cand and os.path.exists(os.path.join(cand, "qq_autoreply_config.json")):
            return cand
    return d


BASE = find_base()
CONFIG_PATH = os.path.join(BASE, "qq_autoreply_config.json")
LOG_PATH = os.path.join(BASE, "qq_autoreply.log")
PID_PATH = os.path.join(BASE, "qq_autoreply.pid")
ACCOUNT_PATH = os.path.join(BASE, "qq_accounts.json")
NAPCAT_BAT = os.path.join(BASE, "NapCat.Shell.Windows.Node", "napcat.bat")

# boot_all：复用一键启动脚本的 NapCat 启动/检测逻辑（环境净化很重要：
# WorkBuddy 注入的 NODE_OPTIONS 会让 NapCat 崩溃，必须用 clean_env）
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    import boot_all as _boot
except Exception:
    _boot = None


def find_python():
    # 源码运行：直接用当前解释器；打包成 exe 后按 PATH → 常见安装位 → 兜底 的顺序找
    if not getattr(sys, "frozen", False):
        return sys.executable or "python"
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, "python.exe")
        if os.path.exists(p):
            return p
    for p in (r"C:\Python\python.exe",
              os.path.join(os.environ.get("ProgramFiles", ""), "Python",
                           "python.exe")):
        if p and os.path.exists(p):
            return p
    return "python"


PYTHON = find_python()

# 后台查询类子进程不弹窗（GUI 程序弹控制台程序会闪窗口）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


# ---------------- 配置读写 ----------------
def cget(cfg, path, default=None):
    cur = cfg
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def cset(cfg, path, val):
    keys = path.split(".")
    cur = cfg
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = val


def to_int_list(text):
    out = []
    for part in (text or "").replace(",", "\n").replace("，", "\n").splitlines():
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def to_str_list(text):
    out = []
    for part in (text or "").replace(",", "\n").replace("，", "\n").splitlines():
        part = part.strip()
        if part:
            out.append(part)
    return out


# ---------------- 系统状态 ----------------
def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            ACCESS = 0x00100000 | 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(ACCESS, False, int(pid))
            if h:
                try:
                    code = ctypes.c_ulong(0)
                    if ctypes.windll.kernel32.GetExitCodeProcess(
                            h, ctypes.byref(code)):
                        return code.value == STILL_ACTIVE
                finally:
                    ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            pass
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10,
                             creationflags=NO_WINDOW).stdout
        return str(pid) in (out or "")
    except Exception:
        return False


def read_pid():
    try:
        return int(open(PID_PATH).read().strip() or 0)
    except Exception:
        return 0


def port_open(port=3001, timeout=0.3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


_ACCOUNT_CACHE = {"mtime": 0.0, "text": "—"}


def current_account():
    global _ACCOUNT_CACHE
    try:
        m = os.path.getmtime(ACCOUNT_PATH)
        if m == _ACCOUNT_CACHE["mtime"]:
            return _ACCOUNT_CACHE["text"]
        d = json.load(open(ACCOUNT_PATH, encoding="utf-8"))
        uins = [k for k in d if k.isdigit()]
        txt = ", ".join("%s(%s)" % (d.get(u, ""), u) for u in uins) or "—"
        _ACCOUNT_CACHE = {"mtime": m, "text": txt}
        return txt
    except Exception:
        return "—"


# ================= ttk 深色样式 =================
def setup_styles():
    st = ttk.Style()
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD)
    st.configure("TLabel", background=BG, foreground=FG, font=FONT)
    st.configure("Card.TLabel", background=CARD, foreground=FG, font=FONT)
    st.configure("Hint.TLabel", background=CARD, foreground=FG2, font=FONT_SM)
    st.configure("TCheckbutton", background=CARD, foreground=FG, font=FONT,
                 focusthickness=0, padding=2)
    st.map("TCheckbutton", background=[("active", CARD)])
    # 滚动条：灰色 thumb + 浅槽
    st.configure("Vertical.TScrollbar", background=CARD3, troughcolor=BG,
                 bordercolor=BG, arrowcolor=FG3, width=9)
    st.map("Vertical.TScrollbar", background=[("active", "#D4D4D8"),
                                              ("pressed", "#A1A1AA")])
    # Combobox 深色
    st.configure("TCombobox", fieldbackground=CARD2, background=CARD2,
                 foreground=FG, bordercolor=LINE, lightcolor=LINE,
                 darkcolor=LINE, borderwidth=1, arrowsize=13, padding=4)
    st.map("TCombobox", fieldbackground=[("readonly", CARD2)],
           foreground=[("readonly", FG)],
           background=[("readonly", CARD2)])
    st.map("TCombobox", selectbackground=[("readonly", PINK)],
           selectforeground=[("readonly", "#FFFFFF")])
    return st


def _bind_text_wheel(box):
    def on_wheel(e):
        first, last = box.yview()
        if (e.delta > 0 and first <= 0.0) or (e.delta < 0 and last >= 1.0):
            return None
        box.yview_scroll(int(-e.delta / 120), "units")
        return "break"
    box.bind("<MouseWheel>", on_wheel)


def _style_scrolled(box, bg, fg, scroll_bg=CARD3, trough=None):
    """统一文本框观感：浅色底、细滚动条。"""
    trough = bg if trough is None else trough
    box.configure(bg=bg, fg=fg, insertbackground=fg, bd=0, highlightthickness=0,
                  padx=10, pady=8, font=MONO if bg == LOG_BG else FONT)
    _bind_text_wheel(box)
    try:
        box.vbar.configure(bg=scroll_bg, troughcolor=trough, bd=0,
                           highlightthickness=0, width=8, activebackground="#C4C4CC")
    except Exception:
        pass


# ================= 圆角卡片 =================
class RCard(tk.Frame):
    """HeroUI 卡片：PIL 圆角底 + inset 内容（inset>半径，直角角永不可见）。"""

    INSET = 16

    def __init__(self, master, title=None, desc=None):
        tk.Frame.__init__(self, master, bg=BG, bd=0, highlightthickness=0)
        self._bg = tk.Label(self, bg=BG, bd=0, highlightthickness=0)
        self._bg.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=CARD, bd=0, highlightthickness=0)
        self.body.pack(fill="both", expand=True,
                       padx=self.INSET, pady=(14, 16))
        self._cw = 0
        self.bind("<Configure>", self._on_conf)
        self.r = 0
        if title or desc:
            head = tk.Frame(self.body, bg=CARD, bd=0)
            head.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
            if title:
                tk.Label(head, text=title, bg=CARD, fg=FG, font=FONT_CARD,
                         anchor="w").pack(side="left")
            if desc:
                tk.Label(head, text=desc, bg=CARD, fg=FG2, font=FONT_SM,
                         anchor="w").pack(side="left", padx=(10, 0), pady=(3, 0))
            self.r = 1
        self.body.columnconfigure(0, minsize=236)
        self.body.columnconfigure(2, weight=1)

    def _on_conf(self, e=None):
        w = self.winfo_width()
        if w and abs(w - self._cw) > 2:
            self._cw = w
            self._bg.configure(
                image=rrect_photo(w, self.winfo_height(), RADIUS, CARD,
                                  outline=LINE, ow=1))

    def _row(self):
        r = self.r
        self.r += 1
        return r

    # ---- 控件 ----
    def switch(self, path, text, hint=None):
        v = tk.BooleanVar(value=bool(cget(self.form_cfg(), path, False)))
        row = tk.Frame(self.body, bg=CARD, bd=0)
        row.grid(row=self._row(), column=0, columnspan=3, sticky="w", pady=5)
        RSwitch(row, v).pack(side="left")
        tk.Label(row, text=text, bg=CARD, fg=FG, font=FONT,
                 anchor="w").pack(side="left", padx=(10, 0))
        if hint:
            tk.Label(row, text=hint, bg=CARD, fg=FG3, font=FONT_SM,
                     anchor="w").pack(side="left", padx=(10, 0))
        self.form_add(path, "bool", v)

    def entry(self, path, label, kind="int", hint=None, width=14):
        tk.Label(self.body, text=label, bg=CARD, fg=FG, font=FONT,
                 anchor="w").grid(row=self._row(), column=0, sticky="w", pady=6)
        v = tk.StringVar(value=str(cget(self.form_cfg(), path, "")))
        REntry(self.body, v, width).grid(row=self.r - 1, column=1, sticky="w")
        if hint:
            tk.Label(self.body, text=hint, bg=CARD, fg=FG3, font=FONT_SM,
                     anchor="w").grid(row=self.r - 1, column=2, sticky="w",
                                      padx=(14, 0))
        self.form_add(path, kind, v)

    def combo(self, path, label, values, hint=None):
        tk.Label(self.body, text=label, bg=CARD, fg=FG, font=FONT,
                 anchor="w").grid(row=self._row(), column=0, sticky="w", pady=6)
        v = tk.StringVar(value=str(cget(self.form_cfg(), path, values[0])))
        RCombo(self.body, v, values).grid(row=self.r - 1, column=1, sticky="w")
        if hint:
            tk.Label(self.body, text=hint, bg=CARD, fg=FG3, font=FONT_SM,
                     anchor="w").grid(row=self.r - 1, column=2, sticky="w",
                                      padx=(14, 0))
        self.form_add(path, "str", v)

    def text(self, path, label, height=6, hint=None, kind="text"):
        tk.Label(self.body, text=label, bg=CARD, fg=FG, font=FONT,
                 anchor="w").grid(row=self._row(), column=0, columnspan=3,
                                  sticky="w", pady=(8, 2))
        if hint:
            tk.Label(self.body, text=hint, bg=CARD, fg=FG3, font=FONT_SM,
                     anchor="w").grid(row=self.r - 1, column=2, sticky="w",
                                      padx=(12, 0))
        rt = RText(self.body, height=height)
        rt.grid(row=self._row(), column=0, columnspan=3, sticky="ew",
                pady=(2, 4))
        val = cget(self.form_cfg(), path, "")
        if isinstance(val, list):
            val = "\n".join(str(x) for x in val)
        rt.box.insert("1.0", str(val))
        self.form_add(path, kind, rt)

    def section(self, text):
        tk.Label(self.body, text=text, bg=CARD, fg=FG2, font=FONT_SM,
                 anchor="w").grid(row=self._row(), column=0, columnspan=3,
                                  sticky="w", pady=(12, 2))

    def form_cfg(self):
        return getattr(self, "_form").cfg if hasattr(self, "_form") else {}

    def form_add(self, path, kind, control):
        getattr(self, "_form").add(path, kind, control)


# ================= 圆角按钮 =================
class RButton(tk.Label):
    """Label 贴圆角图 + 文字，hover 换图。"""

    def __init__(self, master, text, command=None, kind="primary",
                 font=None, padx=18, pady=7, master_bg=BG):
        self._kind = kind
        self._cmd = command
        self._master_bg = master_bg
        font = font or FONT
        try:
            tw = tk.font.Font(font=font).measure(text)
        except Exception:
            tw = len(text) * 10
        w = int(tw + padx * 2 + 6)
        h = 24 + pady * 2
        self._imgs = self._make_imgs(w, h)
        tk.Label.__init__(self, master, image=self._imgs["normal"], text=text,
                          compound="center", bg=master_bg, fg=self._fg,
                          bd=0, highlightthickness=0, font=font, cursor="hand2",
                          takefocus=False)
        self._hover = False
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def _make_imgs(self, w, h):
        k = self._kind
        if k == "primary":
            return {"normal": rrect_photo(w, h, 8, PINK),
                    "hover": rrect_photo(w, h, 8, PINK_H)}
        if k == "danger":
            return {"normal": rrect_photo(w, h, 8, RED_SOFT, outline=RED, ow=1),
                    "hover": rrect_photo(w, h, 8, "#FBD5DE", outline=RED, ow=1)}
        if k == "nav":
            return {"normal": rrect_photo(w, h, 8, None),
                    "hover": rrect_photo(w, h, 8, CARD2),
                    "active": rrect_photo(w, h, 8, CARD)}
        return {"normal": rrect_photo(w, h, 8, None, outline=LINE, ow=1),
                "hover": rrect_photo(w, h, 8, None, outline="#A1A1AA", ow=1)}

    @property
    def _fg(self):
        return "#FFFFFF" if self._kind == "primary" else (
            RED if self._kind == "danger" else FG2)

    def _enter(self, _=None):
        self._hover = True
        if self._kind == "nav" and getattr(self, "_active", False):
            return
        self.configure(image=self._imgs["hover"])

    def _leave(self, _=None):
        self._hover = False
        if self._kind == "nav" and getattr(self, "_active", False):
            return
        self.configure(image=self._imgs["normal"])

    def _click(self, _=None):
        if self._cmd:
            self._cmd()


# ================= 圆角输入框 =================
class REntry(tk.Frame):
    """tk.Entry 贴在 PIL 圆角底上；focus 时边框变粉。"""

    H = 30

    def __init__(self, master, variable=None, width=14):
        bg = master.cget("bg")
        tk.Frame.__init__(self, master, bg=bg, bd=0, highlightthickness=0)
        self._bg = tk.Label(self, bg=bg, bd=0, highlightthickness=0)
        self._bg.place(x=0, y=0, relwidth=1, relheight=1)
        self._var = variable or tk.StringVar()
        self._e = tk.Entry(self, textvariable=self._var, width=width, bg=CARD2,
                           fg=FG, insertbackground=FG, bd=0,
                           highlightthickness=0, font=FONT, relief="flat",
                           selectbackground=PINK, selectforeground="#FFFFFF")
        self._e.place(x=10, y=1, relwidth=1, relheight=1, width=-20,
                      height=-2)
        ew = self._e.winfo_reqwidth() or 100
        self.pack_propagate(False)
        self.configure(width=ew + 20, height=self.H)
        self._focused = False
        self._e.bind("<FocusIn>", lambda e: self._focus(True))
        self._e.bind("<FocusOut>", lambda e: self._focus(False))
        self._redraw()

    def _redraw(self):
        w = max(self.winfo_reqwidth(), 60)
        self._bg.configure(image=rrect_photo(
            w, self.H, 8, CARD2,
            outline=PINK if self._focused else BORDER2, ow=1))

    def _focus(self, on):
        self._focused = on
        self._redraw()

    def get(self):
        return self._var.get()

    def set(self, v):
        self._var.set(v)


# ================= 圆角下拉 =================
class RCombo(ttk.Combobox):
    """ttk.Combobox + 圆角底图。"""

    H = 30

    def __init__(self, master, variable, values):
        bg = master.cget("bg")
        self._wrap = tk.Frame(master, bg=bg, bd=0, highlightthickness=0)
        self._bg = tk.Label(self._wrap, bg=bg, bd=0, highlightthickness=0)
        self._bg.place(x=0, y=0, relwidth=1, relheight=1)
        ttk.Combobox.__init__(self, self._wrap, textvariable=variable,
                              values=values, state="readonly", width=16,
                              style="TCombobox", font=FONT)
        self.place(x=1, y=1, relwidth=1, relheight=1, width=-2, height=-2)
        cw = self.winfo_reqwidth() or 130
        self._wrap.pack_propagate(False)
        self._wrap.configure(width=cw + 2, height=self.H)
        self._wrap.bind("<Configure>", self._on_conf)
        self._on_conf()

    def _on_conf(self, _=None):
        w = self._wrap.winfo_width()
        if w > 4:
            self._bg.configure(image=rrect_photo(w, self.H, 8, None,
                                                 outline=BORDER2, ow=1))

    def grid(self, **kw):
        self._wrap.grid(kw)


# ================= 圆角多行文本 =================
class RText(tk.Frame):
    """ScrolledText 贴在 PIL 圆角底上。"""

    PAD = 2

    def __init__(self, master, height=6, wrap="word", font=None, bg=CARD2):
        mbg = master.cget("bg")
        tk.Frame.__init__(self, master, bg=mbg, bd=0, highlightthickness=0)
        self._fill = bg
        self._bg = tk.Label(self, bg=mbg, bd=0, highlightthickness=0)
        self._bg.place(x=0, y=0, relwidth=1, relheight=1)
        self.box = scrolledtext.ScrolledText(
            self, height=height, wrap=wrap, bg=bg, fg=FG,
            insertbackground=FG, bd=0, highlightthickness=0,
            font=font or FONT, padx=10, pady=8)
        self.box.place(x=self.PAD, y=self.PAD, relwidth=1, relheight=1,
                       width=-2 * self.PAD, height=-2 * self.PAD)
        h = self.box.winfo_reqheight() or height * 16
        self.configure(height=h + 2 * self.PAD)
        self.bind("<Configure>", self._on_conf)
        _bind_text_wheel(self.box)
        try:
            self.box.vbar.configure(bg=CARD3, troughcolor=bg, bd=0,
                                    highlightthickness=0, width=8,
                                    activebackground="#C4C4CC")
        except Exception:
            pass

    def _on_conf(self, _=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w > 4 and h > 4:
            self._bg.configure(image=rrect_photo(w, h, 10, self._fill,
                                                 outline=BORDER2, ow=1))

    def __getattr__(self, name):
        # 未定义的方法/属性转发给内部 ScrolledText（delete/insert/yview/see…）
        if name == "box":
            raise AttributeError(name)
        return getattr(self.box, name)


# ================= 开关（HeroUI Switch）=================
class RSwitch(tk.Canvas):
    W, H = 40, 22

    def __init__(self, master, variable, command=None):
        tk.Canvas.__init__(self, master, width=self.W, height=self.H,
                           bg=master.cget("bg"), bd=0, highlightthickness=0)
        self._var = variable
        self._cmd = command
        self._on_img = switch_photo(self.W, self.H, True)
        self._off_img = switch_photo(self.W, self.H, False)
        self._img = self.create_image(self.W // 2, self.H // 2,
                                      image=self._on_img if variable.get()
                                      else self._off_img)
        self.bind("<Button-1>", self._toggle)
        variable.trace_add("write", self._refresh)
        self.configure(cursor="hand2")

    def _toggle(self, _=None):
        self._var.set(not self._var.get())
        if self._cmd:
            self._cmd()

    def _refresh(self, *_):
        try:
            self.itemconfig(self._img, image=self._on_img if self._var.get()
                            else self._off_img)
        except Exception:
            pass


# ================= 滚动容器 =================
class Scrollable(ttk.Frame):
    def __init__(self, master):
        ttk.Frame.__init__(self, master, style="TFrame")
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview,
                                 style="Vertical.TScrollbar")
        self.inner = ttk.Frame(self.canvas, style="TFrame")
        self.inner.bind("<Configure>", self._on_conf)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_conf)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

    def _on_conf(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_conf(self, e):
        self.canvas.itemconfig(self._win, width=e.width)

    def to_top(self):
        self.canvas.yview_moveto(0)


# ================= 表单 =================
TEXT_KINDS = ("text", "intlist", "strlist", "gap")


class Form(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.fields = []

    def add(self, path, kind, control):
        self.fields.append((path, kind, control))

    def save(self):
        errs = []
        for path, kind, var in self.fields:
            try:
                if kind in TEXT_KINDS:
                    box = getattr(var, "box", var)
                    raw = box.get("1.0", "end").rstrip("\n")
                else:
                    raw = var.get()
                if kind == "bool":
                    cset(self.cfg, path, bool(var.get()))
                elif kind == "int":
                    cset(self.cfg, path, int(str(raw).strip()))
                elif kind == "float":
                    cset(self.cfg, path, float(str(raw).strip()))
                elif kind == "intlist":
                    cset(self.cfg, path, to_int_list(raw))
                elif kind == "strlist":
                    cset(self.cfg, path, to_str_list(raw))
                elif kind == "gap":
                    nums = [int(x) for x in to_int_list(raw)]
                    cset(self.cfg, path, nums[0] if len(nums) == 1
                         else (nums[:2] if nums else 0))
                else:
                    cset(self.cfg, path, raw)
            except Exception as e:
                errs.append("%s: %s" % (path, e))
        return errs


# ================= 主窗口 =================
NAV_ITEMS = (
    ("overview", "概览", "⌂"),
    ("logs", "实时日志", "▤"),
    ("basic", "基本配置", "⚙"),
    ("model", "模型与额度", "⚡"),
    ("persona", "人设风格", "✿"),
    ("memory", "记忆归档", "◉"),
    ("media", "媒体与表情", "✦"),
    ("raw", "原始 JSON", "{}"),
)


class Panel(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title(APP_TITLE)
        self.geometry("1120x760")
        self.minsize(960, 660)
        self.cfg = self.load_cfg()
        self.forms = []
        self.views = {}
        self._scroll = None
        self._scroll_of = {}
        self.st = {}
        self.st_dots = {}

        setup_styles()
        self.configure(bg=BG)
        try:  # Combobox 下拉列表配色
            self.option_add("*TCombobox*Listbox.background", CARD2)
            self.option_add("*TCombobox*Listbox.foreground", FG)
            self.option_add("*TCombobox*Listbox.selectBackground", PINK)
            self.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
            self.option_add("*TCombobox*Listbox.font", FONT)
        except Exception:
            pass
        self._build_side()
        self._build_main()
        self.show("overview")

        self._ticking = False
        self._log_pos = 0
        self._log_init = False
        self._archive_sig = None
        self._archive_cache = "—"
        self._napcat_booting = False
        self._napcat_cache = None
        self._napcat_tick = 0
        self.after(400, self.tick)
        self.bind("<MouseWheel>", self._wheel)
        self.after(120, self._light_title)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _light_title(self):
        """Windows 标题栏跟随浅色（Win11 / Win10 20H2+）。"""
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            light_titlebar(hwnd)
        except Exception:
            pass

    # ---------- 侧栏 ----------
    def _build_side(self):
        side = tk.Frame(self, bg=BG, width=208)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        brand = tk.Frame(side, bg=BG)
        brand.pack(fill="x", padx=16, pady=(20, 14))
        logo = logo_photo(40, "Bot")
        if logo is not None:
            tk.Label(brand, image=logo, bg=BG, bd=0).pack(side="left")
        txt = tk.Frame(brand, bg=BG)
        txt.pack(side="left", padx=(11, 0), pady=(11, 0))
        tk.Label(txt, text="QQ 自动回复", bg=BG, fg=FG, font=FONT_LOGO,
                 anchor="w").pack(anchor="w")

        tk.Frame(side, bg=LINE, height=1).pack(fill="x", padx=12, pady=(0, 10))

        self.nav_btns = {}
        nav_wrap = tk.Frame(side, bg=BG)
        nav_wrap.pack(fill="x", padx=10)
        for key, label, icon in NAV_ITEMS:
            b = RButton(nav_wrap, "%s   %s" % (icon, label), kind="nav",
                        command=lambda k=key: self.show(k), padx=14, pady=8,
                        master_bg=BG)
            b.pack(fill="x", pady=2)
            b._nav_key = key
            self.nav_btns[key] = b

        foot = tk.Frame(side, bg=BG)
        foot.pack(side="bottom", fill="x", padx=18, pady=14)
        self.side_dot = tk.Label(foot, text="●", bg=BG, fg=FG3, font=(_FAM, 8))
        self.side_dot.pack(side="left")
        tk.Label(foot, text=" v3 · 浅色主题", bg=BG, fg=FG3,
                 font=FONT_SM).pack(side="left")

    def _nav_state(self, key, active):
        b = self.nav_btns[key]
        b._active = active
        b.configure(image=b._imgs["active" if active else "normal"],
                    fg=PINK if active else FG2)

    # ---------- 顶栏 ----------
    def _build_main(self):
        main = tk.Frame(self, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=BG, height=62)
        header.pack(fill="x")
        header.pack_propagate(False)
        left = tk.Frame(header, bg=BG)
        left.pack(side="left", fill="y", padx=(26, 0))
        self.page_title = tk.Label(left, text="概览", bg=BG, fg=FG,
                                   font=FONT_TITLE, anchor="w")
        self.page_title.pack(anchor="w", pady=(10, 0))
        self.page_sub = tk.Label(left, text="", bg=BG, fg=FG3, font=FONT_SM,
                                 anchor="w")
        self.page_sub.pack(anchor="w")
        right = tk.Frame(header, bg=BG)
        right.pack(side="right", fill="y", padx=(0, 20))
        self.pill = tk.Label(right, text="● —", bg=CARD, fg=FG2,
                             font=FONT_SM, padx=12, pady=5)
        self.pill.pack(side="left", pady=16, padx=(0, 10))
        self._pill_img = None
        self._set_pill("● —", CARD2, FG2)
        RButton(right, "备份配置", command=self.backup, kind="ghost",
                master_bg=BG).pack(side="left", pady=16, padx=(0, 8))
        RButton(right, "保存配置", command=self.save_all, kind="primary",
                master_bg=BG).pack(side="left", pady=16)
        tk.Frame(main, bg=LINE, height=1).pack(fill="x")

        self.content = tk.Frame(main, bg=BG)
        self.content.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="")
        tk.Label(main, textvariable=self.status_var, bg=BG, fg=GREEN,
                 font=FONT_SM, anchor="w").pack(anchor="w", padx=26, pady=(4, 8))

        self.build_overview()
        self.build_logs()
        self.build_basic()
        self.build_model()
        self.build_persona()
        self.build_memory()
        self.build_media()
        self.build_raw()

    def _wheel(self, e):
        sc = self._scroll
        if sc is not None:
            sc.canvas.yview_scroll(int(-e.delta / 120) or (1 if e.delta > 0 else -1),
                                   "units")
        return "break"

    def show(self, key):
        self._current = key
        for k, view in self.views.items():
            if k == key:
                view.pack(fill="both", expand=True)
            else:
                view.pack_forget()
        for k in self.nav_btns:
            self._nav_state(k, k == key)
        self._scroll = self._scroll_of.get(key)
        title, sub = PAGE_SUB.get(key, ("", ""))
        self.page_title.configure(text=title)
        self.page_sub.configure(text=sub)
        if self._scroll is not None:
            self._scroll.to_top()

    # ---------- 配置 ----------
    def load_cfg(self):
        try:
            return json.load(open(CONFIG_PATH, encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("读取失败", "无法读取配置：\n%s" % e)
            return {}

    def backup(self):
        try:
            dst = CONFIG_PATH + ".bak_" + time.strftime("%m%d_%H%M%S")
            json.dump(self.cfg, open(dst, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            self.status("已备份到 %s" % os.path.basename(dst))
        except Exception as e:
            messagebox.showerror("备份失败", str(e))

    def save_all(self):
        errs = []
        for p in self.forms:
            errs += p.save()
        if errs:
            messagebox.showerror("保存失败", "\n".join(errs))
            return
        try:
            json.dump(self.cfg, open(CONFIG_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("写入失败", str(e))
            return
        self.status("已保存 %s —— 重启守护进程后生效" % time.strftime("%H:%M:%S"))

    def status(self, msg):
        self.status_var.set(msg)

    def _new_page(self, key, scrollable=True):
        view = ttk.Frame(self.content, style="TFrame")
        self.views[key] = view
        if scrollable:
            sc = Scrollable(view)
            sc.pack(fill="both", expand=True)
            self._scroll_of[key] = sc
            return sc.inner
        return view

    # ---------- 概览 ----------
    def build_overview(self):
        page = self._new_page("overview", scrollable=False)
        wrap = ttk.Frame(page, style="TFrame", padding=(22, 18, 22, 0))
        wrap.pack(fill="both", expand=True)

        # 账号大卡（仿 NapCat 概览）
        acct = RCard(wrap)
        acct.pack(fill="x")
        inner = tk.Frame(acct.body, bg=CARD)
        inner.pack(fill="x")
        logo = logo_photo(52, "Bot")
        if logo is not None:
            tk.Label(inner, image=logo, bg=CARD, bd=0).pack(side="left")
        info = tk.Frame(inner, bg=CARD)
        info.pack(side="left", padx=(14, 0))
        tk.Label(info, text="QQ 自动回复机器人", bg=CARD, fg=FG,
                 font=(_FAM, 13, "bold"), anchor="w").pack(anchor="w")
        self.ov_badge = tk.Label(info, text="● 状态读取中", bg=CARD, fg=FG2,
                                 font=FONT_SM, anchor="w")
        self.ov_badge.pack(anchor="w", pady=(3, 0))
        tk.Label(info, text="NapCat OneBot v11", bg=CARD, fg=FG3,
                 font=FONT_SM, anchor="w").pack(anchor="w", pady=(2, 0))
        ops = tk.Frame(inner, bg=CARD)
        ops.pack(side="right")
        RButton(ops, "启动", command=self.start_daemon, kind="primary",
                master_bg=CARD).pack(side="left", padx=(0, 8))
        RButton(ops, "停止", command=self.stop_daemon, kind="danger",
                master_bg=CARD).pack(side="left", padx=(0, 8))
        RButton(ops, "重启", command=self.restart_daemon, kind="ghost",
                master_bg=CARD).pack(side="left")

        # 信息网格
        grid = tk.Frame(wrap, bg=BG)
        grid.pack(fill="x", pady=(14, 0))
        for i in range(3):
            grid.columnconfigure(i, weight=1)
        cells = [("守护进程", "daemon", True), ("NapCat", "napcat", True),
                 ("OneBot :3001", "port", True), ("当前账号", "account", False),
                 ("归档消息", "archive", False), ("主力模型", "model", False)]
        for i, (label, key, dot) in enumerate(cells):
            self._mini_card(grid, i // 3, i % 3, label, key, dot)

        # 快捷入口
        quick = tk.Frame(wrap, bg=BG)
        quick.pack(fill="x", pady=(14, 0))
        for txt, cmd in (("一键启动", self.boot_all), ("启动 NapCat", self.start_napcat),
                         ("打开日志", self.open_log),
                         ("打开归档目录", self.open_archive),
                         ("打开 NapCat WebUI", self.open_napcat_webui)):
            RButton(quick, txt, command=cmd, kind="ghost",
                    master_bg=BG).pack(side="left", padx=(0, 8))

        # 日志预览卡
        log_card = RCard(wrap, "实时日志", "守护进程输出，完整日志见「实时日志」页")
        log_card.pack(fill="both", expand=True, pady=(14, 0))
        log_card.body.rowconfigure(1, weight=1)
        log_card.body.columnconfigure(0, weight=1)
        self.log_box = RText(log_card.body, height=9, bg=LOG_BG, font=MONO)
        self.log_box.grid(row=1, column=0, columnspan=3, sticky="nsew")
        self.log_box.box.configure(fg=LOG_FG)

    def _mini_card(self, parent, r, c, label, key, dot):
        cell = tk.Frame(parent, bg=BG)
        cell.grid(row=r, column=c, sticky="nsew", padx=(0, 12), pady=(0, 12))
        card = RCard(cell)
        card.pack(fill="both", expand=True)
        top = tk.Frame(card.body, bg=CARD)
        top.grid(row=0, column=0, sticky="w")
        if dot:
            d = tk.Label(top, text="●", bg=CARD, fg=FG3, font=(_FAM, 8))
            d.pack(side="left")
            self.st_dots[key] = d
        tk.Label(top, text=label, bg=CARD, fg=FG2, font=FONT_SM).pack(
            side="left", padx=(7, 0))
        v = tk.StringVar(value="—")
        tk.Label(card.body, textvariable=v, bg=CARD, fg=FG, font=FONT_B,
                 anchor="w").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.st[key] = v

    # ---------- 实时日志 ----------
    def build_logs(self):
        inner = self._new_page("logs")
        c = RCard(inner, "实时日志", "qq_autoreply.log 增量读取")
        c.pack(fill="both", expand=True)
        c.body.rowconfigure(2, weight=1)
        c.body.columnconfigure(0, weight=1)
        bar = tk.Frame(c.body, bg=CARD)
        bar.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.auto_refresh = tk.BooleanVar(value=True)
        RSwitch(bar, self.auto_refresh).pack(side="left")
        tk.Label(bar, text="自动刷新（每 3 秒）", bg=CARD, fg=FG,
                 font=FONT).pack(side="left", padx=(10, 16))
        RButton(bar, "清空显示", command=self._clear_log, kind="ghost",
                master_bg=CARD).pack(side="left")
        RButton(bar, "打开文件", command=self.open_log, kind="ghost",
                master_bg=CARD).pack(side="left", padx=(8, 0))
        self.full_log = RText(c.body, height=30, bg=LOG_BG, font=MONO)
        self.full_log.grid(row=2, column=0, columnspan=3, sticky="nsew")
        self.full_log.box.configure(fg=LOG_FG)
        self._full_log_pos = 0

    def _clear_log(self):
        self.full_log.delete("1.0", "end")

    # ---------- 基本 ----------
    def build_basic(self):
        inner = self._new_page("basic")
        form = Form(self.cfg)
        c = RCard(inner, "开关与白名单", "谁能收到回复")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.switch("enabled", "总开关", "关掉后只记录不回复")
        c.switch("reply_in_private", "回复私聊")
        c.switch("reply_in_group_when_at", "群里被 @ 才回")
        c.switch("at_reply_prefix", "群回复开头 @ 对方")
        c.switch("restore_context", "启动时回读历史聊天")
        c.text("allowed_private", "私聊白名单", 4, "每行或逗号分隔一个 QQ 号",
               kind="intlist")
        c.text("allowed_groups", "群白名单", 3, "只回这些群里 @我 的消息",
               kind="intlist")

        c = RCard(inner, "限流与合并", "防刷屏、控制节奏")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.entry("min_interval_sec", "同一会话最小间隔（秒）", "int")
        c.entry("max_replies_per_minute", "每分钟最多回复条数", "int")
        c.entry("merge_window_sec", "连发合并窗口（秒）", "int", "0=关闭合并")
        c.entry("max_parallel", "最多同时处理几个会话", "int")

        c = RCard(inner, "上下文", "直接影响额度消耗")
        c.pack(fill="x")
        c._form = form
        c.entry("context_messages", "进提示词的最近消息条数", "int", "越大越费额度")
        c.entry("reply_max_chars", "单条回复字数上限", "int")
        self.forms.append(form)

    # ---------- 模型 ----------
    def build_model(self):
        inner = self._new_page("model")
        form = Form(self.cfg)
        c = RCard(inner, "生成后端", "默认走 CodeBuddy CLI，不花自己的 Key")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.combo("backend", "生成后端", ["codebuddy_cli", "deepseek"])
        c.entry("codebuddy.model", "主力模型", "str", None, 26)
        c.text("codebuddy.models", "模型链", 5,
               "每行一个，主力限流时按顺序顶上", kind="strlist")
        c.entry("codebuddy.tools", "CLI 工具白名单", "str", "空=全禁", 24)
        c.entry("codebuddy.timeout", "单次调用超时（秒）", "int")

        c = RCard(inner, "限流与兜底")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.entry("cli_cooldown_sec", "429 后的冷却秒数", "int",
                "解析到重置时间则以重置为准")
        c.switch("fallback_to_api", "CLI 失败时改用直连 API", "会花自己的 Key")
        c.text("quota_reply", "全部通道不可用时的占位回复", 2, "留空=不回")

        c = RCard(inner, "直连 API", "fallback 打开时才用到")
        c.pack(fill="x")
        c._form = form
        c.entry("llm.url", "接口地址", "str", None, 44)
        c.entry("llm.model", "模型名", "str", None, 24)
        c.entry("llm.api_key_source", "Key 来源", "str", "workbuddy/env/literal", 14)
        c.entry("llm.api_key", "API Key", "str", "literal 时才填", 40)
        c.entry("llm.temperature", "温度", "float")
        c.entry("llm.max_tokens", "最大输出 tokens", "int")
        self.forms.append(form)

    # ---------- 人设 ----------
    def build_persona(self):
        inner = self._new_page("persona")
        form = Form(self.cfg)
        c = RCard(inner, "主设定", "大肥鱼 · 12 岁普通小女孩")
        c.pack(fill="x")
        c._form = form
        c.text("system_prompt", "人设内容", 12)
        self.forms.append(form)

    # ---------- 记忆 ----------
    def build_memory(self):
        inner = self._new_page("memory")
        form = Form(self.cfg)
        c = RCard(inner, "事实摘要 + 关系档案", "每 N 条刷新一次")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.switch("summary.enabled", "启用")
        c.entry("summary.every", "每多少条刷新一次", "int")
        c.entry("summary.max_points", "最多保留几条要点", "int")
        c.entry("summary.max_chars", "摘要字数上限", "int")
        c.combo("summary.backend", "摘要走哪条路", ["cli", "api"])

        c = RCard(inner, "中期记忆 · 阶段回顾", "永久累积，不丢弃")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.switch("memory.stage_enabled", "启用")
        c.entry("memory.stage_every", "每多少条压缩一段", "int", "调大更省额度")
        c.entry("memory.stage_max", "最多保留几段", "int", "0=全部保留")
        c.entry("memory.backlog_per_run", "启动时最多补压几段", "int", "0=不限")

        c = RCard(inner, "长期 · 说话风格档案")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.switch("style.enabled", "启用")
        c.entry("style.every", "累计多少句提炼一次", "int")
        c.entry("style.max_chars", "风格档案字数上限", "int")

        c = RCard(inner, "其它")
        c.pack(fill="x")
        c._form = form
        c.switch("memory.mood_enabled", "情绪轨迹", "关键词判断，不花额度")
        c.entry("memory.samples", "Few-shot 风格样本条数", "int", "0=关闭")
        self.forms.append(form)

    # ---------- 媒体 ----------
    def build_media(self):
        inner = self._new_page("media")
        form = Form(self.cfg)
        c = RCard(inner, "收到的内容", "怎么处理图片、语音、文件")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.switch("media.enabled", "处理收到的图片", "看懂画面")
        c.entry("media.max_bytes", "单张图片上限（字节）", "int", "2097152=2MB")
        c.entry("media.max_images", "每条消息最多带几张图", "int")
        c.switch("voice.transcribe", "语音转文字")
        c.switch("file.read", "读取收到的文件内容")
        c.entry("file.max_bytes", "文件大小上限（字节）", "int", "5242880=5MB")
        c.entry("file.text_limit", "最多注入多少字", "int")
        c.switch("file.download_all", "把收到的文件存盘", "非白名单会话也会存")

        c = RCard(inner, "表情包")
        c.pack(fill="x", pady=(0, 12))
        c._form = form
        c.switch("reply_images.enabled", "发表情包")
        c.entry("reply_images.max_in_prompt", "提示词里最多列几个", "int", "调小省额度")
        c.text("reply_images.min_gap", "两次表情包之间至少隔几次回复", 2,
               "一个数字=固定；两个数字=随机区间", kind="gap")

        c = RCard(inner, "拆条发送", "主人的连发习惯")
        c.pack(fill="x")
        c._form = form
        c.switch("reply.split_multiline", "按换行拆成多条发送")
        c.entry("reply.max_parts", "最多拆成几条", "int")
        c.entry("reply.split_delay_sec", "两条之间的间隔（秒）", "float")
        self.forms.append(form)

    # ---------- 原始 JSON ----------
    def build_raw(self):
        inner = self._new_page("raw")
        c = RCard(inner, "原始 JSON", "保存前会做 JSON 校验")
        c.pack(fill="x")
        c._form = Form(self.cfg)
        self.raw_box = RText(c.body, height=30, wrap="none", font=MONO)
        self.raw_box.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.raw_box.box.insert("1.0", json.dumps(self.cfg, ensure_ascii=False,
                                                  indent=2))
        btns = tk.Frame(c.body, bg=CARD)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))
        RButton(btns, "校验并应用", command=self.apply_raw, kind="primary",
                master_bg=CARD).pack(side="left")
        RButton(btns, "重新载入", command=self.reload_raw, kind="ghost",
                master_bg=CARD).pack(side="left", padx=8)

    def reload_raw(self):
        self.raw_box.delete("1.0", "end")
        self.raw_box.insert("1.0", json.dumps(self.cfg, ensure_ascii=False, indent=2))

    def apply_raw(self):
        try:
            data = json.loads(self.raw_box.get("1.0", "end"))
        except Exception as e:
            messagebox.showerror("JSON 错误", str(e))
            return
        self.cfg = data
        try:
            json.dump(data, open(CONFIG_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("写入失败", str(e))
            return
        self.status("已应用原始 JSON —— 重启守护进程生效")

    # ---------- NapCat 控制 ----------
    def napcat_running(self):
        """NapCat 是否在运行：进程在 或 OneBot :3001 已监听。"""
        try:
            if _boot is not None and _boot.napcat_pids():
                return True
            return port_open()
        except Exception:
            return False

    def start_napcat(self, on_ready=None):
        """启动 NapCat（已在跑则跳过）；就绪后回调 on_ready。"""
        if self._napcat_booting:
            self.status("NapCat 正在启动中，请稍候…")
            return
        if self.napcat_running():
            self.status("NapCat 已在运行")
            if on_ready:
                on_ready()
            return
        try:
            self._napcat_booting = True
            self.status("正在启动 NapCat…（会弹出新窗口，首次可能要扫码）")
            if _boot is not None:
                _boot.start_napcat()
            else:
                subprocess.Popen(["cmd", "/c", NAPCAT_BAT],
                                 cwd=os.path.dirname(NAPCAT_BAT),
                                 creationflags=getattr(
                                     subprocess, "CREATE_NEW_CONSOLE", 0))
            self._wait_napcat(0, on_ready)
        except Exception as e:
            self._napcat_booting = False
            messagebox.showerror("启动 NapCat 失败", str(e))

    def _wait_napcat(self, n, on_ready=None):
        """轮询等 OneBot :3001 就绪（最多 90 秒）。"""
        if n >= 90:
            self._napcat_booting = False
            self.status("NapCat 未就绪：可能还在扫码/登录，完成后守护进程会自动接上")
            return
        if port_open():
            self._napcat_booting = False
            self.status("NapCat 已就绪（OneBot :3001）")
            if on_ready:
                on_ready()
            return
        self.status("等待 NapCat 就绪… %ds" % n)
        self.after(1000, lambda: self._wait_napcat(n + 1, on_ready))

    # ---------- 守护进程控制 ----------
    def start_daemon(self):
        # NapCat 没起时先拉 NapCat 再启动守护——否则必然 WinError 10061
        if not port_open():
            self.status("NapCat 未就绪，先启动 NapCat…")
            self.start_napcat(on_ready=self._launch_daemon)
            return
        self._launch_daemon()

    def _launch_daemon(self):
        try:
            subprocess.Popen(["cmd", "/c", "start_autoreply.bat"], cwd=BASE,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self.status("已发起启动，稍等几秒看状态")
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def stop_daemon(self):
        pid = read_pid()
        if not pid:
            messagebox.showinfo("停止", "没找到运行中的守护进程")
            return
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
            self.status("已停止 PID %d" % pid)
        except Exception as e:
            messagebox.showerror("停止失败", str(e))

    def restart_daemon(self):
        pid = read_pid()
        if pid and pid_alive(pid):
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15,
                               creationflags=NO_WINDOW)
            except Exception as e:
                messagebox.showerror("停止失败", str(e))
                return
            time.sleep(1.5)
            self.status("已停止 PID %d，正在重启…" % pid)
        self.start_daemon()

    def boot_all(self):
        try:
            subprocess.Popen([PYTHON, "boot_all.py"], cwd=BASE,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self.status("已发起一键启动，会弹出窗口")
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def open_log(self):
        try:
            os.startfile(LOG_PATH)
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    def open_archive(self):
        try:
            os.startfile(os.path.join(BASE, "qq_messages"))
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    def open_napcat_webui(self):
        import webbrowser
        webbrowser.open("http://localhost:6099/webui")

    # ---------- 定时刷新 ----------
    def tick(self):
        if not self._ticking:
            self._ticking = True
            threading.Thread(target=self._tick_worker, daemon=True).start()
        self.after(3000, self.tick)

    def _tick_worker(self):
        log_text, log_reset = "", False
        try:
            pid = read_pid()
            alive = pid_alive(pid)
            port = port_open()
            account = current_account()
            archive = self.count_archive()
            log_text, log_reset = self._read_log_tail()
            # NapCat 进程查询较重（powershell CIM），约每 9 秒查一次
            self._napcat_tick += 1
            if self._napcat_tick % 3 == 1 or self._napcat_cache is None:
                self._napcat_cache = self.napcat_running()
            napcat = self._napcat_cache
        except Exception:
            pid, alive, port, account, archive = 0, False, False, "—", "—"
            napcat = self._napcat_cache if self._napcat_cache is not None else False
        finally:
            self._ticking = False
        try:
            self.after(0, self._apply_status, pid, alive, port, account,
                       archive, log_text, log_reset, napcat)
        except Exception:
            pass

    def _apply_status(self, pid, alive, port, account, archive, log_text,
                      log_reset, napcat=True):
        try:
            self.st["daemon"].set(("运行中 PID %d" % pid) if alive else "未运行")
            self.st["napcat"].set("运行中" if napcat else "未运行")
            self.st["port"].set("已监听" if port else "未连接")
            self.st["account"].set(account)
            self.st["archive"].set(archive)
            self.st["model"].set(str(cget(self.cfg, "codebuddy.model", "—")))
            d = self.st_dots
            d.get("daemon").configure(fg=GREEN if alive else RED)
            d.get("napcat").configure(fg=GREEN if napcat else RED)
            d.get("port").configure(fg=GREEN if port else RED)
            self._set_pill("● 运行中 PID %d" % pid if alive else "● 已停止",
                           GREEN_SOFT if alive else RED_SOFT,
                           GREEN if alive else RED)
            self.side_dot.configure(fg=GREEN if alive else RED)
            self.ov_badge.configure(
                text="● 在线 · PID %d" % pid if alive else "● 离线 · 守护进程未运行",
                fg=GREEN if alive else RED)
            if self.auto_refresh.get():
                self._feed_log(log_text, log_reset)
        except Exception:
            pass

    def _set_pill(self, text, bg, fg):
        """顶栏状态徽章：PIL 圆角胶囊底 + 文字。"""
        try:
            tw = tk.font.Font(font=FONT_SM).measure(text)
            img = rrect_photo(int(tw) + 26, 24, 12, bg)
            self._pill_img = img
            self.pill.configure(image=img, text=text, compound="center",
                                fg=fg)
        except Exception:
            self.pill.configure(text=text, bg=bg, fg=fg)

    def _feed_log(self, text, reset):
        for box, keep in ((self.log_box, 400), (self.full_log, 1000)):
            if reset:
                box.delete("1.0", "end")
            if not text:
                continue
            at_bottom = box.yview()[1] >= 0.999
            box.insert("end", text + "\n")
            total = int(box.index("end-1c").split(".")[0])
            if total > keep:
                box.delete("1.0", "%d.0" % (total - keep))
            if at_bottom:
                box.see("end")

    def count_archive(self):
        d = os.path.join(BASE, "qq_messages")
        try:
            entries = [(f, os.path.getmtime(os.path.join(d, f)))
                       for f in os.listdir(d)]
        except Exception:
            return self._archive_cache
        sig = tuple(sorted(entries))
        if sig == self._archive_sig:
            return self._archive_cache
        n = 0
        try:
            for f, _ in entries:
                if f.endswith(".md") and not f.endswith(("_summary.md", "_stage.md",
                                                         "_profile.md", ".bak")):
                    n += sum(1 for l in open(os.path.join(d, f), encoding="utf-8",
                                             errors="replace") if l.startswith("["))
        except Exception:
            pass
        self._archive_sig = sig
        self._archive_cache = "%d 条" % n
        return self._archive_cache

    def _read_log_tail(self):
        try:
            size = os.path.getsize(LOG_PATH)
        except Exception:
            return "", True
        reset = False
        if self._log_pos > size:
            self._log_pos = 0
            reset = True
        if not self._log_init:
            start = max(0, size - 65536)
            with open(LOG_PATH, "rb") as f:
                f.seek(start)
                chunk = f.read()
            if start and b"\n" in chunk:
                chunk = chunk[chunk.find(b"\n") + 1:]
            tail = b""
            if not chunk.endswith(b"\n"):
                cut = chunk.rfind(b"\n")
                chunk, tail = ((chunk[:cut + 1], chunk[cut + 1:])
                               if cut >= 0 else (b"", chunk))
            self._log_pos = size - len(tail)
            self._log_init = True
            lines = [x for x in chunk.decode("utf-8", "replace").split("\n") if x]
            return ("\n".join(lines[-200:]), True)
        if size == self._log_pos:
            return "", reset
        with open(LOG_PATH, "rb") as f:
            f.seek(self._log_pos)
            chunk = f.read()
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return "", reset
        self._log_pos += cut + 1
        return chunk[:cut].decode("utf-8", "replace"), reset

    def on_close(self):
        self.destroy()


PAGE_SUB = {
    "overview": ("概览", "运行状态、快捷操作与日志预览"),
    "logs": ("实时日志", "守护进程输出 · 每 3 秒增量刷新"),
    "basic": ("基本配置", "回复开关、白名单与限流"),
    "model": ("模型与额度", "模型链、限流冷却与兜底回复"),
    "persona": ("人设风格", "主设定 · 大肥鱼（12 岁小女孩）"),
    "memory": ("记忆归档", "三层记忆与风格提炼"),
    "media": ("媒体与表情", "图片、语音、文件与表情包"),
    "raw": ("原始 JSON", "直接编辑配置文件"),
}


if __name__ == "__main__":
    Panel().mainloop()
