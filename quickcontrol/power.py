import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


class PowerModeError(RuntimeError):
    pass


class LegacyPowerStateRecoveryRequired(PowerModeError):
    pass


_SETTING_IDS = {
    "monitor": ("SUB_VIDEO", "VIDEOIDLE"),
    "sleep": ("SUB_SLEEP", "STANDBYIDLE"),
}
_SOURCES = (("ac", "/setacvalueindex"), ("dc", "/setdcvalueindex"))
_GUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_AC_INDEX_PATTERN = re.compile(
    r"Current AC Power Setting Index:\s*0x([0-9a-f]+)", re.IGNORECASE
)
_DC_INDEX_PATTERN = re.compile(
    r"Current DC Power Setting Index:\s*0x([0-9a-f]+)", re.IGNORECASE
)


def _run_powercfg(command):
    return subprocess.run(
        ["powercfg", *command],
        capture_output=True,
        text=True,
        timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        shell=False,
    )


class PowerModeController:
    def __init__(self, state_path, command_runner=None):
        self.state_path = Path(state_path)
        self.command_runner = command_runner or _run_powercfg

    def _run_checked(self, command):
        try:
            result = self.command_runner(command)
        except Exception as exc:
            raise PowerModeError(f"powercfg {' '.join(command)} failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no error details").strip()
            raise PowerModeError(f"powercfg {' '.join(command)} failed: {detail}")
        return result.stdout

    def _active_scheme(self):
        output = self._run_checked(["/getactivescheme"])
        match = _GUID_PATTERN.search(output)
        if not match:
            raise PowerModeError("Could not identify the active Windows power scheme.")
        return match.group(0).lower()

    def _read_snapshot(self, scheme):
        snapshot = {}
        for kind, (subgroup, setting) in _SETTING_IDS.items():
            output = self._run_checked(["/query", scheme, subgroup, setting])
            ac_match = _AC_INDEX_PATTERN.search(output)
            dc_match = _DC_INDEX_PATTERN.search(output)
            if not ac_match or not dc_match:
                raise PowerModeError(
                    f"Could not read the AC/DC {kind} timeout values from powercfg."
                )
            snapshot[kind] = {
                "ac": int(ac_match.group(1), 16),
                "dc": int(dc_match.group(1), 16),
            }
        return snapshot

    def _capture(self):
        scheme = self._active_scheme()
        return scheme, self._read_snapshot(scheme)

    def _set_snapshot(self, scheme, snapshot):
        for kind, (subgroup, setting) in _SETTING_IDS.items():
            for source, command in _SOURCES:
                value = snapshot[kind][source]
                self._run_checked(
                    [command, scheme, subgroup, setting, str(value)]
                )

        if self._active_scheme() == scheme:
            self._run_checked(["/setactive", scheme])

    def _rollback(self, scheme, snapshot):
        failures = []
        for kind, (subgroup, setting) in _SETTING_IDS.items():
            for source, command in _SOURCES:
                value = snapshot[kind][source]
                try:
                    self._run_checked(
                        [command, scheme, subgroup, setting, str(value)]
                    )
                except PowerModeError as exc:
                    failures.append(str(exc))

        try:
            if self._active_scheme() == scheme:
                self._run_checked(["/setactive", scheme])
        except PowerModeError as exc:
            failures.append(str(exc))
        return failures

    def _write_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{self.state_path.name}.",
                suffix=".tmp",
                dir=self.state_path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.state_path)
        except Exception:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _validate_original(self, original):
        if not isinstance(original, dict):
            raise PowerModeError("Saved power state is missing original timeouts.")
        for kind in _SETTING_IDS:
            values = original.get(kind)
            if not isinstance(values, dict):
                raise PowerModeError(f"Saved power state is missing {kind} timeouts.")
            for source in ("ac", "dc"):
                value = values.get(source)
                if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
                    raise PowerModeError(
                        f"Saved power state has an invalid {source.upper()} {kind} timeout."
                    )
        return original

    def _read_state(self):
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PowerModeError(f"Saved power state is unreadable: {exc}") from exc
        if not isinstance(state, dict):
            raise PowerModeError("Saved power state has an unsupported format.")
        if "schema_version" not in state and "scheme_guid" not in state:
            if not isinstance(state.get("work_mode"), bool):
                raise PowerModeError("Saved power state has an unsupported format.")
            state = dict(state)
            state["original"] = self._validate_original(state.get("original"))
            state["_legacy_format"] = True
            return state
        if type(state.get("schema_version")) is not int or state["schema_version"] != 1:
            raise PowerModeError("Saved power state has an unsupported format.")
        if not isinstance(state.get("work_mode"), bool):
            raise PowerModeError("Saved power state has an invalid Work Mode flag.")
        if "recovery_required" in state and not isinstance(
            state["recovery_required"], bool
        ):
            raise PowerModeError("Saved power state has an invalid recovery flag.")

        scheme = state.get("scheme_guid")
        if not isinstance(scheme, str) or not _GUID_PATTERN.fullmatch(scheme):
            raise PowerModeError("Saved power state has an invalid scheme identifier.")
        state["original"] = self._validate_original(state.get("original"))
        return state

    def is_enabled(self):
        state = self._read_state()
        return bool(state and state["work_mode"])

    def is_recovery_required(self):
        state = self._read_state()
        return bool(
            state and (
                state.get("recovery_required")
                or state.get("_legacy_format") and state["work_mode"]
            )
        )

    def legacy_recovery_scheme(self):
        state = self._read_state()
        if not state or not state.get("_legacy_format") or not state["work_mode"]:
            raise PowerModeError("No legacy Work Mode state is awaiting recovery.")
        return self._active_scheme()

    def toggle(self):
        state = self._read_state()
        if state and state.get("_legacy_format") and state["work_mode"]:
            raise LegacyPowerStateRecoveryRequired(
                "The saved Work Mode state does not identify its original power scheme."
            )
        if state and state.get("recovery_required"):
            self.restore()
            return False
        if state and state["work_mode"]:
            self.restore()
            return False
        self.enable()
        return True

    def enable(self):
        state = self._read_state()
        if state and state.get("_legacy_format") and state["work_mode"]:
            raise LegacyPowerStateRecoveryRequired(
                "The saved Work Mode state must be recovered before enabling Work Mode."
            )
        if state and state.get("recovery_required"):
            raise PowerModeError("Power-state recovery is required before enabling Work Mode.")
        if state and state["work_mode"]:
            raise PowerModeError("Work Mode is already enabled.")
        scheme, original = self._capture()
        work_mode = {
            kind: {source: 0 for source, _ in _SOURCES}
            for kind in _SETTING_IDS
        }
        try:
            self._set_snapshot(scheme, work_mode)
            self._write_state({
                "schema_version": 1,
                "work_mode": True,
                "scheme_guid": scheme,
                "original": original,
            })
        except Exception as exc:
            failures = self._rollback(scheme, original)
            message = f"Could not enable Work Mode: {exc}"
            if failures:
                message += " Rollback was incomplete: " + "; ".join(failures)
                recovery_state = {
                    "schema_version": 1,
                    "work_mode": False,
                    "recovery_required": True,
                    "scheme_guid": scheme,
                    "original": original,
                }
                try:
                    self._write_state(recovery_state)
                except Exception as state_exc:
                    message += f" Could not save recovery state: {state_exc}"
            else:
                message += " Original timeout values were restored."
            raise PowerModeError(message) from exc

    def recover_legacy(self, confirmed_scheme_guid):
        state = self._read_state()
        if not state or not state.get("_legacy_format") or not state["work_mode"]:
            raise PowerModeError("No legacy Work Mode state is awaiting recovery.")
        if (not isinstance(confirmed_scheme_guid, str)
                or not _GUID_PATTERN.fullmatch(confirmed_scheme_guid)):
            raise LegacyPowerStateRecoveryRequired(
                "Legacy recovery requires explicit confirmation of a power scheme."
            )

        scheme = confirmed_scheme_guid.lower()
        if self._active_scheme() != scheme:
            raise LegacyPowerStateRecoveryRequired(
                "The active power scheme changed after confirmation; no settings were changed."
            )

        original = state["original"]
        current = self._read_snapshot(scheme)
        try:
            self._set_snapshot(scheme, original)
            self._write_state({
                "schema_version": 1,
                "work_mode": False,
                "scheme_guid": scheme,
                "original": original,
            })
        except Exception as exc:
            failures = self._rollback(scheme, current)
            message = f"Legacy power-state recovery failed: {exc}"
            if failures:
                message += " Rollback was incomplete: " + "; ".join(failures)
            else:
                message += " Current timeout values were restored; the saved legacy state remains available."
            raise PowerModeError(message) from exc

    def restore(self):
        state = self._read_state()
        if state and state.get("_legacy_format") and state["work_mode"]:
            raise LegacyPowerStateRecoveryRequired(
                "The saved Work Mode state does not identify its original power scheme."
            )
        if state is None or not state["work_mode"] and not state.get("recovery_required"):
            raise PowerModeError("No enabled Work Mode state is available to restore.")
        scheme = state["scheme_guid"].lower()
        original = state["original"]
        current = self._read_snapshot(scheme)
        try:
            self._set_snapshot(scheme, original)
            state["work_mode"] = False
            state.pop("recovery_required", None)
            self._write_state(state)
        except Exception as exc:
            failures = self._rollback(scheme, current)
            message = f"Could not restore the saved power settings: {exc}"
            if failures:
                message += " Work Mode rollback was incomplete: " + "; ".join(failures)
                state["recovery_required"] = True
                try:
                    self._write_state(state)
                except Exception as state_exc:
                    message += f" Could not save recovery state: {state_exc}"
            elif state["work_mode"]:
                message += " Work Mode settings were restored; saved state remains enabled."
            else:
                message += " Recovery is still required; saved original values remain available for retry."
            raise PowerModeError(message) from exc