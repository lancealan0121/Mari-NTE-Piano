; ============================================================================
; NTE Piano - Inno Setup 6 script
; All-users 安裝到 Program Files；exe 自帶 uac_admin，每次啟動會跳 UAC
; 編譯方式：iscc installer.iss  (Inno Setup 6 已安裝且 iscc 在 PATH)
; ============================================================================

#define MyAppName          "NTE Piano"
#define MyAppVersion       "1.0.0"
#define MyAppPublisher     "NTE Piano"
#define MyAppExeName       "NTEPiano.exe"
#define MyAppSourceDir     "dist\NTEPiano"

[Setup]
AppId={{8F2B4A1E-3D5F-4B6C-9E7A-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
OutputDir=dist
OutputBaseFilename=NTEPiano-Setup
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
SetupIconFile=assets\icon.ico
LicenseFile=LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent runascurrentuser

[UninstallDelete]
; 解除安裝時清掉執行期產出，但保留使用者 songs/ 以免誤刪手動加的譜面
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\.tmp"
Type: filesandordirs; Name: "{app}\__pycache__"
