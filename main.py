import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
import psutil
from quickcontrol.power import (
    LegacyPowerStateRecoveryRequired,
    PowerModeController,
    PowerModeError,
)
from quickcontrol.updater import (
    check_for_update,
    download_and_verify_installer,
    launch_installer,
    should_check_on_startup,
)
from quickcontrol.version import APP_VERSION

APP_NAME = "Quick Control Center"
ROOT = Path(__file__).resolve().parent
BUNDLE_CONFIG_PATH = ROOT / "quickcontrol" / "config.json"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "QuickControlCenter"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = STATE_DIR / "config.json"
if not CONFIG_PATH.exists() and BUNDLE_CONFIG_PATH.exists():
    try:
        shutil.copy2(BUNDLE_CONFIG_PATH, CONFIG_PATH)
    except OSError:
        CONFIG_PATH = BUNDLE_CONFIG_PATH
POWER_STATE_PATH = STATE_DIR / "power_state.json"
REFRESH_MS = 2000


def format_bytes(value):
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(value)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024


def run_hidden(command, timeout=8):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout,
        creationflags=flags, shell=False
    )


class QuickControlCenter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("790x610")
        self.minsize(720, 560)
        self.configure(bg="#111827")
        self._closing = False
        self.power_controller = PowerModeController(POWER_STATE_PATH)
        self._last_network_report = None
        self._build_style()
        self._build_ui()
        psutil.cpu_percent(interval=None)
        self.after(500, self.refresh_metrics)
        if should_check_on_startup():
            self.after(1800, self.check_updates_silently)


    def check_updates_silently(self):
        if self._closing:
            return
        threading.Thread(target=self._update_check_worker, args=(True,), daemon=True).start()

    def check_updates_manually(self):
        self.status_var.set("Checking for updates…")
        threading.Thread(target=self._update_check_worker, args=(False,), daemon=True).start()

    def _update_check_worker(self, silent):
        try:
            manifest = check_for_update()
            installer_path = None
            if manifest is not None:
                installer_path = download_and_verify_installer(manifest)
            self.after(
                0,
                lambda: self._handle_update_result(manifest, installer_path, silent),
            )
        except Exception as exc:
            self.after(0, lambda error=str(exc): self._update_check_failed(error, silent))

    def _handle_update_result(self, manifest, installer_path, silent):
        if self._closing:
            return
        if manifest is None:
            self.status_var.set("Ready")
            if not silent:
                self.show_toast("Updates are not configured yet, or you're already on the latest version.", 4500)
            return
        version = manifest["version"]
        if messagebox.askyesno(APP_NAME, f"Quick Control Center {version} is available.\n\nRelease notes:\n{manifest.get('release_notes', 'Bug fixes and improvements')}\n\nThe installer has been downloaded and verified. Install now?"):
            try:
                launch_installer(manifest, installer_path)
                self.show_toast("The verified installer will open next.", 3500)
                self.after(800, self.on_close)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Update could not be installed safely:\n{exc}")
        else:
            self.status_var.set(f"Update {version} available")

    def _update_check_failed(self, error, silent):
        if self._closing:
            return
        self.status_var.set("Ready")
        if not silent:
            self.show_toast(f"Update check failed: {error}", 5000)

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background="#111827", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 8), background="#1f2937",
                        foreground="#e5e7eb")
        style.map("TNotebook.Tab", background=[("selected", "#374151")])
        style.configure("TProgressbar", troughcolor="#273244", background="#60a5fa",
                        bordercolor="#273244", lightcolor="#60a5fa", darkcolor="#60a5fa")
        style.configure("Treeview", background="#1f2937", fieldbackground="#1f2937",
                        foreground="#f9fafb", rowheight=28, borderwidth=0)
        style.configure("Treeview.Heading", background="#374151", foreground="#f9fafb")

    def _build_ui(self):
        header = tk.Frame(self, bg="#111827")
        header.pack(fill="x", padx=22, pady=(18, 10))
        tk.Label(header, text="Quick Control Center", font=("Segoe UI", 20, "bold"),
                 fg="#f9fafb", bg="#111827").pack(anchor="w")
        tk.Label(header, text=f"Lightweight Windows utilities · v{APP_VERSION}",
                 font=("Segoe UI", 10), fg="#9ca3af", bg="#111827").pack(anchor="w", pady=(3, 0))

        self.status_var = tk.StringVar(value="Starting monitor…")
        header_actions = tk.Frame(header, bg="#111827")
        header_actions.pack(anchor="e")
        tk.Label(header_actions, textvariable=self.status_var, font=("Segoe UI", 9),
                 fg="#93c5fd", bg="#111827").pack(side="left", padx=(0, 10))
        self._button(header_actions, "Check for updates", self.check_updates_manually, "#374151").pack(side="left")

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.health_tab = tk.Frame(self.tabs, bg="#111827")
        self.focus_tab = tk.Frame(self.tabs, bg="#111827")
        self.dev_tab = tk.Frame(self.tabs, bg="#111827")
        self.network_tab = tk.Frame(self.tabs, bg="#111827")
        self.storage_tab = tk.Frame(self.tabs, bg="#111827")
        self.tabs.add(self.health_tab, text="System Health")
        self.tabs.add(self.focus_tab, text="Focus & Power")
        self.tabs.add(self.dev_tab, text="Developer")
        self.tabs.add(self.network_tab, text="Network")
        self.tabs.add(self.storage_tab, text="Storage")
        self._build_health()
        self._build_focus()
        self._build_developer()
        self._build_network()
        self._build_storage()

    def _card(self, parent, title, subtitle=""):
        card = tk.Frame(parent, bg="#1f2937", highlightbackground="#374151",
                        highlightthickness=1, padx=15, pady=13)
        tk.Label(card, text=title, font=("Segoe UI", 10), fg="#9ca3af",
                 bg="#1f2937").pack(anchor="w")
        value = tk.Label(card, text="—", font=("Segoe UI", 22, "bold"),
                         fg="#f9fafb", bg="#1f2937")
        value.pack(anchor="w", pady=(6, 2))
        detail = tk.Label(card, text=subtitle, font=("Segoe UI", 9),
                          fg="#9ca3af", bg="#1f2937", wraplength=210, justify="left")
        detail.pack(anchor="w")
        return card, value, detail

    def _build_health(self):
        wrap = tk.Frame(self.health_tab, bg="#111827")
        wrap.pack(fill="both", expand=True, padx=12, pady=12)
        cards = tk.Frame(wrap, bg="#111827")
        cards.pack(fill="x")
        self.cpu_card, self.cpu_value, self.cpu_detail = self._card(cards, "CPU usage", "Total processor utilization")
        self.ram_card, self.ram_value, self.ram_detail = self._card(cards, "Memory usage", "RAM currently in use")
        self.disk_card, self.disk_value, self.disk_detail = self._card(cards, "System drive", "Disk space")
        for card in (self.cpu_card, self.ram_card, self.disk_card):
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.cpu_bar = ttk.Progressbar(wrap, maximum=100)
        self.cpu_bar.pack(fill="x", pady=(12, 7))
        self.ram_bar = ttk.Progressbar(wrap, maximum=100)
        self.ram_bar.pack(fill="x", pady=7)
        self.disk_bar = ttk.Progressbar(wrap, maximum=100)
        self.disk_bar.pack(fill="x", pady=7)

        self.battery_card, self.battery_value, self.battery_detail = self._card(
            wrap, "Battery", "Battery information supplied by Windows")
        self.battery_card.pack(fill="x", pady=(12, 10))
        actions = tk.Frame(wrap, bg="#111827")
        actions.pack(fill="x", pady=(3, 0))
        self._button(actions, "Refresh now", self.refresh_metrics, "#2563eb").pack(side="left", padx=(0, 8))
        self._button(actions, "Copy diagnostics", self.copy_diagnostics).pack(side="left", padx=(0, 8))
        self._button(actions, "Open Task Manager", self.open_task_manager).pack(side="left", padx=(0, 8))
        self._button(actions, "Top RAM processes", self.show_top_ram_processes, "#0f766e").pack(side="left")
        self._button(actions, "About sensors", self.sensor_note).pack(side="right")

    def _build_focus(self):
        frame = self.focus_tab
        self._section_title(frame, "Focus & Power", "Switch screen/sleep timeouts without opening Settings. Original values are saved before Work Mode is enabled.")
        self.power_mode_var = tk.StringVar(value=self.get_power_mode_label())
        self.power_mode_label = tk.Label(frame, textvariable=self.power_mode_var,
                 font=("Segoe UI", 12, "bold"), fg="#93c5fd", bg="#111827")
        self.power_mode_label.pack(anchor="w", padx=18, pady=(8, 8))
        self._button(frame, "Toggle Work Mode", self.toggle_work_mode, "#0f766e").pack(anchor="w", padx=18, pady=5)
        self._button(frame, "Open Windows Do Not Disturb settings", self.open_notifications_settings, "#2563eb").pack(anchor="w", padx=18, pady=(14, 5))
        self._button(frame, "Open Power & battery settings", self.open_power_settings).pack(anchor="w", padx=18, pady=5)
        self._button(frame, "Open Screen & sleep settings", self.open_sleep_settings).pack(anchor="w", padx=18, pady=5)
        note = ("Work Mode sets automatic display-off and sleep timeouts to Never for both battery and plugged-in use. "
                "Switch it off to restore the values captured when Work Mode was first enabled. "
                "Focus Mode currently opens Windows notification settings; direct Do Not Disturb control is not exposed consistently across Windows builds.")
        tk.Label(frame, text=note, font=("Segoe UI", 10), fg="#d1d5db", bg="#111827",
                 wraplength=650, justify="left").pack(anchor="w", padx=18, pady=16)

    def _build_developer(self):
        frame = self.dev_tab
        self._section_title(frame, "Developer Launchpad", "Edit quickcontrol/config.json to customize launch items.")
        self.launch_list = ttk.Treeview(frame, columns=("path", "kind"), show="headings", height=9)
        self.launch_list.heading("path", text="Target")
        self.launch_list.heading("kind", text="Type")
        self.launch_list.column("path", width=420)
        self.launch_list.column("kind", width=90, anchor="center")
        self.launch_list.pack(fill="both", expand=True, padx=18, pady=12)
        self._load_launch_items()
        self._button(frame, "Launch selected", self.launch_selected, "#2563eb").pack(anchor="w", padx=18, pady=(0, 12))

    def _build_network(self):
        frame = self.network_tab
        self._section_title(frame, "Network Diagnostics", "Read-only checks; no adapter settings are changed.")
        self._button(frame, "Run diagnostics", self.run_network_diagnostics, "#2563eb").pack(anchor="w", padx=18, pady=(8, 12))
        self.network_text = tk.Text(frame, height=16, bg="#1f2937", fg="#e5e7eb",
                                    insertbackground="#e5e7eb", relief="flat", wrap="word",
                                    font=("Consolas", 10), padx=12, pady=10)
        self.network_text.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.network_text.insert("1.0", "Run diagnostics to test gateway, DNS, and internet connectivity.")
        self.network_text.configure(state="disabled")

    def _build_storage(self):
        frame = self.storage_tab
        self._section_title(frame, "Storage Cleanup", "Review Windows cleanup candidates before deleting anything.")
        self.storage_card, self.storage_value, self.storage_detail = self._card(frame, "Free space on system drive", "")
        self.storage_card.pack(fill="x", padx=18, pady=12)
        self._button(frame, "Refresh disk space", self.refresh_metrics, "#2563eb").pack(anchor="w", padx=18, pady=4)
        self._button(frame, "Open Windows Temporary Files", self.open_storage_settings).pack(anchor="w", padx=18, pady=4)
        tk.Label(frame, text="This starter never deletes files automatically. The next phase can calculate safe cleanup candidates and ask for confirmation.",
                 font=("Segoe UI", 10), fg="#d1d5db", bg="#111827", wraplength=620,
                 justify="left").pack(anchor="w", padx=18, pady=12)

    def _section_title(self, parent, title, subtitle):
        tk.Label(parent, text=title, font=("Segoe UI", 16, "bold"), fg="#f9fafb",
                 bg="#111827").pack(anchor="w", padx=18, pady=(18, 3))
        tk.Label(parent, text=subtitle, font=("Segoe UI", 10), fg="#9ca3af",
                 bg="#111827", wraplength=680, justify="left").pack(anchor="w", padx=18, pady=(0, 8))

    def _button(self, parent, text, command, color="#374151"):
        return tk.Button(parent, text=text, command=command, bg=color, fg="#f9fafb",
                         activebackground="#4b5563", activeforeground="#ffffff",
                         relief="flat", bd=0, padx=13, pady=8, cursor="hand2",
                         font=("Segoe UI", 9))

    def refresh_metrics(self):
        if self._closing:
            return
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            root = os.environ.get("SystemDrive", "C:") + "\\"
            disk = psutil.disk_usage(root)
            self.cpu_value.configure(text=f"{cpu:.0f}%")
            self.cpu_detail.configure(text=f"{psutil.cpu_count(logical=True) or '?'} logical processors")
            self.cpu_bar["value"] = cpu
            self.ram_value.configure(text=f"{mem.percent:.0f}%")
            self.ram_detail.configure(text=f"{format_bytes(mem.used)} used of {format_bytes(mem.total)}")
            self.ram_bar["value"] = mem.percent
            self.disk_value.configure(text=f"{disk.percent:.0f}% used")
            self.disk_detail.configure(text=f"{format_bytes(disk.free)} free of {format_bytes(disk.total)}")
            self.disk_bar["value"] = disk.percent
            self.storage_value.configure(text=f"{format_bytes(disk.free)} free")
            self.storage_detail.configure(text=f"{format_bytes(disk.used)} used of {format_bytes(disk.total)}")
            battery = psutil.sensors_battery()
            if battery is None:
                self.battery_value.configure(text="Unavailable")
                self.battery_detail.configure(text="This device does not expose battery data to Windows.")
            else:
                charging = " · plugged in" if battery.power_plugged else " · on battery"
                self.battery_value.configure(text=f"{battery.percent:.0f}%")
                self.battery_detail.configure(text=("Charging" if battery.power_plugged else "Discharging") + charging)
            self.status_var.set(f"Updated {time.strftime('%H:%M:%S')} · refreshes every 2 seconds")
        except Exception as exc:
            self.status_var.set(f"Monitor error: {exc}")
        self.after(REFRESH_MS, self.refresh_metrics)

    def copy_diagnostics(self):
        try:
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
            battery = psutil.sensors_battery()
            report = [
                f"{APP_NAME} diagnostics",
                f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Windows: {platform.platform()}",
                f"CPU: {psutil.cpu_percent(interval=0.1):.1f}%",
                f"RAM: {mem.percent:.1f}% ({format_bytes(mem.used)} / {format_bytes(mem.total)})",
                f"System drive: {disk.percent:.1f}% used; {format_bytes(disk.free)} free",
                f"Battery: {str(round(battery.percent)) + '%' if battery else 'Unavailable'}",
            ]
            self.clipboard_clear()
            self.clipboard_append("\n".join(report))
            self.status_var.set("Diagnostics copied to clipboard")
            self.after(5000, lambda: self.status_var.set("Ready") if not self._closing else None)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not copy diagnostics:\n{exc}")

    def open_task_manager(self):
        try:
            os.startfile("taskmgr.exe")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))


    def show_toast(self, message, duration=4500):
        """Small non-modal status popup that closes automatically."""
        toast = tk.Toplevel(self)
        toast.title("Quick Control Center")
        toast.configure(bg="#1f2937")
        toast.resizable(False, False)
        toast.attributes("-topmost", True)
        label = tk.Label(toast, text=message, bg="#1f2937", fg="#f9fafb",
                         font=("Segoe UI", 10), padx=18, pady=14,
                         wraplength=390, justify="left")
        label.pack()
        toast.update_idletasks()
        width, height = toast.winfo_width(), toast.winfo_height()
        x = self.winfo_rootx() + max(10, self.winfo_width() - width - 20)
        y = self.winfo_rooty() + max(10, self.winfo_height() - height - 60)
        toast.geometry(f"+{x}+{y}")
        toast.after(duration, toast.destroy)

    def show_top_ram_processes(self):
        win = tk.Toplevel(self)
        win.title("Top RAM Processes")
        win.geometry("790x480")
        win.minsize(650, 360)
        win.configure(bg="#111827")
        win.transient(self)
        tk.Label(win, text="Processes using the most memory",
                 font=("Segoe UI", 16, "bold"), fg="#f9fafb",
                 bg="#111827").pack(anchor="w", padx=18, pady=(16, 3))
        tk.Label(win, text="Memory use is a snapshot. Don't end processes unless you know what they do.",
                 font=("Segoe UI", 9), fg="#9ca3af", bg="#111827").pack(anchor="w", padx=18, pady=(0, 12))
        cols = ("app", "process", "pid", "ram", "status")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=12)
        for col, title, width in [
            ("app", "Application", 190), ("process", "Process", 190),
            ("pid", "PID", 75), ("ram", "RAM", 100), ("status", "Status", 90)
        ]:
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w" if col in ("app", "process", "status") else "center")
        tree.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        def app_group(name):
            n = name.lower()
            if "chrome" in n or "msedge" in n or "firefox" in n or "brave" in n:
                return "Browser"
            if "code" in n or "python" in n or "node" in n or "git" in n:
                return "Development"
            if "whatsapp" in n or "chatgpt" in n or "slack" in n or "teams" in n:
                return "Messaging / AI"
            if "memcompression" in n:
                return "Windows memory"
            if n in ("explorer.exe", "dwm.exe", "sihost.exe"):
                return "Windows shell"
            return "Other"

        def refresh():
            for row in tree.get_children():
                tree.delete(row)
            records = []
            for proc in psutil.process_iter(["pid", "name", "memory_info", "status"]):
                try:
                    info = proc.info
                    mem = info.get("memory_info")
                    if not mem:
                        continue
                    name = info.get("name") or "Unknown"
                    records.append((mem.rss, app_group(name), name, info["pid"], info.get("status", "unknown")))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            # Grouping is shown by application family while retaining each process/PID for diagnosis.
            for rss, group, name, pid, status in sorted(records, reverse=True)[:30]:
                tree.insert("", "end", values=(group, name, pid, format_bytes(rss), status))

        actions = tk.Frame(win, bg="#111827")
        actions.pack(fill="x", padx=18, pady=(0, 14))
        self._button(actions, "Refresh list", refresh, "#2563eb").pack(side="left", padx=(0, 8))
        self._button(actions, "Open Task Manager", self.open_task_manager).pack(side="left")
        refresh()

    def get_power_mode_label(self):
        try:
            if self.power_controller.is_recovery_required():
                return "Current mode: POWER STATE NEEDS ATTENTION"
            if self.power_controller.is_enabled():
                return "Current mode: WORK MODE ON"
        except PowerModeError:
            return "Current mode: POWER STATE NEEDS ATTENTION"
        return "Current mode: NORMAL (saved Windows timeouts)"

    def toggle_work_mode(self):
        try:
            if self.power_controller.toggle():
                self.power_mode_var.set("Current mode: WORK MODE ON")
                self.show_toast("Work Mode enabled. Automatic screen-off and sleep are disabled.")
                self.status_var.set("Work Mode enabled")
            else:
                self.power_mode_var.set("Current mode: NORMAL (restored saved Windows timeouts)")
                self.show_toast("Normal Mode restored the saved screen-off and sleep timeouts.")
                self.status_var.set("Normal Mode restored")
        except LegacyPowerStateRecoveryRequired:
            self._confirm_legacy_power_recovery()
        except Exception as exc:
            self.show_toast(f"Could not change power mode: {exc}", 6000)
            messagebox.showerror(APP_NAME, f"Power mode change failed:\n{exc}")

    def _confirm_legacy_power_recovery(self):
        try:
            scheme = self.power_controller.legacy_recovery_scheme()
            approved = messagebox.askyesno(
                APP_NAME,
                "This saved Work Mode state predates power-scheme tracking, so its original scheme cannot be identified. "
                f"Restore its saved timeouts to the currently active scheme ({scheme})? "
                "Cancel if this is not the scheme Work Mode changed. The saved state is preserved unless recovery succeeds.",
            )
            if not approved:
                self.status_var.set("Legacy power-state recovery cancelled; saved state preserved")
                return
            self.power_controller.recover_legacy(scheme)
            self.power_mode_var.set("Current mode: NORMAL (restored saved Windows timeouts)")
            self.show_toast("Legacy Work Mode settings were restored.")
            self.status_var.set("Legacy Work Mode restored")
        except Exception as exc:
            self.show_toast(f"Could not recover legacy power state: {exc}", 6000)
            messagebox.showerror(APP_NAME, f"Power-state recovery failed:\n{exc}")

    def sensor_note(self):
        self.show_toast("CPU temperature is not available through a consistent Windows API on every PC.")

    def open_uri(self, uri):
        try:
            os.startfile(uri)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open Windows settings:\n{exc}")

    def open_notifications_settings(self):
        self.open_uri("ms-settings:notifications")

    def open_power_settings(self):
        self.open_uri("ms-settings:powersleep")

    def open_sleep_settings(self):
        self.open_uri("ms-settings:powersleep")

    def open_storage_settings(self):
        self.open_uri("ms-settings:storagesense")

    def _load_launch_items(self):
        self.launch_items = []
        self.launch_list.delete(*self.launch_list.get_children())
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.launch_items = data.get("launch_items", [])
            for idx, item in enumerate(self.launch_items):
                path = os.path.expandvars(item.get("path", ""))
                self.launch_list.insert("", "end", iid=str(idx), values=(path, item.get("kind", "app")))
        except Exception as exc:
            self.launch_list.insert("", "end", values=(f"Config error: {exc}", ""))

    def launch_selected(self):
        selection = self.launch_list.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "Select a launch item first.")
            return
        try:
            item = self.launch_items[int(selection[0])]
            target = os.path.expandvars(item["path"])
            args = item.get("args", "")
            kind = item.get("kind", "app")
            if kind == "folder":
                os.startfile(target)
            else:
                # Use shell parsing only for configured command arguments; no admin elevation.
                import shlex
                command = [target] + shlex.split(args, posix=False) if args else [target]
                subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.status_var.set(f"Launched {item.get('name', target)}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not launch selected item:\n{exc}\n\nEdit quickcontrol/config.json to verify the target.")

    def run_network_diagnostics(self):
        self.network_text.configure(state="normal")
        self.network_text.delete("1.0", "end")
        self.network_text.insert("end", "Running checks…\n")
        self.network_text.configure(state="disabled")
        threading.Thread(target=self._network_worker, daemon=True).start()

    def _network_worker(self):
        lines = [f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        try:
            host = socket.gethostname()
            lines.append(f"Computer: {host}")
            try:
                gateway_result = run_hidden(["ipconfig"], timeout=5)
                gateway_lines = [line.strip() for line in gateway_result.stdout.splitlines()
                                 if "Default Gateway" in line or "Default Gateway" in line]
                lines.append("Gateway information:")
                lines.extend(gateway_lines[:8] or ["No gateway line found in ipconfig output."])
            except Exception as exc:
                lines.append(f"Gateway check error: {exc}")
            try:
                ip = socket.gethostbyname("example.com")
                lines.append(f"DNS resolution: OK (example.com -> {ip})")
            except Exception as exc:
                lines.append(f"DNS resolution: FAILED ({exc})")
            try:
                start = time.perf_counter()
                with socket.create_connection(("1.1.1.1", 443), timeout=3):
                    elapsed = (time.perf_counter() - start) * 1000
                lines.append(f"Internet TCP check: OK (Cloudflare 1.1.1.1:443, {elapsed:.0f} ms)")
            except Exception as exc:
                lines.append(f"Internet TCP check: FAILED ({exc})")
        except Exception as exc:
            lines.append(f"Diagnostic error: {exc}")
        report = "\n".join(lines)
        self.after(0, lambda: self._show_network_report(report))

    def _show_network_report(self, report):
        self.network_text.configure(state="normal")
        self.network_text.delete("1.0", "end")
        self.network_text.insert("1.0", report)
        self.network_text.configure(state="disabled")

    def on_close(self):
        self._closing = True
        self.destroy()


if __name__ == "__main__":
    app = QuickControlCenter()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
