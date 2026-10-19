; Per-user installer (no administrator rights) for the onedir folder that
; build.spec produces in dist\VideoEditor\. Compile with Inno Setup 6:
;   iscc /DAppVersion=0.2.0 installer\VideoEditor.iss
; or run `python build.py --installer`, which passes core.__version__.
; Output: dist\installer\VideoEditor-<version>-setup.exe
;
; .vproj is this app's own format, so the installer registers it (per user,
; under HKCU\Software\Classes): double-clicking a project opens it in the
; editor (main.py takes the .vproj path as its argument).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Open Video Editor"
#define AppExe "VideoEditor.exe"
#define AppKey "OpenVideoEditor"
#define ProjectProgId "OpenVideoEditor.Project"

[Setup]
AppId={{3C5A7565-4CCB-4EE5-982B-6ED509D3C716}
AppName={#AppName}
AppVersion={#AppVersion}
; One publisher name across the projects (K8), the same CompanyName as the exe's version resource.
AppPublisher=MELEJ
DefaultDirName={localappdata}\Programs\{#AppKey}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=VideoEditor-{#AppVersion}-setup
; build.py --sign passes /DSign and the "codesign" tool: Inno Setup signs this installer
; and the uninstaller it writes.
#ifdef Sign
SignTool=codesign
SignedUninstaller=yes
#endif
SetupIconFile=..\assets\icons\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
; Upgrading in place replaces the old files; close a running editor first.
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; An upgrade must not leave files from an older build next to the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\VideoEditor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Classes\.vproj"; ValueType: string; ValueName: ""; ValueData: "{#ProjectProgId}"; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Classes\.vproj\OpenWithProgids"; ValueType: string; ValueName: "{#ProjectProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Classes\{#ProjectProgId}"; ValueType: string; ValueName: ""; ValueData: "Video Editor Project"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#ProjectProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},1"
Root: HKCU; Subkey: "Software\Classes\{#ProjectProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".vproj"; ValueData: ""

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
