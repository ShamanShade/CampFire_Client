; Inno Setup script for Campfire-Setup.exe (the chat client only).
; Built automatically by .github/workflows/build-windows.yml after PyInstaller has made dist\Campfire.
; To build by hand on Windows:  pyinstaller --noconfirm campfire.spec
;                               "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\campfire.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Campfire"
#define MyAppExe "Campfire.exe"
#define MyAppId "{{8F3C1E52-6B1A-4D7E-9C55-2B7D4E9A1F30}"
#define MyOutput "Campfire-Setup"
#define MyModelId "Campfire.Client"
; the old separate "Campfire Admin" app (now part of Campfire): removed when Campfire installs
#define OldAdminKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{3D9B7A10-52C4-4F8E-A1D6-7E0C9B4F2A61}_is1"

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
; after an update started from inside Campfire (setup run with /relaunch=1), open it again
Filename: "{app}\{#MyAppExe}"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;

{ Campfire Admin was a separate app before Campfire learned to find a server on the same PC.
  If it's installed, uninstall it quietly (its settings are shared with Campfire, so nothing is lost). }
procedure RemoveOldAdmin(Root: Integer);
var
  Uninstaller: String;
  Code: Integer;
begin
  if RegQueryStringValue(Root, '{#OldAdminKey}', 'UninstallString', Uninstaller) then
    Exec(RemoveQuotes(Uninstaller), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE,
         ewWaitUntilTerminated, Code);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    RemoveOldAdmin(HKCU);
    if IsAdminInstallMode then
      RemoveOldAdmin(HKLM);
  end;
end;
