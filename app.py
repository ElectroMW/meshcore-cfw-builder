"""
MeshCore Firmware Web Builder
Clones the latest MeshCore from GitHub, applies custom flags, builds, and serves the merged .bin
"""

import os
import re
import json
import uuid
import shutil
import subprocess
import threading
import tempfile
import time
import queue
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context

import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)  # no per-request logs

app = Flask(__name__)

GITHUB_URL = "https://github.com/meshcore-dev/MeshCore.git"

# Allow operators to override the firmware source repository via environment variable.
# Example: docker run -e MESHCORE_REPO_URL=https://github.com/myfork/MeshCore.git ...
MESHCORE_REPO_URL = os.environ.get("MESHCORE_REPO_URL", GITHUB_URL).strip()

# Locate the PlatformIO executable — works on both Windows (dev) and Linux (Docker)
if os.name == "nt":  # Windows
    PIO_EXE = str(Path.home() / ".platformio" / "penv" / "Scripts" / "pio.exe")
else:               # Linux / Docker
    import shutil as _shutil
    PIO_EXE = _shutil.which("pio") or str(Path.home() / ".platformio" / "penv" / "bin" / "pio")

BUILDS_DIR = Path(tempfile.gettempdir()) / "meshcore_builds"
BUILDS_DIR.mkdir(exist_ok=True)

# Extra example folders bundled with this app that may not be in the GitHub repo
EXTRA_EXAMPLES_DIR = Path(__file__).parent / "extra_examples"

# ── Startup: wipe any leftover build dirs from previous runs ─────────────────
for _old in BUILDS_DIR.iterdir():
    try:
        shutil.rmtree(_old, ignore_errors=True)
    except Exception:
        pass

# ── Concurrency limit ───────────────────────────────────────────────────────
MAX_CONCURRENT_BUILDS = 2          # max pio processes running at the same time
build_semaphore = threading.Semaphore(MAX_CONCURRENT_BUILDS)

# ── Build registry ──────────────────────────────────────────────────────────
# job_id -> { status, log_queue, bin_path, error, env_id }
builds: dict[str, dict] = {}
builds_lock = threading.Lock()

# ── Variant / environment discovery ─────────────────────────────────────────
# Firmware types we expose, matched against env names in each variant ini.
# _repeater_room_hybrid is matched first (before _repeater) to avoid false match.
ENV_SUFFIXES = [
    ("_companion_radio_ble",     "Companion Radio \u2013 BLE",      "companion_radio_ble"),
    ("_companion_radio_wifi",    "Companion Radio \u2013 WiFi",     "companion_radio_wifi"),
    ("_companion_radio_usb",     "Companion Radio \u2013 USB",      "companion_radio_usb"),
    ("_room_server",             "Room Server",                  "room_server"),
    ("_repeater_room_hybrid",    "Repeater + Room Server",       "repeater_room_hybrid"),
    ("_repeater",                "Repeater",                     "repeater"),
]

# Map PlatformIO platform identifiers → short architecture names shown in the UI.
ARCH_ESP32 = "esp32"
ARCH_NRF52 = "nrf52"

PLATFORM_ARCH_MAP = {
    "espressif32": ARCH_ESP32,
    "nordicnrf52": ARCH_NRF52,
}

VARIANT_CACHE_LOCK         = threading.Lock()
VARIANT_READY              = threading.Event()

# Per-branch caches  – branch name → list-of-variant-dicts (or None while loading)
BRANCH_VARIANT_CACHE:     dict[str, list | None] = {}
BRANCH_ENV_TO_VARIANT:    dict[str, dict]        = {}  # env_id → variant folder
BRANCH_ENV_TYPE_MAP:      dict[str, dict]        = {}  # env_id → type_key
BRANCH_ENV_INJECT_INFO:   dict[str, dict]        = {}  # env_id → inject_info
BRANCH_ENV_ARCH_MAP:      dict[str, dict]        = {}  # env_id → arch

# Keep a plain reference to the default-branch cache for routes that don't specify a branch
VARIANT_CACHE: list | None = None   # None = not yet loaded (legacy alias for default branch)
ENV_TO_VARIANT: dict       = {}     # env_id → variant folder name  (default branch)
ENV_TYPE_MAP:   dict       = {}     # env_id → type_key             (default branch)
ENV_INJECT_INFO: dict      = {}     # env_id → inject_info          (default branch)
ENV_ARCH_MAP:   dict       = {}     # env_id → arch                 (default branch)


def _variant_folder_to_label(name: str) -> str:
    """heltec_v3 → 'Heltec V3',  lilygo_tbeam_supreme_SX1262 → 'LilyGo TBeam Supreme SX1262'"""
    UPPER_WORDS = {
        "sx1262", "sx1268", "sx1276", "ble", "usb", "gps", "nrf", "nrf52",
        "rp2040", "esp32", "e22", "ct62", "c3", "c6", "s3", "l1",
        "v2", "v3", "v4", "m1", "m2", "m3", "m5", "m6",
    }
    parts = re.split(r'[-_]', name)
    result = [p.upper() if p.lower() in UPPER_WORDS else p.capitalize() for p in parts if p]
    return ' '.join(result)


def _env_matches_suffix(env_name: str, suffix: str) -> bool:
    """Return True if env_name (possibly with trailing underscore) ends with suffix."""
    return env_name.rstrip('_').lower().endswith(suffix.lower())


def _detect_arch(content: str) -> str:
    """Return the short architecture name detected from a platformio.ini content string."""
    m = re.search(r'^\s*platform\s*=\s*(\S+)', content, re.MULTILINE | re.IGNORECASE)
    if m:
        # Strip any version specifier (e.g. "espressif32@6.5.0" → "espressif32")
        platform = m.group(1).split('@')[0].lower()
        return PLATFORM_ARCH_MAP.get(platform, platform)
    return ""


def _parse_github_repo(url: str):
    """
    Return (owner, repo) if *url* is a GitHub HTTPS URL, otherwise None.
    Handles both  https://github.com/owner/repo  and  …/owner/repo.git
    """
    m = re.match(r'https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$', url)
    if m:
        return m.group(1), m.group(2)
    return None


def _get_branches(repo_url: str) -> list[str]:
    """
    Return a sorted list of branch names for *repo_url*.
    Uses ``git ls-remote --heads`` so it works with any public Git host.
    Returns an empty list on any error.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", repo_url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )
        branches = []
        for line in result.stdout.splitlines():
            m = re.match(r'^[0-9a-f]+\trefs/heads/(.+)$', line)
            if m:
                branches.append(m.group(1))
        return sorted(branches)
    except Exception:
        return []


def _fetch_variant_ini(args):
    """Fetch a variant's platformio.ini from GitHub raw. Returns (folder, content|None)."""
    folder, owner, repo, branch = args
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/variants/{folder}/platformio.ini"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MeshCore-Builder/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return folder, resp.read().decode("utf-8")
    except Exception:
        return folder, None


def _discover_variants(branch: str = "main") -> list:
    """Fetch variants from GitHub; return sorted list of {id, label, envs} dicts."""
    gh = _parse_github_repo(MESHCORE_REPO_URL)
    if gh is None:
        raise ValueError(f"MESHCORE_REPO_URL is not a recognised GitHub HTTPS URL: {MESHCORE_REPO_URL!r}")
    owner, repo = gh
    tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    req = urllib.request.Request(tree_url, headers={"User-Agent": "MeshCore-Builder/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        tree = json.loads(resp.read())

    folders = [
        item["path"].split('/')[1]
        for item in tree["tree"]
        if re.match(r'^variants/[^/]+/platformio\.ini$', item["path"])
    ]

    results = {}
    args_list = [(folder, owner, repo, branch) for folder in folders]
    with ThreadPoolExecutor(max_workers=12) as pool:
        for folder, content in pool.map(_fetch_variant_ini, args_list):
            if not content:
                continue
            all_envs = re.findall(r'\[env:([^\]]+)\]', content)
            matching = []
            found_types = set()
            for suffix, label, type_key in ENV_SUFFIXES:
                for env in all_envs:
                    if _env_matches_suffix(env, suffix):
                        matching.append({"id": env, "label": label, "type": type_key})
                        found_types.add(type_key)
                        break

            # Synthesise a hybrid env for variants that have repeater + room_server
            # but no native repeater_room_hybrid env
            if ("repeater" in found_types and "room_server" in found_types
                    and "repeater_room_hybrid" not in found_types):
                repeater_entry = next(e for e in matching if e["type"] == "repeater")
                hybrid_id = re.sub(r'_repeater_?$', '_repeater_room_hybrid',
                                   repeater_entry["id"].rstrip('_'))
                base_sec  = _find_variant_base_section(content)
                matching.append({
                    "id":              hybrid_id,
                    "label":           "Repeater + Room Server",
                    "type":            "repeater_room_hybrid",
                    "inject":          True,
                    "base_section":    base_sec,
                    "repeater_env_id": repeater_entry["id"],
                })

            if matching:
                results[folder] = {"envs": matching, "arch": _detect_arch(content)}

    return [
        {
            "id":    folder,
            "label": _variant_folder_to_label(folder),
            "arch":  results[folder]["arch"],
            "envs":  results[folder]["envs"],
        }
        for folder in sorted(results, key=str.lower)
    ]


VARIANT_REFRESH_INTERVAL = 3600  # seconds between background refreshes

_DEFAULT_BRANCH = "main"   # updated to first branch in the repo list once branches are known


def _update_branch_cache(branch: str, data: list):
    """Store *data* in the per-branch cache and refresh the global default-branch aliases."""
    global VARIANT_CACHE, _DEFAULT_BRANCH
    e2v = {}
    etm = {}
    eii = {}
    eam = {}
    for v in data:
        for e in v["envs"]:
            e2v[e["id"]] = v["id"]
            etm[e["id"]] = e["type"]
            eam[e["id"]] = v.get("arch", ARCH_ESP32)
            if e.get("inject"):
                eii[e["id"]] = {
                    "base_section":    e["base_section"],
                    "repeater_env_id": e["repeater_env_id"],
                }
    with VARIANT_CACHE_LOCK:
        BRANCH_VARIANT_CACHE[branch]   = data
        BRANCH_ENV_TO_VARIANT[branch]  = e2v
        BRANCH_ENV_TYPE_MAP[branch]    = etm
        BRANCH_ENV_INJECT_INFO[branch] = eii
        BRANCH_ENV_ARCH_MAP[branch]    = eam
        # Keep legacy default-branch aliases in sync
        if branch == _DEFAULT_BRANCH:
            VARIANT_CACHE = data
            ENV_TO_VARIANT.clear()
            ENV_TO_VARIANT.update(e2v)
            ENV_TYPE_MAP.clear()
            ENV_TYPE_MAP.update(etm)
            ENV_INJECT_INFO.clear()
            ENV_INJECT_INFO.update(eii)
            ENV_ARCH_MAP.clear()
            ENV_ARCH_MAP.update(eam)


def _load_variants_background():
    global VARIANT_CACHE, _DEFAULT_BRANCH
    first_run = True
    while True:
        try:
            data = _discover_variants(branch=_DEFAULT_BRANCH)
        except Exception as exc:
            app.logger.error(f"Variant discovery failed: {exc}")
            data = None  # keep existing cache on failure

        if data is not None:
            _update_branch_cache(_DEFAULT_BRANCH, data)
            if first_run:
                VARIANT_READY.set()
                first_run = False

        time.sleep(VARIANT_REFRESH_INTERVAL)


threading.Thread(target=_load_variants_background, daemon=True).start()


# ── Configurable flags ───────────────────────────────────────────────────────
# section:   "$env"          → inject into [env:{env_id}]
#            "$variant_base" → inject into the first non-env section of variant ini
# env_types: "all" | list of type_key strings (values from ENV_SUFFIXES)
FLAGS = [
    # ── Node / Repeater / Room Server ─────────────────────────────────────────
    {"key": "ADVERT_NAME",    "label": "Advertised Name",  "default": "My Node",
     "file": "variant", "section": "$env", "group": "Node Settings", "quoted": True,
     "env_types": ["repeater", "room_server", "repeater_room_hybrid"]},
    {"key": "ADVERT_LAT",     "label": "Latitude",         "default": "0.0",
     "file": "variant", "section": "$env", "group": "Node Settings",
     "env_types": ["repeater", "room_server", "repeater_room_hybrid"]},
    {"key": "ADVERT_LON",     "label": "Longitude",        "default": "0.0",
     "file": "variant", "section": "$env", "group": "Node Settings",
     "env_types": ["repeater", "room_server", "repeater_room_hybrid"]},
    {"key": "ADMIN_PASSWORD", "label": "Admin Password",   "default": "password",
     "file": "variant", "section": "$env", "group": "Node Settings", "quoted": True,
     "env_types": ["repeater", "room_server", "repeater_room_hybrid"]},
    # ── Room Server / Hybrid ─────────────────────────────────────────────────
    {"key": "ROOM_PASSWORD",  "label": "Room Password",    "default": "hello",
     "file": "variant", "section": "$env", "group": "Room Server Settings", "quoted": True,
     "env_types": ["room_server", "repeater_room_hybrid"]},
    # ── BLE ───────────────────────────────────────────────────────────────────
    {"key": "BLE_PIN_CODE",   "label": "BLE PIN Code",     "default": "123456",
     "file": "variant", "section": "$env", "group": "BLE Settings",
     "validate": "digits6",
     "env_types": ["companion_radio_ble"]},
    # ── WiFi ──────────────────────────────────────────────────────────────────
    {"key": "WIFI_SSID",      "label": "WiFi SSID",        "default": "myssid",
     "file": "variant", "section": "$env", "group": "WiFi Settings", "quoted": True,
     "env_types": ["companion_radio_wifi"]},
    {"key": "WIFI_PWD",       "label": "WiFi Password",    "default": "mypwd",
     "file": "variant", "section": "$env", "group": "WiFi Settings", "quoted": True,
     "env_types": ["companion_radio_wifi"]},
]


# ── Flag injection helpers ───────────────────────────────────────────────────

def set_flag_in_file(filepath: Path, section: str, flag_key: str, raw_value: str):
    """
    Replace or append  -D FLAG_KEY=value  inside the named [section] of an INI file.
    raw_value is the literal text that should appear in the file after the '='.
    Uses lambda replacements so raw_value is never interpreted as a regex pattern.
    """
    content = filepath.read_text(encoding="utf-8")

    # Match -D FLAG_KEY=<anything to end of line, excluding trailing whitespace/comment>
    # This handles both  -D PIN=46  and  -D NAME='"hello world"'
    pattern = re.compile(r'-D\s+' + re.escape(flag_key) + r'=[^\n]*')

    if pattern.search(content):
        new_content = pattern.sub(
            lambda m: f"-D {flag_key}={raw_value}",
            content
        )
        filepath.write_text(new_content, encoding="utf-8")
        return

    # Not found – append inside the correct [section]'s build_flags block
    sec_pattern = re.compile(
        r'(\[' + re.escape(section) + r'\][^\[]*?)'
        r'(build_flags\s*=[^\n]*((\n[ \t]+[^\n]+)*))',
        re.DOTALL
    )

    def appender(m):
        return m.group(1) + m.group(2).rstrip() + f"\n  -D {flag_key}={raw_value}"

    new_content, n = sec_pattern.subn(appender, content)
    if n:
        filepath.write_text(new_content, encoding="utf-8")
        return

    # Last resort: add a build_flags key to the section
    sec_header = re.compile(r'(\[' + re.escape(section) + r'\]\n)')
    new_content, n = sec_header.subn(
        lambda m: m.group(1) + f"build_flags =\n  -D {flag_key}={raw_value}\n",
        content
    )
    if n:
        filepath.write_text(new_content, encoding="utf-8")


def _find_variant_base_section(content: str) -> str:
    """Return the first non-env [section] name found in the INI content."""
    for m in re.finditer(r'^\[([^\]]+)\]', content, re.MULTILINE):
        sec = m.group(1)
        if not sec.startswith('env:'):
            return sec
    return ""


def apply_custom_flags(repo_dir: Path, env_id: str, variant_folder: str,
                       env_type: str, custom: dict):
    """Apply user-supplied flag overrides to the cloned repo files."""
    root_ini    = repo_dir / "platformio.ini"
    variant_ini = repo_dir / "variants" / variant_folder / "platformio.ini"

    variant_base = ""
    if variant_ini.exists():
        variant_base = _find_variant_base_section(
            variant_ini.read_text(encoding="utf-8")
        )

    for flag in FLAGS:
        key = flag["key"]
        if key not in custom:
            continue
        value = str(custom[key]).strip()
        if not value:
            continue

        # Filter by env type
        et = flag.get("env_types", "all")
        if et != "all" and env_type not in et:
            continue

        quoted = flag.get("quoted", False)
        raw_value = ("'" + '"' + value + '"' + "'") if quoted else value

        # Resolve dynamic section placeholders
        sec_tmpl = flag["section"]
        if sec_tmpl == "$env":
            section = f"env:{env_id}"
        elif sec_tmpl == "$variant_base":
            section = variant_base
        else:
            section = sec_tmpl

        target_file = root_ini if flag["file"] == "root" else variant_ini
        if target_file.exists():
            set_flag_in_file(target_file, section, key, raw_value)


# ── Hybrid env injection ──────────────────────────────────────────────────

def _inject_hybrid_env(variant_ini: Path, env_id: str,
                       base_section: str, repeater_env_id: str):
    """
    Append a [env:{env_id}] block (repeater + room server hybrid) to the variant
    platformio.ini.  Build flags and src filter are derived from the existing
    repeater env so display-class and other board-specific settings carry over.
    """
    content = variant_ini.read_text(encoding="utf-8")

    # Extract the repeater env's section body (up to the next section header)
    rep_block_m = re.search(
        r'\[env:' + re.escape(repeater_env_id) + r'\](.*?)(?=\n\[|\Z)',
        content, re.DOTALL
    )
    rep_block = rep_block_m.group(1) if rep_block_m else ""

    # Pull out any -D DISPLAY_CLASS=... line from the repeater env
    display_flag = ""
    dm = re.search(r'(-D\s+DISPLAY_CLASS=[^\n]+)', rep_block)
    if dm:
        display_flag = f"\n  {dm.group(1).strip()}"

    # Pull display .cpp lines from the repeater's build_src_filter
    display_src = ""
    src_block_m = re.search(r'build_src_filter[^\n]*\n((?:[ \t]+[^\n]+\n)*)', rep_block)
    if src_block_m:
        for line in src_block_m.group(1).splitlines():
            if re.search(r'[Dd]isplay', line):
                display_src += f"\n  {line.strip()}"

    block = (
        f"\n[env:{env_id}]\n"
        f"extends = {base_section}\n"
        f"build_flags =\n"
        f"  ${{{base_section}.build_flags}}{display_flag}\n"
        f"  -D ADVERT_NAME='\"Room Repeater\"'\n"
        f"  -D ADVERT_LAT=0.0\n"
        f"  -D ADVERT_LON=0.0\n"
        f"  -D ADMIN_PASSWORD='\"password\"'\n"
        f"  -D ROOM_PASSWORD='\"hello\"'\n"
        f"  -D MAX_NEIGHBOURS=50\n"
        f"build_src_filter = ${{{base_section}.build_src_filter}}{display_src}\n"
        f"  +<../examples/simple_repeater_room_server>\n"
        f"lib_deps =\n"
        f"  ${{{base_section}.lib_deps}}\n"
        f"  ${{esp32_ota.lib_deps}}\n"
        f"  bakercp/CRC32 @ ^2.0.0\n"
    )
    variant_ini.write_text(content + block, encoding="utf-8")


# ── Build worker ─────────────────────────────────────────────────────────────

def run_build(job_id: str, env_id: str, variant_folder: str, env_type: str, custom_flags: dict,
              branch: str = "main", arch: str = ARCH_ESP32):
    job_dir = BUILDS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    q: queue.Queue = builds[job_id]["log_queue"]
    _semaphore_acquired = False

    def log(msg: str):
        q.put(msg)

    def is_cancelled() -> bool:
        with builds_lock:
            return builds.get(job_id, {}).get("cancelled", False)

    def set_proc(proc):
        """Store the current subprocess so cancel can kill it."""
        with builds_lock:
            if job_id in builds:
                builds[job_id]["current_proc"] = proc

    try:
        # ── Wait for a free build slot ────────────────────────────────────────
        if not build_semaphore.acquire(blocking=False):
            with builds_lock:
                builds[job_id]["status"] = "queued"
            log("[builder] Build queued — waiting for a free slot ...")
            # Poll with a timeout so a cancellation can unblock the wait
            while True:
                if is_cancelled():
                    log("[builder] Build cancelled while queued.")
                    return
                if build_semaphore.acquire(blocking=True, timeout=2):
                    _semaphore_acquired = True
                    break
            if is_cancelled():
                log("[builder] Build cancelled.")
                return
            with builds_lock:
                builds[job_id]["status"] = "running"
            log("[builder] Slot acquired, starting build.")
        else:
            _semaphore_acquired = True
            with builds_lock:
                builds[job_id]["status"] = "running"

        if is_cancelled():
            return

        # 1. Clone repo
        log(f"[builder] Cloning {MESHCORE_REPO_URL} (branch: {branch}) ...")
        clone_proc = subprocess.Popen(
            ["git", "clone", "--depth=1", "--branch", branch, MESHCORE_REPO_URL, str(job_dir / "repo")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace"
        )
        set_proc(clone_proc)
        clone_out, _ = clone_proc.communicate()
        if is_cancelled():
            return
        if clone_proc.returncode != 0:
            raise RuntimeError(f"git clone failed:\n{clone_out}")
        log("[builder] Clone complete.")

        repo_dir = job_dir / "repo"

        # 1a. Copy any bundled extra examples into the cloned repo
        if EXTRA_EXAMPLES_DIR.exists():
            for extra in EXTRA_EXAMPLES_DIR.iterdir():
                dest = repo_dir / "examples" / extra.name
                if not dest.exists():
                    shutil.copytree(str(extra), str(dest))
                    log(f"[builder] Injected bundled example: examples/{extra.name}")

        if is_cancelled():
            return

        # 1b. Inject hybrid env block if this is a synthetic repeater+room env
        with VARIANT_CACHE_LOCK:
            branch_inject = BRANCH_ENV_INJECT_INFO.get(branch, {})
            inject_info = branch_inject.get(env_id)
        if inject_info:
            variant_ini = repo_dir / "variants" / variant_folder / "platformio.ini"
            if variant_ini.exists():
                log("[builder] Injecting repeater+room hybrid env block ...")
                _inject_hybrid_env(
                    variant_ini,
                    env_id,
                    inject_info["base_section"],
                    inject_info["repeater_env_id"],
                )
                log("[builder] Hybrid env injected.")
            else:
                raise RuntimeError(f"Variant ini not found: {variant_ini}")

        if is_cancelled():
            return

        # 2. Apply custom flags
        log("[builder] Applying custom build flags ...")
        apply_custom_flags(repo_dir, env_id, variant_folder, env_type, custom_flags)
        log("[builder] Flags applied.")

        # 2b. Capture files for the debug viewer
        debug_files = {}

        # Variant platformio.ini (with any injected hybrid block + flag overrides)
        _v_ini = repo_dir / "variants" / variant_folder / "platformio.ini"
        if _v_ini.exists():
            debug_files[f"variants/{variant_folder}/platformio.ini"] = \
                _v_ini.read_text(encoding="utf-8")

        # Check whether examples/simple_repeater_room_server exists
        _hybrid_dir = repo_dir / "examples" / "simple_repeater_room_server"
        _bundled    = EXTRA_EXAMPLES_DIR / "simple_repeater_room_server"
        if _hybrid_dir.exists() and _hybrid_dir.is_dir():
            _files  = sorted(p.name for p in _hybrid_dir.iterdir())
            _source = "bundled with builder app" if _bundled.exists() else "present in GitHub repo"
            debug_files["examples/simple_repeater_room_server"] = (
                f"; Folder EXISTS ({_source})\n"
                f"; Path: {_hybrid_dir}\n"
                f";\n"
                f"; Files:\n" +
                "".join(f";   {f}\n" for f in _files)
            )
        else:
            debug_files["examples/simple_repeater_room_server"] = (
                f"; *** Folder NOT FOUND ***\n"
                f"; Checked: {_hybrid_dir}\n"
                f"; Bundled copy: {'FOUND at ' + str(_bundled) if _bundled.exists() else 'NOT FOUND'}\n"
                f"; This firmware type will fail to build.\n"
            )

        with builds_lock:
            if job_id in builds:
                builds[job_id]["debug_files"] = debug_files

        if is_cancelled():
            return

        # 3. Run PlatformIO build
        log(f"[builder] Starting build for env: {env_id} ...")
        pio_cmd = [PIO_EXE, "run", "-e", env_id, "-j", "2"]
        if arch != ARCH_NRF52:
            pio_cmd += ["-t", "mergebin"]
        proc = subprocess.Popen(
            pio_cmd,
            cwd=str(repo_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        set_proc(proc)
        for line in proc.stdout:
            if is_cancelled():
                proc.kill()
                return
            log(line.rstrip())
        proc.wait()

        if is_cancelled():
            return

        if proc.returncode != 0:
            raise RuntimeError(f"PlatformIO build failed (exit {proc.returncode})")

        # 4. Find output firmware file
        build_dir = repo_dir / ".pio" / "build" / env_id
        if arch == ARCH_NRF52:
            # nRF52 produces a .hex file (no mergebin step)
            bin_path = build_dir / "firmware.hex"
            if not bin_path.exists():
                matches = list(build_dir.glob("*.hex"))
                if not matches:
                    raise RuntimeError("Build succeeded but firmware .hex not found.")
                bin_path = matches[0]
        else:
            bin_path = build_dir / "firmware-merged.bin"
            if not bin_path.exists():
                # fallback - look for any merged bin
                matches = list(build_dir.glob("*merged*.bin"))
                if not matches:
                    raise RuntimeError("Build succeeded but merged .bin not found.")
                bin_path = matches[0]

        with builds_lock:
            builds[job_id]["status"] = "done"
            builds[job_id]["bin_path"] = str(bin_path)

        log(f"[builder] ✓ Build complete! {bin_path.name}")

    except Exception as exc:
        if not is_cancelled():
            with builds_lock:
                builds[job_id]["status"] = "error"
                builds[job_id]["error"] = str(exc)
            log(f"[builder] ✗ Error: {exc}")
    finally:
        if _semaphore_acquired:
            build_semaphore.release()
        q.put(None)  # sentinel


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", flags=FLAGS)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/credits")
def credits():
    return render_template("credits.html")


@app.route("/api/branches")
def api_branches():
    """Return the sorted list of branch names for the configured firmware repo."""
    branches = _get_branches(MESHCORE_REPO_URL)
    return jsonify({"branches": branches, "repo_url": MESHCORE_REPO_URL})


@app.route("/api/variants")
def api_variants():
    """Return the list of hardware variants and their available envs.

    Optional query parameter:
        branch  – which branch to fetch variants for (default: the background-loaded branch).
                  If the requested branch is not yet cached a synchronous fetch is performed.
    """
    branch = request.args.get("branch", "").strip() or _DEFAULT_BRANCH
    with VARIANT_CACHE_LOCK:
        data = BRANCH_VARIANT_CACHE.get(branch)

    if data is None:
        # First request for this branch – check if the default is still loading
        if branch == _DEFAULT_BRANCH and not VARIANT_READY.is_set():
            return jsonify({"status": "loading", "variants": []})
        # Fetch on demand for non-default (or never-loaded default) branches
        try:
            data = _discover_variants(branch=branch)
            _update_branch_cache(branch, data)
        except Exception as exc:
            app.logger.error(f"Variant discovery for branch {branch!r} failed: {exc}")
            return jsonify({"status": "error", "error": str(exc), "variants": []}), 502

    return jsonify({"status": "ready", "variants": data})


@app.route("/api/flags")
def api_flags():
    return jsonify(FLAGS)


@app.route("/api/build", methods=["POST"])
def api_build():
    data = request.get_json(force=True)
    env_id = data.get("env")
    branch = data.get("branch", "").strip() or _DEFAULT_BRANCH

    with VARIANT_CACHE_LOCK:
        branch_e2v  = BRANCH_ENV_TO_VARIANT.get(branch, ENV_TO_VARIANT)
        branch_etm  = BRANCH_ENV_TYPE_MAP.get(branch, ENV_TYPE_MAP)
        branch_eam  = BRANCH_ENV_ARCH_MAP.get(branch, ENV_ARCH_MAP)
        variant_folder = branch_e2v.get(env_id)
        env_type       = branch_etm.get(env_id, "")
        env_arch       = branch_eam.get(env_id, ARCH_ESP32)

    if not variant_folder:
        if not VARIANT_READY.is_set():
            return jsonify({"error": "Variants still loading – please wait and try again"}), 503
        return jsonify({"error": "Invalid env"}), 400

    custom_flags = data.get("flags", {})
    job_id = str(uuid.uuid4())

    with builds_lock:
        builds[job_id] = {
            "status": "running",
            "log_queue": queue.Queue(),
            "bin_path": None,
            "error": None,
            "env_id": env_id,
            "variant_folder": variant_folder,
            "cancelled": False,
            "current_proc": None,
        }

    t = threading.Thread(
        target=run_build,
        args=(job_id, env_id, variant_folder, env_type, custom_flags, branch, env_arch),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/debug/<job_id>")
def api_debug(job_id: str):
    """Return captured ini file contents for the debug viewer."""
    with builds_lock:
        if job_id not in builds:
            return jsonify({"error": "Unknown job"}), 404
        files = builds[job_id].get("debug_files")
    if files is None:
        return jsonify({"ready": False, "files": {}})
    return jsonify({"ready": True, "files": files})


@app.route("/api/log/<job_id>")
def api_log(job_id: str):
    """Server-Sent Events stream of build log lines."""
    with builds_lock:
        if job_id not in builds:
            return jsonify({"error": "Unknown job"}), 404
        q: queue.Queue = builds[job_id]["log_queue"]

    def generate():
        while True:
            try:
                line = q.get(timeout=30)
            except queue.Empty:
                # Check if the job is still alive (queued or running) — if so,
                # send a keepalive comment and keep waiting indefinitely.
                with builds_lock:
                    status = builds.get(job_id, {}).get("status", "gone")
                if status in ("queued", "running"):
                    yield ": keepalive\n\n"
                    continue
                # Job is gone, done, error, or cancelled — stop streaming.
                yield "data: [timeout]\n\n"
                break
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {json.dumps(line)}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id: str):
    """Cancel a queued or running build. Called via sendBeacon when the page unloads."""
    with builds_lock:
        if job_id not in builds:
            return jsonify({"ok": True})  # already gone
        job = builds[job_id]
        if job["status"] in ("done", "error", "cancelled"):
            return jsonify({"ok": True})  # nothing to do
        job["cancelled"] = True
        job["status"] = "cancelled"
        proc = job.get("current_proc")

    # Kill the subprocess outside the lock to avoid blocking
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass

    # Clean up temp dir and job record (best-effort; the thread may still be winding down)
    job_dir = BUILDS_DIR / job_id

    def _cleanup():
        shutil.rmtree(job_dir, ignore_errors=True)
        with builds_lock:
            builds.pop(job_id, None)

    threading.Thread(target=_cleanup, daemon=True).start()

    return jsonify({"ok": True})


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    with builds_lock:
        if job_id not in builds:
            return jsonify({"error": "Unknown job"}), 404
        job = builds[job_id]
        env_id = job.get("env_id", "")

        # Calculate queue position (1-indexed) among jobs with status "queued"
        queue_position = None
        if job["status"] == "queued":
            pos = 0
            for jid, j in builds.items():
                if j["status"] == "queued":
                    pos += 1
                if jid == job_id:
                    break
            queue_position = pos

        return jsonify({
            "status":         job["status"],
            "error":          job["error"],
            "filename":       _env_filename(env_id),
            "queue_position": queue_position,
        })


def _env_filename(env_id: str) -> str:
    """Generate a download filename from env_id."""
    return f"meshcore_{env_id}.bin"


def _env_display_label(env_id: str) -> str:
    """Return 'Variant Label – Firmware Type' for an env_id."""
    with VARIANT_CACHE_LOCK:
        folder = ENV_TO_VARIANT.get(env_id, "")
        for v in (VARIANT_CACHE or []):
            if v["id"] == folder:
                for e in v["envs"]:
                    if e["id"] == env_id:
                        return f"{v['label']} – {e['label']}"
    return env_id

@app.route("/api/download/<job_id>")
def api_download(job_id: str):
    with builds_lock:
        if job_id not in builds:
            return jsonify({"error": "Unknown job"}), 404
        job = dict(builds[job_id])  # copy so we can delete safely

    if job["status"] != "done" or not job["bin_path"]:
        return jsonify({"error": "Build not ready"}), 400

    bin_path = Path(job["bin_path"])
    env_id = job.get("env_id", "")
    download_name = _env_filename(env_id)

    # Read binary into memory so we can clean up the temp dir immediately
    bin_data = bin_path.read_bytes()

    # ── Privacy: wipe all build artefacts and job record right away ──────────
    job_dir = BUILDS_DIR / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
    with builds_lock:
        builds.pop(job_id, None)

    return send_file(
        __import__('io').BytesIO(bin_data),
        as_attachment=True,
        download_name=download_name,
        mimetype="application/octet-stream"
    )


@app.route("/api/manifest/<job_id>")
def api_manifest(job_id: str):
    """
    Returns an esp-web-tools manifest JSON for the given build job.
    The merged .bin is flashed at offset 0x0 (it already includes bootloader + partitions).
    """
    with builds_lock:
        if job_id not in builds:
            return jsonify({"error": "Unknown job"}), 404
        job = builds[job_id]

    if job["status"] != "done" or not job["bin_path"]:
        return jsonify({"error": "Build not ready"}), 400

    env_id  = job.get("env_id", "")
    label   = _env_display_label(env_id)

    manifest = {
        "name": f"MeshCore – {label}",
        "version": "custom-build",
        "builds": [
            {
                "chipFamily": "ESP32-S3",
                "parts": [
                    {
                        "path": f"/api/firmware/{job_id}",
                        "offset": 0
                    }
                ]
            }
        ]
    }
    resp = jsonify(manifest)
    # esp-web-tools fetches the manifest cross-origin from the button element
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/firmware/<job_id>")
def api_firmware(job_id: str):
    """Serves the raw merged .bin for esp-web-tools, then wipes all build artefacts."""
    with builds_lock:
        if job_id not in builds:
            return jsonify({"error": "Unknown job"}), 404
        job = dict(builds[job_id])

    if job["status"] != "done" or not job["bin_path"]:
        return jsonify({"error": "Build not ready"}), 400

    bin_path = Path(job["bin_path"])
    bin_data = bin_path.read_bytes()

    # Privacy: wipe artefacts and job record immediately
    job_dir = BUILDS_DIR / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
    with builds_lock:
        builds.pop(job_id, None)

    resp = send_file(
        __import__('io').BytesIO(bin_data),
        mimetype="application/octet-stream"
    )
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))

    # HTTPS support: provide SSL_CERT + SSL_KEY paths for a real certificate,
    # or set SSL_ADHOC=false to disable HTTPS entirely.
    # By default (SSL_ADHOC not set to false and no cert/key pair supplied),
    # Flask uses a self-signed certificate via pyopenssl.
    ssl_context = None
    ssl_cert = os.environ.get("SSL_CERT", "").strip()
    ssl_key  = os.environ.get("SSL_KEY",  "").strip()
    if ssl_cert and ssl_key:
        ssl_context = (ssl_cert, ssl_key)
    elif os.environ.get("SSL_ADHOC", "true").strip().lower() in ("0", "false", "no"):
        ssl_context = None  # HTTPS explicitly disabled
    else:
        ssl_context = "adhoc"

    url_scheme = "https" if ssl_context else "http"
    print(f"Using pio: {PIO_EXE}")
    print(f"Build temp dir: {BUILDS_DIR}")
    print(f"Listening on {url_scheme}://{host}:{port}")
    app.run(host=host, port=port, debug=False, ssl_context=ssl_context)
