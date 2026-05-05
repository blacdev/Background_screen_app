#define AppName "Background Screen Controller"
#define AppExeName "Background Screen Controller.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppPublisher "Cosoro"
#define AppId "{{A6946DE6-163C-4B5B-A398-7A61D3EA88EE}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVerName={#AppName} {#AppVersion}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={code:GetInstallDir}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
UsedUserAreasWarning=no
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
OutputDir=installer-dist
OutputBaseFilename=BackgroundScreenControllerSetup-{#AppVersion}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
CloseApplicationsFilter={#AppExeName}
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExeName}"; Parameters: "--stop-engine"; Flags: runhidden waituntilterminated skipifdoesntexist

[Code]
function DataScopeName(): string;
begin
  if IsAdminInstallMode then
    Result := 'machine'
  else
    Result := 'user';
end;

function GetDataDir(Param: string): string;
begin
  if IsAdminInstallMode then
    Result := ExpandConstant('{commonappdata}\{#AppName}')
  else
    Result := ExpandConstant('{userappdata}\{#AppName}');
end;

function GetInstallDir(Param: string): string;
begin
  if IsAdminInstallMode then
    Result := ExpandConstant('{autopf}\{#AppName}')
  else
    Result := ExpandConstant('{localappdata}\Programs\{#AppName}');
end;

procedure WriteInstallContext();
var
  ContextJson: string;
  ContextPath: string;
  DataDir: string;
begin
  DataDir := GetDataDir('');
  ForceDirectories(DataDir);
  ContextPath := ExpandConstant('{app}\install_context.json');
  ContextJson :=
    '{' + #13#10 +
    '  "scope": "' + DataScopeName() + '",' + #13#10 +
    '  "data_dir": "' + StringChangeEx(DataDir, '\', '\\', True) + '"' + #13#10 +
    '}' + #13#10;
  SaveStringToFile(ContextPath, ContextJson, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteInstallContext();
end;
