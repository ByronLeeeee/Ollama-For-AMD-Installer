import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import requests
import os
import subprocess
import ctypes
import sys
import threading
import time
import logging
import shutil
import tempfile
import json
import webbrowser
try:
    import winreg
except ImportError:
    winreg = None
from typing import Dict, Optional, List
from pathlib import Path
from urllib.parse import urlsplit
from installer_core import (AMD_REPO, LIB_REPO, GPU_PACKAGES, ROCM_VERSIONS,
                            select_release, select_gpu_asset, match_gpu, verify_asset,
                            extract_archive, prepare_payload, deploy_payload,
                            latest_backup, restore_backup, CompatibilityError, parse_client_version)

# Version constant
VERSION = "0.5.0"


class APILimitRateError(Exception):
    """Exception raised when GitHub API rate limit is reached."""
    pass


# Configure logging for debugging and audit
logging.basicConfig(
    filename="ollama_installer.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(module)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Profiles have separate verified packages for each supported ROCm SDK.
GPU_ROCM_MAPPING = GPU_PACKAGES


def get_system_amd_gpus() -> List[str]:
    """Identify installed AMD GPUs using PowerShell."""
    try:
        cmd = 'powershell -NoProfile "Get-CimInstance -ClassName Win32_VideoController | Select-Object -ExpandProperty Name"'
        output = subprocess.check_output(
            cmd, shell=True, encoding='utf-8', errors='ignore', creationflags=subprocess.CREATE_NO_WINDOW).strip()
        gpus = [line.strip() for line in output.split('\n') if line.strip()]
        return [gpu for gpu in gpus if "AMD" in gpu.upper() or "RADEON" in gpu.upper()]
    except Exception as e:
        logging.error(f"GPU detection failed: {e}")
        return []


def auto_match_gpu_to_key(gpu_name: str) -> str:
    return match_gpu(gpu_name)


def restart_as_admin():
    """Request UAC elevation and restart the application."""
    try:
        if getattr(sys, 'frozen', False):
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, "", None, 1)
        else:
            script_path = os.path.abspath(sys.argv[0])
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, f'"{script_path}"', None, 1)
    except Exception as e:
        logging.error(f"Elevation request failed: {e}")
    sys.exit()


def is_admin() -> bool:
    """Check if the current process has administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


class ProxySelector:
    """Network proxy configuration and performance testing tool."""
    DEFAULT_PROXIES = {
        "Default (No Proxy)": "",
        "GHProxy": "https://ghfast.top/",
        "GitHub Mirror": "https://github.moeyy.xyz/",
        "CF Worker": "https://gh.api.99988866.xyz/"
    }

    def __init__(self, master_gui):
        self.master_gui = master_gui
        self.root = master_gui.content
        self.proxies: Dict[str, str] = self.load_proxies()
        self.selected_proxy = tk.StringVar(value="Default (No Proxy)")
        self.custom_proxy = tk.StringVar()
        self.create_widgets()

    def create_widgets(self):
        """Build proxy selection interface."""
        self.proxy_frame = ttk.LabelFrame(
            self.root, text="🌐 Proxy & Network Settings", padding=(10, 5))
        self.proxy_frame.grid(row=2, column=0, columnspan=2,
                              pady=5, padx=10, sticky="ew")
        self.proxy_frame.columnconfigure(1, weight=1)

        ttk.Label(self.proxy_frame, text="Select Proxy:").grid(
            row=0, column=0, pady=5, padx=5, sticky="w")
        self.proxy_combo = ttk.Combobox(
            self.proxy_frame, textvariable=self.selected_proxy, state="readonly")
        self.update_proxy_list()
        self.proxy_combo.grid(row=0, column=1, pady=5, padx=5, sticky="ew")

        ttk.Label(self.proxy_frame, text="Custom Proxy:").grid(
            row=1, column=0, pady=5, padx=5, sticky="w")
        self.custom_entry = ttk.Entry(
            self.proxy_frame, textvariable=self.custom_proxy)
        self.custom_entry.grid(row=1, column=1, pady=5, padx=5, sticky="ew")
        ttk.Button(self.proxy_frame, text="Add", command=self.add_custom_proxy,
                   width=8).grid(row=1, column=2, pady=5, padx=5, sticky="e")

        self.test_btn = ttk.Button(
            self.proxy_frame, text="⚡ Test Proxies", command=self.start_proxy_test)
        self.test_btn.grid(row=2, column=0, columnspan=3,
                           pady=5, padx=5, sticky="ew")

        self.result_text = tk.Text(
            self.proxy_frame, height=3, font=("Consolas", 8), bg="#f4f4f4")
        self.result_text.grid(row=3, column=0, columnspan=3,
                              pady=(0, 5), padx=5, sticky="ew")
        self.result_text.insert(
            tk.END, "Proxy test results will appear here...")
        self.result_text.config(state="disabled")

    def update_proxy_list(self):
        """Refresh proxy dropdown list."""
        proxy_list = list(self.proxies.keys())
        self.proxy_combo["values"] = proxy_list
        if self.selected_proxy.get() not in proxy_list:
            self.selected_proxy.set(proxy_list[0])

    def get_selected_proxy_url(self) -> str:
        """Get the base URL of the active proxy."""
        name = self.selected_proxy.get()
        return self.proxies.get(name, "")

    def add_custom_proxy(self):
        """Integrate a user-defined proxy into the list."""
        url = self.custom_proxy.get().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not url.endswith("/"):
            url += "/"
        name = f"Custom ({url})"
        self.proxies[name] = url
        self.save_proxies()
        self.update_proxy_list()
        self.proxy_combo.set(name)
        self.custom_proxy.set("")

    def load_proxies(self) -> Dict[str, str]:
        """Load persistent proxy configurations from disk."""
        try:
            if os.path.exists("proxy_config.json"):
                with open("proxy_config.json", "r") as f:
                    saved = json.load(f)
                    proxies = self.DEFAULT_PROXIES.copy()
                    proxies.update(saved)
                    return proxies
        except Exception:
            pass
        return self.DEFAULT_PROXIES.copy()

    def save_proxies(self):
        """Save non-default proxies to local storage."""
        try:
            save_proxies = {k: v for k,
                            v in self.proxies.items() if "Default" not in k}
            with open("proxy_config.json", "w") as f:
                json.dump(save_proxies, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save proxy configuration: {e}")

    def test_proxy(self, name: str, url: str) -> float:
        """Measure proxy latency using a GitHub repository URL."""
        test_url = f"{url}https://github.com/likelovewant/ollama-for-amd"
        if name == "Default (No Proxy)":
            test_url = "https://github.com/likelovewant/ollama-for-amd"
        try:
            start_time = time.time()
            with requests.get(test_url, timeout=5) as response:
                response.raise_for_status()
            return time.time() - start_time
        except Exception:
            return float('inf')

    def start_proxy_test(self):
        """Initialize threaded proxy testing."""
        self.test_btn.config(state="disabled")
        self.result_text.config(state="normal")
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, "Testing latency (Please wait)...\n")
        self.result_text.config(state="disabled")
        threading.Thread(target=self.test_all_proxies, daemon=True).start()

    def test_all_proxies(self):
        """Execute connectivity tests for all known endpoints."""
        ping_results = {}
        for name, url in list(self.proxies.items()):
            r_time = self.test_proxy(name, url)
            ping_results[name] = r_time
            self.root.after(0, self.update_result_display, name, r_time)

        sorted_p = sorted(ping_results.items(), key=lambda x: x[1])
        valid_proxies = [p for p in sorted_p if p[1] != float('inf')]

        if valid_proxies:
            best_name = valid_proxies[0][0]
            self.root.after(0, self.proxy_combo.set, best_name)
            self.root.after(0, self._append_result,
                            f"\n✅ Optimal endpoint: {best_name}")
        else:
            self.root.after(0, self._append_result,
                            "\n❌ All endpoints timed out!")

        self.root.after(0, lambda: self.test_btn.config(state="normal"))

    def update_result_display(self, name, r_time):
        """Show latency for a specific proxy name."""
        res = f"{name}: Timeout/Fail\n" if r_time == float(
            'inf') else f"{name}: {r_time:.2f}s\n"
        self._append_result(res)

    def _append_result(self, text):
        """Thread-safe log append to proxy console."""
        self.result_text.config(state="normal")
        self.result_text.insert(tk.END, text)
        self.result_text.see(tk.END)
        self.result_text.config(state="disabled")


class OllamaInstallerGUI:
    """Primary application GUI class."""

    def __init__(self, master):
        self.master = master
        master.title("Ollama For AMD Installer")
        available_height = max(500, master.winfo_screenheight() - 100)
        master.geometry(f"850x{min(1000, available_height)}")
        master.minsize(700, min(650, available_height))
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(master, highlightthickness=0)
        scrollbar = ttk.Scrollbar(master, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas)
        content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(4, weight=1)
        self.content.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(content_window, width=event.width))
        master.bind("<MouseWheel>", self._scroll_window)

        self.repo = "likelovewant/ollama-for-amd"
        self.base_url = f"https://github.com/{self.repo}/releases/download"
        self.github_url = "https://github.com/ByronLeeeee/Ollama-For-AMD-Installer"
        self.gpu_var = tk.StringVar()
        self.ollama_path_var = tk.StringVar()
        self.github_access_token_var = tk.StringVar()
        self.rocm_version_var = tk.StringVar(value="7.1.1")
        self.framework_path_var = tk.StringVar()
        self.gpu_archive_path_var = tk.StringVar()
        self.igpu_var = tk.BooleanVar(value=False)
        self._busy = False
        self._task_options = {}

        self.create_widgets()
        self.proxy_selector = ProxySelector(self)
        self.load_settings()

    def create_widgets(self):
        """Construct the graphical interface components."""
        # Hardware configuration section
        gpu_frame = ttk.LabelFrame(
            self.content, text="💻 GPU Configuration", padding=(10, 5))
        gpu_frame.grid(row=0, column=0, columnspan=2,
                       pady=10, padx=10, sticky="ew")
        gpu_frame.columnconfigure(1, weight=1)

        ttk.Label(gpu_frame, text="GPU Model:").grid(
            row=0, column=0, pady=5, padx=5, sticky="w")
        self.gpu_combo = ttk.Combobox(gpu_frame, textvariable=self.gpu_var, values=list(
            GPU_ROCM_MAPPING.keys()), state="readonly", width=45)
        self.gpu_combo.grid(row=0, column=1, pady=5, padx=5, sticky="ew")

        self.detect_btn = ttk.Button(
            gpu_frame, text="🔍 Auto-Detect", command=self.detect_gpu)
        self.detect_btn.grid(row=0, column=2, pady=5, padx=5, sticky="e")

        ttk.Label(gpu_frame, text="ROCm SDK:").grid(row=1, column=0, padx=5, sticky="w")
        self.rocm_combo = ttk.Combobox(gpu_frame, textvariable=self.rocm_version_var,
                                      values=ROCM_VERSIONS, state="readonly")
        self.rocm_combo.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        self.igpu_check = ttk.Checkbutton(gpu_frame, text="Enable integrated GPU",
                                         variable=self.igpu_var)
        self.igpu_check.grid(row=1, column=2, padx=5)

        # Operational tasks section
        actions_frame = ttk.LabelFrame(
            self.content, text="🚀 Installation Actions", padding=(10, 5))
        actions_frame.grid(row=1, column=0, columnspan=2,
                           pady=5, padx=10, sticky="ew")
        actions_frame.columnconfigure(1, weight=1)

        # Path Selection
        ttk.Label(actions_frame, text="Ollama Path:").grid(
            row=0, column=0, pady=5, padx=5, sticky="w")
        self.path_entry = ttk.Entry(
            actions_frame, textvariable=self.ollama_path_var)
        self.path_entry.grid(row=0, column=1, pady=5, padx=5, sticky="ew")
        
        path_btns_frame = ttk.Frame(actions_frame)
        path_btns_frame.grid(row=0, column=2, pady=5, padx=5, sticky="e")
        
        self.browse_button = ttk.Button(path_btns_frame, text="📂 Browse", command=self.browse_path, width=10)
        self.browse_button.pack(side="left", padx=2)
        self.reset_button = ttk.Button(path_btns_frame, text="🔄 Reset", command=self.reset_path, width=10)
        self.reset_button.pack(side="left", padx=2)

        self.check_button = ttk.Button(
            actions_frame, text="1. Install Matched App + AMD Libs", command=self.full_install_thread)
        self.check_button.grid(
            row=1, column=0, columnspan=3, pady=5, padx=5, sticky="ew")

        self.replace_button = ttk.Button(
            actions_frame, text="2. Inject AMD Libs Only", command=self.replace_only_thread)
        self.replace_button.grid(
            row=2, column=0, columnspan=3, pady=5, padx=5, sticky="ew")

        self.vulkan_button = ttk.Button(
            actions_frame, text="3. Enable Vulkan (available by default in current Ollama)",
            command=self.enable_vulkan_thread)
        self.vulkan_button.grid(
            row=3, column=0, columnspan=3, pady=5, padx=5, sticky="ew")

        # Configuration and manual fixes section
        troubleshoot_frame = ttk.LabelFrame(
            self.content, text="🛠️ Troubleshooting & Configuration", padding=(10, 5))
        troubleshoot_frame.grid(
            row=3, column=0, columnspan=2, pady=5, padx=10, sticky="ew")
        troubleshoot_frame.columnconfigure(1, weight=1)

        self.fix_button = ttk.Button(
            troubleshoot_frame, text="Fix 0xc0000005 Error", command=self.fix_05Error_thread)
        self.fix_button.grid(row=0, column=0, pady=5, padx=5, sticky="ew")

        self.cleanup_button = ttk.Button(
            troubleshoot_frame, text="Restore Last Injection Backup", command=self.cleanup_thread)
        self.cleanup_button.grid(row=0, column=1, pady=5, padx=5, sticky="ew")

        ttk.Label(troubleshoot_frame, text="GitHub PAT:").grid(
            row=1, column=0, pady=5, sticky="w")
        self.github_entry = ttk.Entry(
            troubleshoot_frame, textvariable=self.github_access_token_var, show="*")
        self.github_entry.grid(row=1, column=1, pady=5, padx=5, sticky="ew")

        ttk.Label(actions_frame, text="Local packages (optional, both required for offline injection):").grid(
            row=4, column=0, columnspan=3, padx=5, sticky="w")
        self.local_entries = []
        self.local_buttons = []
        for row, label, variable in ((5, "Framework:", self.framework_path_var),
                                      (6, "GPU libs:", self.gpu_archive_path_var)):
            ttk.Label(actions_frame, text=label).grid(row=row, column=0, padx=5, sticky="w")
            entry = ttk.Entry(actions_frame, textvariable=variable)
            entry.grid(row=row, column=1, pady=3, padx=5, sticky="ew")
            button = ttk.Button(actions_frame, text="Browse", command=lambda v=variable: self.browse_archive(v))
            button.grid(row=row, column=2, padx=5)
            self.local_entries.append(entry)
            self.local_buttons.append(button)

        # Output console and status section
        console_frame = ttk.LabelFrame(
            self.content, text="🖥️ Console Output", padding=(10, 5))
        console_frame.grid(row=4, column=0, columnspan=2,
                           pady=5, padx=10, sticky="nsew")
        console_frame.columnconfigure(0, weight=1)
        console_frame.rowconfigure(0, weight=1)

        self.log_area = scrolledtext.ScrolledText(
            console_frame, height=10, font=("Consolas", 9), state="disabled", bg="#f4f4f4")
        self.log_area.grid(row=0, column=0, sticky="nsew", pady=5)

        self.progress = ttk.Progressbar(
            console_frame, length=100, mode="determinate")
        self.progress.grid(row=1, column=0, pady=5, sticky="ew")
        self.speed_label = ttk.Label(console_frame, text="System standby.")
        self.speed_label.grid(row=2, column=0, sticky="w")

        # Application metadata
        footer_frame = ttk.Frame(self.master)
        footer_frame.grid(row=5, column=0, columnspan=2,
                          padx=10, pady=5, sticky="e")
        tk.Label(footer_frame, text=f"v{VERSION} GitHub:",
                 font=("Arial", 8)).pack(side="left")
        link = tk.Label(footer_frame, text="ByronLeeeee/Ollama-For-AMD-Installer",
                        font=("Arial", 8, "underline"), fg="blue", cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e: webbrowser.open_new_tab(self.github_url))

        self.log_msg("Ready for input.")

    def _scroll_window(self, event):
        if event.widget.winfo_class() not in ("Text", "TCombobox"):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def browse_archive(self, variable):
        filename = filedialog.askopenfilename(filetypes=[("Archives", "*.zip *.7z")])
        if filename:
            variable.set(filename)

    def on_close(self):
        if self._busy:
            messagebox.showinfo("Operation Running", "Wait for the current operation to finish before closing.")
            return
        self.save_settings()
        self.master.destroy()

    def browse_path(self):
        """Open a directory selection dialog."""
        directory = filedialog.askdirectory()
        if directory:
            self.ollama_path_var.set(os.path.normpath(directory))

    def reset_path(self):
        """Clear manual path and revert to auto-detection."""
        self.ollama_path_var.set("")
        self.log_msg("Manual path cleared. Reverting to auto-detection.")

    def log_msg(self, message: str):
        """Append a message to the UI console and the log file."""
        logging.info(message)
        self.master.after(0, self._log_msg_sync, message)

    def _log_msg_sync(self, message: str):
        self.log_area.config(state="normal")
        self.log_area.insert(
            tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_area.see(tk.END)
        self.log_area.config(state="disabled")
        self.master.update_idletasks()

    def _update_progress_sync(self, current_val, max_val):
        if max_val > 0:
            self.progress.configure(mode="determinate", maximum=max_val)
            self.progress["value"] = current_val
        else:
            self.progress.configure(mode="indeterminate")

    def _update_speed_sync(self, text):
        self.speed_label.config(text=text)

    def _show_info(self, title, msg):
        self.master.after(0, messagebox.showinfo, title, msg)

    def _show_warning(self, title, msg):
        self.master.after(0, messagebox.showwarning, title, msg)

    def _show_error(self, title, msg):
        self.master.after(0, messagebox.showerror, title, msg)

    def detect_gpu(self):
        """Trigger automatic hardware identification."""
        self.log_msg("Initiating GPU discovery...")
        gpus = get_system_amd_gpus()

        if not gpus:
            self.log_msg("AMD hardware not found.")
            messagebox.showinfo("Hardware Status", "No AMD GPU detected.")
            return

        detected_str = ", ".join(gpus)
        self.log_msg(f"Hardware found: {detected_str}")
        
        matched_key = ""
        matched_gpu_name = gpus[0]
        
        for gpu in gpus:
            key = auto_match_gpu_to_key(gpu)
            if key and key != "AMBIGUOUS_APU":
                matched_key = key
                matched_gpu_name = gpu
                break
                
        if not matched_key:
            for gpu in gpus:
                key = auto_match_gpu_to_key(gpu)
                if key == "AMBIGUOUS_APU":
                    matched_key = key
                    matched_gpu_name = gpu
                    break

        if matched_key == "AMBIGUOUS_APU":
            messagebox.showwarning(
                "Generic Device", f"Integrated GPU detected: {matched_gpu_name}\nCheck the GPU model or gfx architecture before selecting a profile.")
        elif matched_key:
            self.gpu_var.set(matched_key)
            self.igpu_var.set(any(x in matched_gpu_name.upper() for x in ("680M", "780M", "880M", "890M", "8060S", "8050S", "840M", "860M", "820M")))
            self.log_msg(f"Auto-selected: {matched_key}")
            messagebox.showinfo(
                "GPU Identified", f"Device: {matched_gpu_name}\nProfile: {matched_key}")
        else:
            messagebox.showinfo(
                "GPU Identified", f"Device: {matched_gpu_name}\nPlease choose profile manually.")

    def kill_ollama(self):
        """Close running Ollama instances to unlock system files."""
        self.log_msg("Closing Ollama services...")
        subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"],
                       capture_output=True)
        subprocess.run(
            ["taskkill", "/F", "/IM", "ollama app.exe"], capture_output=True)
        time.sleep(1)

    def find_ollama_path(self) -> Optional[str]:
        manual_path = self._task_options.get("path", "").strip()
        if manual_path:
            if not os.path.isfile(os.path.join(manual_path, "ollama.exe")):
                raise ValueError("Select the installation folder containing ollama.exe.")
            return os.path.abspath(manual_path)
        registry_path = self.find_ollama_path_from_registry()
        if registry_path:
            return registry_path
        default_path = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama")
        if os.path.isfile(os.path.join(default_path, "ollama.exe")):
            return default_path
        executable = shutil.which("ollama.exe")
        if executable:
            return os.path.dirname(executable)
        raise ValueError("Ollama installation not found. Select its folder with Browse.")

    def find_ollama_path_from_registry(self) -> Optional[str]:
        for root_key in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
            try:
                with winreg.OpenKey(root_key, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Ollama") as hkey:
                    install_path = winreg.QueryValueEx(hkey, "InstallLocation")[0]
                    if os.path.isfile(os.path.join(install_path, "ollama.exe")):
                        return install_path
            except OSError:
                pass
        return None

    def _get_auth_headers(self, url="https://api.github.com"):
        token = self._task_options.get("token", "")
        # Never send the PAT to a user-configured download mirror.
        if token and urlsplit(url).hostname in ("api.github.com", "github.com"):
            return {"Authorization": f"Bearer {token}"}
        return None

    def check_rate_limit(self, response):
        if response.status_code == 429 or (response.status_code == 403 and
                response.headers.get("x-ratelimit-remaining") == "0"):
            raise APILimitRateError("GitHub API rate limit reached. Enter a PAT or use local packages.")
        response.raise_for_status()

    def _github_json(self, endpoint):
        url = f"https://api.github.com/{endpoint}"
        with requests.get(url, headers=self._get_auth_headers(url), timeout=(10, 60)) as response:
            self.check_rate_limit(response)
            return response.json()

    def get_release_plan(self, installed_tag=None):
        version = self._task_options["rocm"]
        # Empty newer releases are normal upstream; examine actual downloadable assets.
        releases = self._github_json(f"repos/{AMD_REPO}/releases?per_page=100")
        if installed_tag:
            releases = [release for release in releases if release["tag_name"] == installed_tag]
            try:
                plan = select_release(releases, version)
            except CompatibilityError as error:
                raise CompatibilityError(f"No matched ROCm {version} backend for installed Ollama {installed_tag}. "
                                         "Use Install Matched App + AMD Libs, or the current official Vulkan backend.") from error
        else:
            plan = select_release(releases, version)
        libraries = self._github_json(f"repos/{LIB_REPO}/releases/tags/v0.{version}")
        plan["gpu"] = select_gpu_asset(libraries, self._task_options["gpu"], version)
        self.log_msg(f"Selected Ollama {plan['tag']} + ROCm {version}: {plan['framework']['name']}")
        return plan

    def installed_client_tag(self):
        executable = os.path.join(self.find_ollama_path(), "ollama.exe")
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return parse_client_version(result.stdout + "\n" + result.stderr)

    def _start_task(self, target):
        if self._busy:
            return
        self._task_options = {
            "gpu": self.gpu_var.get(), "path": self.ollama_path_var.get().strip(),
            "token": self.github_access_token_var.get().strip(),
            "proxy": self.proxy_selector.get_selected_proxy_url(),
            "rocm": self.rocm_version_var.get(), "igpu": self.igpu_var.get(),
            "framework": self.framework_path_var.get().strip(),
            "gpu_archive": self.gpu_archive_path_var.get().strip(),
        }
        self._busy = True
        self._set_ui_state_sync("disabled")
        def run():
            try:
                target()
            except Exception as error:
                logging.exception("Operation failed")
                self.log_msg(f"Operation failed: {error}")
                self._show_error("Error", str(error))
            finally:
                self.master.after(0, self._finish_task)
        threading.Thread(target=run, daemon=True).start()

    def _finish_task(self):
        self._busy = False
        self._task_options.pop("token", None)
        self._set_ui_state_sync("normal")

    def full_install_thread(self):
        self._start_task(self._execute_full_install)

    def replace_only_thread(self):
        self._start_task(self._execute_replace_only)

    def enable_vulkan_thread(self):
        self._start_task(self._execute_enable_vulkan)

    def fix_05Error_thread(self):
        self._start_task(self.fix_05Error)

    def cleanup_thread(self):
        if self._busy:
            return
        if messagebox.askyesno("Restore Injection", "Restore files saved before the last AMD injection?\nOllama will be stopped while restoring."):
            self._start_task(self._execute_cleanup)

    def _execute_cleanup(self):
        ollama_path = self.find_ollama_path()
        backup = latest_backup(ollama_path)
        if not backup:
            raise ValueError("No injection backup found for this installation.")
        self.kill_ollama()
        restore_backup(ollama_path, backup)
        self.log_msg("Original files restored from the last injection backup.")
        self._show_info("Restored", "Previous files restored. Restart Ollama manually.")

    def _execute_enable_vulkan(self):
        self.log_msg("Enabling Vulkan acceleration...")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "OLLAMA_VULKAN", 0, winreg.REG_SZ, "1")
        self.log_msg("OLLAMA_VULKAN=1 set. Current official Ollama enables Vulkan by default.")
        self._show_info("Vulkan Enabled", "Sign out/in to refresh the environment, then restart Ollama.")

    def _validate_injection_options(self):
        options = self._task_options
        if options["gpu"] not in GPU_ROCM_MAPPING:
            raise ValueError("Select a GPU profile.")
        if bool(options["framework"]) != bool(options["gpu_archive"]):
            raise ValueError("Select both local framework and GPU library archives, or clear both fields.")

    def _download_asset(self, asset, cache):
        filename = os.path.join(cache, asset["name"])
        os.makedirs(cache, exist_ok=True)
        if os.path.isfile(filename):
            try:
                verify_asset(filename, asset)
                self.log_msg(f"Verified cached package: {asset['name']}")
                return filename
            except ValueError:
                self.log_msg(f"Replacing invalid cache: {asset['name']}")
        self.download_file(self._task_options["proxy"] + asset["browser_download_url"], filename,
                           asset=asset)
        return filename

    def _stage_injection(self, work, plan=None):
        self._validate_injection_options()
        options = self._task_options
        if options["framework"]:
            framework, gpu_archive = options["framework"], options["gpu_archive"]
            self.log_msg(f"Using local packages with selected ROCm SDK {options['rocm']}.")
        else:
            plan = plan or self.get_release_plan()
            framework = self._download_asset(plan["framework"], os.path.join("downloads", plan["tag"]))
            gpu_archive = self._download_asset(plan["gpu"], os.path.join("downloads", "rocm-" + plan["rocm"]))
        fw_dir, gpu_dir, stage = (os.path.join(work, part) for part in ("framework", "gpu", "stage"))
        self.log_msg("Extracting and validating packages before changing the installation...")
        extract_archive(framework, fw_dir)
        extract_archive(gpu_archive, gpu_dir)
        targets = prepare_payload(fw_dir, gpu_dir, stage, options["rocm"])
        return stage, targets

    def _apply_injection(self, stage, targets):
        ollama_path = self.find_ollama_path()
        self.kill_ollama()
        backup = deploy_payload(stage, ollama_path, targets)
        self.log_msg(f"Injection complete. Backup: {backup}")
        if self._task_options["igpu"]:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, "OLLAMA_IGPU_ENABLE", 0, winreg.REG_SZ, "1")
                self.log_msg("OLLAMA_IGPU_ENABLE=1 set. Sign out/in before restarting Ollama to refresh its environment.")
            except OSError as error:
                self.log_msg(f"Files installed; integrated GPU setting failed: {error}")
                self._show_warning("Integrated GPU Setting", "Files installed, but OLLAMA_IGPU_ENABLE could not be set. Set it manually.")
        self._show_info("Success", "Matched AMD libraries installed. Restart Ollama manually.\nUse Restore Last Injection Backup to undo file changes.")

    def _execute_full_install(self):
        self._validate_injection_options()
        if self._task_options["framework"]:
            raise ValueError("For local packages, install Ollama first and use Inject AMD Libs Only.")
        plan = self.get_release_plan()
        setup = plan["setup"]
        if not setup:
            # An exact-tag official installer prevents accidental latest-version ABI mixing.
            official = self._github_json(f"repos/ollama/ollama/releases/tags/{plan['tag']}")
            setup = next((a for a in official["assets"] if a["name"] == "OllamaSetup.exe"), None)
            if not setup:
                raise CompatibilityError("No installer for the selected compatible release. Install Ollama manually, then inject.")
        exe_filename = self._download_asset(setup, os.path.join("downloads", plan["tag"], "setup"))
        with tempfile.TemporaryDirectory(prefix="ollama-amd-") as work:
            stage, targets = self._stage_injection(work, plan)
            self.log_msg(f"Installing matched Ollama {plan['tag']}...")
            self.kill_ollama()
            install_args = [os.path.abspath(exe_filename), "/SILENT", "/NORESTART"]
            custom_dir = self._task_options["path"]
            if custom_dir:
                install_args.append(f"/DIR={os.path.abspath(custom_dir)}")
            subprocess.run(install_args, check=True)
            self._apply_injection(stage, targets)

    def _execute_replace_only(self):
        self._validate_injection_options()
        self.find_ollama_path()
        plan = None
        if not self._task_options["framework"]:
            plan = self.get_release_plan(self.installed_client_tag())
        with tempfile.TemporaryDirectory(prefix="ollama-amd-") as work:
            stage, targets = self._stage_injection(work, plan)
            self._apply_injection(stage, targets)

    def fix_05Error(self):
        """Repair shared DLL placement only in an existing legacy runner layout."""
        ollama_path = self.find_ollama_path()
        base_lib = Path(ollama_path) / "lib" / "ollama"
        target_root = base_lib / "runners"
        runners = sorted(target_root.glob("rocm_v*")) if target_root.is_dir() else []
        libraries = list(base_lib.glob("*.dll"))
        if len(runners) != 1 or not libraries:
            raise CompatibilityError("This fix applies only to a legacy installation with one ROCm runner and shared DLLs. "
                                     "For current layouts, inject matched packages or restore the last backup.")
        self.kill_ollama()
        for library in libraries:
            shutil.copy2(library, runners[0] / library.name)
        self.log_msg("Shared DLLs copied to the existing legacy ROCm runner.")
        self._show_info("Runtime Files Updated", "Restart Ollama and retry. Check server.log if the error persists.")

    def download_file(self, url: str, filename: str, is_github_url: bool = True, asset=None):
        """Download atomically; failed/truncated downloads never become cache hits."""
        partial = filename + ".part"
        try:
            with requests.get(url, headers=self._get_auth_headers(url), stream=True,
                              timeout=(10, 60)) as response:
                self.check_rate_limit(response)
                file_size = int(response.headers.get("content-length", 0))
                download_count = 0
                start_ts = time.time()
                self.master.after(0, self._update_progress_sync, 0, file_size)
                with open(partial, "wb") as output:
                    for segment in response.iter_content(chunk_size=256 * 1024):
                        if segment:
                            output.write(segment)
                            download_count += len(segment)
                            self.master.after(0, self._update_progress_sync, download_count, file_size)
                            self._update_speed(download_count, start_ts)
                if file_size and download_count != file_size:
                    raise ValueError("Download was truncated. Retry the download.")
                if asset:
                    verify_asset(partial, asset)
                os.replace(partial, filename)
                self.master.after(0, self._update_speed_sync, "Download verified.")
        finally:
            if os.path.exists(partial):
                os.remove(partial)

    def _update_speed(self, bytes_received: int, start_time: float):
        """Refresh download statistics in the interface."""
        duration = time.time() - start_time
        if duration > 0.5:
            mb_rate = (bytes_received / (1024 * 1024)) / duration
            text = f"Rate: {mb_rate:.2f} MB/s | Transferred: {bytes_received/(1024*1024):.2f} MB"
            self.master.after(0, self._update_speed_sync, text)

    def set_ui_state(self, state):
        """Toggle availability of interactive interface elements."""
        self.master.after(0, self._set_ui_state_sync, state)

    def _set_ui_state_sync(self, state):
        for widget in (self.path_entry, self.check_button, self.replace_button, self.vulkan_button,
                       self.fix_button, self.cleanup_button, self.detect_btn, self.browse_button,
                       self.reset_button, self.github_entry, self.igpu_check,
                       *self.local_entries, *self.local_buttons):
            widget.config(state=state)
        for combo in (self.gpu_combo, self.rocm_combo, self.proxy_selector.proxy_combo):
            combo.config(state="readonly" if state == "normal" else "disabled")

    def load_settings(self):
        """Retrieve user configuration from local storage."""
        try:
            if os.path.exists("settings.txt"):
                with open("settings.txt", "r", encoding="utf-8") as config_file:
                    lines = config_file.readlines()
                    if len(lines) >= 1:
                        gpu_val = lines[0].strip()
                        if gpu_val in GPU_ROCM_MAPPING:
                            self.gpu_var.set(gpu_val)
                        elif gpu_val.startswith("Official Support"):
                            self.gpu_var.set(next(iter(GPU_ROCM_MAPPING)))
                        elif gpu_val.startswith("gfx"):
                            # Migrate saved architecture names from 0.4.x.
                            prefix = gpu_val.split(" ")[0]
                            match = next((key for key in GPU_ROCM_MAPPING if key.split(" ")[0] == prefix), None)
                            if match:
                                self.gpu_var.set(match)
                    if len(lines) >= 2:
                        path_val = lines[1].strip()
                        if os.path.exists(path_val):
                            self.ollama_path_var.set(path_val)
                    if len(lines) >= 3 and lines[2].strip() in ROCM_VERSIONS:
                        self.rocm_version_var.set(lines[2].strip())
                    if len(lines) >= 4:
                        self.igpu_var.set(lines[3].strip() == "1")
        except Exception:
            pass

    def save_settings(self):
        """Persist current user configuration to disk."""
        try:
            with open("settings.txt", "w", encoding="utf-8") as config_file:
                config_file.write(f"{self.gpu_var.get()}\n")
                config_file.write(f"{self.ollama_path_var.get()}\n")
                config_file.write(f"{self.rocm_version_var.get()}\n")
                config_file.write("1\n" if self.igpu_var.get() else "0\n")
        except Exception:
            pass


def main():
    """Main application entry point with privilege check."""
    if sys.platform != "win32":
        print("This installer supports Windows only. For Linux, use the official Ollama installation instructions.")
        return
    if not is_admin():
        if messagebox.askyesno("Elevation Required", "Access to system directories is required.\nElevate now?"):
            restart_as_admin()
        return
    root_window = tk.Tk()
    app_instance = OllamaInstallerGUI(root_window)
    root_window.protocol("WM_DELETE_WINDOW", app_instance.on_close)
    root_window.mainloop()


if __name__ == "__main__":
    main()
