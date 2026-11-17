; ==============================================================
; Jarvis Maps AI — Professional Windows Installer Script
; Inno Setup 6.x Compliant
; ==============================================================

#define MyAppName "Jarvis Maps AI"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Jarvis Spatial Intelligence"
#define MyAppURL "http://localhost:8080"
#define MyAppExeName "JarvisMapsAI.exe"
#define DistDir "..\dist\JarvisMapsAI"
#define AssetsDir "..\assets"

[Setup]
; Unique Application GUID for version tracking and updates
AppId={{D37E6B20-5FA1-4E65-98B2-74E06A32F1A8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; Smart installation directory (User-level or Machine-level without mandatory admin)
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes

; Destination of the compiled single setup installer
OutputDir=..\dist
OutputBaseFilename=JarvisMapsAI_Setup_v1.1
SetupIconFile={#AssetsDir}\app_icon.ico
UninstallDisplayIcon={app}\assets\app_icon.ico

; Modern look & feel
WizardStyle=modern
WizardResizable=no
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64

; Allow installing for current user without UAC prompt, or system-wide if elevated
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Application binaries and dependencies compiled by PyInstaller
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; High-resolution application icon
Source: "{#AssetsDir}\app_icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Dirs]
; Pre-provision persistent user data directory in %LOCALAPPDATA%\JarvisMapsAI
Name: "{localappdata}\JarvisMapsAI"; Flags: uninsneveruninstall
Name: "{localappdata}\JarvisMapsAI\logs"; Flags: uninsneveruninstall
Name: "{localappdata}\JarvisMapsAI\cache"; Flags: uninsneveruninstall

[Icons]
; Start Menu and Desktop shortcuts with custom Jarvis Maps AI icon (Always created unconditionally)
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app_icon.ico"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app_icon.ico"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app_icon.ico"; WorkingDir: "{app}"

[Run]
; Launch application option after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
// Guarantee shortcut exists on the active Windows Desktop even if redirected by OneDrive
procedure CurStepChanged(CurStep: TSetupStep);
var
  ActiveDesktop: String;
  SourceLnk: String;
  DestLnk: String;
begin
  if CurStep = ssPostInstall then
  begin
    SourceLnk := ExpandConstant('{autodesktop}\{#MyAppName}.lnk');
    if not FileExists(SourceLnk) then
      SourceLnk := ExpandConstant('{userdesktop}\{#MyAppName}.lnk');

    // Query active Explorer desktop folder from registry (handles OneDrive redirection)
    if RegQueryStringValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders', 'Desktop', ActiveDesktop) then
    begin
      ActiveDesktop := ExpandConstant(ActiveDesktop);
      if (ActiveDesktop <> '') and DirExists(ActiveDesktop) and FileExists(SourceLnk) then
      begin
        DestLnk := ActiveDesktop + '\{#MyAppName}.lnk';
        if not FileExists(DestLnk) then
          FileCopy(SourceLnk, DestLnk, False);
      end;
    end;
  end;
end;
