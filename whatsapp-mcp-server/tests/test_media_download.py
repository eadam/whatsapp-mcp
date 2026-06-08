"""Tests for Phase A image-receive helpers in whatsapp.py.

Coverage:
  - _normalize_jid()
  - _parse_max_download_bytes()
  - _load_download_allowlist()
  - download_media_as_image() (mocked bridge + filesystem)
"""

import io
import os
import struct
import zlib
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image as PilImage

import whatsapp as wa


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_jpeg(width: int = 8, height: int = 8) -> bytes:
    """Return a minimal valid JPEG at width×height."""
    img = PilImage.new("RGB", (width, height), color=(100, 150, 200))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_png(width: int = 8, height: int = 8) -> bytes:
    """Return a minimal valid PNG at width×height."""
    img = PilImage.new("RGB", (width, height), color=(100, 150, 200))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_fake_response(status_code: int, json_data: dict | None = None) -> MagicMock:
    """Return a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = Exception("no body")
    return resp


# ─── _normalize_jid ───────────────────────────────────────────────────────────

class TestNormalizeJid:
    def test_plain_number(self):
        assert wa._normalize_jid("15551234567") == "15551234567@s.whatsapp.net"

    def test_leading_plus_stripped(self):
        assert wa._normalize_jid("+15551234567") == "15551234567@s.whatsapp.net"

    def test_existing_whatsapp_net_passthrough(self):
        assert wa._normalize_jid("15551234567@s.whatsapp.net") == "15551234567@s.whatsapp.net"

    def test_device_suffix_stripped(self):
        assert wa._normalize_jid("15551234567:2@s.whatsapp.net") == "15551234567@s.whatsapp.net"

    def test_whitespace_stripped(self):
        assert wa._normalize_jid("  +15551234567  ") == "15551234567@s.whatsapp.net"

    def test_group_jid_rejected(self):
        with pytest.raises(ValueError, match="non-personal"):
            wa._normalize_jid("1234567890@g.us")

    def test_lid_rejected(self):
        with pytest.raises(ValueError):
            wa._normalize_jid("1234567890@lid")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            wa._normalize_jid("")

    def test_non_digits_rejected(self):
        with pytest.raises(ValueError):
            wa._normalize_jid("abc")

    def test_too_short_rejected(self):
        with pytest.raises(ValueError, match="6-15"):
            wa._normalize_jid("123")  # only 3 digits

    def test_too_long_rejected(self):
        with pytest.raises(ValueError, match="6-15"):
            wa._normalize_jid("1" * 16)  # 16 digits

    def test_minimum_length_allowed(self):
        result = wa._normalize_jid("123456")  # exactly 6 digits
        assert result == "123456@s.whatsapp.net"

    def test_maximum_length_allowed(self):
        result = wa._normalize_jid("1" * 15)
        assert result == ("1" * 15) + "@s.whatsapp.net"


# ─── _parse_max_download_bytes ────────────────────────────────────────────────

class TestSizeParsing:
    def test_missing_env_returns_5mib(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHATSAPP_MAX_DOWNLOAD_BYTES", None)
            assert wa._parse_max_download_bytes() == 5 * 1024 * 1024

    def test_empty_string_returns_5mib(self):
        with patch.dict(os.environ, {"WHATSAPP_MAX_DOWNLOAD_BYTES": ""}):
            assert wa._parse_max_download_bytes() == 5 * 1024 * 1024

    def test_zero_returns_zero(self):
        with patch.dict(os.environ, {"WHATSAPP_MAX_DOWNLOAD_BYTES": "0"}):
            assert wa._parse_max_download_bytes() == 0

    def test_positive_value_parsed(self):
        with patch.dict(os.environ, {"WHATSAPP_MAX_DOWNLOAD_BYTES": "1048576"}):
            assert wa._parse_max_download_bytes() == 1_048_576

    def test_negative_value_returns_zero(self):
        with patch.dict(os.environ, {"WHATSAPP_MAX_DOWNLOAD_BYTES": "-1"}):
            assert wa._parse_max_download_bytes() == 0

    def test_non_numeric_returns_zero(self):
        with patch.dict(os.environ, {"WHATSAPP_MAX_DOWNLOAD_BYTES": "five_mib"}):
            assert wa._parse_max_download_bytes() == 0

    def test_float_string_returns_zero(self):
        with patch.dict(os.environ, {"WHATSAPP_MAX_DOWNLOAD_BYTES": "5.5"}):
            assert wa._parse_max_download_bytes() == 0


# ─── _load_download_allowlist ─────────────────────────────────────────────────

class TestAllowlist:
    def test_empty_env_returns_empty_deny_all(self):
        with patch.dict(os.environ, {"WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS": ""}):
            jids, allow_all = wa._load_download_allowlist()
        assert jids == set()
        assert allow_all is False

    def test_missing_env_returns_empty_deny_all(self):
        env = {k: v for k, v in os.environ.items() if k != "WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS"}
        with patch.dict(os.environ, env, clear=True):
            jids, allow_all = wa._load_download_allowlist()
        assert jids == set()
        assert allow_all is False

    def test_star_sentinel_returns_allow_all(self):
        with patch.dict(os.environ, {"WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS": "*"}):
            jids, allow_all = wa._load_download_allowlist()
        assert allow_all is True
        assert jids == set()

    def test_single_jid_normalized(self):
        with patch.dict(os.environ, {"WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS": "+15551234567"}):
            jids, allow_all = wa._load_download_allowlist()
        assert allow_all is False
        assert "15551234567@s.whatsapp.net" in jids

    def test_multiple_jids_parsed(self):
        with patch.dict(os.environ, {"WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS": "+15551234567,+15557654321"}):
            jids, allow_all = wa._load_download_allowlist()
        assert "15551234567@s.whatsapp.net" in jids
        assert "15557654321@s.whatsapp.net" in jids
        assert allow_all is False

    def test_invalid_jid_skipped(self):
        with patch.dict(os.environ, {"WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS": "+15551234567,invalid"}):
            jids, allow_all = wa._load_download_allowlist()
        assert "15551234567@s.whatsapp.net" in jids

    def test_normalization_consistency(self):
        """Allowlist entry '+15551234567' and call with '15551234567@s.whatsapp.net' must match."""
        with patch.dict(os.environ, {"WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS": "+15551234567"}):
            jids, _ = wa._load_download_allowlist()
        normalized = wa._normalize_jid("+15551234567")
        assert normalized in jids


# ─── download_media_as_image ──────────────────────────────────────────────────
#
# All tests patch the module-level globals and requests.post so no real
# bridge or filesystem is hit.

FAKE_STORE = "/fake/store"

# Convenience: patch the three module-level media globals.
def _media_patch(allow_all=True, allowed=None, cap=5 * 1024 * 1024):
    allowed = allowed if allowed is not None else set()
    return [
        patch.object(wa, "MEDIA_DOWNLOAD_ALLOW_ALL", allow_all),
        patch.object(wa, "MEDIA_DOWNLOAD_ALLOWED_JIDS", allowed),
        patch.object(wa, "MEDIA_SIZE_CAP_BYTES", cap),
        patch.object(wa, "MEDIA_STORE_BASE", FAKE_STORE),
    ]


def _apply_patches(patches):
    for p in patches:
        p.start()
    return patches


def _stop_patches(patches):
    for p in patches:
        p.stop()


class TestDownloadMediaInvalidJid:
    def test_invalid_jid_returns_code(self):
        patches = _apply_patches(_media_patch())
        try:
            result = wa.download_media_as_image("msgid", "not-a-jid@g.us")
            assert result["code"] == "invalid_jid"
        finally:
            _stop_patches(patches)


class TestDownloadMediaNotAllowed:
    def test_jid_not_in_allowlist(self):
        patches = _apply_patches(_media_patch(allow_all=False, allowed={"99999999999@s.whatsapp.net"}))
        try:
            result = wa.download_media_as_image("msgid", "+15551234567")
            assert result["code"] == "not_allowed"
        finally:
            _stop_patches(patches)


class TestDownloadMediaCapZero:
    def test_cap_zero_returns_media_too_large_early(self):
        patches = _apply_patches(_media_patch(allow_all=True, cap=0))
        try:
            result = wa.download_media_as_image("msgid", "+15551234567")
            assert result["code"] == "media_too_large"
        finally:
            _stop_patches(patches)


class TestDownloadMediaBridgeErrors:
    def setup_method(self):
        self._patches = _apply_patches(_media_patch(allow_all=True))

    def teardown_method(self):
        _stop_patches(self._patches)

    def test_bridge_unreachable(self):
        import requests as req
        with patch.object(req, "post", side_effect=req.RequestException("timeout")):
            result = wa.download_media_as_image("msgid", "+15551234567")
        assert result["code"] == "bridge_unreachable"

    def test_403_maps_to_path_confinement(self):
        with patch("whatsapp.requests.post", return_value=_make_fake_response(403)):
            result = wa.download_media_as_image("msgid", "+15551234567")
        assert result["code"] == "path_confinement"

    def test_404_maps_to_not_found(self):
        with patch("whatsapp.requests.post", return_value=_make_fake_response(404)):
            result = wa.download_media_as_image("msgid", "+15551234567")
        assert result["code"] == "not_found"

    def test_non_200_with_json_code_uses_that_code(self):
        resp = _make_fake_response(400, {"code": "unsupported_media_type"})
        with patch("whatsapp.requests.post", return_value=resp):
            result = wa.download_media_as_image("msgid", "+15551234567")
        assert result["code"] == "unsupported_media_type"

    def test_malformed_bridge_response_json(self):
        resp = _make_fake_response(200, {"bad_key": "no path or filename"})
        with patch("whatsapp.requests.post", return_value=resp):
            result = wa.download_media_as_image("msgid", "+15551234567")
        assert result["code"] == "invalid_bridge_response"


class TestDownloadMediaPathConfinement:
    def setup_method(self):
        self._patches = _apply_patches(_media_patch(allow_all=True))

    def teardown_method(self):
        _stop_patches(self._patches)

    def test_path_outside_store_base_rejected(self, tmp_path):
        """Bridge returns a path outside MEDIA_STORE_BASE → path_confinement."""
        outside_file = tmp_path / "evil.jpg"
        outside_file.write_bytes(_make_jpeg())

        resp = _make_fake_response(200, {"path": str(outside_file), "filename": "evil.jpg"})
        with patch("whatsapp.requests.post", return_value=resp):
            result = wa.download_media_as_image("msgid", "+15551234567")
        assert result["code"] == "path_confinement"

    def test_traversal_path_rejected(self, tmp_path):
        """Path that resolves outside the store (even if it starts inside) is rejected."""
        traversal_path = FAKE_STORE + "/../../etc/passwd"
        resp = _make_fake_response(200, {"path": traversal_path, "filename": "passwd"})
        with patch("whatsapp.requests.post", return_value=resp):
            result = wa.download_media_as_image("msgid", "+15551234567")
        assert result["code"] in ("path_confinement", "media_file_missing")


class TestDownloadMediaFileNotFound:
    def setup_method(self):
        self._patches = _apply_patches(_media_patch(allow_all=True))

    def teardown_method(self):
        _stop_patches(self._patches)

    def test_file_deleted_between_download_and_read(self, tmp_path):
        """File exists on bridge but is deleted before Python reads it."""
        store_dir = tmp_path / "store"
        store_dir.mkdir()

        with patch.object(wa, "MEDIA_STORE_BASE", str(store_dir)):
            file_path = store_dir / "15551234567@s.whatsapp.net" / "img.jpg"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(_make_jpeg())

            resp = _make_fake_response(200, {"path": str(file_path), "filename": "img.jpg"})
            file_path.unlink()

            with patch("whatsapp.requests.post", return_value=resp):
                result = wa.download_media_as_image("msgid", "+15551234567")

        assert result["code"] == "media_file_missing"


class TestDownloadMediaSizeLimits:
    def setup_method(self):
        self._patches = _apply_patches(_media_patch(allow_all=True, cap=100))

    def teardown_method(self):
        _stop_patches(self._patches)

    def test_file_larger_than_cap_rejected(self, tmp_path):
        store_dir = tmp_path / "store"
        store_dir.mkdir()

        with patch.object(wa, "MEDIA_STORE_BASE", str(store_dir)):
            jid_dir = store_dir / "15551234567@s.whatsapp.net"
            jid_dir.mkdir()
            big_file = jid_dir / "big.jpg"
            big_file.write_bytes(b"X" * 200)  # 200 bytes > cap 100

            resp = _make_fake_response(200, {"path": str(big_file), "filename": "big.jpg"})
            with patch("whatsapp.requests.post", return_value=resp):
                result = wa.download_media_as_image("msgid", "+15551234567")

        assert result["code"] == "media_too_large"
        assert "file_size_bytes" in result


class TestDownloadMediaPillowValidation:
    def setup_method(self):
        self._patches = _apply_patches(_media_patch(allow_all=True, cap=10 * 1024 * 1024))

    def teardown_method(self):
        _stop_patches(self._patches)

    def _run_with_bytes(self, tmp_path, image_bytes: bytes, filename: str = "img.jpg"):
        store_dir = tmp_path / "store"
        store_dir.mkdir(exist_ok=True)
        jid_dir = store_dir / "15551234567@s.whatsapp.net"
        jid_dir.mkdir(exist_ok=True)
        img_file = jid_dir / filename
        img_file.write_bytes(image_bytes)

        with patch.object(wa, "MEDIA_STORE_BASE", str(store_dir)):
            resp = _make_fake_response(200, {"path": str(img_file), "filename": filename})
            with patch("whatsapp.requests.post", return_value=resp):
                return wa.download_media_as_image("msgid", "+15551234567")

    def test_valid_jpeg_succeeds(self, tmp_path):
        result = self._run_with_bytes(tmp_path, _make_jpeg())
        assert result.get("ok") is True
        assert result["format"] == "jpeg"
        assert isinstance(result["data"], bytes)

    def test_valid_png_succeeds(self, tmp_path):
        result = self._run_with_bytes(tmp_path, _make_png(), "img.png")
        assert result.get("ok") is True
        assert result["format"] == "png"

    def test_truncated_jpeg_rejected(self, tmp_path):
        truncated = _make_jpeg()[:50]
        result = self._run_with_bytes(tmp_path, truncated)
        assert result["code"] == "invalid_image"

    def test_not_an_image_bytes_rejected(self, tmp_path):
        result = self._run_with_bytes(tmp_path, b"not an image at all 12345")
        assert result["code"] == "invalid_image"

    def test_exif_rotation_applied(self, tmp_path):
        """EXIF-rotated image should be transposed — no error."""
        img = PilImage.new("RGB", (20, 10))
        buf = BytesIO()
        # Encode with EXIF orientation=6 (90° CW → result is 10×20).
        from PIL import ExifTags
        exif = img.getexif()
        exif[0x0112] = 6  # Orientation = 90 CW
        img.save(buf, format="JPEG", exif=exif.tobytes())
        result = self._run_with_bytes(tmp_path, buf.getvalue())
        assert result.get("ok") is True

    def test_decompression_bomb_rejected(self, tmp_path):
        """A PNG with reported dimensions that trigger DecompressionBombWarning."""
        original_threshold = PilImage.MAX_IMAGE_PIXELS
        try:
            PilImage.MAX_IMAGE_PIXELS = 100
            img = PilImage.new("RGB", (20, 20))
            buf = BytesIO()
            img.save(buf, format="PNG")
            result = self._run_with_bytes(tmp_path, buf.getvalue())
            assert result["code"] in ("decompression_bomb", "invalid_image")
        finally:
            PilImage.MAX_IMAGE_PIXELS = original_threshold

    def test_format_from_bytes_not_extension(self, tmp_path):
        """A .png extension with JPEG bytes → Pillow detects JPEG → ok."""
        result = self._run_with_bytes(tmp_path, _make_jpeg(), "img.png")
        assert result.get("ok") is True
        assert result["format"] == "jpeg"  # Pillow reports JPEG regardless of filename


class TestDownloadMediaPreview:
    def _run_with_bytes(self, tmp_path, image_bytes, store_base_patch, filename="img.jpg"):
        store_dir = tmp_path / "store"
        store_dir.mkdir(exist_ok=True)
        jid_dir = store_dir / "15551234567@s.whatsapp.net"
        jid_dir.mkdir(exist_ok=True)
        img_file = jid_dir / filename
        img_file.write_bytes(image_bytes)

        patches = [
            patch.object(wa, "MEDIA_STORE_BASE", str(store_dir)),
            patch.object(wa, "MEDIA_DOWNLOAD_ALLOW_ALL", True),
            patch.object(wa, "MEDIA_DOWNLOAD_ALLOWED_JIDS", set()),
            patch.object(wa, "MEDIA_SIZE_CAP_BYTES", 10 * 1024 * 1024),
        ]
        for p in patches:
            p.start()
        try:
            resp = _make_fake_response(200, {"path": str(img_file), "filename": filename})
            with patch("whatsapp.requests.post", return_value=resp):
                return wa.download_media_as_image("msgid", "+15551234567")
        finally:
            for p in patches:
                p.stop()

    def test_small_image_passthrough(self, tmp_path):
        """Small JPEG (8×8) below all thresholds → returned as-is (same bytes)."""
        img_bytes = _make_jpeg(8, 8)
        result = self._run_with_bytes(tmp_path, img_bytes, str(tmp_path / "store"))
        assert result.get("ok") is True
        assert result["data"] == img_bytes
        assert result["format"] == "jpeg"

    def test_large_dimension_triggers_resize(self, tmp_path):
        """Image wider than PREVIEW_MAX_DIM (1600) is downscaled."""
        img = PilImage.new("RGB", (2000, 1000))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        result = self._run_with_bytes(tmp_path, buf.getvalue(), str(tmp_path / "store"))
        assert result.get("ok") is True
        assert result["format"] == "jpeg"
        # Verify the preview dimensions are within PREVIEW_MAX_DIM.
        preview_img = PilImage.open(BytesIO(result["data"]))
        assert max(preview_img.size) <= wa.PREVIEW_MAX_DIM

    def test_preview_bytes_within_cap(self, tmp_path):
        """Preview output must never exceed PREVIEW_MAX_BYTES."""
        img = PilImage.new("RGB", (2000, 2000), color=(128, 128, 128))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        result = self._run_with_bytes(tmp_path, buf.getvalue(), str(tmp_path / "store"))
        assert result.get("ok") is True
        assert len(result["data"]) <= wa.PREVIEW_MAX_BYTES


class TestNoInternalDetailLeaked:
    """Error results must never contain paths, tracebacks, or raw exception text."""

    def setup_method(self):
        self._patches = _apply_patches(_media_patch(allow_all=True, cap=5 * 1024 * 1024))

    def teardown_method(self):
        _stop_patches(self._patches)

    def _assert_safe(self, result: dict):
        as_str = str(result)
        assert "/opt/" not in as_str, f"server path leaked: {result}"
        assert "/fake/store" not in as_str, f"fake store path leaked: {result}"
        assert "Traceback" not in as_str, f"traceback leaked: {result}"
        assert "Exception" not in as_str, f"exception class leaked: {result}"
        assert "ok" not in result or result["ok"] is True, "ok=False present"
        if "code" in result:
            assert isinstance(result["code"], str)
            # code must be one of the documented stable strings
            assert "_" in result["code"] or result["code"] in ("unknown_error",)

    def test_bridge_unreachable_no_detail(self):
        import requests as req
        with patch.object(req, "post", side_effect=req.RequestException("Internal addr: 127.0.0.1:8080")):
            result = wa.download_media_as_image("msgid", "+15551234567")
        self._assert_safe(result)

    def test_403_no_detail(self):
        with patch("whatsapp.requests.post", return_value=_make_fake_response(403)):
            result = wa.download_media_as_image("msgid", "+15551234567")
        self._assert_safe(result)

    def test_not_found_no_detail(self):
        with patch("whatsapp.requests.post", return_value=_make_fake_response(404)):
            result = wa.download_media_as_image("msgid", "+15551234567")
        self._assert_safe(result)

    def test_invalid_image_no_detail(self, tmp_path):
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        jid_dir = store_dir / "15551234567@s.whatsapp.net"
        jid_dir.mkdir()
        bad_file = jid_dir / "bad.jpg"
        bad_file.write_bytes(b"not an image")

        with patch.object(wa, "MEDIA_STORE_BASE", str(store_dir)):
            resp = _make_fake_response(200, {"path": str(bad_file), "filename": "bad.jpg"})
            with patch("whatsapp.requests.post", return_value=resp):
                result = wa.download_media_as_image("msgid", "+15551234567")

        self._assert_safe(result)
        assert result["code"] == "invalid_image"
