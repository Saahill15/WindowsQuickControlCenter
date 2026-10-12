# Windows Quick Control Center — Improved Starter

## New in this version
- Top RAM process viewer with application-family grouping and individual PIDs.
- Work Mode toggle that saves current AC/DC screen-off and sleep timeout values before disabling them.
- Normal Mode restores the saved values.
- CPU sensor information is a non-modal toast that closes automatically.

## Install
1. Extract the ZIP to a stable folder.
2. Run `Install.bat`.
3. Double-click `Launch.vbs` to start without a console.
4. Keep `quickcontrol/config.json` to preserve Developer Launchpad entries.

## Notes
- Power timeout changes use Windows `powercfg`; the app does not require administrator privileges on typical personal Windows installations.
- Display-off and sleep timeout values are restored exactly in seconds using Windows power-setting indexes.
- Direct Do Not Disturb toggling is not implemented; the Focus tab opens Windows notification settings.
- Storage cleanup is still review-only and opens Windows Storage settings.
- Temperature sensors are not universally exposed by Windows.
