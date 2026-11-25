; Inno Setup script for Campfire-Setup.exe (the chat client only).
; Built automatically by .github/workflows/build-windows.yml after PyInstaller has made dist\Campfire.
; To build by hand on Windows:  pyinstaller --noconfirm campfire.spec
;                               "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\campfire.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
; ISCC /DAdmin=1 builds Campfire-Admin-Setup.exe instead: the admin client for the server PC,
; installed separately (own folder, shortcuts and uninstall)
#ifdef Admin
  #define MyAppName "Campfire Admin"
  #define MyAppExe "Campfire Admin.exe"
  #define MyAppId "{{3D9B7A10-52C4-4F8E-A1D6-7E0C9B4F2A61}"
  #define MyOutput "Campfire-Admin-Setup"
  #define MyModelId "Campfire.Admin"
#else
  #define MyAppName "Campfire"
  #define MyAppExe "Campfire.exe"
  #define MyAppId "{{8F3C1E52-6B1A-4D7E-9C55-2B7D4E9A1F30}"
  #define MyOutput "Campfire-Setup"
  #define MyModelId "Campfire.Client"
#endif

[Setup]
; keep this AppId the same forever: it's how a new setup file knows to upgrade the old install
AppId={#MyAppId}
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
OutputBaseFilename={#MyOutput}
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
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; AppUserModelID: "{#MyModelId}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; AppUserModelID: "{#MyModelId}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Start {#MyAppName} now"; Flags: nowait postinstall skipifsilent
