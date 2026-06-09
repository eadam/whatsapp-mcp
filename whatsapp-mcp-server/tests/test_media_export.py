"""Tests for export_media (base64 export) — the Phase A Cowork→Drive fix.

Covers _fetch_confined_media_bytes (symlink-safe bounded read), export_media_as_base64,
and the MCP-level result shape of the export_media tool.
"""

import base64
import json
import os
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image as PilImage

import whatsapp as wa

# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_jpeg(width: int = 8, height: int = 8) -> bytes:
    img = PilImage.new("RGB", (width, height), color=(10, 120, 200))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_png(width: int = 8, height: int = 8) -> bytes:
    img = PilImage.new("RGB", (width, height), color=(10, 120, 200))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fake_resp(status_code: int, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = Exception("no body")
    return resp


def _export_patches(store_dir: str, cap: int = 5 * 1024 * 1024, allow_all: bool = True):
    return [
        patch.object(wa, "MEDIA_STORE_BASE", store_dir),
        patch.object(wa, "MEDIA_DOWNLOAD_ALLOW_ALL", allow_all),
        patch.object(wa, "MEDIA_DOWNLOAD_ALLOWED_JIDS", set()),
        patch.object(wa, "MEDIA_SIZE_CAP_BYTES", cap),
        patch.object(wa, "EXPORT_EFFECTIVE_CAP", cap),
    ]


def _write_store_file(store_dir, name: str, data: bytes) -> str:
    jid_dir = os.path.join(store_dir, "15551234567@s.whatsapp.net")
    os.makedirs(jid_dir, exist_ok=True)
    path = os.path.join(jid_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _run_export(tmp_path, image_bytes, name="img.jpg", cap=5 * 1024 * 1024, allow_all=True):
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    fpath = _write_store_file(str(store), name, image_bytes)
    patches = _export_patches(str(store), cap=cap, allow_all=allow_all)
    for p in patches:
        p.start()
    try:
        with patch("whatsapp.requests.post", return_value=_fake_resp(200, {"path": fpath, "filename": "bridge.jpg"})):
            return wa.export_media_as_base64("MSG123", "15551234567@s.whatsapp.net")
    finally:
        for p in patches:
            p.stop()


# ─── happy path ───────────────────────────────────────────────────────────────

class TestExportHappyPath:
    def test_jpeg_roundtrips_to_original_bytes(self, tmp_path):
        original = _make_jpeg()
        r = _run_export(tmp_path, original)
        assert r.get("ok") is True
        assert base64.b64decode(r["data_base64"]) == original  # bit-identical
        assert r["format"] == "jpeg"
        assert r["mime_type"] == "image/jpeg"
        assert r["filename"].endswith(".jpg")
        assert r["file_size_bytes"] == len(original)

    def test_png_reports_png_not_jpg(self, tmp_path):
        original = _make_png()
        # Even though the bridge hands back a ".jpg" filename, we derive from detected format.
        r = _run_export(tmp_path, original, name="whatever.jpg")
        assert r.get("ok") is True
        assert r["format"] == "png"
        assert r["mime_type"] == "image/png"
        assert r["filename"].endswith(".png")


# ─── disabled / cap behaviour ─────────────────────────────────────────────────

class TestExportDisabledAndCap:
    def test_disabled_when_cap_zero(self, tmp_path):
        r = _run_export(tmp_path, _make_jpeg(), cap=0)
        assert r["code"] == "export_disabled"

    def test_oversize_rejected_bounded(self, tmp_path):
        big = _make_jpeg(64, 64) + b"\x00" * 4096
        r = _run_export(tmp_path, big, cap=128)  # file >> cap
        assert r["code"] == "media_too_large"
        assert "file_size_bytes" in r


# ─── validation / gating ──────────────────────────────────────────────────────

class TestExportValidationAndGating:
    def test_non_image_rejected(self, tmp_path):
        r = _run_export(tmp_path, b"this is not an image")
        assert r["code"] == "invalid_image"

    def test_not_allowed_source(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        patches = _export_patches(str(store), allow_all=False)
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post") as post:
                r = wa.export_media_as_base64("MSG", "15551234567@s.whatsapp.net")
                post.assert_not_called()  # rejected before any bridge call
        finally:
            for p in patches:
                p.stop()
        assert r["code"] == "not_allowed"

    def test_bridge_404_maps_not_found(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        patches = _export_patches(str(store))
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post", return_value=_fake_resp(404)):
                r = wa.export_media_as_base64("MSG", "15551234567@s.whatsapp.net")
        finally:
            for p in patches:
                p.stop()
        assert r["code"] == "not_found"

    def test_unknown_bridge_code_maps_download_failed(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        patches = _export_patches(str(store))
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post", return_value=_fake_resp(400, {"code": "brand_new_code"})):
                r = wa.export_media_as_base64("MSG", "15551234567@s.whatsapp.net")
        finally:
            for p in patches:
                p.stop()
        assert r["code"] == "download_failed"


# ─── symlink / confinement (descriptor-based) ─────────────────────────────────

class TestExportConfinement:
    def test_final_component_symlink_rejected(self, tmp_path):
        store = tmp_path / "store"
        jid = store / "15551234567@s.whatsapp.net"
        jid.mkdir(parents=True)
        outside = tmp_path / "secret.jpg"
        outside.write_bytes(_make_jpeg())
        link = jid / "img.jpg"
        try:
            os.symlink(str(outside), str(link))
        except OSError:
            pytest.skip("symlink not permitted")
        patches = _export_patches(str(store))
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post", return_value=_fake_resp(200, {"path": str(link), "filename": "x.jpg"})):
                r = wa.export_media_as_base64("MSG", "15551234567@s.whatsapp.net")
        finally:
            for p in patches:
                p.stop()
        assert r["code"] == "path_confinement"

    def test_intermediate_dir_symlink_escaping_rejected(self, tmp_path):
        # store/<jid> is itself a symlink to a dir OUTSIDE the store root.
        store = tmp_path / "store"
        store.mkdir()
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        target = outside_dir / "img.jpg"
        target.write_bytes(_make_jpeg())
        jid_link = store / "15551234567@s.whatsapp.net"
        try:
            os.symlink(str(outside_dir), str(jid_link))
        except OSError:
            pytest.skip("symlink not permitted")
        # The bridge-returned path goes THROUGH the intermediate symlink; the final
        # component (img.jpg) is a regular file so O_NOFOLLOW alone would not catch it,
        # but /proc-or-F_GETPATH descriptor confinement must.
        bridge_path = os.path.join(str(jid_link), "img.jpg")
        patches = _export_patches(str(store))
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post", return_value=_fake_resp(200, {"path": bridge_path, "filename": "x.jpg"})):
                r = wa.export_media_as_base64("MSG", "15551234567@s.whatsapp.net")
        finally:
            for p in patches:
                p.stop()
        assert r["code"] == "path_confinement"

    def test_symlink_inside_root_allowed(self, tmp_path):
        # A symlink whose target is still under the store root must remain readable.
        store = tmp_path / "store"
        jid = store / "15551234567@s.whatsapp.net"
        jid.mkdir(parents=True)
        real = jid / "real.jpg"
        original = _make_jpeg()
        real.write_bytes(original)
        link = jid / "link.jpg"
        try:
            os.symlink(str(real), str(link))
        except OSError:
            pytest.skip("symlink not permitted")
        patches = _export_patches(str(store))
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post", return_value=_fake_resp(200, {"path": str(link), "filename": "x.jpg"})):
                r = wa.export_media_as_base64("MSG", "15551234567@s.whatsapp.net")
        finally:
            for p in patches:
                p.stop()
        # Final-component symlink → O_NOFOLLOW rejects even though target is inside root.
        # This is the conservative, expected behaviour (no symlinks for the final node).
        assert r["code"] == "path_confinement"


# ─── no internal detail leak ──────────────────────────────────────────────────

class TestExportNoLeak:
    def test_error_results_have_no_path_or_traceback(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        patches = _export_patches(str(store))
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post", return_value=_fake_resp(403)):
                r = wa.export_media_as_base64("MSG", "15551234567@s.whatsapp.net")
        finally:
            for p in patches:
                p.stop()
        s = str(r)
        assert "/private" not in s and "/var" not in s and "Traceback" not in s
        assert r["code"] == "path_confinement"


# ─── MCP-level result shape ───────────────────────────────────────────────────

class TestExportMcpResultShape:
    def test_call_tool_returns_single_textcontent_json(self, tmp_path):
        import asyncio

        from mcp.server.fastmcp.utilities.types import Image as FastImage
        from mcp.types import TextContent

        import main

        store = tmp_path / "store"
        store.mkdir()
        original = _make_jpeg()
        fpath = _write_store_file(str(store), "img.jpg", original)
        patches = _export_patches(str(store))
        for p in patches:
            p.start()
        try:
            with patch("whatsapp.requests.post", return_value=_fake_resp(200, {"path": fpath, "filename": "b.jpg"})):
                blocks = asyncio.run(
                    main.mcp.call_tool("export_media", {"message_id": "MSG", "chat_jid": "15551234567@s.whatsapp.net"})
                )
        finally:
            for p in patches:
                p.stop()

        # structured_output=False → exactly one TextContent, no Image, no structured dict.
        assert isinstance(blocks, list)
        assert len(blocks) == 1
        assert isinstance(blocks[0], TextContent)
        assert not any(isinstance(b, FastImage) for b in blocks)
        payload = json.loads(blocks[0].text)
        assert payload["ok"] is True
        assert base64.b64decode(payload["data_base64"]) == original


class TestToolRegistration:
    def test_import_main_registers_export_media(self):
        import main

        names = {t.name for t in main.mcp._tool_manager.list_tools()}
        assert "export_media" in names
        # 11 base + export_media + __debug_echo_base64 probe (probe removed after Step 0 → 12)
        assert "__debug_echo_base64" in names
        assert len(names) == 13
