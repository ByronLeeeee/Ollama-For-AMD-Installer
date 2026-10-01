"""Release selection and reversible ROCm deployment, independent of the GUI."""

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import uuid
import zipfile

AMD_REPO = "likelovewant/ollama-for-amd"
LIB_REPO = "likelovewant/ROCmLibs-for-gfx1103-AMD780M-APU"
ROCM_VERSIONS = ("7.1.1", "6.4.2")
OFFICIAL_PROFILE = "Official gfx906/1030/1100/1101/1102/1150/1151/1200/1201"
# Package names are verified against the release asset API, never guessed URLs.
GPU_PACKAGES = {
    OFFICIAL_PROFILE: ("official.rocm.7z", "rocm.for.official.Support.7z"),
    "gfx1010/1011/1012 xnack- (RX 5700/5600/5500 XT)": (
        "gfx1010-xnack-gfx1011-xnack-gfx1012-xnack-.7z", "rocm.gfx1010-xnack-gfx1012-xnack-.for.hip6.4.2.7z"),
    "gfx1010/1011/1012 without xnack-": (
        "gfx1010-gfx1011-gfx1012.7z", "rocm.gfx1100.gfx1012.for.hip.6.4.2.7z"),
    "gfx1031 (RX 6700/6750 XT)": ("gfx1031-gfx1032.7z", "rocm.gfx1031.for.hip.6.4.2.7z"),
    "gfx1032 (RX 6600/6650 XT)": ("gfx1031-gfx1032.7z", "rocm.gfx1032.for.hip.6.4.2.7z"),
    "gfx1034/1035/1036 (RX 6500/6400, 680M APU)": (
        "gfx1034-gfx1035-gfx1036.7z", "rocm.gfx1034.gfx1035.gfx1036.for.hip.6.4.2.7z"),
    "gfx1103 (780M APU)": ("gfx1103.7z", "rocm.gfx1103.for.hip.6.4.2.7z"),
    "gfx1152 (840M/860M APU)": ("gfx1152-gfx1153.7z", "rocm.gfx1152.for.hip.6.4.2.7z"),
    "gfx1153 (820M APU)": ("gfx1152-gfx1153.7z", "rocm.gfx1153.for.hip.6.4.2.7z"),
}
FRAMEWORK_NAMES = (
    "ollama-windows-amd64-rocm.zip", "ollama-windows-amd64-rocm.7z",
    "ollama-windows-amd64.7z", "ollama-windows-amd64.zip",
)
MAX_EXTRACT_SIZE = 16 * 1024 ** 3


class CompatibilityError(ValueError):
    pass


def rocm_version_from_release(release):
    versions = re.findall(r"v0\.(7\.1\.1|6\.4\.2)\b", release.get("body") or "")
    if len(set(versions)) == 1:
        return versions[0]
    return None


def select_release(releases, rocm_version="7.1.1"):
    """Skip empty, prerelease and unknown ABI releases instead of mixing SDKs."""
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        if rocm_version_from_release(release) != rocm_version:
            continue
        tag = release.get("tag_name", "")
        if not re.fullmatch(r"v\d+(?:\.\d+){1,3}", tag):
            continue
        assets = {asset["name"]: asset for asset in release.get("assets", [])}
        for name in FRAMEWORK_NAMES:
            if name in assets:
                return {"tag": tag, "rocm": rocm_version, "framework": assets[name],
                        "setup": assets.get("OllamaSetup.exe")}
    raise CompatibilityError(f"No downloadable Windows release with verified ROCm {rocm_version} metadata. "
                             "Use local packages or select another supported ROCm version.")


def select_gpu_asset(release, profile, rocm_version):
    if profile not in GPU_PACKAGES or rocm_version not in ROCM_VERSIONS:
        raise CompatibilityError("Select a supported GPU profile and ROCm version.")
    if release.get("tag_name") != "v0." + rocm_version:
        raise CompatibilityError("ROCm library release does not match the framework SDK.")
    name = GPU_PACKAGES[profile][ROCM_VERSIONS.index(rocm_version)]
    for asset in release.get("assets", []):
        if asset["name"] == name:
            return asset
    raise CompatibilityError(f"Upstream has no {name} asset for ROCm {rocm_version}.")


def match_gpu(gpu_name):
    name = gpu_name.upper()
    profiles = list(GPU_PACKAGES)
    arch = re.search(r"\bGFX(\d+)\b", name)
    if arch:
        gfx = arch.group(1)
        if gfx in ("906", "1030", "1100", "1101", "1102", "1150", "1151", "1200", "1201"):
            return OFFICIAL_PROFILE
        for profile in profiles[1:]:
            if gfx in profile.split(" ")[0].replace("gfx", "").split("/"):
                return profile
    # Match GPU names, not CPU model numbers such as Ryzen 7 6800H.
    if re.search(r"\b(?:RX\s*|RADEON\s+)(?:7900|7800|7700|7600|6950|6900|6800|9070|9060)\b", name):
        return OFFICIAL_PROFILE
    if re.search(r"\b(?:890M|880M|8060S|8050S|VII)\b", name):
        return OFFICIAL_PROFILE
    for pattern, index in ((r"\b780M\b", 6), (r"\b(?:6700|6750)\b", 3),
                           (r"\b(?:6600|6650)\b", 4), (r"\b(?:6500|6400|680M)\b", 5),
                           (r"\b(?:5700|5600|5500)\b", 1), (r"\b(?:840M|860M)\b", 7),
                           (r"\b820M\b", 8)):
        if re.search(pattern, name):
            return profiles[index]
    return "AMBIGUOUS_APU" if "GRAPHICS" in name and not any(c.isdigit() for c in name) else ""


def parse_client_version(output):
    # A running server may report a different version. Prefer the client warning.
    client = re.search(r"client version is (\S+)", output)
    server = re.search(r"ollama version is (\S+)", output)
    match = client or server
    if not match or not re.fullmatch(r"\d+(?:\.\d+){2}", match.group(1)):
        raise CompatibilityError("Cannot determine the installed Ollama client version.")
    return "v" + match.group(1)


def verify_asset(filename, asset):
    path = Path(filename)
    if asset.get("size") and path.stat().st_size != asset["size"]:
        raise ValueError(f"Incomplete download: {path.name}")
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        actual = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                actual.update(chunk)
        if actual.hexdigest() != digest[7:]:
            raise ValueError(f"SHA-256 verification failed: {path.name}")


def _safe_member(name, destination):
    # Reject Windows drive/UNC/ADS names as well as ../ paths on every host OS.
    windows = PureWindowsPath(name)
    parts = name.replace("\\", "/").split("/")
    if windows.root or windows.drive or any(p == ".." or ":" in p for p in parts):
        raise ValueError(f"Unsafe archive member: {name}")
    resolved = (Path(destination) / name.replace("\\", "/")).resolve()
    if not resolved.is_relative_to(Path(destination).resolve()):
        raise ValueError(f"Unsafe archive member: {name}")


def extract_archive(filename, destination):
    """Accept ZIP and 7z with traversal/link checks before extracting anything."""
    Path(destination).mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(filename):
        with zipfile.ZipFile(filename) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > MAX_EXTRACT_SIZE:
                raise ValueError("Archive exceeds the 16 GiB extraction limit.")
            for entry in archive.infolist():
                _safe_member(entry.filename, destination)
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("Archive links are unsupported.")
            archive.extractall(destination)
    else:
        import py7zr
        if not py7zr.is_7zfile(filename):
            raise ValueError("Expected a ZIP or 7z archive; download may be an HTML error page.")
        with py7zr.SevenZipFile(filename, max_extract_size=MAX_EXTRACT_SIZE) as archive:
            for entry in archive.list():
                _safe_member(entry.filename, destination)
            # py7zr metadata exposes symlinks/junctions through ArchiveFile.
            if any(entry.is_symlink or entry.is_junction for entry in archive.files):
                raise ValueError("Archive links are unsupported.")
            archive.extractall(path=destination)


def _find_one(root, filename):
    matches = [p for p in Path(root).rglob("*") if p.is_file() and p.name.lower() == filename.lower()]
    if len(matches) != 1:
        raise CompatibilityError(f"Expected one {filename} in archive, found {len(matches)}.")
    return matches[0]


def prepare_payload(framework_dir, gpu_dir, stage_dir, rocm_version):
    """Normalize full distributions, backend-only archives and nested GPU libs."""
    if rocm_version not in ROCM_VERSIONS:
        raise CompatibilityError("Unsupported ROCm SDK version.")
    hip = _find_one(framework_dir, "ggml-hip.dll")
    expected = "rocm_v7_1" if rocm_version == "7.1.1" else "rocm"
    versioned = next((p for p in hip.parents if re.fullmatch(r"rocm_v\d+_\d+", p.name)), None)
    if versioned and versioned.name != expected:
        raise CompatibilityError(f"Archive backend {versioned.name} does not match ROCm {rocm_version}.")
    backend = versioned or hip.parent
    stage = Path(stage_dir)
    target_backend = stage / "lib" / "ollama" / expected
    shutil.copytree(backend, target_backend)
    rocblas = _find_one(gpu_dir, "rocblas.dll")
    libraries = [p for p in Path(gpu_dir).rglob("library") if p.is_dir()]
    if len(libraries) != 1 or not any(p.is_file() for p in libraries[0].rglob("*")):
        raise CompatibilityError("GPU archive must contain one nonempty rocblas library directory.")
    shutil.copy2(rocblas, target_backend / "rocblas.dll")
    # Remove old kernels in the staged copy; mixing them causes unsupported GPU failures.
    old_library = target_backend / "rocblas" / "library"
    if old_library.exists():
        shutil.rmtree(old_library)
    shutil.copytree(libraries[0], old_library)
    targets = [Path("lib") / "ollama" / expected]
    # Full legacy archives need the matching executable and shared runtime DLLs.
    executables = list(Path(framework_dir).rglob("ollama.exe"))
    if len(executables) > 1:
        raise CompatibilityError("Framework archive contains multiple ollama.exe files.")
    if executables:
        for source in executables[0].parent.iterdir():
            if source.is_file() and (source.name.lower() == "ollama.exe" or source.suffix.lower() == ".dll"):
                shutil.copy2(source, stage / source.name)
                targets.append(Path(source.name))
    return targets


def _checked_target(root, relative):
    _safe_member(str(relative), root)
    if not str(relative) or str(relative) in (".", "/", "\\"):
        raise ValueError("Deployment target cannot be the installation root.")
    target = Path(root) / relative
    if target.is_symlink() or target.resolve() != target.absolute():
        raise ValueError(f"Linked deployment target is unsupported: {target}")
    return target


def _deployment_target(root, relative):
    # A backup manifest must never address models, unrelated libraries or the root.
    parts = str(relative).replace("\\", "/").split("/")
    backend = len(parts) == 3 and parts[:2] == ["lib", "ollama"] and parts[2] in ("rocm", "rocm_v7_1")
    binary = len(parts) == 1 and (parts[0] == "ollama.exe" or parts[0].lower().endswith(".dll"))
    if not (backend or binary):
        raise ValueError(f"Unsupported deployment target: {relative}")
    return _checked_target(root, relative)


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _remove(target):
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def deploy_payload(stage_dir, install_dir, targets):
    """Back up every affected target, then restore all of them on copy failure."""
    install = Path(install_dir).resolve()
    if not (install / "ollama.exe").is_file():
        raise ValueError("Select the installation folder containing ollama.exe.")
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("Expected unique deployment targets.")
    for relative in targets:
        _deployment_target(install, relative)
        if not (Path(stage_dir) / relative).exists():
            raise ValueError(f"Staged target is missing: {relative}")
    backup = _checked_target(install, ".amd-installer-backups") / uuid.uuid4().hex
    backup.mkdir(parents=True)
    entries = []
    try:
        for relative in targets:
            target = _checked_target(install, relative)
            existed = target.exists()
            if existed:
                _copy(target, backup / "files" / relative)
            entries.append({"path": str(relative), "existed": existed})
        (backup / "manifest.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(backup)
        raise
    try:
        for relative in targets:
            target = _checked_target(install, relative)
            _remove(target)
            _copy(Path(stage_dir) / relative, target)
    except Exception as error:
        try:
            restore_backup(install, backup)
        except Exception as rollback_error:
            raise OSError(f"Injection failed: {error}. Restore failed: {rollback_error}. "
                          f"Original files remain in {backup}") from error
        raise
    return backup


def latest_backup(install_dir):
    base = _checked_target(Path(install_dir).resolve(), ".amd-installer-backups")
    backups = list(base.glob("*/manifest.json"))
    return max(backups, key=lambda p: p.stat().st_mtime_ns).parent if backups else None


def restore_backup(install_dir, backup):
    install = Path(install_dir).resolve()
    backup = Path(backup).resolve()
    base = _checked_target(install, ".amd-installer-backups")
    if not backup.is_relative_to(base):
        raise ValueError("Backup must belong to the selected installation.")
    entries = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    # Validate the whole manifest before modifying the installation.
    for entry in entries:
        _deployment_target(install, entry["path"])
        if entry["existed"] and not (backup / "files" / entry["path"]).exists():
            raise ValueError("Incomplete backup; original files are missing.")
    for entry in entries:
        target = _checked_target(install, entry["path"])
        _remove(target)
        if entry["existed"]:
            _copy(backup / "files" / entry["path"], target)
    shutil.rmtree(backup)
