import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import requests
import os
import subprocess
import py7zr
import ctypes
import sys
import threading
import time
from tqdm import tqdm
import logging
import shutil
import tempfile
import json
import webbrowser
import winreg
from typing import Dict, Optional, List

# Version constant
VERSION = "0.4.1"


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

# Repository and download metadata
ROCM_VERSION_TAG = "v0.6.4.2"
BASE_URL = f"https://github.com/likelovewant/ROCmLibs-for-gfx1103-AMD780M-APU/releases/download/{ROCM_VERSION_TAG}/"

# Mapping of GPU families to specific ROCm library packages
GPU_ROCM_MAPPING = {
    "Official Support (Navi 31/32, Vega 20, 890M, RX9000)": "rocm.for.official.Support.7z",
    "gfx1010/1012 xnack- ('Navi 10', RX 5700/5600/5500 XT)": "rocm.gfx1010-xnack-gfx1012-xnack-.for.hip6.4.2.7z",
    "gfx1012 without xnack- / gfx1100 Mixed": "rocm.gfx1100.gfx1012.for.hip.6.4.2.7z",
    "gfx1031 ('Navi 22', RX 6700/6750 XT)": "rocm.gfx1031.for.hip.6.4.2.7z",
    "gfx1032 ('Navi 23', RX 6600/6650 XT, RX 7600)": "rocm.gfx1032.for.hip.6.4.2.7z",
    "gfx1034/1035/1036 ('Navi 24', RX 6500 XT, 6400, 680M APU)": "rocm.gfx1034.gfx1035.gfx1036.for.hip.6.4.2.7z",
    "gfx1103 ('Phoenix', 780M/880M APU)": "rocm.gfx1103.for.hip.6.4.2.7z",
    "gfx1152 (Strix / Ryzen AI 300 APU)": "rocm.gfx1152.for.hip.6.4.2.7z",
    "gfx1153 (Strix / Ryzen AI 300 APU)": "rocm.gfx1153.for.hip.6.4.2.7z"
}


def get_rocm_url(gpu_model: str) -> Optional[str]:
    """Retrieve ROCm download URL for a specific GPU model."""
    if gpu_model in GPU_ROCM_MAPPING:
        return BASE_URL + GPU_ROCM_MAPPING[gpu_model]
    return None


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
    """Map detected GPU string to the internal architecture key."""
    gpu_name_upper = gpu_name.upper()

    if any(x in gpu_name_upper for x in ["7900", "7800", "7700", "6900", "6950", "6800", "890M", "9070", "9060"]):
        return "Official Support (Navi 31/32, Vega 20, 890M, RX9000)"

    if "780M" in gpu_name_upper or "880M" in gpu_name_upper:
        return "gfx1103 ('Phoenix', 780M/880M APU)"
    elif any(x in gpu_name_upper for x in ["6700", "6750"]):
        return "gfx1031 ('Navi 22', RX 6700/6750 XT)"
    elif any(x in gpu_name_upper for x in ["6600", "6650", "7600"]):
        return "gfx1032 ('Navi 23', RX 6600/6650 XT, RX 7600)"
    elif any(x in gpu_name_upper for x in ["6500", "6400", "680M"]):
        return "gfx1034/1035/1036 ('Navi 24', RX 6500 XT, 6400, 680M APU)"
    elif any(x in gpu_name_upper for x in ["5700", "5600", "5500"]):
        return "gfx1010/1012 xnack- ('Navi 10', RX 5700/5600/5500 XT)"

    if "GRAPHICS" in gpu_name_upper and not any(char.isdigit() for char in gpu_name):
        return "AMBIGUOUS_APU"

    return ""


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
        self.root = master_gui.master
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
            requests.get(test_url, timeout=5)
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
        for name, url in self.proxies.items():
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
        master.geometry("750x920")
        master.minsize(700, 850)

        master.columnconfigure(0, weight=1)
        master.rowconfigure(4, weight=1)

        self.repo = "likelovewant/ollama-for-amd"
        self.base_url = f"https://github.com/{self.repo}/releases/download"
        self.github_url = "https://github.com/ByronLeeeee/Ollama-For-AMD-Installer"
        self.gpu_var = tk.StringVar()
        self.github_access_token_var = tk.StringVar()

        self.create_widgets()
        self.proxy_selector = ProxySelector(self)
        self.load_settings()

    def create_widgets(self):
        """Construct the graphical interface components."""
        # Hardware configuration section
        gpu_frame = ttk.LabelFrame(
            self.master, text="💻 GPU Configuration", padding=(10, 5))
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

        # Operational tasks section
        actions_frame = ttk.LabelFrame(
            self.master, text="🚀 Installation Actions", padding=(10, 5))
        actions_frame.grid(row=1, column=0, columnspan=2,
                           pady=5, padx=10, sticky="ew")
        actions_frame.columnconfigure(0, weight=1)

        self.check_button = ttk.Button(
            actions_frame, text="1. Full Install (App + AMD Libs)", command=self.full_install_thread)
        self.check_button.grid(
            row=0, column=0, columnspan=2, pady=5, padx=5, sticky="ew")

        self.replace_button = ttk.Button(
            actions_frame, text="2. Inject AMD Libs Only", command=self.replace_only_thread)
        self.replace_button.grid(
            row=1, column=0, columnspan=2, pady=5, padx=5, sticky="ew")

        self.vulkan_button = ttk.Button(
            actions_frame, text="3. Force Vulkan Mode (Optional fix if GPU still not detected)",
            command=self.enable_vulkan_thread)
        self.vulkan_button.grid(
            row=2, column=0, columnspan=2, pady=5, padx=5, sticky="ew")

        # Configuration and manual fixes section
        troubleshoot_frame = ttk.LabelFrame(
            self.master, text="🛠️ Troubleshooting & Configuration", padding=(10, 5))
        troubleshoot_frame.grid(
            row=3, column=0, columnspan=2, pady=5, padx=10, sticky="ew")
        troubleshoot_frame.columnconfigure(1, weight=1)

        self.fix_button = ttk.Button(
            troubleshoot_frame, text="Fix 0xc0000005 Error", command=self.fix_05Error_thread)
        self.fix_button.grid(row=0, column=0, columnspan=2,
                             pady=5, padx=5, sticky="ew")

        ttk.Label(troubleshoot_frame, text="GitHub PAT:").grid(
            row=1, column=0, pady=5, sticky="w")
        self.github_entry = ttk.Entry(
            troubleshoot_frame, textvariable=self.github_access_token_var)
        self.github_entry.grid(row=1, column=1, pady=5, padx=5, sticky="ew")

        # Output console and status section
        console_frame = ttk.LabelFrame(
            self.master, text="🖥️ Console Output", padding=(10, 5))
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
                "Generic Device", f"Integrated GPU detected: {matched_gpu_name}\nRyzen 7000+ -> gfx1103\nRyzen 6000 -> gfx1034")
        elif matched_key:
            self.gpu_var.set(matched_key)
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
        """Discover the installation directory of Ollama."""
        for root_key in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
            try:
                hkey = winreg.OpenKey(
                    root_key, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Ollama")
                install_path = winreg.QueryValueEx(hkey, "InstallLocation")[0]
                winreg.CloseKey(hkey)
                if os.path.exists(install_path):
                    return install_path
            except Exception:
                pass

        default_path = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama")
        if os.path.exists(default_path):
            return default_path
        return None

    def _get_auth_headers(self):
        """Generate headers for GitHub API requests."""
        token = self.github_access_token_var.get().strip()
        return {"Authorization": f"Bearer {token}"} if token else None

    def check_rate_limit(self, response):
        """Verify GitHub API quota status."""
        if response.status_code in [403, 429]:
            if response.headers.get("x-ratelimit-remaining") == "0":
                raise APILimitRateError()
        response.raise_for_status()

    def get_latest_release(self) -> str:
        """Fetch the latest version tag of the ROCm library project."""
        url = "https://api.github.com/repos/likelovewant/ollama-for-amd/releases/latest"
        response = requests.get(url, headers=self._get_auth_headers())
        self.check_rate_limit(response)
        return response.json()["tag_name"]

    def full_install_thread(self):
        """Start full installation in a background thread."""
        threading.Thread(target=self._execute_full_install,
                         daemon=True).start()

    def replace_only_thread(self):
        """Start library injection in a background thread."""
        threading.Thread(target=self._execute_replace_only,
                         daemon=True).start()

    def enable_vulkan_thread(self):
        """Start Vulkan configuration in a background thread."""
        threading.Thread(target=self._execute_enable_vulkan,
                         daemon=True).start()

    def fix_05Error_thread(self):
        """Start 0xc0000005 error fix in a background thread."""
        threading.Thread(target=self.fix_05Error, daemon=True).start()

    def _execute_enable_vulkan(self):
        """Configure system environment for Vulkan acceleration."""
        try:
            self.set_ui_state("disabled")
            self.log_msg("Activating Vulkan Acceleration Mode...")
            reg_key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_ALL_ACCESS)
            winreg.SetValueEx(reg_key, "OLLAMA_VULKAN", 0, winreg.REG_SZ, "1")
            self.log_msg("Configuration saved: OLLAMA_VULKAN = 1")
            try:
                winreg.DeleteValue(reg_key, "HSA_OVERRIDE_GFX_VERSION")
                self.log_msg("Cleaned up legacy ROCm overrides.")
            except FileNotFoundError:
                pass
            winreg.CloseKey(reg_key)
            self.kill_ollama()
            self.log_msg("✅ Vulkan configuration complete.")
            self._show_info(
                "Vulkan Enabled", "Settings applied. Please manually restart Ollama to take effect.")
        except Exception as e:
            self.log_msg(f"Registry operation failed: {e}")
            self._show_error("Error", f"Failed to update environment: {e}")
        finally:
            self.set_ui_state("normal")

    def _execute_full_install(self):
        """Orchestrate official app installation followed by library injection."""
        try:
            self.set_ui_state("disabled")
            exe_url = "https://ollama.com/download/OllamaSetup.exe"
            exe_filename = "OllamaSetup_Official.exe"
            self.log_msg("Acquiring official installer...")
            self.download_file(exe_url, exe_filename, is_github_url=False)
            self.log_msg("Launching setup...")
            self.kill_ollama()
            subprocess.run([exe_filename, "/SILENT"], check=True)
            self._execute_replace_only()
        except Exception as e:
            self.log_msg(f"Installer error: {e}")
            self._show_error("Error", f"Setup failed: {e}")
        finally:
            self.set_ui_state("normal")

    def _execute_replace_only(self):
        """Manage library retrieval and injection process."""
        try:
            self.set_ui_state("disabled")
            gpu_model = self.gpu_var.get()
            if not gpu_model:
                self._show_warning(
                    "Incomplete Input", "Select a GPU profile.")
                return

            version_tag = self.get_latest_release()
            os.makedirs(version_tag, exist_ok=True)
            proxy_url = self.proxy_selector.get_selected_proxy_url()
            ollama_path = self.find_ollama_path()
            if not ollama_path:
                return

            rocm_lib_dir = os.path.join(ollama_path, "lib", "ollama", "rocm")
            base_archive = os.path.join(version_tag, "ollama-windows-amd64.7z")

            if not os.path.exists(base_archive):
                self.log_msg("Downloading foundational components...")
                self.download_file(
                    f"{proxy_url}{self.base_url}/{version_tag}/ollama-windows-amd64.7z", base_archive)

            self.kill_ollama()
            self.log_msg("Extracting framework...")
            
            temp_fw_dir = tempfile.mkdtemp()
            with py7zr.SevenZipFile(base_archive, 'r') as archive:
                archive.extractall(path=temp_fw_dir)
                
            fw_extracted_root = os.path.join(temp_fw_dir, "windows-amd64")
            if not os.path.exists(fw_extracted_root):
                fw_extracted_root = temp_fw_dir
                
            shutil.copytree(fw_extracted_root, ollama_path, dirs_exist_ok=True)
            shutil.rmtree(temp_fw_dir, ignore_errors=True)

            gpu_url = get_rocm_url(gpu_model)
            if not gpu_url:
                raise ValueError("No URL for selected profile.")

            gpu_archive = os.path.join(version_tag, os.path.basename(gpu_url))
            if not os.path.exists(gpu_archive):
                self.log_msg(f"Downloading driver libs for {gpu_model}...")
                self.download_file(f"{proxy_url}{gpu_url}", gpu_archive)

            self.log_msg("Deploying DLLs...")
            temp_dir = tempfile.mkdtemp()
            with py7zr.SevenZipFile(gpu_archive, "r") as zip_ref:
                zip_ref.extractall(path=temp_dir)

            payload_root = temp_dir
            if len(os.listdir(temp_dir)) == 1:
                payload_root = os.path.join(temp_dir, os.listdir(temp_dir)[0])

            shutil.copy2(os.path.join(
                payload_root, "rocblas.dll"), rocm_lib_dir)
            lib_content = os.path.join(payload_root, "library")
            if os.path.exists(lib_content):
                shutil.copytree(lib_content, os.path.join(
                    rocm_lib_dir, "rocblas", "library"), dirs_exist_ok=True)

            self.log_msg("✅ Library injection successful.")
            self._show_info(
                "Success", "Hardware acceleration libraries updated.")
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            self.log_msg(f"Injection failure: {e}")
            self._show_error("Error", f"Process failed: {e}")
        finally:
            self.set_ui_state("normal")

    def fix_05Error(self):
        """Relocate DLLs to fix application startup errors."""
        try:
            self.set_ui_state("disabled")
            self.log_msg("Applying runtime fix...")
            ollama_path = self.find_ollama_path()
            if not ollama_path:
                return
            self.kill_ollama()
            base_lib = os.path.join(ollama_path, "lib", "ollama")
            target_root = os.path.join(base_lib, "runners")
            os.makedirs(target_root, exist_ok=True)

            dirs = [d for d in os.listdir(
                target_root) if d.startswith("rocm_v")]
            final_dest = os.path.join(target_root, sorted(
                dirs)[-1] if dirs else "rocm_v6.4")
            os.makedirs(final_dest, exist_ok=True)

            for file_item in [f for f in os.listdir(base_lib) if f.endswith(".dll")]:
                shutil.copy2(os.path.join(base_lib, file_item), final_dest)
            self.log_msg("✅ Fix deployment complete.")
            self._show_info("Success", "0xc0000005 fix implemented.")
        except Exception as e:
            self.log_msg(f"Fix failed: {e}")
        finally:
            self.set_ui_state("normal")

    def download_file(self, url: str, filename: str, is_github_url: bool = True):
        """Execute stream-based file download with progress monitoring."""
        try:
            auth_headers = self._get_auth_headers() if is_github_url else None
            response = requests.get(url, headers=auth_headers, stream=True)
            if is_github_url:
                self.check_rate_limit(response)
            else:
                response.raise_for_status()

            file_size = int(response.headers.get("content-length", 0))
            download_count = 0
            start_ts = time.time()
            
            self.master.after(0, self._update_progress_sync, 0, file_size if file_size > 0 else 100)

            display_name = os.path.basename(filename) if filename else "payload"

            with open(filename, "wb") as file_out, tqdm(
                total=file_size if file_size > 0 else None, 
                unit="iB", 
                unit_scale=True, 
                desc=display_name, 
                disable=(sys.stderr is None)
            ) as pbar:
                for segment in response.iter_content(chunk_size=8192):
                    if not segment:
                        continue
                    file_out.write(segment)
                    download_count += len(segment)
                    pbar.update(len(segment))
                    
                    if file_size > 0:
                        self.master.after(0, self._update_progress_sync, download_count, file_size)
                    
                    self._update_speed(download_count, start_ts)
                    
            self.master.after(0, self._update_speed_sync, "Download finished.")
        except Exception as e:
            self.log_msg(f"Network error: {e}")
            if os.path.exists(filename):
                os.remove(filename)
            raise

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
        self.check_button.config(state=state)
        self.replace_button.config(state=state)
        self.vulkan_button.config(state=state)
        self.fix_button.config(state=state)
        self.detect_btn.config(state=state)

    def load_settings(self):
        """Retrieve user configuration from local storage."""
        try:
            if os.path.exists("settings.txt"):
                with open("settings.txt", "r") as config_file:
                    saved_val = config_file.readline().strip()
                    if saved_val in GPU_ROCM_MAPPING:
                        self.gpu_var.set(saved_val)
        except Exception:
            pass

    def save_settings(self):
        """Persist current user configuration to disk."""
        try:
            with open("settings.txt", "w") as config_file:
                config_file.write(f"{self.gpu_var.get()}\n")
        except Exception:
            pass


def main():
    """Main application entry point with privilege check."""
    if not is_admin():
        if messagebox.askyesno("Elevation Required", "Access to system directories is required.\nElevate now?"):
            restart_as_admin()
        return
    root_window = tk.Tk()
    app_instance = OllamaInstallerGUI(root_window)
    root_window.protocol("WM_DELETE_WINDOW", lambda: (
        app_instance.save_settings(), root_window.destroy()))
    root_window.mainloop()


if __name__ == "__main__":
    main()
