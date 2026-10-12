#define MyAppName "Quick Control Center"
#ifndef MyAppVersion
  #error MyAppVersion must be supplied by Build-Windows.bat.
#endif
#define MyAppPublisher "Quick Control Center"
#define MyAppExeName "QuickControlCenter.exe"

[Setup]
AppId={{A7D23F15-7BCE-4D63-9D32-6E88A1E0CDA1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\QuickControlCenter
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=QuickControlCenter-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\QuickControlCenter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Quick Control Center"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Quick Control Center"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Quick Control Center"; Flags: postinstall nowait skipifsilent
