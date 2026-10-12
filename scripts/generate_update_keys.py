from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

root = Path(__file__).resolve().parent.parent
key_dir = Path.home() / ".quick-control-center-signing"
key_dir.mkdir(parents=True, exist_ok=True)
private_path = key_dir / "update_private_key.pem"
public_path = root / "quickcontrol" / "update_public_key.pem"

if private_path.exists():
    raise SystemExit(f"Private key already exists: {private_path}\nRefusing to overwrite it.")

private_key = Ed25519PrivateKey.generate()
private_bytes = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)
public_bytes = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
)
private_path.write_bytes(private_bytes)
public_path.write_bytes(public_bytes)
try:
    private_path.chmod(0o600)
except OSError:
    pass
print(f"Public key written to: {public_path}")
print(f"PRIVATE signing key saved outside the project: {private_path}")
print("Back up the private key securely. Never commit or distribute it.")
print("Rebuild the app after generating the public key.")
