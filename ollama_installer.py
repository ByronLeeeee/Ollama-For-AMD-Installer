import tkinter as tk
from tkinter import ttk, messagebox
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

# Set up logging
logging.basicConfig(filename='ollama_installer.log', level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

GPU_ROCM_MAPPING = {
    "gfx1010-xnack-": "rocm.gfx1010-xnack-.for.hip.sdk.6.1.2.7z",
    "gfx1011": "rocm.gfx1011.for.hip.sdk.6.1.2.7z",
    "gfx1012-xnack-": "rocm.gfx1012-xnack-.for.hip.sdk.6.1.2.7z",
    "gfx1031": "rocm.gfx1031.for.hip.sdk.6.1.2.7z",
    "gfx1031 (optimized)": "rocm.gfx1031.for.hip.sdk.6.1.2.optimized.with.little.wu.s.logic.7z",
    "gfx1032": "rocm.gfx1032.for.hip.sdk.6.1.2.7z",
    "gfx1034": "rocm.gfx1034.for.hip.sdk.6.1.2.7z",
    "gfx1035": "rocm.gfx1035.for.hip.sdk.6.1.2.7z",
    "gfx1036": "rocm.gfx1036.for.hip.sdk.6.1.2.7z",
    "gfx1103 (AMD 780M)": "rocm.gfx1103.AMD.780M.phoenix.V4.0.for.hip.sdk.6.1.2.7z",
    "gfx803 (Vega 10)": "rocm.gfx803.optic.vega10.logic.hip.sdk.6.1.2.7z",
    "gfx902": "rocm.gfx902.for.hip.sdk.6.1.2.7z",
    "gfx90c-xnack-": "rocm.gfx90c-xnack-.for.hip.sdk.6.1.2.7z",
    "gfx90c": "rocm.gfx90c.for.hip.sdk.6.1.2.7z"
}

BASE_URL = "https://github.com/likelovewant/ROCmLibs-for-gfx1103-AMD780M-APU/releases/download/v0.6.1.2/"


def get_rocm_url(gpu_model):
    if gpu_model in GPU_ROCM_MAPPING:
        return BASE_URL + GPU_ROCM_MAPPING[gpu_model]
    else:
        return None


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def restart_as_admin():
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    sys.exit()


class OllamaInstallerGUI:
    def __init__(self, master):
        self.master = master
        master.title("Ollama For AMD Installer")
        master.geometry("400x350")  # Increase height to accommodate new widgets

        self.repo = "likelovewant/ollama-for-amd"
        self.base_url = f"https://github.com/{self.repo}/releases/download"
        self.rocm_url = "https://github.com/likelovewant/ROCmLibs-for-gfx1103-AMD780M-APU/releases/download/v0.6.1.2"

        self.use_proxy = tk.BooleanVar()
        self.create_widgets()

        # Load saved settings
        self.load_settings()

    def create_widgets(self):
        ttk.Label(self.master, text="GPU Model:").grid(row=0, column=0, pady=5, padx=10, sticky="w")
        self.gpu_var = tk.StringVar()
        self.gpu_combo = ttk.Combobox(self.master, textvariable=self.gpu_var)
        self.gpu_combo['values'] = list(GPU_ROCM_MAPPING.keys())
        self.gpu_combo.grid(row=0, column=1, pady=5, padx=10, sticky="w")

        self.proxy_check = ttk.Checkbutton(
            self.master, text="Use Proxy Mirror", variable=self.use_proxy)
        self.proxy_check.grid(row=1, column=0, columnspan=2, pady=5, padx=10, sticky="w")

        self.check_button = ttk.Button(
            self.master, text="Check for New Version", command=self.check_version_thread)
        self.check_button.grid(row=2, column=0, columnspan=2, pady=10, padx=10, sticky="ew")

        self.replace_button = ttk.Button(
            self.master, text="Replace ROCm Libraries Only", command=self.download_and_replace_rocblas)
        self.replace_button.grid(row=3, column=0, pady=10, padx=10, sticky="ew")

        self.fix_button = ttk.Button(
            self.master, text="Fix 0xc0000005 Error", command=self.fix_05Error)
        self.fix_button.grid(row=3, column=1, columnspan=2, pady=10, padx=10, sticky="ew")

        self.progress = ttk.Progressbar(
            self.master, length=300, mode='determinate')
        self.progress.grid(row=4, column=0, columnspan=2, pady=10, padx=10, sticky="ew")

        self.speed_label = ttk.Label(self.master, text="Download Speed: 0 KB/s")
        self.speed_label.grid(row=5, column=0, columnspan=2, pady=5, padx=10, sticky="w")

        self.status_label = ttk.Label(self.master, text="")
        self.status_label.grid(row=6, column=0, columnspan=2, pady=5, padx=10, sticky="w")

    def get_url_with_proxy(self, url):
        return f"https://ghp.ci/{url}" if self.use_proxy.get() else url

    def check_version_thread(self):
        threading.Thread(target=self.check_version, daemon=True).start()

    def check_version(self):
        try:
            self.status_label.config(text="Checking for new version...")
            latest_version = self.get_latest_release()
            if messagebox.askyesno("New Version", f"New version found: {latest_version}\nDo you want to download and install?"):
                self.download_and_install(latest_version)
        except requests.RequestException as e:
            logging.error(f"Network error: {e}")
            messagebox.showerror("Error", f"Network error: {e}")
        except Exception as e:
            logging.error(f"Unknown error: {e}")
            messagebox.showerror("Error", f"An unknown error occurred: {e}")

    def get_latest_release(self):
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        response = requests.get(url)
        response.raise_for_status()
        return response.json()["tag_name"]

    def download_and_install(self, version):
        exe_url = self.get_url_with_proxy(
            f"{self.base_url}/{version}/OllamaSetup.exe")
        exe_filename = "OllamaSetup.exe"

        try:
            self.download_file(exe_url, exe_filename)
            self.install_exe(exe_filename)
            self.download_and_replace_rocblas()
        except Exception as e:
            logging.error(f"Error during download or installation: {e}")
            messagebox.showerror("Error", f"Error during download or installation: {e}")

    def download_file(self, url, filename):
        response = requests.get(url, stream=True)
        total_size = int(response.headers.get('content-length', 0))
        block_size = 1024  # 1 KB
        written = 0
        start_time = time.time()

        with open(filename, 'wb') as file, tqdm(
                desc=filename,
                total=total_size,
                unit='iB',
                unit_scale=True,
                unit_divisor=1024,
        ) as progress_bar:
            for data in response.iter_content(block_size):
                size = file.write(data)
                written += size
                progress_bar.update(size)
                self.update_progress(written, total_size)
                self.update_speed(written, start_time)

    def update_progress(self, current, total):
        progress = int((current / total) * 100)
        self.progress['value'] = progress
        self.master.update_idletasks()

    def update_speed(self, downloaded, start_time):
        elapsed_time = time.time() - start_time
        speed = downloaded / (1024 * elapsed_time)
        self.speed_label.config(text=f"Download Speed: {speed:.2f} KB/s")
        self.master.update_idletasks()

    def install_exe(self, filename):
        self.status_label.config(text="Installing...")
        self.master.update_idletasks()
        subprocess.run([filename, "/SILENT"], check=True)
        self.status_label.config(text="OLLAMA For AMD installed")

    def download_and_replace_rocblas(self):
        gpu_model = self.gpu_var.get()
        rocm_url = get_rocm_url(gpu_model)
        if rocm_url:
            rocm_url = self.get_url_with_proxy(rocm_url)
            local_path = os.path.join("rocblas", os.path.basename(rocm_url))
            if not os.path.exists(local_path):
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                self.download_file(rocm_url, local_path)
            self.extract_and_replace_rocblas(local_path)
        else:
            messagebox.showerror("Error", f"No ROCm file found for {gpu_model}")

    def extract_and_replace_rocblas(self, zip_path: str):
        rocblas_path = os.path.expandvars(
            r'%LOCALAPPDATA%\Programs\Ollama\lib\ollama')
        library_path = os.path.join(rocblas_path, 'rocblas\library')

        try:
            # Create temporary directory
            with tempfile.TemporaryDirectory() as temp_dir:
                print(f"Extracting to temporary directory: {temp_dir}")

                # Extract to temporary directory
                with py7zr.SevenZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(path=temp_dir)

                # Check if rocblas.dll exists in the temporary directory
                rocblas_dll_path = os.path.join(temp_dir, 'rocblas.dll')
                if not os.path.exists(rocblas_dll_path):
                    raise FileNotFoundError("rocblas.dll file not found")

                # Check if library folder exists in the temporary directory
                temp_library_path = os.path.join(temp_dir, 'library')
                if not os.path.exists(temp_library_path):
                    raise FileNotFoundError("library folder not found")

                # Copy rocblas.dll to the target path
                shutil.copy(rocblas_dll_path, rocblas_path)
                print(f"Copied rocblas.dll to {rocblas_path}")

                # Copy library folder to the target path
                if os.path.exists(library_path):
                    shutil.rmtree(library_path)  # Delete existing library folder
                shutil.copytree(temp_library_path, library_path)  # Copy entire folder
                print(f"Copied library folder to {library_path}")

                self.status_label.config(text="ROCm libraries updated")

        except Exception as e:
            self.status_label.config(text=f"Extraction failed: {str(e)}")

    def fix_05Error(self):
        self.status_label.config(text="Fixing 0xc0000005 Error...")
        source_dir = os.path.expandvars(
            r'%LOCALAPPDATA%\Programs\Ollama\lib\ollama')
        library_dir = os.path.expandvars(
            r'%LOCALAPPDATA%\Programs\Ollama\lib\ollama\rocblas\library')
        destination_dir = os.path.expandvars(
            r'%LOCALAPPDATA%\Programs\Ollama\lib\ollama\runners\rocm_v6.1')

        try:
            # Traverse all files in the source directory
            for filename in os.listdir(source_dir):
                source_file = os.path.join(source_dir, filename)

                # Check if it is a file (not a folder)
                if os.path.isfile(source_file):
                    destination_file = os.path.join(destination_dir, filename)
                    shutil.copy2(source_file, destination_file)
            
            for filename in os.listdir(library_dir):
                library_file = os.path.join(library_dir, filename)

                # Check if it is a file (not a folder)
                if os.path.isfile(library_file):
                    library_path = os.path.join(destination_dir, 'library')
                    if not os.path.exists(library_path):
                        os.makedirs(library_path)
                    destination_file = os.path.join(library_path, filename)
                    shutil.copy2(library_file, destination_file)    

            self.status_label.config(text="Fix successful!")
        except Exception as e:
            self.status_label.config(text=f"File copy failed: {str(e)}")

    def load_settings(self):
        try:
            with open('settings.txt', 'r') as f:
                self.gpu_var.set(f.readline().strip())
                self.use_proxy.set(f.readline().strip() == 'True')
        except FileNotFoundError:
            pass

    def save_settings(self):
        with open('settings.txt', 'w') as f:
            f.write(f"{self.gpu_var.get()}\n")
            f.write(f"{self.use_proxy.get()}\n")

    def on_closing(self):
        self.save_settings()
        self.master.destroy()


if __name__ == "__main__":
    if not is_admin():
        if messagebox.askyesno("Insufficient Permissions", "Administrator privileges are required to run this program.\nDo you want to restart as administrator?"):
            restart_as_admin()
        else:
            sys.exit()

    root = tk.Tk()
    app = OllamaInstallerGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()