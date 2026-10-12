# Release-ready architecture

## Distribution model
Keep this source repository public on GitHub so users can inspect the code, report issues, and contribute. Publish versioned installers and their signed update manifests/signatures as assets on GitHub Releases. The updater uses public HTTPS URLs and requires no GitHub token. Never commit or distribute a PAT, private signing key, or other secret.

## Local development
1. Install Python 3.11+ on Windows.
2. Run `Install.bat`.
3. Run `Launch.vbs`.
4. For visible errors, run `run_debug.bat`.

## One-time update signing setup
1. Run `scripts\\generate_update_keys.py` from the project environment.
2. This writes the private Ed25519 key to `%USERPROFILE%\\.quick-control-center-signing\\update_private_key.pem` and the public key to `quickcontrol/update_public_key.pem`.
3. Back up the private key securely. Never commit it or include it in the installer.
4. Set `quickcontrol/release_config.json` `manifest_url` to the HTTPS URL of `manifest.json` in the binary distribution location.
5. Rebuild the app after the public key is generated. This pins the trusted key into the packaged application.

## Update manifest contract
The updater accepts a UTF-8 JSON object containing `version`, `installer_url`, and `sha256`; `release_notes` is optional for compatibility and limited to 4,000 characters. The version must use three numeric components, the installer URL must be a safe HTTPS URL, and the SHA-256 must contain exactly 64 hexadecimal characters. Unknown fields are rejected. `manifest.json.sig` is base64 Ed25519 over the exact manifest bytes; do not reformat or re-encode the manifest after signing.

The checked-in `manifest_url` is intentionally empty, and `update_public_key.pem` is only a provisioning reminder. Update releases will not verify or install until a release maintainer configures the actual public manifest URL, provisions the matching public key from the protected signing key, and rebuilds the application. Never place the private key in the repository or application.

## Build the Windows app
1. Install Python 3.10 or newer and Inno Setup 6.
2. Run `Install.bat` to create or update the existing `.venv` with runtime dependencies.
3. Run `Build-Windows.bat`. It installs the pinned build dependencies, reads the authoritative version from `quickcontrol/version.py`, builds the onedir app, and compiles the installer.
4. Inspect `dist\\QuickControlCenter\\` and `installer\\output\\QuickControlCenter-Setup-<version>.exe`.
5. Run `.venv\\Scripts\\python.exe -m unittest discover -s tests -v` and `.venv\\Scripts\\python.exe -m compileall -q main.py quickcontrol scripts tests` before packaging. Test installation, launch, shortcuts, and uninstall in a disposable Windows account or VM; verify user settings remain under `%LOCALAPPDATA%\\QuickControlCenter`.

`quickcontrol/version.py` is the single application-version source. The build passes that value to Inno Setup, so do not edit an installer version constant separately. Only `config.json`, `release_config.json`, and the public-key file are added as explicit data assets; private keys, credentials, `.env` files, and `.venv` contents are not package data.

## Publish a release
1. Increment `APP_VERSION` in `quickcontrol/version.py`; the build supplies the same version to the installer. Use that version for the signed manifest.
2. Build and test the installer on a clean Windows test account/VM.
3. Create a GitHub Release for the version and upload the installer. Use the final public HTTPS asset URL.
4. Run (PowerShell example; replace URL and version):
   `python scripts/sign_release.py --version 0.1.1 --installer installer/output/QuickControlCenter-Setup-0.1.1.exe --installer-url https://YOUR-HOST/QuickControlCenter-Setup-0.1.1.exe --release-notes "Fixes and improvements"`
5. Upload `manifest.json` and `manifest.json.sig` as release assets, and configure the app's manifest URL to the public `manifest.json` asset URL; the signature must be available at the sibling `.sig` URL.
6. Verify that the updater detects the new version and rejects a deliberately modified manifest or installer.

## Security model
- The app fetches manifest and signature over HTTPS, verifies the manifest with a pinned Ed25519 public key, then verifies the installer SHA-256 against the signed manifest.
- The installer is downloaded and verified before the app offers installation; the app asks the user before launching it.
- The installer is launched normally, not silently or elevated by the updater.
- Do not put private-repository credentials or a signing key into the application.
- If the signing key is lost, ship a new application build with a newly pinned public key through a trusted manual channel.

## Release validation
- Run `.venv\\Scripts\\python.exe -m unittest discover -s tests` before building.
- Build the executable with `Build-Windows.bat` and compile the installer with Inno Setup 6 on Windows.
- Test installation, uninstall, upgrade, and the signed update flow on a clean Windows test account or VM before publishing. Do not treat unit tests as a substitute for these Windows release checks.
