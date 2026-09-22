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
BG = "#EFF4F9"
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
        root.geometry("900x735")
        root.minsize(820, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.folder = tk.StringVar(value=str(APP_DIR / "Photos"))
        self.camera = tk.StringVar(value="0")
        self.interval = tk.StringVar(value="3")
        self.delay = tk.StringVar(value="15")
        self.limit = tk.StringVar(value="2")
        self.prevent_sleep = tk.BooleanVar(value=True)
        self.compatible = tk.BooleanVar(value=False)
        self.pet_mode = tk.StringVar(value="silent")
        self.load_settings()
        self.build_ui()
        root.update_idletasks()
        width = min(max(900, root.winfo_reqwidth() + 20), root.winfo_screenwidth() - 60)
        height = min(max(735, root.winfo_reqheight() + 20), root.winfo_screenheight() - 90)
        root.geometry(f"{width}x{height}")
        root.minsize(min(820, width), min(700, height))
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
        except (OSError, ValueError, TypeError):
            pass

    def save_settings(self):
        data = {key: getattr(self, key).get() for key in
                ("folder", "camera", "interval", "delay", "limit", "prevent_sleep", "compatible", "pet_mode")}
        try:
            self.settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.log("设置未保存：程序目录不可写。本次布防仍可使用。")

    def build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=(FONT, 10))
        style.configure("TButton", font=(FONT, 10), padding=(14, 9))
        style.configure("TEntry", padding=6, font=(FONT, 10))
        style.configure("TSpinbox", padding=6, font=(FONT, 10))
        style.configure("TCheckbutton", background=BG, font=(FONT, 10), foreground=INK)
        style.map("TCheckbutton", background=[("active", BG)])
        body = ttk.Frame(self.root, padding=(28, 22, 28, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        tk.Label(body, text="离席守护", bg=BG, fg=INK,
                 font=(FONT, 25, "bold"), anchor="w").grid(row=0, column=0, sticky="ew")
        ttk.Label(body, text="让任务继续运行，为电脑上的输入活动留下一张照片。",
                  foreground=MUTED).grid(row=1, column=0, sticky="w", pady=(3, 17))
        status = tk.Frame(body, bg=INK, padx=20, pady=16)
        status.grid(row=2, column=0, sticky="ew")
        self.status_label = tk.Label(status, text="● 未布防", bg=INK, fg="white",
                                     font=(FONT, 19, "bold"), anchor="w")
        self.status_label.pack(fill="x")
        self.detail_label = tk.Label(status, text="摄像头未启用。先测试拍照，再开始布防。", bg=INK,
                                     fg="#CBDDED", font=(FONT, 10), anchor="w", justify="left", wraplength=740)
        self.detail_label.pack(fill="x", pady=(5, 0))
        options = ttk.Frame(body)
        options.grid(row=3, column=0, sticky="ew", pady=(17, 10))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="照片位置").grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.folder_entry = ttk.Entry(options, textvariable=self.folder)
        self.folder_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(options, text="选择…", command=self.browse)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))
        numbers = ttk.Frame(options)
        numbers.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 5))
        self.inputs = [self.folder_entry, self.browse_button]
        for i, (name, var, lo, hi) in enumerate([
            ("摄像头编号", self.camera, 0, 10), ("最短间隔 / 秒", self.interval, 1, 3600),
            ("离开倒计时 / 秒", self.delay, 3, 300), ("照片上限 / GB", self.limit, 1, 100)]):
            numbers.columnconfigure(i, weight=1)
            box = ttk.Frame(numbers)
            box.grid(row=0, column=i, sticky="ew", padx=(0, 18 if i < 3 else 0))
            ttk.Label(box, text=name, foreground=MUTED).pack(anchor="w", pady=(0, 5))
            widget = ttk.Spinbox(box, from_=lo, to=hi, textvariable=var, width=10)
            widget.pack(fill="x")
            self.inputs.append(widget)
        self.awake_check = ttk.Checkbutton(options, text="布防期间保持电脑唤醒（允许屏幕熄灭，不修改系统设置）",
                                          variable=self.prevent_sleep)
        self.awake_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.compat_check = ttk.Checkbutton(options, text="兼容模式：检测会话活动，适合触屏测试；也可能被 Codex 的操作触发",
                                           variable=self.compatible)
        self.compat_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.inputs.extend([self.awake_check, self.compat_check])
        pet_options = ttk.Frame(options)
        pet_options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ttk.Label(pet_options, text="桌宠模式").pack(side="left", padx=(0, 12))
        self.pet_widgets = []
        for label, value in (("静默保护", "silent"), ("鹅鹅巡逻", "patrol")):
            widget = ttk.Radiobutton(pet_options, text=label, variable=self.pet_mode, value=value,
                                     command=self.change_pet_mode)
            widget.pack(side="left", padx=(0, 12))
            self.pet_widgets.append(widget)
        ttk.Label(pet_options, text="巡逻只沿屏幕边缘移动，不抢焦点、不拦截点击。",
                  foreground=MUTED).pack(side="left")
        self.inputs.extend(self.pet_widgets)
        self.hint = ttk.Label(body, text="Ctrl + Alt + F9  布防 / 停止     ·     Ctrl + Alt + F10  显示 / 隐藏窗口\n默认过滤系统标记的模拟输入；触屏支持及 Codex 误触发情况，请在本机试用确认。",
                              foreground=MUTED, wraplength=790)
        self.hint.grid(row=5, column=0, sticky="w", pady=(0, 12))
        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, sticky="ew")
        self.arm_button = tk.Button(actions, text="开始布防", command=self.arm, bg=BLUE, fg="white",
                                    activebackground=INK, activeforeground="white", relief="flat", bd=0,
                                    padx=26, pady=11, font=(FONT, 11, "bold"), cursor="hand2")
        self.arm_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=9)
        self.test_button = ttk.Button(actions, text="测试拍照", command=self.test_photo)
        self.test_button.pack(side="left")
        ttk.Button(actions, text="打开照片目录", command=self.open_folder).pack(side="right")
        stats = ttk.Frame(body)
        stats.grid(row=7, column=0, sticky="ew", pady=(16, 6))
        self.stats_label = ttk.Label(stats, text="本次已保存 0 张 · 尚无照片", foreground=TEAL)
        self.stats_label.pack(side="left")
        self.view_button = ttk.Button(stats, text="查看最近照片", command=self.view_latest, state="disabled")
        self.view_button.pack(side="right")
        self.log_box = tk.Text(body, height=4, relief="flat", bg="white", fg=INK, font=(FONT, 9),
                               padx=12, pady=8, state="disabled", wrap="word")
        self.log_box.grid(row=8, column=0, sticky="nsew")
        body.rowconfigure(8, weight=1)
        ttk.Label(body, text="布防时保持相机会话，触发后请求拍照；无声音、无逐次弹窗。照片保存在所选目录。\n"
                  "锁屏 / 安全桌面会暂停拍摄。此工具用于记录，不能阻止他人操作或保护未锁定的桌面。",
                  foreground=MUTED, wraplength=790, font=(FONT, 9)).grid(row=9, column=0, sticky="w", pady=(12, 0))
        self.log("就绪。程序启动不会自动开启摄像头，也不会自动布防。")

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
        self.arm_button.configure(state="disabled" if enabled else "normal")
        self.test_button.configure(state="disabled" if enabled else "normal")
        self.stop_button.configure(state="normal" if enabled else "disabled")

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
