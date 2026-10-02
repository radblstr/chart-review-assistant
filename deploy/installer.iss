; Inno Setup script for the Physics Chart Review Assistant
; Per-user install (no admin) under %LOCALAPPDATA%. A fixed AppId makes re-running the same
; Setup.exe an upgrade-in-place (or a repair). Python and all packages ship in python\; nothing is
; downloaded. User settings in data\ are preserved across updates. Defines are supplied by
; build_installer.ps1.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "build\payload"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{9C6D2E8A-4B3F-4A1E-9E2D-7A6B5C4D3E2F}
AppName=Physics Chart Review Assistant
AppVersion={#AppVersion}
AppPublisher=Alex Egan
DefaultDirName={localappdata}\ChartReviewAssistant
DefaultGroupName=Physics Chart Review Assistant
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=ChartReviewAssistant-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#PayloadDir}\icon.ico
UninstallDisplayIcon={app}\icon.ico

[Dirs]
Name: "{app}\data"

[InstallDelete]
; Replace the bundled runtime wholesale so no files from an older version linger.
Type: filesandordirs; Name: "{app}\python"

; Leftovers from the old first-launch provisioning layout.
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\install"
Type: files; Name: "{app}\uv.exe"
Type: files; Name: "{app}\provision.cmd"

[Files]
Source: "{#PayloadDir}\THIRD_PARTY_LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\ChartReviewAssistant.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\ChartReviewAssistant-Demo.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\ChartReviewAssistant-debug.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; Config templates: the site copies clinic_config.example.toml to clinic_config.toml and
; db_config.example.toml to db_config.toml in data\ and edits them.
Source: "{#PayloadDir}\data\clinic_config.example.toml"; DestDir: "{app}\data"; Flags: ignoreversion uninsneveruninstall
Source: "{#PayloadDir}\data\db_config.example.toml"; DestDir: "{app}\data"; Flags: ignoreversion uninsneveruninstall

[Icons]
Name: "{userdesktop}\Physics Chart Review Assistant"; Filename: "{app}\ChartReviewAssistant.exe"; IconFilename: "{app}\icon.ico"; WorkingDir: "{app}"
Name: "{userprograms}\Physics Chart Review Assistant"; Filename: "{app}\ChartReviewAssistant.exe"; IconFilename: "{app}\icon.ico"; WorkingDir: "{app}"
Name: "{userprograms}\Physics Chart Review Assistant (Demo)"; Filename: "{app}\ChartReviewAssistant-Demo.exe"; IconFilename: "{app}\icon.ico"; WorkingDir: "{app}"

[UninstallDelete]
; __pycache__ dirs written at runtime are not in the uninstall log.
Type: filesandordirs; Name: "{app}\python"
