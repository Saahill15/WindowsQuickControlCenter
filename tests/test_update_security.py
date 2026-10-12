import base64
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quickcontrol import updater


class FakeResponse:
    def __init__(self, data, url, headers=None):
        self.stream = io.BytesIO(data)
        self.url = url
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stream.close()

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.stream.read(size)


class InterruptedResponse(FakeResponse):
    def __init__(self, url):
        super().__init__(b"partial installer", url, headers={})
        self.first_read = True

    def read(self, size=-1):
        if self.first_read:
            self.first_read = False
            return b"partial"
        raise OSError("simulated connection reset")


class UpdateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key_path = Path(self.temp_dir.name) / "test_public_key.pem"
        self.public_key_path.write_bytes(self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        self.key_patcher = patch.object(updater, "PUBLIC_KEY_PATH", self.public_key_path)
        self.key_patcher.start()
        self.addCleanup(self.key_patcher.stop)

    def _fields(self, installer_bytes=b"verified installer"):
        return {
            "version": "0.1.1",
            "installer_url": "https://downloads.example.com/QuickControlCenter-Setup-0.1.1.exe",
            "sha256": hashlib.sha256(installer_bytes).hexdigest(),
            "release_notes": "Security fixes",
        }

    def _sign(self, manifest_bytes):
        return base64.b64encode(self.private_key.sign(manifest_bytes)) + b"\n"

    def _serialize(self, fields):
        return (json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n").encode("utf-8")

    def _check_signed_bytes(self, manifest_bytes, signature_bytes=None):
        if signature_bytes is None:
            signature_bytes = self._sign(manifest_bytes)
        config = {
            **updater.DEFAULT_CONFIG,
            "manifest_url": "https://updates.example.com/manifest.json",
        }
        with patch.object(updater, "_load_config", return_value=config), \
                patch.object(updater, "_read_url", side_effect=[manifest_bytes, signature_bytes]), \
                patch.object(updater, "_app_version", return_value="0.1.0"):
            return updater.check_for_update()

    def _verified_manifest(self, installer_bytes=b"verified installer"):
        return self._check_signed_bytes(self._serialize(self._fields(installer_bytes)))

    def _updates_directory(self):
        return Path(self.temp_dir.name) / "QuickControlCenter" / "updates"

    def test_version_comparison_and_format(self):
        self.assertGreater(updater._version_tuple("1.2.0"), updater._version_tuple("1.1.9"))
        self.assertEqual(updater._version_tuple("2.0.0"), (2, 0, 0))
        with self.assertRaises(updater.UpdateSecurityError):
            updater._version_tuple("1.2")

    def test_ed25519_signature_accepts_exact_manifest_bytes(self):
        manifest = self._serialize(self._fields())
        updater._verify_signature(manifest, self._sign(manifest))
        with self.assertRaises(updater.UpdateSecurityError):
            updater._verify_signature(manifest + b" ", self._sign(manifest))

    def test_invalid_signature_rejected_before_manifest_parsing(self):
        with self.assertRaisesRegex(updater.UpdateSecurityError, "signature verification"):
            self._check_signed_bytes(b"not JSON", base64.b64encode(b"x" * 64))

    def test_duplicate_manifest_fields_are_rejected(self):
        valid = self._fields()
        duplicate = (
            '{"version":"0.1.1","installer_url":"' + valid["installer_url"]
            + '","sha256":"' + valid["sha256"] + '","sha256":"' + "0" * 64 + '"}'
        ).encode("utf-8")
        with self.assertRaisesRegex(updater.UpdateSecurityError, "duplicate field"):
            self._check_signed_bytes(duplicate)

    def test_rejects_missing_typed_malformed_and_unexpected_manifest_fields(self):
        valid = self._fields()
        cases = []
        missing = dict(valid)
        missing.pop("sha256")
        cases.append(("missing hash", missing))
        wrong_version = {**valid, "version": [0, 1, 1]}
        cases.append(("wrong version type", wrong_version))
        malformed_version = {**valid, "version": "0.01.1"}
        cases.append(("malformed version", malformed_version))
        long_version = {**valid, "version": "1" * 100}
        cases.append(("overlong version", long_version))
        malformed_url = {**valid, "installer_url": "http://downloads.example.com/setup.exe"}
        cases.append(("insecure URL", malformed_url))
        malformed_hash = {**valid, "sha256": "z" * 64}
        cases.append(("malformed hash", malformed_hash))
        unexpected = {**valid, "silent_install": True}
        cases.append(("unexpected field", unexpected))
        wrong_notes = {**valid, "release_notes": 7}
        cases.append(("wrong release notes type", wrong_notes))

        for label, fields in cases:
            with self.subTest(label=label):
                with self.assertRaises(updater.UpdateSecurityError):
                    self._check_signed_bytes(self._serialize(fields))

    def test_release_notes_remain_optional_for_older_signed_manifests(self):
        fields = self._fields()
        fields.pop("release_notes")
        manifest = self._check_signed_bytes(self._serialize(fields))
        self.assertEqual(manifest["release_notes"], "Bug fixes and improvements")

    def test_rejects_unsafe_urls_and_redirect_downgrades(self):
        for url in (
            "http://downloads.example.com/setup.exe",
            "ftp://downloads.example.com/setup.exe",
            "https://user:pass@downloads.example.com/setup.exe",
            "https://localhost/setup.exe",
            "https://127.0.0.1/setup.exe",
            "https://downloads.example.com:99999/setup.exe",
        ):
            with self.subTest(url=url):
                with self.assertRaises(updater.UpdateSecurityError):
                    updater.validate_https_url(url, "installer_url")

        request = updater.urllib.request.Request("https://updates.example.com/manifest.json")
        handler = updater._HTTPSRedirectHandler()
        with self.assertRaises(updater.urllib.error.HTTPError) as error:
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://downloads.example.com/file.exe"
            )
        error.exception.close()
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://cdn.example.com/file.exe"
        )
        self.assertEqual(redirected.full_url, "https://cdn.example.com/file.exe")

    def test_response_final_url_and_size_are_checked(self):
        with patch.object(updater, "_open_url", return_value=FakeResponse(
                b"ok", "http://updates.example.com/redirected")):
            with self.assertRaises(updater.UpdateSecurityError):
                updater._read_url("https://updates.example.com/manifest.json", 4)

        with patch.object(updater, "_open_url", return_value=FakeResponse(
            b"12345", "https://updates.example.com/manifest.json",
            headers={"Content-Length": "1"})):
            with self.assertRaisesRegex(updater.UpdateError, "size"):
                updater._read_url("https://updates.example.com/manifest.json", 4, max_bytes=4)

    def test_hash_mismatch_rejects_installer_and_removes_partial_file(self):
        manifest = self._check_signed_bytes(
            self._serialize(self._fields(b"expected installer"))
        )
        response = FakeResponse(
            b"different installer", manifest["installer_url"]
        )
        with patch.dict(updater.os.environ, {"LOCALAPPDATA": self.temp_dir.name}), \
                patch.object(updater, "_load_config", return_value=updater.DEFAULT_CONFIG), \
                patch.object(updater, "_open_url", return_value=response):
            with self.assertRaisesRegex(updater.UpdateSecurityError, "SHA-256 mismatch"):
                updater.download_and_verify_installer(manifest)
        self.assertEqual(list(self._updates_directory().iterdir()), [])

    def test_oversized_installer_is_rejected_and_cleaned_up(self):
        installer_bytes = b"12345"
        manifest = self._check_signed_bytes(self._serialize(self._fields(installer_bytes)))
        for content_length in ("5", "1"):
            with self.subTest(content_length=content_length):
                response = FakeResponse(
                    installer_bytes,
                    manifest["installer_url"],
                    headers={"Content-Length": content_length},
                )
                with patch.dict(updater.os.environ, {"LOCALAPPDATA": self.temp_dir.name}), \
                        patch.object(updater, "_load_config", return_value=updater.DEFAULT_CONFIG), \
                        patch.object(updater, "MAX_INSTALLER_BYTES", 4), \
                        patch.object(updater, "_open_url", return_value=response):
                    with self.assertRaisesRegex(updater.UpdateError, "size"):
                        updater.download_and_verify_installer(manifest)
                self.assertEqual(list(self._updates_directory().iterdir()), [])

    def test_interrupted_installer_download_removes_partial_file(self):
        manifest = self._verified_manifest(b"partial installer")
        response = InterruptedResponse(manifest["installer_url"])
        with patch.dict(updater.os.environ, {"LOCALAPPDATA": self.temp_dir.name}), \
                patch.object(updater, "_load_config", return_value=updater.DEFAULT_CONFIG), \
                patch.object(updater, "_open_url", return_value=response):
            with self.assertRaisesRegex(updater.UpdateError, "connection reset"):
                updater.download_and_verify_installer(manifest)
        self.assertEqual(list(self._updates_directory().iterdir()), [])

    def test_download_timeout_removes_partial_file(self):
        manifest = self._verified_manifest(b"timeout installer")
        response = FakeResponse(b"timeout installer", manifest["installer_url"])
        with patch.dict(updater.os.environ, {"LOCALAPPDATA": self.temp_dir.name}), \
                patch.object(updater, "_load_config", return_value=updater.DEFAULT_CONFIG), \
                patch.object(updater, "MAX_INSTALLER_SECONDS", 1), \
                patch.object(updater.time, "monotonic", side_effect=[0, 0, 2]), \
                patch.object(updater, "_open_url", return_value=response):
            with self.assertRaisesRegex(updater.UpdateError, "time limit"):
                updater.download_and_verify_installer(manifest)
        self.assertEqual(list(self._updates_directory().iterdir()), [])

    def test_verified_download_is_not_launched_until_explicit_approval(self):
        installer_bytes = b"verified installer"
        manifest = self._verified_manifest(installer_bytes)
        response = FakeResponse(installer_bytes, manifest["installer_url"])
        with patch.dict(updater.os.environ, {"LOCALAPPDATA": self.temp_dir.name}), \
                patch.object(updater, "_load_config", return_value=updater.DEFAULT_CONFIG), \
                patch.object(updater, "_open_url", return_value=response), \
                patch.object(updater, "_start_installer") as start_installer:
            installer_path = updater.download_and_verify_installer(manifest)
            start_installer.assert_not_called()
            self.assertEqual(installer_path.read_bytes(), installer_bytes)

            with patch.object(updater.sys, "platform", "win32"):
                updater.launch_installer(manifest, installer_path)
            start_installer.assert_called_once_with(installer_path)

    def test_cached_installer_tampering_prevents_execution(self):
        installer_bytes = b"verified installer"
        manifest = self._verified_manifest(installer_bytes)
        response = FakeResponse(installer_bytes, manifest["installer_url"])
        with patch.dict(updater.os.environ, {"LOCALAPPDATA": self.temp_dir.name}), \
                patch.object(updater, "_load_config", return_value=updater.DEFAULT_CONFIG), \
                patch.object(updater, "_open_url", return_value=response), \
                patch.object(updater, "_start_installer") as start_installer:
            installer_path = updater.download_and_verify_installer(manifest)
            installer_path.write_bytes(b"tampered")
            with patch.object(updater.sys, "platform", "win32"):
                with self.assertRaisesRegex(updater.UpdateSecurityError, "changed after verification"):
                    updater.launch_installer(manifest, installer_path)
            start_installer.assert_not_called()

    def test_startup_setting_can_disable_automatic_checks(self):
        config_path = Path(self.temp_dir.name) / "release_config.json"
        config_path.write_text(json.dumps({
            "manifest_url": "https://updates.example.com/manifest.json",
            "check_on_startup": False,
            "timeout_seconds": 4,
        }), encoding="utf-8")
        with patch.object(updater, "CONFIG_PATH", config_path):
            self.assertFalse(updater.should_check_on_startup())

        config_path.write_text(json.dumps({"manifest_url": ""}), encoding="utf-8")
        with patch.object(updater, "CONFIG_PATH", config_path):
            self.assertTrue(updater.should_check_on_startup())

    def test_rejects_non_https_legacy_helper(self):
        with self.assertRaises(ValueError):
            updater._https_url("http://example.com/manifest.json", "manifest_url")


class SignReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.installer_path = self.root / "installer.exe"
        self.installer_path.write_bytes(b"test installer placeholder")
        self.script_path = Path(__file__).resolve().parent.parent / "scripts" / "sign_release.py"

    def test_invalid_inputs_fail_before_private_key_access_or_output_creation(self):
        invalid_cases = (
            ("version", "--version", "0.01.1", "Invalid update version"),
            ("url", "--installer-url", "https://user:pass@downloads.example.com/setup.exe", "safe HTTPS URL"),
            ("notes", "--release-notes", "x" * 4001, "invalid release notes"),
        )
        for name, invalid_option, invalid_value, expected_error in invalid_cases:
            with self.subTest(name=name):
                output_dir = self.root / f"output-{name}"
                command = [
                    sys.executable,
                    str(self.script_path),
                    "--version", "0.1.1",
                    "--installer", str(self.installer_path),
                    "--installer-url", "https://downloads.example.com/setup.exe",
                    "--release-notes", "valid notes",
                    "--private-key", str(self.root / "missing-test-key.pem"),
                    "--out-dir", str(output_dir),
                ]
                option_index = command.index(invalid_option)
                command[option_index + 1] = invalid_value

                result = subprocess.run(
                    command,
                    cwd=self.script_path.parent.parent,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr + result.stdout)
                self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
