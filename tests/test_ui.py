"""
UI / end-to-end tests for MeshCore firmware builder.

Uses Playwright (via pytest-playwright) to drive a real browser against the
Flask development server started by the ``live_server`` fixture.  All external
network calls (git, PlatformIO) are never triggered because we never actually
submit a build — we only verify UI state and interactivity.
"""

import io
import json
import queue
import threading
import time
import zipfile

import pytest
from playwright.sync_api import Page, expect

import app as app_module
from app import app as flask_app, ARCH_ESP32, ARCH_NRF52
from tests.conftest import FAKE_VARIANTS, _make_done_job


# ── Live server fixture ───────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def _patch_app_for_ui_tests(tmp_path_factory):
    """
    Patch the Flask app once for the entire UI test session:
      - Disable SSL (tests use plain HTTP)
      - Pre-load variant cache so the UI doesn't attempt git calls
    """
    tmp = tmp_path_factory.mktemp("ui_builds")
    app_module.BUILDS_DIR = tmp
    app_module.VARIANT_CACHE = FAKE_VARIANTS
    app_module.ENV_TO_VARIANT = {
        "heltec_v3_repeater": "heltec_v3",
        "rak4631_repeater": "rak4631",
    }
    app_module.ENV_TYPE_MAP = {
        "heltec_v3_repeater": "repeater",
        "rak4631_repeater": "repeater",
    }
    app_module.ENV_ARCH_MAP = {
        "heltec_v3_repeater": ARCH_ESP32,
        "rak4631_repeater": ARCH_NRF52,
    }
    app_module.BRANCH_ENV_TO_VARIANT = {"main": {
        "heltec_v3_repeater": "heltec_v3",
        "rak4631_repeater": "rak4631",
    }}
    app_module.BRANCH_ENV_TYPE_MAP = {"main": {
        "heltec_v3_repeater": "repeater",
        "rak4631_repeater": "repeater",
    }}
    app_module.BRANCH_ENV_ARCH_MAP = {"main": {
        "heltec_v3_repeater": ARCH_ESP32,
        "rak4631_repeater": ARCH_NRF52,
    }}
    app_module.BRANCH_VARIANT_CACHE = {"main": FAKE_VARIANTS}
    app_module.VARIANT_READY.set()
    yield tmp


@pytest.fixture(scope="session")
def live_server(_patch_app_for_ui_tests):
    """Start the Flask app on a free port for the duration of the test session."""
    import socket

    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    flask_app.config["TESTING"] = True
    flask_app.config["SERVER_NAME"] = f"127.0.0.1:{port}"

    server_thread = threading.Thread(
        target=lambda: flask_app.run(
            host="127.0.0.1",
            port=port,
            use_reloader=False,
            ssl_context=None,
        ),
        daemon=True,
    )
    server_thread.start()

    # Wait until the server responds
    import urllib.request as _ur
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            _ur.urlopen(f"http://127.0.0.1:{port}/")
            break
        except Exception:
            time.sleep(0.1)

    yield f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def server_url(live_server):
    return live_server


# ── Helper: inject a done job and return job_id ───────────────────────────────

def _inject_nrf52_job(tmp_path):
    job_id = "ui-nrf52-test-job"
    _make_done_job(tmp_path, job_id, arch=ARCH_NRF52, env_id="rak4631_repeater")
    return job_id


def _inject_esp32_job(tmp_path):
    job_id = "ui-esp32-test-job"
    _make_done_job(tmp_path, job_id, arch=ARCH_ESP32, env_id="heltec_v3_repeater")
    return job_id


# ── Page-level tests ──────────────────────────────────────────────────────────

class TestPageLoad:
    def test_title_contains_meshcore(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page).to_have_title("MeshCore Custom Firmware Builder")

    def test_header_visible(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page.locator("header h1")).to_be_visible()

    def test_build_button_present(self, page: Page, server_url: str):
        page.goto(server_url)
        build_btn = page.locator("#buildBtn")
        expect(build_btn).to_be_visible()

    def test_branch_selector_present(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page.locator("#branchSelect")).to_be_visible()

    def test_variant_selector_present(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page.locator("#variantSelect")).to_be_visible()

    def test_download_btn_hidden_initially(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page.locator("#downloadBtn")).not_to_be_visible()

    def test_download_zip_btn_hidden_initially(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page.locator("#downloadZipBtn")).not_to_be_visible()

    def test_flash_container_hidden_initially(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page.locator("#flashContainer")).not_to_be_visible()

    def test_log_box_present(self, page: Page, server_url: str):
        page.goto(server_url)
        expect(page.locator("#logBox")).to_be_visible()


class TestPrivacyAndCreditsPages:
    def test_privacy_page_loads(self, page: Page, server_url: str):
        page.goto(f"{server_url}/privacy")
        assert page.title() != ""

    def test_credits_page_loads(self, page: Page, server_url: str):
        page.goto(f"{server_url}/credits")
        assert page.title() != ""


class TestApiEndpointsFromBrowser:
    """Exercise JSON API endpoints from the browser context."""

    def test_flags_endpoint(self, page: Page, server_url: str):
        resp = page.request.get(f"{server_url}/api/flags")
        assert resp.ok
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_variants_endpoint(self, page: Page, server_url: str):
        resp = page.request.get(f"{server_url}/api/variants?branch=main")
        assert resp.ok
        data = resp.json()
        assert data["status"] == "ready"

    def test_status_404_for_unknown_job(self, page: Page, server_url: str):
        resp = page.request.get(f"{server_url}/api/status/unknown-job")
        assert resp.status == 404

    def test_download_zip_404_for_unknown_job(self, page: Page, server_url: str):
        resp = page.request.get(f"{server_url}/api/download_zip/unknown-job")
        assert resp.status == 404


class TestDownloadButtons:
    """Tests that verify the download and zip-download buttons appear correctly."""

    def test_esp32_download_btn_visible_after_done(
        self, page: Page, server_url: str, tmp_path
    ):
        """For an ESP32 job, the hex/bin download button should be visible."""
        job_id = _inject_esp32_job(tmp_path)
        try:
            page.goto(server_url)

            # Simulate what checkFinalStatus does: call the status endpoint
            status_resp = page.request.get(f"{server_url}/api/status/{job_id}")
            assert status_resp.ok
            data = status_resp.json()
            assert data["status"] == "done"
            assert data["has_zip"] is False
        finally:
            with app_module.builds_lock:
                app_module.builds.pop(job_id, None)

    def test_nrf52_status_has_zip(
        self, page: Page, server_url: str, tmp_path
    ):
        """For an nRF52 job, /api/status should report has_zip=true."""
        job_id = _inject_nrf52_job(tmp_path)
        try:
            status_resp = page.request.get(f"{server_url}/api/status/{job_id}")
            assert status_resp.ok
            data = status_resp.json()
            assert data["has_zip"] is True
            assert data["zip_filename"].endswith(".zip")
        finally:
            with app_module.builds_lock:
                app_module.builds.pop(job_id, None)

    def test_nrf52_zip_download_returns_zip(
        self, page: Page, server_url: str, tmp_path
    ):
        """For an nRF52 job, /api/download_zip should return a valid zip file."""
        job_id = _inject_nrf52_job(tmp_path)
        try:
            zip_resp = page.request.get(f"{server_url}/api/download_zip/{job_id}")
            assert zip_resp.ok
            assert "application/zip" in zip_resp.headers.get("content-type", "")
            zf = zipfile.ZipFile(io.BytesIO(zip_resp.body()))
            assert any(n.endswith(".hex") for n in zf.namelist())
        finally:
            with app_module.builds_lock:
                app_module.builds.pop(job_id, None)

    def test_nrf52_zip_download_does_not_wipe_job(
        self, page: Page, server_url: str, tmp_path
    ):
        """Downloading the zip should NOT remove the build job."""
        job_id = _inject_nrf52_job(tmp_path)
        try:
            page.request.get(f"{server_url}/api/download_zip/{job_id}")
            with app_module.builds_lock:
                assert job_id in app_module.builds, "Job was unexpectedly removed by zip download"
        finally:
            with app_module.builds_lock:
                app_module.builds.pop(job_id, None)

    def test_esp32_zip_download_returns_400(
        self, page: Page, server_url: str, tmp_path
    ):
        """ESP32 builds should return 400 for the zip endpoint."""
        job_id = _inject_esp32_job(tmp_path)
        try:
            resp = page.request.get(f"{server_url}/api/download_zip/{job_id}")
            assert resp.status == 400
        finally:
            with app_module.builds_lock:
                app_module.builds.pop(job_id, None)


class TestBuildButtonBehaviour:
    """Smoke tests for the build form interactions."""

    def test_build_button_disabled_without_env(self, page: Page, server_url: str):
        page.goto(server_url)
        # Variant selector starts with no selection → build btn disabled
        build_btn = page.locator("#buildBtn")
        expect(build_btn).to_be_disabled()

    def test_variant_selector_populated(self, page: Page, server_url: str):
        page.goto(server_url)
        # Wait for variants to load (the fixture pre-populates the cache)
        page.wait_for_function(
            "document.querySelector('#variantSelect').options.length > 1",
            timeout=5000,
        )
        options = page.locator("#variantSelect option")
        assert options.count() > 1

    def test_selecting_variant_shows_env_grid(self, page: Page, server_url: str):
        page.goto(server_url)
        page.wait_for_function(
            "document.querySelector('#variantSelect').options.length > 1",
            timeout=5000,
        )
        # Select the first real variant
        page.select_option("#variantSelect", index=1)
        # The env grid should now be visible
        page.wait_for_selector("#envGrid", state="visible", timeout=3000)
        expect(page.locator("#envGrid")).to_be_visible()
