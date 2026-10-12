import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quickcontrol.power import (
    LegacyPowerStateRecoveryRequired,
    PowerModeController,
    PowerModeError,
)


SCHEME = "a1841308-3541-4fab-bc81-f71556f20b4a"
OTHER_SCHEME = "b1841308-3541-4fab-bc81-f71556f20b4a"
INITIAL_VALUES = {
    "monitor": {"ac": 61, "dc": 125},
    "sleep": {"ac": 731, "dc": 10003},
}
SETTING_IDS = {
    "SUB_VIDEO": "monitor",
    "SUB_SLEEP": "sleep",
}


class FakePowerCfg:
    def __init__(self, fail_call=None, fail_calls=None, active_scheme=SCHEME):
        self.values = json.loads(json.dumps(INITIAL_VALUES))
        self.fail_calls = set(fail_calls or ())
        self.active_scheme = active_scheme
        if fail_call is not None:
            self.fail_calls.add(fail_call)
        self.calls = []

    def __call__(self, command):
        self.calls.append(list(command))
        if len(self.calls) in self.fail_calls:
            return subprocess.CompletedProcess(
                command, 1, "", "simulated powercfg failure"
            )

        operation = command[0]
        if operation == "/getactivescheme":
            return subprocess.CompletedProcess(
                command, 0, f"Power Scheme GUID: {self.active_scheme} (Balanced)\n", ""
            )
        if operation == "/query":
            kind = SETTING_IDS[command[2]]
            values = self.values[kind]
            output = (
                f"Current AC Power Setting Index: 0x{values['ac']:08x}\n"
                f"Current DC Power Setting Index: 0x{values['dc']:08x}\n"
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if operation in ("/setacvalueindex", "/setdcvalueindex"):
            source = "ac" if operation == "/setacvalueindex" else "dc"
            kind = SETTING_IDS[command[2]]
            self.values[kind][source] = int(command[4])
            return subprocess.CompletedProcess(command, 0, "", "")
        if operation == "/setactive":
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"Unexpected powercfg command: {command}")


class PowerModeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "power_state.json"

    def controller(self, runner):
        return PowerModeController(self.state_path, command_runner=runner)

    def write_legacy_state(self):
        state = {"work_mode": True, "original": INITIAL_VALUES}
        contents = json.dumps(state)
        self.state_path.write_text(contents, encoding="utf-8")
        return contents

    def test_enable_and_restore_preserves_exact_ac_dc_values(self):
        runner = FakePowerCfg()
        controller = self.controller(runner)

        self.assertTrue(controller.toggle())
        self.assertTrue(controller.is_enabled())
        self.assertEqual(
            runner.values,
            {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}},
        )

        self.assertFalse(controller.toggle())
        self.assertFalse(controller.is_enabled())
        self.assertEqual(runner.values, INITIAL_VALUES)
        self.assertEqual(json.loads(self.state_path.read_text())["original"], INITIAL_VALUES)

    def test_failed_power_setting_command_rolls_back(self):
        runner = FakePowerCfg(fail_call=4)
        controller = self.controller(runner)

        with self.assertRaisesRegex(PowerModeError, "simulated powercfg failure"):
            controller.enable()

        self.assertEqual(runner.values, INITIAL_VALUES)
        self.assertFalse(self.state_path.exists())

    def test_partial_enable_failure_restores_every_captured_value(self):
        runner = FakePowerCfg(fail_call=6)
        controller = self.controller(runner)

        with self.assertRaisesRegex(PowerModeError, "Original timeout values were restored"):
            controller.enable()

        self.assertEqual(runner.values, INITIAL_VALUES)
        self.assertFalse(self.state_path.exists())

    def test_incomplete_rollback_persists_recovery_for_next_launch(self):
        runner = FakePowerCfg(fail_calls={6, 7})
        controller = self.controller(runner)

        with self.assertRaisesRegex(PowerModeError, "Rollback was incomplete"):
            controller.enable()
        saved_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(saved_state["work_mode"])
        self.assertTrue(saved_state["recovery_required"])
        self.assertNotEqual(runner.values, INITIAL_VALUES)

        runner.fail_calls.clear()
        restarted_controller = self.controller(runner)
        self.assertTrue(restarted_controller.is_recovery_required())
        self.assertFalse(restarted_controller.toggle())
        self.assertEqual(runner.values, INITIAL_VALUES)
        self.assertFalse(restarted_controller.is_recovery_required())

    def test_failed_restore_returns_to_work_mode_and_keeps_state(self):
        runner = FakePowerCfg()
        controller = self.controller(runner)
        controller.enable()
        runner.fail_calls.add(len(runner.calls) + 3)

        with self.assertRaisesRegex(PowerModeError, "saved state remains enabled"):
            controller.restore()

        self.assertTrue(controller.is_enabled())
        self.assertEqual(
            runner.values,
            {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}},
        )
        runner.fail_calls.clear()
        self.assertFalse(controller.toggle())
        self.assertEqual(runner.values, INITIAL_VALUES)

    def test_state_write_failure_rolls_back_power_changes(self):
        runner = FakePowerCfg()
        controller = self.controller(runner)

        with patch.object(controller, "_write_state", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(PowerModeError, "Original timeout values were restored"):
                controller.enable()

        self.assertEqual(runner.values, INITIAL_VALUES)
        self.assertFalse(self.state_path.exists())

    def test_legacy_work_mode_is_not_silently_migrated(self):
        runner = FakePowerCfg()
        runner.values = {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}}
        original_state = self.write_legacy_state()

        controller = self.controller(runner)
        with self.assertRaisesRegex(LegacyPowerStateRecoveryRequired, "does not identify"):
            controller.toggle()
        self.assertTrue(controller.is_recovery_required())
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), original_state)
        self.assertEqual(runner.values, {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}})
        self.assertEqual(runner.calls, [])

    def test_legacy_recovery_cancellation_and_scheme_change_preserve_state(self):
        runner = FakePowerCfg()
        runner.values = {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}}
        original_state = self.write_legacy_state()
        controller = self.controller(runner)
        confirmed_scheme = controller.legacy_recovery_scheme()

        with self.assertRaisesRegex(LegacyPowerStateRecoveryRequired, "explicit confirmation"):
            controller.recover_legacy(None)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), original_state)

        runner.active_scheme = OTHER_SCHEME
        with self.assertRaisesRegex(LegacyPowerStateRecoveryRequired, "changed after confirmation"):
            controller.recover_legacy(confirmed_scheme)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), original_state)
        self.assertEqual(runner.values, {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}})
        self.assertFalse(any(call[0] in ("/setacvalueindex", "/setdcvalueindex", "/setactive")
                             for call in runner.calls))

    def test_successful_legacy_recovery_migrates_state_after_restore(self):
        runner = FakePowerCfg()
        runner.values = {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}}
        self.write_legacy_state()
        controller = self.controller(runner)

        confirmed_scheme = controller.legacy_recovery_scheme()
        controller.recover_legacy(confirmed_scheme)

        self.assertEqual(runner.values, INITIAL_VALUES)
        saved_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_state["schema_version"], 1)
        self.assertFalse(saved_state["work_mode"])
        self.assertEqual(saved_state["scheme_guid"], SCHEME)
        self.assertEqual(saved_state["original"], INITIAL_VALUES)
        self.assertFalse(controller.is_enabled())

    def test_failed_legacy_recovery_preserves_saved_state(self):
        runner = FakePowerCfg()
        runner.values = {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}}
        original_state = self.write_legacy_state()
        controller = self.controller(runner)
        confirmed_scheme = controller.legacy_recovery_scheme()
        runner.fail_calls.add(len(runner.calls) + 4)

        with self.assertRaisesRegex(PowerModeError, "saved legacy state remains available"):
            controller.recover_legacy(confirmed_scheme)

        self.assertEqual(runner.values, {"monitor": {"ac": 0, "dc": 0}, "sleep": {"ac": 0, "dc": 0}})
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), original_state)

    def test_invalid_or_missing_saved_state_is_not_guessed(self):
        runner = FakePowerCfg()
        controller = self.controller(runner)
        self.state_path.write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(PowerModeError, "unreadable"):
            controller.toggle()
        self.assertEqual(runner.calls, [])

        self.state_path.unlink()
        with self.assertRaisesRegex(PowerModeError, "No enabled Work Mode state"):
            controller.restore()
        self.assertEqual(runner.calls, [])

    def test_state_survives_restart_and_repeated_toggles(self):
        runner = FakePowerCfg()

        self.assertTrue(self.controller(runner).toggle())
        restarted_controller = self.controller(runner)
        self.assertTrue(restarted_controller.is_enabled())
        self.assertFalse(restarted_controller.toggle())
        self.assertEqual(runner.values, INITIAL_VALUES)

        self.assertTrue(self.controller(runner).toggle())
        self.assertFalse(self.controller(runner).toggle())
        self.assertEqual(runner.values, INITIAL_VALUES)


if __name__ == "__main__":
    unittest.main()