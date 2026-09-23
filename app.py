"""DeskGuard / 离席守护. Visible, manually armed, local-only Windows utility."""
import ctypes
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
from queue import Empty, Full, Queue
import sys
import subprocess
import tempfile
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from camera_worker import run_camera, decode_photo, HOST_PATH
from desktop_pet import DesktopPet
from guard_core import TriggerGate, check_storage, save_jpeg, validate_output_dir
from windows_input import InputMonitor, is_interactive_desktop, keep_awake, read_last_input_tick
from windows_tray import DesktopControls

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
if APP_DIR.name == "_app":
    APP_DIR = APP_DIR.parent
ICON_PATH = APP_DIR / "assets" / "deskguard.ico"
BG = "#EEF3F8"
WHITE = "#FFFFFF"
ORANGE = "#F5A34A"
INK = "#142D4E"
MUTED = "#53677C"
BLUE = "#235F9A"
TEAL = "#35738D"
AMBER = "#936611"
RED = "#B03737"
FONT = "Microsoft YaHei UI"


class GuardApp:
    def __init__(self, root, settings_file=None, poll=True):
        self.root = root
        self.settings_file = settings_file or APP_DIR / "settings.json"
        self.state = "idle"
        self.process = None
        self.monitor = None
        self.controls = None
        self.control_events = Queue()
        self.awake = False
        self.count = 0
        self.latest = None
        self.config = {}
        self.shot_pending = False
        self.deadline = 0.0
        self.pause_seen = False
        self.closing = False
        self.pet = DesktopPet(root, APP_DIR / "assets" / "deskguard-pet.png")
        root.title("离席守护 · 未布防")
        try:
            root.iconbitmap(default=str(ICON_PATH))
        except (tk.TclError, OSError):
            pass  # Keep Tk's default icon if optional artwork is unavailable.
        root.configure(bg=BG)
        root.geometry("820x760")
        root.minsize(760, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.folder = tk.StringVar(value=str(APP_DIR / "Photos"))
        self.camera = tk.StringVar(value="0")
        self.interval = tk.StringVar(value="3")
        self.delay = tk.StringVar(value="15")
        self.limit = tk.StringVar(value="2")
        self.prevent_sleep = tk.BooleanVar(value=True)
        self.compatible = tk.BooleanVar(value=False)
        self.pet_mode = tk.StringVar(value="silent")
        self.pet_size = tk.StringVar(value="large")
        self.pet_speed = tk.StringVar(value="normal")
        self.pet_gentle = tk.BooleanVar(value=False)
        self.pet_preview_after = None
        self.load_settings()
        self.build_ui()
        root.update_idletasks()
        width = min(max(820, root.winfo_reqwidth() + 20), root.winfo_screenwidth() - 60)
        height = min(max(760, root.winfo_reqheight() + 20), root.winfo_screenheight() - 90)
        root.geometry(f"{width}x{height}")
        root.minsize(min(760, width), min(700, height))
        if poll:
            self.controls = DesktopControls(self.control_events.put, icon_path=ICON_PATH)
            if not self.controls.start():
                self.log("托盘控制不可用，请使用主窗口操作。" + (self.controls.error or ""))
                self.controls.stop()
                self.controls = None
            elif not self.controls.hotkeys_available:
                self.log("部分快捷键被其他程序占用，请使用托盘菜单。" + (self.controls.error or ""))
            root.after(100, self.tick)

    def load_settings(self):
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for key in ("folder", "camera", "interval", "delay", "limit"):
                if key in data:
                    getattr(self, key).set(str(data[key]))
            for key in ("prevent_sleep", "compatible"):
                if isinstance(data.get(key), bool):
                    getattr(self, key).set(data[key])
            if data.get("pet_mode") in ("silent", "patrol"):
                self.pet_mode.set(data["pet_mode"])
            for key, choices in (("pet_size", ("small", "medium", "large")),
                                 ("pet_speed", ("slow", "normal", "brisk"))):
                if data.get(key) in choices:
                    getattr(self, key).set(data[key])
            if isinstance(data.get("pet_gentle"), bool):
                self.pet_gentle.set(data["pet_gentle"])
        except (OSError, ValueError, TypeError):
            pass

    def save_settings(self):
        data = {key: getattr(self, key).get() for key in
                ("folder", "camera", "interval", "delay", "limit", "prevent_sleep", "compatible", "pet_mode", "pet_size", "pet_speed", "pet_gentle")}
        try:
            self.settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.log("设置未保存：程序目录不可写。本次布防仍可使用。")

    def build_ui(self):
        # A calm duty desk: navy status, white working surface, goose-beak action.
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=(FONT, 10))
        style.configure("TButton", font=(FONT, 10), padding=(12, 7), background=WHITE,
                        foreground=INK, bordercolor="#CCD7E2", focuscolor=BLUE)
        style.map("TButton", background=[("active", "#E3ECF5")])
        style.configure("Stop.TButton", background="#FFD8D2", foreground="#8B2626")
        style.map("Stop.TButton", background=[("disabled", WHITE), ("active", "#FFC2BB")],
                  foreground=[("disabled", "#9099A4")])
        style.configure("TEntry", padding=5, font=(FONT, 10))
        style.configure("TSpinbox", padding=5, font=(FONT, 10))
        style.configure("Paper.TFrame", background=WHITE)
        style.configure("Paper.TLabel", background=WHITE, foreground=INK, font=(FONT, 10))
        for kind in ("TCheckbutton", "TRadiobutton"):
            style.configure(kind, background=WHITE, font=(FONT, 10), foreground=INK)
            style.map(kind, background=[("active", "#EDF3F9")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=(FONT, 10), padding=(16, 7), background=BG)
        style.map("TNotebook.Tab", background=[("selected", WHITE)], foreground=[("selected", BLUE)])
        body = ttk.Frame(self.root, padding=(22, 12, 22, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        heading = ttk.Frame(body)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        try:
            artwork = tk.PhotoImage(file=str(APP_DIR / "assets" / "deskguard-pet.png"))
            mark_size = max(44, round(float(self.root.tk.call("tk", "scaling")) * 24))
            self.brand_image = artwork.subsample(max(1, math.ceil(artwork.width() / mark_size)),
                                                max(1, math.ceil(artwork.height() / mark_size)))
            tk.Label(heading, image=self.brand_image, bg=BG).pack(side="left", padx=(0, 10))
        except (tk.TclError, OSError):
            pass
        tk.Label(heading, text="保安鹅值班台", bg=BG, fg=INK,
                 font=(FONT, 21, "bold")).pack(side="left")
        tk.Label(heading, text="DESKGUARD  /  离席守护", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="right", anchor="s", pady=7)

        status = tk.Frame(body, bg=INK, padx=20, pady=12)
        status.grid(row=1, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_label = tk.Label(status, text="● 未布防 · 鹅鹅待命", bg=INK, fg=WHITE,
                                     font=(FONT, 18, "bold"), anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.detail_label = tk.Label(status, text="摄像头未启用。首次使用，先测试拍照确认取景。", bg=INK,
                                     fg="#D0DEEC", font=(FONT, 10), anchor="w", justify="left", wraplength=660)
        self.detail_label.grid(row=1, column=0, sticky="ew", pady=(6, 13))
        actions = tk.Frame(status, bg=INK)
        actions.grid(row=2, column=0, sticky="ew")
        self.arm_button = tk.Button(actions, text="开始布防 →", command=self.arm, bg=ORANGE, fg=INK,
                                    activebackground="#FFC277", activeforeground=INK, relief="flat", bd=0,
                                    padx=22, pady=7, font=(FONT, 11, "bold"), cursor="hand2")
        self.arm_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止守护", command=self.stop, state="disabled", style="Stop.TButton")
        self.stop_button.pack(side="left", padx=9)
        self.test_button = ttk.Button(actions, text="测试拍照", command=self.test_photo)
        self.test_button.pack(side="right")

        modes = tk.Frame(body, bg=WHITE, padx=14, pady=8)
        modes.grid(row=2, column=0, sticky="ew", pady=(12, 10))
        self.pet_widgets = []
        for col, (label, value, caption) in enumerate((
                ("静默保护", "silent", "只在托盘值班，桌面不显示鹅"),
                ("鹅鹅巡逻", "patrol", "守护时沿屏幕底部走动，不挡点击"))):
            modes.columnconfigure(col, weight=1)
            card = tk.Frame(modes, bg=WHITE)
            card.grid(row=0, column=col, sticky="ew", padx=(0, 12))
            radio = ttk.Radiobutton(card, text=label, variable=self.pet_mode, value=value,
                                    command=self.change_pet_mode)
            radio.pack(anchor="w")
            tk.Label(card, text=caption, bg=WHITE, fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=23, pady=(3, 0))
            self.pet_widgets.append(radio)

        notebook = ttk.Notebook(body)
        notebook.grid(row=3, column=0, sticky="ew")
        options = ttk.Frame(notebook, style="Paper.TFrame", padding=(14, 12))
        appearance = ttk.Frame(notebook, style="Paper.TFrame", padding=(14, 12))
        notebook.add(options, text="守护设置")
        notebook.add(appearance, text="巡逻外观")
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="照片位置", style="Paper.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.folder_entry = ttk.Entry(options, textvariable=self.folder)
        self.folder_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(options, text="选择…", command=self.browse)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))
        self.inputs = [self.folder_entry, self.browse_button]
        numbers = ttk.Frame(options, style="Paper.TFrame")
        numbers.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        for i, (name, var, lo, hi) in enumerate([
            ("摄像头编号", self.camera, 0, 10), ("拍照间隔 / 秒", self.interval, 1, 3600),
            ("离开倒计时 / 秒", self.delay, 3, 300), ("照片上限 / GB", self.limit, 1, 100)]):
            numbers.columnconfigure(i, weight=1)
            box = ttk.Frame(numbers, style="Paper.TFrame")
            box.grid(row=0, column=i, sticky="ew", padx=(0, 12 if i < 3 else 0))
            ttk.Label(box, text=name, style="Paper.TLabel", foreground=MUTED).pack(anchor="w", pady=(0, 4))
            widget = ttk.Spinbox(box, from_=lo, to=hi, textvariable=var, width=8)
            widget.pack(fill="x")
            self.inputs.append(widget)
        self.awake_check = ttk.Checkbutton(options, text="守护时保持电脑唤醒，让 Codex 继续运行（允许屏幕熄灭）",
                                          variable=self.prevent_sleep)
        self.awake_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(3, 0))
        self.compat_check = ttk.Checkbutton(options, text="兼容触屏输入（也可能记录 Codex 的操作，建议先测试）",
                                           variable=self.compatible)
        self.compat_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.inputs.extend([self.awake_check, self.compat_check])
        # Mode remains changeable from the tray while armed; match that in the panel.
        for row, (label, variable, choices) in enumerate((
                ("鹅鹅大小", self.pet_size, (("小巧", "small"), ("适中", "medium"), ("大鹅", "large"))),
                ("巡逻速度", self.pet_speed, (("慢悠悠", "slow"), ("日常", "normal"), ("小快步", "brisk"))))):
            ttk.Label(appearance, text=label, style="Paper.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 22), pady=7)
            for col, (name, value) in enumerate(choices, 1):
                ttk.Radiobutton(appearance, text=name, variable=variable, value=value,
                                command=self.change_pet_mode).grid(row=row, column=col, sticky="w", padx=(0, 25))
        ttk.Checkbutton(appearance, text="平稳巡逻 · 减少上下起伏和转身晃动", variable=self.pet_gentle,
                        command=self.change_pet_mode).grid(row=2, column=0, columnspan=4, sticky="w", pady=8)
        self.preview_button = ttk.Button(appearance, text="预览巡逻 · 6 秒", command=self.preview_pet)
        self.preview_button.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(appearance, text="不启用摄像头；系统关闭动画时静止。", style="Paper.TLabel", foreground=MUTED).grid(
            row=3, column=2, columnspan=2, sticky="w")

        stats = ttk.Frame(body)
        stats.grid(row=4, column=0, sticky="ew", pady=(8, 6))
        self.stats_label = ttk.Label(stats, text="本次已保存 0 张 · 尚无照片", foreground=TEAL)
        self.stats_label.pack(side="left")
        ttk.Button(stats, text="照片目录", command=self.open_folder).pack(side="right")
        self.view_button = ttk.Button(stats, text="查看最近照片", command=self.view_latest, state="disabled")
        self.view_button.pack(side="right", padx=7)
        self.log_box = tk.Text(body, height=2, relief="flat", bg=WHITE, fg=MUTED, font=(FONT, 9),
                               padx=12, pady=8, state="disabled", wrap="word")
        self.log_box.grid(row=5, column=0, sticky="nsew")
        body.rowconfigure(5, weight=1)
        self.hint = ttk.Label(body, text="Ctrl + Alt + F9  布防 / 停止    ·    Ctrl + Alt + F10  显示 / 隐藏窗口",
                              foreground=INK, font=(FONT, 9))
        self.hint.grid(row=6, column=0, sticky="w", pady=(10, 4))
        ttk.Label(body, text="布防后窗口自动收起至托盘。照片仅存本机，无声音、无拍照弹窗。\n"
                  "仅用于自己的电脑。锁屏暂停拍摄；不能阻止他人操作。",
                  foreground=MUTED, wraplength=740, font=(FONT, 9)).grid(row=7, column=0, sticky="w")
        self.log("就绪。启动不会开启摄像头；默认过滤系统标记的模拟输入。")

    def cancel_pet_preview(self):
        pending = getattr(self, "pet_preview_after", None)
        if pending is not None:
            self.root.after_cancel(pending)
            self.pet_preview_after = None

    def preview_pet(self):
        if self.state != "idle":
            return
        self.cancel_pet_preview()
        try:
            self.pet.configure(size=self.pet_size.get(), speed=self.pet_speed.get(), gentle=self.pet_gentle.get())
            self.pet.set_visible(True)
            self.pet_preview_after = self.root.after(6000, self.finish_pet_preview)
        except (OSError, tk.TclError, RuntimeError) as exc:
            self.log("桌宠预览未能显示：" + str(exc))
            self.sync_desktop_pet()

    def finish_pet_preview(self):
        self.pet_preview_after = None
        self.sync_desktop_pet()

    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", time.strftime("%H:%M:%S") + "  " + str(text) + "\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 100:
            self.log_box.delete("1.0", "2.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_status(self, title, detail, error=False):
        self.status_label.configure(text="● " + title, fg="#FFC2BB" if error else "white")
        self.detail_label.configure(text=detail)
        self.root.title("离席守护 · " + title)
        self.sync_desktop_pet()
        if self.controls:
            self.controls.set_status("离席守护 · " + title, self.state != "idle",
                                     self.pet_mode.get() == "patrol")

    def pet_should_patrol(self):
        return (self.pet_mode.get() == "patrol" and not getattr(self, "testing", False)
                and self.state in ("opening", "countdown", "armed"))

    def sync_desktop_pet(self):
        try:
            self.pet.configure(size=self.pet_size.get(), speed=self.pet_speed.get(), gentle=self.pet_gentle.get())
            self.pet.set_visible(self.pet_should_patrol())
        except (OSError, tk.TclError, RuntimeError) as exc:
            self.pet_mode.set("silent")
            try:
                self.pet.set_visible(False)
            except (OSError, tk.TclError, RuntimeError):
                self.pet.close()
            try:
                self.save_settings()
            except Exception:
                pass
            if getattr(self, "controls", None):
                self.controls.set_status("离席守护 · 桌宠不可用，静默保护", self.state != "idle", False)
            if hasattr(self, "log_box"):
                self.log("桌宠未能显示，已改用静默模式：" + str(exc))

    def change_pet_mode(self):
        self.cancel_pet_preview()
        if self.pet_mode.get() not in ("silent", "patrol"):
            self.pet_mode.set("silent")
        self.sync_desktop_pet()
        self.save_settings()
        if self.controls:
            self.controls.set_status("离席守护 · " + self.root.title().split(" · ", 1)[-1],
                                     self.state != "idle", self.pet_mode.get() == "patrol")

    def busy(self, enabled):
        for widget in self.inputs:
            widget.configure(state="disabled" if enabled else "normal")
        self.arm_button.configure(state="disabled" if enabled else "normal",
                                  bg="#38506C" if enabled else ORANGE, disabledforeground="#AABBD0")
        self.test_button.configure(state="disabled" if enabled else "normal")
        self.stop_button.configure(state="normal" if enabled else "disabled")
        self.preview_button.configure(state="disabled" if enabled else "normal")

    def read_config(self):
        values = {}
        for key, minimum, maximum in (("camera", 0, 10), ("interval", 1, 3600), ("delay", 3, 300), ("limit", 1, 100)):
            try:
                values[key] = int(getattr(self, key).get())
            except ValueError:
                raise ValueError("请使用整数填写摄像头编号、拍照间隔、倒计时和容量。") from None
            if not minimum <= values[key] <= maximum:
                raise ValueError(f"{ {'camera':'摄像头编号', 'interval':'拍照间隔', 'delay':'倒计时', 'limit':'照片容量'}[key] }需在 {minimum}–{maximum} 之间。")
        raw = self.folder.get().strip()
        if not raw:
            raise ValueError("请选择照片保存目录。")
        values["folder"] = validate_output_dir(Path(raw).expanduser())
        values["max_bytes"] = values["limit"] * 1024**3
        check_storage(values["folder"], values["max_bytes"], 1024**2)
        values["compatible"] = self.compatible.get()
        values["awake"] = self.prevent_sleep.get()
        values["pet_mode"] = self.pet_mode.get()
        return values

    def browse(self):
        folder = filedialog.askdirectory(title="选择照片保存目录", parent=self.root)
        if folder:
            self.folder.set(folder)

    def open_folder(self):
        try:
            folder = validate_output_dir(Path(self.folder.get()).expanduser())
            os.startfile(str(folder))
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def view_latest(self):
        if self.latest:
            try:
                os.startfile(str(self.latest))
            except OSError as exc:
                messagebox.showerror("无法打开照片", str(exc), parent=self.root)

    def toggle(self):
        if self.state == "idle":
            self.arm()
        else:
            self.stop()

    def toggle_window(self):
        if self.root.state() in ("withdrawn", "iconic"):
            self.show_window()
        elif self.controls:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()

    def arm(self):
        self.begin(test=False)

    def test_photo(self):
        self.begin(test=True)

    def begin(self, test):
        if self.state != "idle":
            return
        self.cancel_pet_preview()
        try:
            self.config = self.read_config()
            if not is_interactive_desktop():
                raise RuntimeError("请解锁 Windows 并返回普通桌面后再启动。")
            self.save_settings()
            self.testing = test
            if not test:
                self.count = 0
                self.stats_label.configure(text="本次已保存 0 张 · 等待新的照片")
                self.gate = TriggerGate(self.config["interval"])
                if not self.config["compatible"]:
                    self.monitor = InputMonitor(ignore_injected=True)
                    if not self.monitor.start():
                        raise RuntimeError(self.monitor.error or "无法启用输入检测。")
                if self.config["awake"]:
                    if not keep_awake(True):
                        raise RuntimeError("无法保持电脑唤醒。请检查系统电源策略。")
                    self.awake = True
            self.busy(True)
            self.start_camera()
        except Exception as exc:
            self.fail(str(exc))

    def start_camera(self):
        self.commands = mp.Queue(maxsize=1)
        self.events = mp.Queue(maxsize=16)
        self.camera_stop = mp.Event()
        self.heartbeat = mp.Value("d", time.monotonic())
        self.process = mp.Process(target=run_camera, args=(self.config["camera"], str(self.config["folder"]),
            self.config["max_bytes"], self.commands, self.events, self.camera_stop, self.heartbeat), daemon=True)
        try:
            self.process.start()
        except Exception:
            self.process.close()
            self.process = None
            for q in (self.commands, self.events):
                q.cancel_join_thread()
                q.close()
            raise
        self.opened_at = time.monotonic()
        self.state = "opening"
        self.set_status("摄像头正在启用", "正在连接摄像头；初始化完成前不会进入布防状态。")
        self.log("正在启用摄像头。指示灯由系统和摄像头硬件控制。")

    def input_sequence(self):
        if self.config["compatible"]:
            return read_last_input_tick()
        if self.monitor.error:
            raise RuntimeError(self.monitor.error)
        return self.monitor.snapshot()

    def request_photo(self, reason):
        if self.shot_pending:
            return
        try:
            self.commands.put_nowait(reason)
            self.shot_pending = True
        except Full:
            pass

    def tick(self):
        try:
            now = time.monotonic()
            if self.controls and not self.controls.running:
                self.log("托盘控制已停止，请使用主窗口操作。" + self.controls.error)
                self.controls.stop()
                self.controls = None
                self.root.deiconify()
            for _ in range(20):
                try:
                    action = self.control_events.get_nowait()
                except Empty:
                    break
                if action == "toggle":
                    self.toggle()
                elif action == "show":
                    self.show_window()
                elif action == "toggle_window":
                    self.toggle_window()
                elif action == "toggle_pet_mode":
                    self.pet_mode.set("silent" if self.pet_mode.get() == "patrol" else "patrol")
                    self.change_pet_mode()
                elif action == "quit":
                    self.close()
                    return
            if self.state in ("opening", "countdown", "armed", "testing"):
                if not is_interactive_desktop():
                    cleanup_errors = self.stop_camera()
                    if cleanup_errors:
                        raise RuntimeError("；".join(cleanup_errors))
                    if self.testing:
                        self.stop()
                    else:
                        self.state = "paused"
                        self.set_status("锁屏期间暂停", "已释放摄像头。返回桌面后将重新连接并倒计时。")
                        self.log("已切换到锁屏或安全桌面，暂停拍摄。")
                else:
                    self.read_events(now)
                    if self.process and self.state not in ("idle", "paused"):
                        if not self.process.is_alive():
                            self.fail("摄像头进程意外退出，请重新布防。")
                        elif now - self.heartbeat.value > 20:
                            self.fail("摄像头响应超时。请检查设备、权限或占用情况后重试。")
            elif self.state == "paused" and is_interactive_desktop():
                self.log("已返回普通桌面，重新连接摄像头。")
                if self.monitor:
                    self.monitor.stop()
                    self.monitor = InputMonitor(ignore_injected=True)
                    if not self.monitor.start():
                        raise RuntimeError(self.monitor.error or "无法重新启用输入检测。")
                self.start_camera()
            if self.state == "countdown":
                remaining = math.ceil(self.deadline - now)
                if remaining > 0:
                    self.set_status(f"{remaining} 秒后布防", "摄像头已启用。请离开电脑，倒计时结束后才会响应输入。")
                else:
                    self.gate.reset(self.input_sequence(), now)
                    self.state = "armed"
                    self.set_status("已布防 · 摄像头已启用", "检测到输入活动时保存照片；最短间隔 " + str(self.config["interval"]) + " 秒。")
                    self.log("已布防。" + ("兼容模式会包含模拟输入。" if self.config["compatible"] else "已过滤系统标记的模拟输入。"))
                    if self.controls:
                        self.root.withdraw()
            elif self.state == "armed":
                sequence = self.input_sequence()
                if not self.shot_pending and self.gate.due(sequence, now):
                    self.request_photo("session_activity" if self.config["compatible"] else "input_activity")
        except Exception as exc:
            self.fail(str(exc))
        finally:
            if not self.closing:
                self.root.after(100, self.tick)

    def read_events(self, now):
        # Stop() may close the queue while processing an event, so break after it.
        while self.process is not None:
            try:
                event = self.events.get_nowait()
            except Empty:
                break
            if event[0] == "ready":
                if event[1] and event[2]:
                    self.log(f"摄像头就绪，画面 {event[1]} × {event[2]}。")
                else:
                    self.log("摄像头已连接，画面尺寸将在拍照时由设备确定。")
                if self.testing:
                    self.state = "testing"
                    self.set_status("正在测试拍照", "正在保存一张测试照片，完成后会关闭摄像头。")
                    self.request_photo("manual_test")
                else:
                    self.state = "countdown"
                    self.deadline = now + self.config["delay"]
            elif event[0] == "saved":
                self.shot_pending = False
                self.latest = Path(event[1])
                self.count += 1
                self.stats_label.configure(text=f"本次已保存 {self.count} 张 · 最近 {time.strftime('%H:%M:%S')}")
                self.view_button.configure(state="normal")
                self.log("已保存 " + self.latest.name)
                if self.testing:
                    self.stop()
                    self.set_status("测试照片已保存", "点击“查看最近照片”检查取景。确认后再开始布防。")
                    break
            elif event[0] == "error":
                self.fail("相机或保存失败：" + event[1])
                break

    def stop_camera(self):
        errors = []
        if self.process is not None:
            try:
                self.camera_stop.set()
                if self.process.pid is not None:
                    self.process.join(timeout=0.5)
                    if self.process.is_alive():
                        self.process.terminate()
                        self.process.join(timeout=1.5)
                    if self.process.is_alive():
                        self.process.kill()
                        self.process.join(timeout=0.5)
                if self.process.is_alive():
                    errors.append("摄像头进程尚未退出，请再次停止或退出程序。")
                else:
                    self.process.close()
                    self.process = None
            except Exception as exc:
                errors.append("停止摄像头时出错：" + str(exc))
            if self.process is None:
                for q in (self.commands, self.events):
                    try:
                        q.cancel_join_thread()
                        q.close()
                    except Exception as exc:
                        errors.append("关闭照片队列时出错：" + str(exc))
        self.shot_pending = False
        return errors

    def stop(self):
        errors = self.stop_camera()
        if self.monitor is not None:
            try:
                self.monitor.stop()
            except Exception as exc:
                errors.append("停止输入检测时出错：" + str(exc))
            finally:
                self.monitor = None
        if self.awake:
            try:
                keep_awake(False)
            except Exception as exc:
                errors.append("恢复电源状态时出错：" + str(exc))
            else:
                self.awake = False
        self.state = "idle" if self.process is None else "stopping"
        self.busy(self.state != "idle")
        if errors:
            self.set_status("停止时需要检查", "；".join(errors), error=True)
            for error in errors:
                self.log(error)
            self.root.deiconify()
        else:
            self.set_status("已停止", "摄像头已关闭，输入检测已停止。")
            self.log("已停止。布防期间的保持唤醒请求已释放。")
        return errors

    def fail(self, message):
        cleanup_errors = self.stop()
        title = "需要检查 · 已停止" if self.state == "idle" else "需要检查 · 正在停止"
        self.set_status(title, message + ("；" + "；".join(cleanup_errors) if cleanup_errors else ""), error=True)
        self.log("错误：" + message)
        self.root.deiconify()
        # A persistent status replaces disruptive recurring popups.

    def close(self):
        self.cancel_pet_preview()
        self.closing = True
        self.stop()
        if self.controls:
            self.controls.stop()
            self.controls = None
        self.pet.close()
        self.root.destroy()


def self_test(destination):
    """Offline smoke test, uses synthetic pixels; never opens camera or hooks."""
    result = {"camera_opened": False, "input_monitor_started": False}
    with tempfile.TemporaryDirectory(prefix="deskguard-test-") as folder:
        output = validate_output_dir(Path(folder) / "照片测试")
        run = subprocess.run([str(HOST_PATH), "--self-test"], capture_output=True, text=True,
                             encoding="utf-8", timeout=20, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        line = next(line for line in run.stdout.splitlines() if line.startswith("PHOTO "))
        jpeg = decode_photo(line)
        photo = save_jpeg(output, jpeg, "synthetic_self_test")
        assert photo.read_bytes() == jpeg
        result["native_jpeg_save"] = True
        root = tk.Tk()
        root.withdraw()
        app = GuardApp(root, Path(folder) / "settings.json", poll=False)
        root.update_idletasks()
        assert app.state == "idle" and app.process is None and app.monitor is None
        result["ui_constructed"] = True
        result["requested_size"] = [root.winfo_reqwidth(), root.winfo_reqheight()]
        result["native_desktop_query"] = isinstance(is_interactive_desktop(), bool)
        root.destroy()
    result["passed"] = True
    Path(destination).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        self_test(sys.argv[2])
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    mutex = kernel.CreateMutexW(None, False, "Local\\DeskGuard_Visible_1")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    already_running = ctypes.get_last_error() == 183
    root = tk.Tk()
    try:
        if already_running:
            root.withdraw()
            messagebox.showinfo("离席守护已在运行", "请按 Ctrl + Alt + F10 显示窗口，或在系统托盘中找到离席守护图标。", parent=root)
            root.destroy()
            return
        GuardApp(root)
        root.mainloop()
    finally:
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    mp.freeze_support()
    try:
        main()
    except Exception as exc:
        import traceback
        if "--self-test" in sys.argv:
            raise
        try:
            (APP_DIR / "startup-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(None, "离席守护启动失败。请完整解压程序文件夹后重试。\n\n" + str(exc),
                                         "离席守护", 0x10)
        raise SystemExit(1)
