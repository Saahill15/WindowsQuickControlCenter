import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quickcontrol.updater import (
    UpdateError,
    validate_https_url,
    validate_release_notes,
    validate_version,
)

parser = argparse.ArgumentParser(description="Create a signed update manifest for a built installer.")
parser.add_argument("--version", required=True, help="Release version, e.g. 0.1.1")
parser.add_argument("--installer", required=True, help="Path to the installer .exe")
parser.add_argument("--installer-url", required=True, help="Public HTTPS URL for the installer")
parser.add_argument("--release-notes", default="Bug fixes and improvements")
parser.add_argument("--private-key", default=str(Path.home() / ".quick-control-center-signing" / "update_private_key.pem"))
parser.add_argument("--out-dir", default="release-output")
args = parser.parse_args()

try:
    validate_version(args.version)
    installer_url = validate_https_url(args.installer_url, "installer_url")
    release_notes = validate_release_notes(args.release_notes)
except UpdateError as exc:
    raise SystemExit(str(exc)) from exc

installer = Path(args.installer)
if not installer.is_file():
    raise SystemExit(f"Installer not found: {installer}")
private_key = serialization.load_pem_private_key(Path(args.private_key).read_bytes(), password=None)
if not isinstance(private_key, Ed25519PrivateKey):
    raise SystemExit("Private key must be Ed25519.")

manifest = {
    "version": args.version,
    "installer_url": installer_url,
    "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
    "release_notes": release_notes
}
# Exact bytes are signed; don't reformat manifest.json after signing.
manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
signature = private_key.sign(manifest_bytes)
out_dir = Path(args.out_dir)
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "manifest.json").write_bytes(manifest_bytes)
(out_dir / "manifest.json.sig").write_text(base64.b64encode(signature).decode("ascii") + "\n", encoding="ascii")
print(f"Signed manifest: {out_dir / 'manifest.json'}")
print(f"Signature:       {out_dir / 'manifest.json.sig'}")
