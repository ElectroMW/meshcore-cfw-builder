"""
Backend API tests for MeshCore firmware builder.

Tests cover all Flask routes without requiring PlatformIO or network access.
Build jobs are injected directly into the builds registry using fixtures
defined in conftest.py.
"""

import io
import json
import queue
import zipfile

import pytest

import app as app_module
from app import ARCH_ESP32, ARCH_NRF52
from tests.conftest import make_done_job


# ── Index / static routes ─────────────────────────────────────────────────────

class TestStaticRoutes:
    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"MeshCore" in resp.data

    def test_privacy_returns_200(self, client):
        resp = client.get("/privacy")
        assert resp.status_code == 200

    def test_credits_returns_200(self, client):
        resp = client.get("/credits")
        assert resp.status_code == 200


# ── /api/flags ────────────────────────────────────────────────────────────────

class TestApiFlags:
    def test_returns_list(self, client):
        resp = client.get("/api/flags")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_flags_have_required_keys(self, client):
        data = client.get("/api/flags").get_json()
        for flag in data:
            assert "key" in flag
            assert "label" in flag
            assert "default" in flag


# ── /api/variants ─────────────────────────────────────────────────────────────

class TestApiVariants:
    def test_returns_variants(self, client):
        resp = client.get("/api/variants?branch=main")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ready"
        variants = data["variants"]
        assert isinstance(variants, list)
        assert len(variants) > 0

    def test_variant_has_arch(self, client):
        data = client.get("/api/variants?branch=main").get_json()
        for v in data["variants"]:
            assert "arch" in v
            assert v["arch"] in (ARCH_ESP32, ARCH_NRF52, "rp2040", "stm32")


# ── /api/build ────────────────────────────────────────────────────────────────

class TestApiBuild:
    def test_invalid_env_returns_400(self, client):
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "nonexistent_env", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_valid_env_returns_job_id(self, client, monkeypatch):
        # Prevent the actual build thread from doing anything
        monkeypatch.setattr(
            app_module.threading.Thread, "start", lambda self: None
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "job_id" in data
        assert data["job_id"]


# ── /api/status ───────────────────────────────────────────────────────────────

class TestApiStatus:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/status/nonexistent-job-id")
        assert resp.status_code == 404

    def test_done_esp32_job(self, client, done_esp32_job):
        resp = client.get(f"/api/status/{done_esp32_job}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["filename"].endswith(".bin")
        assert data["has_zip"] is False
        assert data["zip_filename"] is None

    def test_done_nrf52_job_has_zip(self, client, done_nrf52_job):
        resp = client.get(f"/api/status/{done_nrf52_job}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["filename"].endswith(".hex")
        assert data["has_zip"] is True
        assert data["zip_filename"] is not None
        assert data["zip_filename"].endswith(".zip")

    def test_queued_job_has_queue_position(self, client):
        job_id = "queued-test-job"
        with app_module.builds_lock:
            app_module.builds[job_id] = {
                "status": "queued",
                "log_queue": queue.Queue(),
                "bin_path": None,
                "zip_path": None,
                "error": None,
                "env_id": "heltec_v3_repeater",
                "arch": ARCH_ESP32,
                "cancelled": False,
                "current_proc": None,
                "debug_files": {},
            }
        try:
            resp = client.get(f"/api/status/{job_id}")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "queued"
            assert data["queue_position"] is not None
        finally:
            with app_module.builds_lock:
                app_module.builds.pop(job_id, None)


# ── /api/download ─────────────────────────────────────────────────────────────

class TestApiDownload:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/download/nonexistent-job-id")
        assert resp.status_code == 404

    def test_esp32_download_returns_bin(self, client, done_esp32_job):
        resp = client.get(f"/api/download/{done_esp32_job}")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        cd = resp.headers.get("Content-Disposition", "")
        assert ".bin" in cd

    def test_nrf52_download_returns_hex(self, client, done_nrf52_job):
        resp = client.get(f"/api/download/{done_nrf52_job}")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        cd = resp.headers.get("Content-Disposition", "")
        assert ".hex" in cd

    def test_download_cleans_up_job(self, client, done_esp32_job):
        client.get(f"/api/download/{done_esp32_job}")
        with app_module.builds_lock:
            assert done_esp32_job not in app_module.builds


# ── /api/download_zip ─────────────────────────────────────────────────────────

class TestApiDownloadZip:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/download_zip/nonexistent-job-id")
        assert resp.status_code == 404

    def test_esp32_job_returns_400(self, client, done_esp32_job):
        """ESP32 builds don't produce a zip."""
        resp = client.get(f"/api/download_zip/{done_esp32_job}")
        assert resp.status_code == 400

    def test_nrf52_download_zip_returns_zip(self, client, done_nrf52_job):
        resp = client.get(f"/api/download_zip/{done_nrf52_job}")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        cd = resp.headers.get("Content-Disposition", "")
        assert ".zip" in cd

    def test_nrf52_zip_content_contains_hex(self, client, done_nrf52_job):
        """The downloaded zip must contain a .hex file."""
        resp = client.get(f"/api/download_zip/{done_nrf52_job}")
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        names = zf.namelist()
        assert any(n.endswith(".hex") for n in names), f"No .hex found in zip: {names}"

    def test_nrf52_zip_does_not_clean_up_job(self, client, done_nrf52_job):
        """Downloading the zip must NOT wipe the build job (firmware fetch should do that)."""
        client.get(f"/api/download_zip/{done_nrf52_job}")
        with app_module.builds_lock:
            assert done_nrf52_job in app_module.builds


# ── /api/firmware ─────────────────────────────────────────────────────────────

class TestApiFirmware:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/firmware/nonexistent-job-id")
        assert resp.status_code == 404

    def test_esp32_firmware_returns_binary(self, client, done_esp32_job):
        resp = client.get(f"/api/firmware/{done_esp32_job}")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert len(resp.data) > 0

    def test_firmware_cleans_up_job(self, client, done_esp32_job):
        client.get(f"/api/firmware/{done_esp32_job}")
        with app_module.builds_lock:
            assert done_esp32_job not in app_module.builds

    def test_firmware_cors_header(self, client, done_esp32_job):
        resp = client.get(f"/api/firmware/{done_esp32_job}")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


# ── /api/debug ────────────────────────────────────────────────────────────────

class TestApiDebug:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/debug/nonexistent-job-id")
        assert resp.status_code == 404

    def test_known_job_returns_debug_info(self, client, done_esp32_job):
        resp = client.get(f"/api/debug/{done_esp32_job}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "ready" in data
        assert "files" in data


# ── /api/cancel ───────────────────────────────────────────────────────────────

class TestApiCancel:
    def test_cancel_unknown_job_returns_ok(self, client):
        resp = client.post("/api/cancel/nonexistent-job-id")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_cancel_running_job(self, client):
        job_id = "running-cancel-test"
        with app_module.builds_lock:
            app_module.builds[job_id] = {
                "status": "running",
                "log_queue": queue.Queue(),
                "bin_path": None,
                "zip_path": None,
                "error": None,
                "env_id": "heltec_v3_repeater",
                "arch": ARCH_ESP32,
                "cancelled": False,
                "current_proc": None,
                "debug_files": {},
            }
        resp = client.post(f"/api/cancel/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


# ── /api/manifest ─────────────────────────────────────────────────────────────

class TestApiManifest:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/manifest/nonexistent-job-id")
        assert resp.status_code == 404

    def test_esp32_manifest_is_valid_json(self, client, done_esp32_job):
        resp = client.get(f"/api/manifest/{done_esp32_job}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "name" in data
        assert "builds" in data
        assert isinstance(data["builds"], list)


# ── Internal helpers ──────────────────────────────────────────────────────────

class TestHelpers:
    def test_env_filename_esp32(self):
        from app import _env_filename
        assert _env_filename("heltec_v3_repeater", ARCH_ESP32).endswith(".bin")

    def test_env_filename_nrf52(self):
        from app import _env_filename
        assert _env_filename("rak4631_repeater", ARCH_NRF52).endswith(".hex")

    def test_detect_arch_from_extends(self):
        from app import _detect_arch
        ini = "[env:myenv]\nextends = nrf52_base\n"
        assert _detect_arch(ini) == ARCH_NRF52

    def test_detect_arch_from_platform(self):
        from app import _detect_arch
        ini = "[env:myenv]\nplatform = espressif32\n"
        assert _detect_arch(ini) == ARCH_ESP32

    def test_variant_folder_to_label(self):
        from app import _variant_folder_to_label
        assert _variant_folder_to_label("heltec_v3") == "Heltec V3"
        assert "SX1262" in _variant_folder_to_label("lilygo_tbeam_sx1262")


# ── _fix_pio_packages ─────────────────────────────────────────────────────────

class TestFixPioPackages:
    """Unit tests for the corrupt-package-manifest repair helper."""

    def _packages_dir(self, tmp_path: "Path") -> "Path":
        d = tmp_path / ".platformio" / "packages"
        d.mkdir(parents=True)
        return d

    def test_no_packages_dir_returns_empty(self, tmp_path, monkeypatch):
        from app import _fix_pio_packages
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nonexistent")
        assert _fix_pio_packages() == []

    def test_valid_package_not_removed(self, tmp_path, monkeypatch):
        from app import _fix_pio_packages
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        pkg = self._packages_dir(tmp_path) / "toolchain-xtensa-esp32"
        pkg.mkdir()
        (pkg / "package.json").write_text('{"name": "toolchain-xtensa-esp32", "version": "1.0.0"}')
        result = _fix_pio_packages()
        assert result == []
        assert pkg.exists()

    def test_missing_manifest_removed(self, tmp_path, monkeypatch):
        from app import _fix_pio_packages
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        pkg = self._packages_dir(tmp_path) / "toolchain-xtensa-esp32s3"
        pkg.mkdir()
        # No package.json created — simulates a failed/interrupted download
        result = _fix_pio_packages()
        assert "toolchain-xtensa-esp32s3" in result
        assert not pkg.exists()

    def test_empty_manifest_removed(self, tmp_path, monkeypatch):
        from app import _fix_pio_packages
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        pkg = self._packages_dir(tmp_path) / "toolchain-xtensa-esp32s3"
        pkg.mkdir()
        (pkg / "package.json").write_text("")
        result = _fix_pio_packages()
        assert "toolchain-xtensa-esp32s3" in result
        assert not pkg.exists()

    def test_invalid_json_manifest_removed(self, tmp_path, monkeypatch):
        from app import _fix_pio_packages
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        pkg = self._packages_dir(tmp_path) / "toolchain-xtensa-esp32s3"
        pkg.mkdir()
        (pkg / "package.json").write_text("{not valid json")
        result = _fix_pio_packages()
        assert "toolchain-xtensa-esp32s3" in result
        assert not pkg.exists()

    def test_mixed_packages(self, tmp_path, monkeypatch):
        """Valid packages are kept; corrupt ones are removed."""
        from app import _fix_pio_packages
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        pkgs_dir = self._packages_dir(tmp_path)

        good = pkgs_dir / "tool-cmake"
        good.mkdir()
        (good / "package.json").write_text('{"name": "tool-cmake"}')

        corrupt = pkgs_dir / "toolchain-xtensa-esp32s3"
        corrupt.mkdir()
        # empty manifest

        result = _fix_pio_packages()
        assert result == ["toolchain-xtensa-esp32s3"]
        assert good.exists()
        assert not corrupt.exists()

    def test_non_directory_entries_ignored(self, tmp_path, monkeypatch):
        """Regular files inside packages/ do not trigger removal."""
        from app import _fix_pio_packages
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        pkgs_dir = self._packages_dir(tmp_path)
        (pkgs_dir / "some_file.txt").write_text("hello")
        result = _fix_pio_packages()
        assert result == []


# ── Build cache ───────────────────────────────────────────────────────────────

class TestBuildCache:
    """Unit tests for the firmware build result cache.

    The cache key is composed of four independent dimensions:
      - hardware variant  (variant folder name, e.g. "heltec_v3")
      - firmware type     (e.g. "repeater", "room_server")
      - branch            (e.g. "main", "develop")
      - git commit hash   (resolved via git ls-remote; "" when unavailable)

    Builds that use any custom flags are never written to or read from the
    cache — they are always built fresh.
    """

    def _prime_cache(self, tmp_path, variant, firmware_type, branch,
                     arch=ARCH_ESP32, commit=""):
        """Create a cache entry for the given parameters and return the cache key."""
        from app import _build_cache_key, BUILD_CACHE, BUILD_CACHE_LOCK

        key = _build_cache_key(variant, firmware_type, branch, commit)
        cache_dir = tmp_path / "cache" / key
        cache_dir.mkdir(parents=True)

        ext = ".hex" if arch == ARCH_NRF52 else ".bin"
        bin_file = cache_dir / f"firmware{ext}"
        bin_file.write_bytes(b"\xca\xfe\xba\xbe" * 4)

        entry = {
            "bin_path": str(bin_file),
            "zip_path": None,
            "arch": arch,
            "env_id": f"{variant}_{firmware_type}",
        }
        with BUILD_CACHE_LOCK:
            BUILD_CACHE[key] = entry
        return key

    def teardown_method(self):
        """Remove any cache entries and associated files added by tests."""
        import shutil as _shutil
        import app as app_module
        with app_module.BUILD_CACHE_LOCK:
            for entry in app_module.BUILD_CACHE.values():
                try:
                    from pathlib import Path as _Path
                    bin_parent = _Path(entry["bin_path"]).parent
                    if bin_parent.is_dir():
                        _shutil.rmtree(str(bin_parent), ignore_errors=True)
                except Exception:
                    pass
            app_module.BUILD_CACHE.clear()

    # ── Key unit tests ────────────────────────────────────────────────────────

    def test_cache_key_is_deterministic(self):
        from app import _build_cache_key
        commit = "a" * 40
        k1 = _build_cache_key("heltec_v3", "repeater", "main", commit)
        k2 = _build_cache_key("heltec_v3", "repeater", "main", commit)
        assert k1 == k2

    def test_cache_key_differs_for_different_variant(self):
        from app import _build_cache_key
        k1 = _build_cache_key("heltec_v3", "repeater", "main", "")
        k2 = _build_cache_key("rak4631",   "repeater", "main", "")
        assert k1 != k2

    def test_cache_key_differs_for_different_firmware_type(self):
        from app import _build_cache_key
        k1 = _build_cache_key("heltec_v3", "repeater",    "main", "")
        k2 = _build_cache_key("heltec_v3", "room_server", "main", "")
        assert k1 != k2

    def test_cache_key_differs_for_different_branch(self):
        from app import _build_cache_key
        k1 = _build_cache_key("heltec_v3", "repeater", "main",    "")
        k2 = _build_cache_key("heltec_v3", "repeater", "develop", "")
        assert k1 != k2

    def test_cache_key_differs_for_different_commit(self):
        from app import _build_cache_key
        k1 = _build_cache_key("heltec_v3", "repeater", "main", "a" * 40)
        k2 = _build_cache_key("heltec_v3", "repeater", "main", "b" * 40)
        assert k1 != k2

    def test_cache_key_with_commit_differs_from_without(self):
        from app import _build_cache_key
        k1 = _build_cache_key("heltec_v3", "repeater", "main", "")
        k2 = _build_cache_key("heltec_v3", "repeater", "main", "a" * 40)
        assert k1 != k2

    def test_cache_key_all_dimensions_independent(self):
        """Changing any single dimension produces a distinct key."""
        from app import _build_cache_key
        base         = _build_cache_key("heltec_v3", "repeater",    "main",    "")
        diff_variant = _build_cache_key("rak4631",   "repeater",    "main",    "")
        diff_fw_type = _build_cache_key("heltec_v3", "room_server", "main",    "")
        diff_branch  = _build_cache_key("heltec_v3", "repeater",    "develop", "")
        diff_commit  = _build_cache_key("heltec_v3", "repeater",    "main",    "a" * 40)
        assert len({base, diff_variant, diff_fw_type, diff_branch, diff_commit}) == 5

    # ── Parametrized key-uniqueness tests ─────────────────────────────────────

    def test_all_key_combinations_are_unique(self):
        """Every combination of test dimensions must produce a distinct cache key."""
        from app import _build_cache_key
        combos = [
            ("heltec_v3", "repeater",    "main",    ""),
            ("heltec_v3", "repeater",    "develop", ""),
            ("heltec_v3", "room_server", "main",    ""),
            ("rak4631",   "repeater",    "main",    ""),
            ("heltec_v3", "repeater",    "main",    "a" * 40),
            ("rak4631",   "repeater",    "main",    "b" * 40),
            ("heltec_v3", "room_server", "develop", "a" * 40),
            ("rak4631",   "repeater",    "develop", "b" * 40),
        ]
        keys = [_build_cache_key(*c) for c in combos]
        assert len(set(keys)) == len(keys), "Duplicate cache key detected"

    # ── Cache hit / miss via /api/build ───────────────────────────────────────

    def test_cache_hit_returns_done_job_immediately(self, client, tmp_path):
        """Cache hit must return a job already in 'done' state — no build thread."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main")

        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        job_id = resp.get_json()["job_id"]
        with app_module.builds_lock:
            job = app_module.builds.get(job_id)
        assert job is not None
        assert job["status"] == "done"

    def test_cache_hit_job_has_correct_bin_path(self, client, tmp_path):
        """Cached job bin_path must point to an existing file."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main")

        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        job_id = resp.get_json()["job_id"]
        with app_module.builds_lock:
            job = app_module.builds[job_id]
        from pathlib import Path
        assert Path(job["bin_path"]).exists()

    def test_cache_miss_starts_build_thread(self, client, monkeypatch):
        """Cache miss must start a build thread."""
        from app import BUILD_CACHE, BUILD_CACHE_LOCK
        with BUILD_CACHE_LOCK:
            BUILD_CACHE.clear()

        thread_started = []
        monkeypatch.setattr(
            app_module.threading.Thread, "start",
            lambda self: thread_started.append(True),
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert thread_started, "Build thread was not started on cache miss"

    def test_status_endpoint_for_cached_job(self, client, tmp_path):
        """The /api/status endpoint must report 'done' for a cache-hit job."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main")

        build_resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        job_id = build_resp.get_json()["job_id"]
        assert client.get(f"/api/status/{job_id}").get_json()["status"] == "done"

    # ── Commit-hash cache behaviour ───────────────────────────────────────────

    def test_cache_miss_for_different_commit(self, client, tmp_path, monkeypatch):
        """Cache entry stored under commit_A must not serve a request for commit_B."""
        commit_a = "a" * 40
        commit_b = "b" * 40
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main", commit=commit_a)
        monkeypatch.setattr(app_module, "_get_branch_head_commit", lambda branch: commit_b)

        thread_started = []
        monkeypatch.setattr(
            app_module.threading.Thread, "start",
            lambda self: thread_started.append(True),
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert thread_started, "Build thread should start when commit differs from cached entry"

    def test_cache_hit_with_matching_commit(self, client, tmp_path, monkeypatch):
        """Cache entry for a specific commit is served when the same commit is current."""
        commit = "a" * 40
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main", commit=commit)
        monkeypatch.setattr(app_module, "_get_branch_head_commit", lambda branch: commit)

        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        job_id = resp.get_json()["job_id"]
        with app_module.builds_lock:
            job = app_module.builds.get(job_id)
        assert job is not None
        assert job["status"] == "done"

    def test_cache_miss_when_ls_remote_fails(self, client, tmp_path, monkeypatch):
        """When ls-remote returns '' (network failure), entries stored with a real
        commit hash must not be served."""
        commit = "d" * 40
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main", commit=commit)
        monkeypatch.setattr(app_module, "_get_branch_head_commit", lambda branch: "")

        thread_started = []
        monkeypatch.setattr(
            app_module.threading.Thread, "start",
            lambda self: thread_started.append(True),
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert thread_started, "Build should start when ls-remote returns no commit"

    @pytest.mark.parametrize("commit", ["", "a" * 40, "f" * 40])
    def test_cache_hit_parametrized_by_commit(self, client, tmp_path, monkeypatch, commit):
        """Cache hit works for any commit value (including empty)."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main", commit=commit)
        monkeypatch.setattr(app_module, "_get_branch_head_commit", lambda branch: commit)

        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        job_id = resp.get_json()["job_id"]
        with app_module.builds_lock:
            job = app_module.builds.get(job_id)
        assert job is not None
        assert job["status"] == "done"

    # ── Custom-flags bypass ───────────────────────────────────────────────────

    @pytest.mark.parametrize("flags", [
        {"ADVERT_NAME": "mynode"},
        {"ADVERT_NAME": "mynode", "WIFI_SSID": "myhome"},
        {"BLE_PIN_CODE": "123456"},
    ])
    def test_flagged_build_skips_cache_lookup(self, client, tmp_path, monkeypatch, flags):
        """Any non-empty custom_flags dict must bypass the cache and start a build."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main")

        thread_started = []
        monkeypatch.setattr(
            app_module.threading.Thread, "start",
            lambda self: thread_started.append(True),
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "main", "flags": flags}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert thread_started, f"Flagged build with {flags!r} should bypass cache"

    # ── Variant and firmware_type dimension hit/miss ──────────────────────────

    def test_cache_miss_for_different_variant(self, client, tmp_path, monkeypatch):
        """Cache entry for heltec_v3 must not be served for rak4631."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main")

        thread_started = []
        monkeypatch.setattr(
            app_module.threading.Thread, "start",
            lambda self: thread_started.append(True),
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "rak4631_repeater", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert thread_started, "Different variant should cause cache miss"

    def test_cache_miss_for_different_firmware_type(self, client, tmp_path, monkeypatch):
        """Cache entry for 'repeater' must not be served for 'room_server'."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main")

        thread_started = []
        monkeypatch.setattr(
            app_module.threading.Thread, "start",
            lambda self: thread_started.append(True),
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_room_server", "branch": "main"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert thread_started, "Different firmware type should cause cache miss"

    def test_cache_miss_for_different_branch(self, client, tmp_path, monkeypatch):
        """Cache entry for 'main' must not be served when 'develop' is requested."""
        self._prime_cache(tmp_path, "heltec_v3", "repeater", "main")

        monkeypatch.setattr(
            app_module, "BRANCH_ENV_TO_VARIANT",
            {
                "main":    {"heltec_v3_repeater": "heltec_v3", "heltec_v3_room_server": "heltec_v3", "rak4631_repeater": "rak4631"},
                "develop": {"heltec_v3_repeater": "heltec_v3"},
            },
        )
        monkeypatch.setattr(
            app_module, "BRANCH_ENV_TYPE_MAP",
            {
                "main":    {"heltec_v3_repeater": "repeater", "heltec_v3_room_server": "room_server", "rak4631_repeater": "repeater"},
                "develop": {"heltec_v3_repeater": "repeater"},
            },
        )
        monkeypatch.setattr(
            app_module, "BRANCH_ENV_ARCH_MAP",
            {
                "main":    {"heltec_v3_repeater": ARCH_ESP32, "heltec_v3_room_server": ARCH_ESP32, "rak4631_repeater": ARCH_NRF52},
                "develop": {"heltec_v3_repeater": ARCH_ESP32},
            },
        )

        thread_started = []
        monkeypatch.setattr(
            app_module.threading.Thread, "start",
            lambda self: thread_started.append(True),
        )
        resp = client.post(
            "/api/build",
            data=json.dumps({"env": "heltec_v3_repeater", "branch": "develop"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert thread_started, "Different branch should cause cache miss"

    # ── _get_branch_head_commit unit tests ────────────────────────────────────

    def test_get_branch_head_commit_returns_empty_on_timeout(self, monkeypatch):
        """A subprocess.TimeoutExpired must be swallowed and '' returned."""
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run",
            lambda *a, **kw: (_ for _ in ()).throw(_sp.TimeoutExpired(cmd="git", timeout=10)))
        from app import _get_branch_head_commit
        assert _get_branch_head_commit("main") == ""

    def test_get_branch_head_commit_returns_empty_on_oserror(self, monkeypatch):
        """An OSError (e.g. git not installed) must be swallowed and '' returned."""
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("not found")))
        from app import _get_branch_head_commit
        assert _get_branch_head_commit("main") == ""

    def test_get_branch_head_commit_ignores_invalid_hash(self, monkeypatch):
        """A result whose hash is not 40 or 64 chars must be ignored and '' returned."""
        import subprocess as _sp
        class _FakeResult:
            returncode = 0
            stdout = "deadbeef\trefs/heads/main\n"  # only 8 chars
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: _FakeResult())
        from app import _get_branch_head_commit
        assert _get_branch_head_commit("main") == ""

    def test_get_branch_head_commit_returns_valid_sha1(self, monkeypatch):
        """A well-formed 40-char SHA-1 hash must be returned as-is."""
        import subprocess as _sp
        sha = "a" * 40
        class _FakeResult:
            returncode = 0
            stdout = f"{sha}\trefs/heads/main\n"
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: _FakeResult())
        from app import _get_branch_head_commit
        assert _get_branch_head_commit("main") == sha


# ── Room-server existence check removed ────────────────────────────────────────

class TestRoomServerCheckRemoved:
    """Verify that run_build no longer adds a room-server folder check to debug_files."""

    def test_debug_files_do_not_contain_room_server_key(self, client, done_esp32_job):
        """The debug_files dict must not include the removed room-server existence entry."""
        resp = client.get(f"/api/debug/{done_esp32_job}")
        assert resp.status_code == 200
        data = resp.get_json()
        files = data.get("files", {})
        assert "examples/simple_repeater_room_server" not in files

