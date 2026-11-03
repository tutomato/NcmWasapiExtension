#ifndef AppVersion
  #define AppVersion "0.7.0-preview.6"
#endif
#ifndef WinVersion
  #define WinVersion "0.7.0.6"
#endif
#ifndef PayloadDir
  #error PayloadDir must be supplied by scripts/build.ps1
#endif
#ifndef ReleaseDir
  #error ReleaseDir must be supplied by scripts/build.ps1
#endif

[Setup]
AppId={{97D3C8CF-7EB4-4710-9BBA-A8266F44CDAC}
AppName=网易云输出扩展
AppVersion={#AppVersion}
AppVerName=网易云输出扩展 {#AppVersion}
VersionInfoVersion={#WinVersion}
AppPublisher=NcmWasapiExtension contributors
DefaultDirName={autopf}\NcmWasapiExtension
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#ReleaseDir}
OutputBaseFilename=NcmWasapiExtension-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
MergeDuplicateFiles=yes
WizardStyle=modern
DisableDirPage=yes
DisableProgramGroupPage=yes
LicenseFile={#PayloadDir}\LICENSE
CloseApplications=no
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=no
UsePreviousTasks=no
UninstallDisplayName=网易云输出扩展

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Same absolute Source paths as the normal entries are merged and stored once.
; These entries are first so preflight need not decompress Frida's native DLL.
Source: "{#PayloadDir}\runtime\*"; DestDir: "{tmp}\NcmWasapiPreflight\runtime"; Excludes: "Lib\*,__pycache__\*,*.pyc"; Flags: dontcopy noencryption recursesubdirs
Source: "{#PayloadDir}\autoload_install.py"; DestDir: "{tmp}\NcmWasapiPreflight"; Flags: dontcopy noencryption
Source: "{#PayloadDir}\compatibility.py"; DestDir: "{tmp}\NcmWasapiPreflight"; Flags: dontcopy noencryption
Source: "{#PayloadDir}\installation.py"; DestDir: "{tmp}\NcmWasapiPreflight"; Flags: dontcopy noencryption
Source: "{#PayloadDir}\supported-clients.json"; DestDir: "{tmp}\NcmWasapiPreflight"; Flags: dontcopy noencryption
Source: "{#PayloadDir}\native\msimg32.dll"; DestDir: "{tmp}\NcmWasapiPreflight\native"; Flags: dontcopy noencryption
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; PrepareToInstall checks root identity and the stopped client before these run.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\licenses"
Type: filesandordirs; Name: "{app}\docs"

[UninstallDelete]
; CurUninstallStepChanged(usUninstall) removes the app-local loader first.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\installed-client.ini"

[Code]
const
  OwnershipMarker = 'NcmWasapiExtension:97D3C8CF-7EB4-4710-9BBA-A8266F44CDAC:v1';

var
  ClientPage: TInputFileWizardPage;
  SelectedClient: String;
  PreflightExtracted: Boolean;
  PostInstallFailed: Boolean;
  PostInstallMessage: String;
  PreviousClientApplied: Boolean;

function IsOwnedDirectory: Boolean;
var
  Marker: AnsiString;
begin
  Result := LoadStringFromFile(ExpandConstant('{app}\.ncm-wasapi-install'), Marker) and
    (String(Marker) = OwnershipMarker) and
    FileExists(ExpandConstant('{app}\controller.py')) and
    FileExists(ExpandConstant('{app}\build-info.json')) and
    not FileExists(ExpandConstant('{app}\cloudmusic.exe')) and
    not FileExists(ExpandConstant('{app}\cloudmusic.dll'));
end;

function HasFiles(const Directory: String): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Directory) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Result := True;
          Break;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function QuoteArg(const Value: String): String;
begin
  if (Pos('"', Value) > 0) or (Pos(#13, Value) > 0) or (Pos(#10, Value) > 0) then
    RaiseException('Unsupported quotation or newline in path. / 路径含不支持的字符。');
  Result := '"' + Value + '"';
end;

function ReadInstalledClientAt(const Directory: String): String;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if LoadStringsFromFile(AddBackslash(Directory) + 'installed-client.ini', Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos('ClientPath=', Lines[I]) = 1 then
      begin
        Result := Copy(Lines[I], 12, MaxInt);
        Exit;
      end;
end;

function ReadInstalledClient: String;
begin
  // Installation preparation and uninstall already have a final app directory.
  Result := ReadInstalledClientAt(ExpandConstant('{app}'));
end;

function IsClientExecutable(const Candidate: String): Boolean;
begin
  Result := (CompareText(ExtractFileName(Candidate), 'cloudmusic.exe') = 0) and FileExists(Candidate);
end;

function FindClient: String;
var
  Candidate: String;
begin
  Candidate := ExpandConstant('{param:NCMCLIENT|}');
  if Candidate <> '' then
  begin
    Result := Candidate;
    Exit;
  end;
  // InitializeWizard calls this before the app constant is initialized.
  // Only consult explicit arguments and client registry entries here.
  // Read previous installation state later, in CurPageChanged.
  Result := '';
  if RegQueryStringValue(HKCU64, 'Software\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe', '', Candidate) and IsClientExecutable(RemoveQuotes(Candidate)) then
    Result := RemoveQuotes(Candidate)
  else if RegQueryStringValue(HKLM64, 'Software\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe', '', Candidate) and IsClientExecutable(RemoveQuotes(Candidate)) then
    Result := RemoveQuotes(Candidate)
  else if RegQueryStringValue(HKLM32, 'Software\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe', '', Candidate) and IsClientExecutable(RemoveQuotes(Candidate)) then
    Result := RemoveQuotes(Candidate)
  else
  begin
    Candidate := ExpandConstant('{commonpf}\NetEase\CloudMusic\cloudmusic.exe');
    if IsClientExecutable(Candidate) then Result := Candidate
    else
    begin
      Candidate := ExpandConstant('{commonpf32}\NetEase\CloudMusic\cloudmusic.exe');
      if IsClientExecutable(Candidate) then Result := Candidate;
    end;
  end;
end;

procedure InitializeWizard;
begin
  ClientPage := CreateInputFilePage(wpWelcome,
    '选择网易云音乐', '安装后仍从原网易云图标启动',
    '请选择 cloudmusic.exe。安装程序只增加本扩展的自动加载文件，不替换网易云程序。安装前请完全退出网易云。');
  ClientPage.Add('网易云音乐程序：', '网易云音乐|cloudmusic.exe|程序文件|*.exe', '.exe');
  ClientPage.Values[0] := FindClient;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  PreviousClient: String;
begin
  if (CurPageID = ClientPage.ID) and not PreviousClientApplied then
  begin
    PreviousClientApplied := True;
    if ExpandConstant('{param:NCMCLIENT|}') = '' then
    begin
      // This page precedes directory selection. WizardDirValue safely returns
      // the current/default directory after the wizard is shown; app may not.
      PreviousClient := ReadInstalledClientAt(WizardDirValue);
      if IsClientExecutable(PreviousClient) then
        ClientPage.Values[0] := PreviousClient;
    end;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ClientPage.ID then
  begin
    Result := IsClientExecutable(ClientPage.Values[0]);
    if not Result then
      SuppressibleMsgBox('请选择有效的 cloudmusic.exe。', mbError, MB_OK, IDOK);
  end;
end;

function ReportMessage(const FileName: String): String;
var
  Lines: TArrayOfString;
  Text: String;
  I, P: Integer;
  Escaped: Boolean;
begin
  Result := '';
  if not LoadStringsFromFile(FileName, Lines) then Exit;
  Text := '';
  for I := 0 to GetArrayLength(Lines) - 1 do Text := Text + Lines[I];
  Log('Autoload helper report: ' + Text);
  P := Pos('"message"', Text);
  if P = 0 then Exit;
  Text := Copy(Text, P + 9, MaxInt);
  P := Pos(':', Text);
  if P = 0 then Exit;
  Text := Trim(Copy(Text, P + 1, MaxInt));
  if (Length(Text) = 0) or (Text[1] <> '"') then Exit;
  Escaped := False;
  for I := 2 to Length(Text) do
  begin
    if Escaped then
    begin
      if Text[I] = 'n' then Result := Result + #13#10
      else if Text[I] = 'r' then Result := Result + #13
      else if Text[I] = 't' then Result := Result + #9
      else Result := Result + Text[I];
      Escaped := False;
    end
    else if Text[I] = '\' then Escaped := True
    else if Text[I] = '"' then Exit
    else Result := Result + Text[I];
  end;
end;

function RunHelper(const RuntimeRoot, Operation, Client, ReportName: String; var Failure: String): Boolean;
var
  ResultCode: Integer;
  ReportPath, Arguments, PythonPath, ScriptPath: String;
begin
  ReportPath := ExpandConstant('{tmp}\') + ReportName;
  DeleteFile(ReportPath);
  PythonPath := AddBackslash(RuntimeRoot) + 'runtime\python.exe';
  ScriptPath := AddBackslash(RuntimeRoot) + 'autoload_install.py';
  Failure := '';
  if not FileExists(PythonPath) or not FileExists(ScriptPath) then
  begin
    Failure := '安装器所需运行文件缺失，请重新下载安装包。';
    Result := False;
    Exit;
  end;
  Arguments := '-B ' + QuoteArg(ScriptPath) + ' ' + Operation +
    ' --client ' + QuoteArg(Client) + ' --root ' + QuoteArg(ExpandConstant('{app}')) +
    ' --report-file ' + QuoteArg(ReportPath);
  Result := Exec(PythonPath, Arguments, RuntimeRoot, SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not Result then
  begin
    Failure := '无法启动自动加载安装检查。';
    Exit;
  end;
  Failure := ReportMessage(ReportPath);
  Result := ResultCode = 0;
  if (not Result) and (Failure = '') then
    Failure := '自动加载操作失败，未继续。错误代码：' + IntToStr(ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PreviousClient, Failure, PreflightRoot: String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\cloudmusic.exe')) or
    FileExists(ExpandConstant('{app}\cloudmusic.dll')) or
    (DirExists(ExpandConstant('{app}')) and HasFiles(ExpandConstant('{app}')) and not IsOwnedDirectory) then
  begin
    Result := '安装目录不属于本扩展，未修改文件。';
    Exit;
  end;
  SelectedClient := ClientPage.Values[0];
  if not IsClientExecutable(SelectedClient) then
  begin
    Result := '请选择有效的网易云 cloudmusic.exe（静默安装可用 /NCMCLIENT="完整路径"）。';
    Exit;
  end;
  PreviousClient := ReadInstalledClient;
  if (PreviousClient <> '') and (CompareText(PreviousClient, SelectedClient) <> 0) then
  begin
    Result := '此扩展已经关联另一处网易云安装。请先卸载原扩展，或选择原客户端路径。';
    Exit;
  end;
  try
    if not PreflightExtracted then
    begin
      ExtractTemporaryFiles('{tmp}\NcmWasapiPreflight\*');
      PreflightExtracted := True;
    end;
    PreflightRoot := ExpandConstant('{tmp}\NcmWasapiPreflight');
    if not RunHelper(PreflightRoot, '--check', SelectedClient, 'ncm-autoload-preflight.json', Failure) then
      Result := Failure;
  except
    Result := '无法完成安装前检查：' + GetExceptionMessage;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  State: TArrayOfString;
  Failure: String;
begin
  if CurStep = ssPostInstall then
  begin
    PostInstallFailed := True;
    SetArrayLength(State, 3);
    State[0] := '[NcmWasapiExtension]';
    State[1] := 'Schema=1';
    State[2] := 'ClientPath=' + SelectedClient;
    if not SaveStringsToUTF8File(ExpandConstant('{app}\installed-client.ini'), State, False) then
    begin
      PostInstallMessage := '无法记录关联的网易云位置，自动加载尚未安装。请重试或卸载本扩展。';
      RaiseException(PostInstallMessage);
    end;
    if not RunHelper(ExpandConstant('{app}'), '--install', SelectedClient, 'ncm-autoload-install.json', Failure) then
    begin
      PostInstallMessage := '扩展文件已复制，但自动加载没有安装成功。' + #13#10 + Failure + #13#10 + '可重新运行安装程序，或卸载本扩展。';
      RaiseException(PostInstallMessage);
    end;
    PostInstallFailed := False;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  if PostInstallFailed then Result := 2 else Result := 0;
end;

function InitializeUninstall: Boolean;
var
  Failure, Client: String;
begin
  Result := False;
  if not IsOwnedDirectory then
  begin
    SuppressibleMsgBox('无法确认此目录属于本扩展，未删除文件。', mbError, MB_OK, IDOK);
    Exit;
  end;
  Client := ReadInstalledClient;
  if Client = '' then
  begin
    SuppressibleMsgBox('关联的网易云位置记录缺失。请先修复安装，以免留下自动加载文件。', mbError, MB_OK, IDOK);
    Exit;
  end;
  Result := RunHelper(ExpandConstant('{app}'), '--check --for-remove', Client, 'ncm-autoload-remove-check.json', Failure);
  if not Result then SuppressibleMsgBox(Failure, mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Failure, Client: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Client := ReadInstalledClient;
    if (Client = '') or not RunHelper(ExpandConstant('{app}'), '--remove', Client, 'ncm-autoload-remove.json', Failure) then
    begin
      SuppressibleMsgBox('自动加载模块未能安全移除，卸载已停止；扩展运行文件会保留。' + #13#10 + Failure, mbError, MB_OK, IDOK);
      Abort;
    end;
  end;
end;
