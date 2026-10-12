import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "quickcontrol" / "release_config.json"
PUBLIC_KEY_PATH = ROOT / "quickcontrol" / "update_public_key.pem"

DEFAULT_CONFIG = {
    "manifest_url": "",
    "check_on_startup": True,
    "timeout_seconds": 4,
}
MAX_MANIFEST_BYTES = 1_000_000
MAX_SIGNATURE_BYTES = 4096
MAX_INSTALLER_BYTES = 250_000_000
MAX_INSTALLER_SECONDS = 600
DOWNLOAD_CHUNK_SIZE = 64 * 1024
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
REQUIRED_MANIFEST_FIELDS = {"version", "installer_url", "sha256"}
ALLOWED_MANIFEST_FIELDS = REQUIRED_MANIFEST_FIELDS | {"release_notes"}


class UpdateError(RuntimeError):
    pass


class UpdateSecurityError(UpdateError):
    pass


@dataclass(frozen=True)
class VerifiedManifest(Mapping):
    version: str
    installer_url: str
    sha256: str
    release_notes: str
    _manifest_bytes: bytes = field(repr=False, compare=False)
    _signature_bytes: bytes = field(repr=False, compare=False)

    def __getitem__(self, key):
        if key in ALLOWED_MANIFEST_FIELDS:
            return getattr(self, key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(("version", "installer_url", "sha256", "release_notes"))

    def __len__(self):
        return 4


def _app_version():
    from quickcontrol.version import APP_VERSION
    return APP_VERSION


def _load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Update configuration could not be read: {exc}") from exc
    if not isinstance(config, dict):
        raise UpdateError("Update configuration must be a JSON object.")
    return {**DEFAULT_CONFIG, **config}


def should_check_on_startup():
    try:
        return _load_config().get("check_on_startup") is True
    except UpdateError:
        return False


def _timeout_seconds(config, installer=False):
    value = config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"])
    if type(value) is not int or value < 1:
        raise UpdateError("timeout_seconds must be a positive integer.")
    if installer:
        return max(5, min(value, 30))
    return max(2, min(value, 10))


def validate_https_url(value, field_name):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise UpdateSecurityError(f"{field_name} must be a valid HTTPS URL.")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UpdateSecurityError(f"{field_name} is malformed: {exc}") from exc
    if (parsed.scheme.lower() != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or "\\" in parsed.netloc):
        raise UpdateSecurityError(f"{field_name} must be a safe HTTPS URL.")
    if port is not None and not 1 <= port <= 65535:
        raise UpdateSecurityError(f"{field_name} has an invalid port.")
    hostname = hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise UpdateSecurityError(f"{field_name} must not target a local host.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname.encode("idna")
        except UnicodeError as exc:
            raise UpdateSecurityError(f"{field_name} has an invalid hostname.") from exc
    else:
        if not address.is_global:
            raise UpdateSecurityError(f"{field_name} must not target a non-public IP address.")
    return value


def _https_url(value, field):
    try:
        return validate_https_url(value, field)
    except UpdateSecurityError as exc:
        raise ValueError(str(exc)) from exc


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        try:
            validate_https_url(new_url, "Redirect destination")
        except UpdateSecurityError as exc:
            raise urllib.error.HTTPError(
                request.full_url, code, str(exc), headers, response
            ) from exc
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


def _open_url(url, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": "QuickControlCenter-Updater/1"}
    )
    opener = urllib.request.build_opener(_HTTPSRedirectHandler())
    return opener.open(request, timeout=timeout)


def _check_content_length(response, max_bytes):
    headers = getattr(response, "headers", None)
    length = headers.get("Content-Length") if headers is not None else None
    if length is None:
        return
    try:
        parsed_length = int(length)
    except (TypeError, ValueError) as exc:
        raise UpdateError("Server returned an invalid Content-Length.") from exc
    if parsed_length < 0 or parsed_length > max_bytes:
        raise UpdateError("Update response exceeds the allowed download size.")


def _read_url(url, timeout, max_bytes=MAX_MANIFEST_BYTES):
    validate_https_url(url, "Download URL")
    deadline = time.monotonic() + max(30, timeout)
    data = bytearray()
    try:
        with _open_url(url, timeout) as response:
            validate_https_url(response.geturl(), "Redirect destination")
            _check_content_length(response, max_bytes)
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError("Update response timed out.")
                chunk = response.read(min(DOWNLOAD_CHUNK_SIZE, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise UpdateError("Update response exceeds the allowed download size.")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Update download failed: {exc}") from exc
    return bytes(data)


def _verify_signature(manifest_bytes, signature_bytes):
    if not PUBLIC_KEY_PATH.exists():
        raise UpdateSecurityError("Update signing key is not configured.")
    raw_key = PUBLIC_KEY_PATH.read_bytes()
    if raw_key.lstrip().startswith(b"#"):
        raise UpdateSecurityError("Update signing key is not configured.")
    try:
        public_key = serialization.load_pem_public_key(raw_key)
    except (TypeError, ValueError) as exc:
        raise UpdateSecurityError("Pinned update public key is invalid.") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise UpdateSecurityError("The pinned update key is not Ed25519.")
    try:
        signature = base64.b64decode(signature_bytes.strip(), validate=True)
    except (binascii.Error, AttributeError) as exc:
        raise UpdateSecurityError("Update signature is not valid base64.") from exc
    if len(signature) != 64:
        raise UpdateSecurityError("Update signature has an invalid length.")
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise UpdateSecurityError("Update manifest signature verification failed.") from exc


def validate_version(version):
    if (not isinstance(version, str) or len(version) > 64
            or not VERSION_PATTERN.fullmatch(version)):
        raise UpdateSecurityError(f"Invalid update version: {version!r}")
    return version


def _version_tuple(version):
    validate_version(version)
    return tuple(int(part) for part in version.split("."))


def validate_release_notes(release_notes):
    if (not isinstance(release_notes, str) or len(release_notes) > 4000
            or any(ord(character) < 32 and character not in "\r\n\t" for character in release_notes)):
        raise UpdateSecurityError("Signed manifest contains invalid release notes.")
    return release_notes


def _unique_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateSecurityError(f"Signed manifest contains duplicate field: {key}")
        result[key] = value
    return result


def _validate_manifest_bytes(manifest_bytes):
    try:
        manifest = json.loads(
            manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_object_pairs
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateSecurityError(f"Signed manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise UpdateSecurityError("Signed manifest must be a JSON object.")
    missing = REQUIRED_MANIFEST_FIELDS - manifest.keys()
    unexpected = manifest.keys() - ALLOWED_MANIFEST_FIELDS
    if missing:
        raise UpdateSecurityError(
            "Signed manifest is missing required fields: " + ", ".join(sorted(missing))
        )
    if unexpected:
        raise UpdateSecurityError(
            "Signed manifest contains unexpected fields: " + ", ".join(sorted(unexpected))
        )
    version = manifest["version"]
    _version_tuple(version)
    installer_url = validate_https_url(manifest["installer_url"], "installer_url")
    sha256 = manifest["sha256"]
    if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
        raise UpdateSecurityError("Signed manifest contains an invalid installer SHA-256.")
    release_notes = validate_release_notes(
        manifest.get("release_notes", "Bug fixes and improvements")
    )
    return {
        "version": version,
        "installer_url": installer_url,
        "sha256": sha256.lower(),
        "release_notes": release_notes,
    }


def _signature_url(manifest_url):
    parsed = urllib.parse.urlsplit(manifest_url)
    return urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc, parsed.path + ".sig", parsed.query, ""
    ))


def check_for_update():
    config = _load_config()
    manifest_url = config.get("manifest_url", "")
    if manifest_url == "":
        return None
    validate_https_url(manifest_url, "manifest_url")
    timeout = _timeout_seconds(config)
    manifest_bytes = _read_url(manifest_url, timeout, MAX_MANIFEST_BYTES)
    signature_bytes = _read_url(
        _signature_url(manifest_url), timeout, MAX_SIGNATURE_BYTES
    )
    _verify_signature(manifest_bytes, signature_bytes)
    fields = _validate_manifest_bytes(manifest_bytes)
    if _version_tuple(fields["version"]) <= _version_tuple(_app_version()):
        return None
    return VerifiedManifest(
        **fields,
        _manifest_bytes=manifest_bytes,
        _signature_bytes=signature_bytes,
    )


def _require_verified_manifest(manifest):
    if not isinstance(manifest, VerifiedManifest):
        raise UpdateSecurityError("Update manifest was not signature-verified.")
    _verify_signature(manifest._manifest_bytes, manifest._signature_bytes)
    fields = _validate_manifest_bytes(manifest._manifest_bytes)
    if any(fields[key] != manifest[key] for key in fields):
        raise UpdateSecurityError("Verified update manifest data was modified.")
    return fields


def _updates_directory():
    root = os.environ.get("LOCALAPPDATA", tempfile.gettempdir())
    return Path(root) / "QuickControlCenter" / "updates"


def _installer_path(version):
    return _updates_directory() / f"QuickControlCenter-Setup-{version}.exe"


def download_and_verify_installer(manifest):
    fields = _require_verified_manifest(manifest)
    config = _load_config()
    timeout = _timeout_seconds(config, installer=True)
    installer_path = _installer_path(fields["version"])
    installer_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{installer_path.name}.", suffix=".part", dir=installer_path.parent
    )
    temporary_path = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + MAX_INSTALLER_SECONDS
    try:
        with os.fdopen(descriptor, "wb") as output:
            with _open_url(fields["installer_url"], timeout) as response:
                validate_https_url(response.geturl(), "Redirect destination")
                _check_content_length(response, MAX_INSTALLER_BYTES)
                while True:
                    if time.monotonic() > deadline:
                        raise UpdateError("Installer download exceeded the time limit.")
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_INSTALLER_BYTES:
                        raise UpdateError("Installer exceeds the maximum download size.")
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != fields["sha256"]:
            raise UpdateSecurityError("Installer SHA-256 mismatch; the update was rejected.")
        os.replace(temporary_path, installer_path)
        return installer_path
    except Exception as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"Installer download failed: {exc}") from exc


def _verify_cached_installer(manifest, installer_path):
    expected_path = _installer_path(manifest["version"]).resolve()
    actual_path = Path(installer_path).resolve()
    if actual_path != expected_path:
        raise UpdateSecurityError("Installer path does not match the verified update.")
    try:
        size = actual_path.stat().st_size
        if size <= 0 or size > MAX_INSTALLER_BYTES:
            raise UpdateSecurityError("Cached installer has an invalid size.")
        digest = hashlib.sha256()
        with actual_path.open("rb") as installer_file:
            for chunk in iter(lambda: installer_file.read(DOWNLOAD_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise UpdateError(f"Verified installer is unavailable: {exc}") from exc
    if digest.hexdigest() != manifest["sha256"]:
        raise UpdateSecurityError("Cached installer changed after verification.")


def _start_installer(installer_path):
    os.startfile(str(installer_path))


def launch_installer(manifest, installer_path):
    fields = _require_verified_manifest(manifest)
    _verify_cached_installer(fields, installer_path)
    if sys.platform != "win32":
        raise UpdateError("Installer execution is only supported on Windows.")
    try:
        _start_installer(installer_path)
    except OSError as exc:
        raise UpdateError(f"Could not launch the verified installer: {exc}") from exc
