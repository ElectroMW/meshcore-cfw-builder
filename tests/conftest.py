"""
Shared pytest fixtures for MeshCore firmware builder tests.
"""

import queue
import zipfile
import pytest

import app as app_module
from app import app as flask_app, ARCH_NRF52, ARCH_ESP32


# ── Minimal variant data used throughout tests ───────────────────────────────

FAKE_VARIANTS = [
    {
        "id": "heltec_v3",
        "label": "Heltec V3",
        "arch": ARCH_ESP32,
        "envs": [
            {"id": "heltec_v3_repeater", "label": "Repeater", "type": "repeater"},
            {"id": "heltec_v3_room_server", "label": "Room Server", "type": "room_server"},
        ],
    },
    {
        "id": "rak4631",
        "label": "RAK4631",
        "arch": ARCH_NRF52,
        "envs": [
            {"id": "rak4631_repeater", "label": "Repeater", "type": "repeater"},
        ],
    },
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Flask test client with all external I/O mocked out."""
    flask_app.config["TESTING"] = True

    # Patch builds dir so tests are isolated
    monkeypatch.setattr(app_module, "BUILDS_DIR", tmp_path)

    # Patch variant caches so routes work without a network call
    monkeypatch.setattr(app_module, "VARIANT_CACHE", FAKE_VARIANTS)
    monkeypatch.setattr(
        app_module,
        "ENV_TO_VARIANT",
        {
            "heltec_v3_repeater": "heltec_v3",
            "heltec_v3_room_server": "heltec_v3",
            "rak4631_repeater": "rak4631",
        },
    )
    monkeypatch.setattr(
        app_module,
        "ENV_TYPE_MAP",
        {
            "heltec_v3_repeater": "repeater",
            "heltec_v3_room_server": "room_server",
            "rak4631_repeater": "repeater",
        },
    )
    monkeypatch.setattr(
        app_module,
        "ENV_ARCH_MAP",
        {
            "heltec_v3_repeater": ARCH_ESP32,
            "heltec_v3_room_server": ARCH_ESP32,
            "rak4631_repeater": ARCH_NRF52,
        },
    )
    monkeypatch.setattr(
        app_module,
        "BRANCH_ENV_TO_VARIANT",
        {"main": {
            "heltec_v3_repeater": "heltec_v3",
            "rak4631_repeater": "rak4631",
        }},
    )
    monkeypatch.setattr(
        app_module,
        "BRANCH_ENV_TYPE_MAP",
        {"main": {
            "heltec_v3_repeater": "repeater",
            "rak4631_repeater": "repeater",
        }},
    )
    monkeypatch.setattr(
        app_module,
        "BRANCH_ENV_ARCH_MAP",
        {"main": {
            "heltec_v3_repeater": ARCH_ESP32,
            "rak4631_repeater": ARCH_NRF52,
        }},
    )
    monkeypatch.setattr(
        app_module,
        "BRANCH_VARIANT_CACHE",
        {"main": FAKE_VARIANTS},
    )
    # Mark variant loading as complete
    app_module.VARIANT_READY.set()

    # Prevent network calls for branch HEAD commit resolution during tests
    monkeypatch.setattr(app_module, "_get_branch_head_commit", lambda branch: "")

    with flask_app.test_client() as c:
        yield c


def make_done_job(tmp_path, job_id: str, arch: str = ARCH_ESP32, env_id: str = "heltec_v3_repeater"):
    """Inject a completed build job into the builds registry and create real temp files."""
    import app as app_module

    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    if arch == ARCH_NRF52:
        bin_path = job_dir / "firmware.hex"
        bin_path.write_bytes(b":00000001FF\n")  # minimal valid Intel HEX end record

        # Create the DFU zip (as app.py would)
        zip_path = job_dir / "firmware_dfu.zip"
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(str(bin_path), bin_path.name)
    else:
        bin_path = job_dir / "firmware-merged.bin"
        bin_path.write_bytes(b"\xde\xad\xbe\xef" * 4)
        zip_path = None

    q: queue.Queue = queue.Queue()
    q.put(None)  # sentinel so SSE stream ends immediately

    job = {
        "status": "done",
        "log_queue": q,
        "bin_path": str(bin_path),
        "zip_path": str(zip_path) if zip_path else None,
        "error": None,
        "env_id": env_id,
        "variant_folder": "rak4631" if arch == ARCH_NRF52 else "heltec_v3",
        "arch": arch,
        "cancelled": False,
        "current_proc": None,
        "debug_files": {},
    }
    with app_module.builds_lock:
        app_module.builds[job_id] = job
    return job


@pytest.fixture()
def done_esp32_job(client, tmp_path):
    """A completed ESP32 build job injected into the registry."""
    job_id = "esp32-test-job-id"
    make_done_job(tmp_path, job_id, arch=ARCH_ESP32, env_id="heltec_v3_repeater")
    yield job_id
    with app_module.builds_lock:
        app_module.builds.pop(job_id, None)


@pytest.fixture()
def done_nrf52_job(client, tmp_path):
    """A completed nRF52 build job injected into the registry."""
    job_id = "nrf52-test-job-id"
    make_done_job(tmp_path, job_id, arch=ARCH_NRF52, env_id="rak4631_repeater")
    yield job_id
    with app_module.builds_lock:
        app_module.builds.pop(job_id, None)
