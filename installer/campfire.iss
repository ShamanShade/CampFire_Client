; Inno Setup script for Campfire-Setup.exe (the chat client only).
; Built automatically by .github/workflows/build-windows.yml after PyInstaller has made dist\Campfire.
; To build by hand on Windows:  pyinstaller --noconfirm campfire.spec
;                               "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\campfire.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Campfire"
#define MyAppExe "Campfire.exe"

[Setup]
; keep this AppId the same forever: it's how a new setup file knows to upgrade the old install
AppId={{8F3C1E52-6B1A-4D7E-9C55-2B7D4E9A1F30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=ShamanShade
AppPublisherURL=https://github.com/ShamanShade/CampFire_Client
AppSupportURL=https://github.com/ShamanShade/CampFire_Client
; no admin needed: installs for the current user in %LocalAppData%\Programs\Campfire
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Campfire-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; closes a running Campfire before updating it
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

[InstallDelete]
; clear out libraries from an older version before copying the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; AppUserModelID: "Campfire.Client"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; AppUserModelID: "Campfire.Client"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Start Campfire now"; Flags: nowait postinstall skipifsilent
