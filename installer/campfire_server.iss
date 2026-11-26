; Inno Setup script for Campfire-Server-Setup.exe (the server, with its window).
; Built by .github/workflows/build-windows.yml after PyInstaller has made dist\Campfire.
; By hand on Windows:  pyinstaller --noconfirm campfire.spec
;                      "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\campfire_server.iss
;
; The server keeps its data (campfire.db, campfire_files, logs) next to Campfire Server.exe, i.e. in
; the install folder. Updating installs over it and never touches that data; uninstalling leaves
; it there too.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Campfire Server"
#define MyAppExe "Campfire Server.exe"
#define MyAppId "{{C61D4A27-9E0B-4F3A-8B7C-5D2E1A6F9B44}"
#define MyModelId "Campfire.Server"

[Setup]
; keep this AppId the same forever: it's how a new setup file knows to upgrade the old install
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=ShamanShade
AppPublisherURL=https://github.com/ShamanShade/CampFire_Client
AppSupportURL=https://github.com/ShamanShade/CampFire_Client
; per-user only: the server writes its data next to itself, so it must be somewhere it can write
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Campfire-Server-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Campfire\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Campfire\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; clear out libraries from an older version before copying the new ones (data files aren't in here)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; AppUserModelID: "{#MyModelId}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; AppUserModelID: "{#MyModelId}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Start {#MyAppName} now"; Flags: nowait postinstall skipifsilent
; after "Update server" in the server window (setup run with /relaunch=1), start it again
Filename: "{app}\{#MyAppExe}"; Parameters: "--updated"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;
