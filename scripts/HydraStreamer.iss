#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=x.y.z
#endif

#define AppName "HydraStreamer"
#define AppPublisher "HydraaLabs"
#define AppExeName "HydraStreamer.exe"

[Setup]
AppId={{F6FEBAE4-591B-4FE9-B62D-2392D8C10AC5}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\HydraStreamer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=HydraStreamer-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=yes
RestartApplications=no

[Dirs]
Name: "{app}\bin"
Name: "{app}\logs"

[Files]
Source: "..\dist\HydraStreamer.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\bin\windows\x64\ffmpeg.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\bin\windows\x64\ffprobe.exe"; DestDir: "{app}\bin"; Flags: ignoreversion

[Icons]
Name: "{group}\HydraStreamer"; Filename: "{app}\{#AppExeName}"; Parameters: "--log-file ""{app}\logs\hydrastreamer.log"""
Name: "{userstartup}\HydraStreamer"; Filename: "{app}\{#AppExeName}"; Parameters: "--log-file ""{app}\logs\hydrastreamer.log"""

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--log-file ""{app}\logs\hydrastreamer.log"""; Description: "Start HydraStreamer"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExeName} /F"; Flags: runhidden; RunOnceId: "StopHydraStreamer"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM {#AppExeName} /F', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
