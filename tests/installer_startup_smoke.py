"""Run real Inno interactive initialization, cancel before any installation.

The default case passes no /NCMCLIENT, /DIR, /SILENT or /VERYSILENT. Additional
cases check a previous Unicode client association and an explicit path override.
Only our self-closing test setup is launched. File-copy/deletion sections are
removed and installation is guarded. No user installer or client is operated.

Example (Windows, after the build tool has been bootstrapped):
    python tests/installer_startup_smoke.py

An optional --baseline source zip verifies the earlier initialization failure.
Generated installers, logs and data-only fixtures stay in the output directory.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
import zipfile

def instrument(source: str) -> str:
    # Runtime/helper/deployment sections are intentionally absent in the test.
    source = re.sub(r'(?ms)^\[(?:Files|InstallDelete|UninstallDelete|Icons|Run)\]\s*\n.*?(?=^\[|\Z)', '', source)
    source = re.sub(r'(?m)^AppId=.*$', 'AppId={{' + str(uuid.uuid4()).upper() + '}', source)
    source = re.sub(r'(?m)^AppName=.*$', 'AppName=NCM default startup regression (self-closing)', source)
    source = re.sub(r'(?m)^AppVerName=.*$', 'AppVerName=NCM default startup regression', source)
    source = re.sub(r'(?m)^OutputBaseFilename=.*$', 'OutputBaseFilename=default-startup-regression', source)
    source = source.replace('procedure InitializeWizard;', 'procedure RegressionInitializeWizard;', 1)
    if 'procedure CurPageChanged(' in source:
        source = source.replace('procedure CurPageChanged(', 'procedure RegressionCurPageChanged(', 1)
    else:
        source += '\nprocedure RegressionCurPageChanged(CurPageID: Integer);\nbegin\nend;\n'
    source = source.replace('function PrepareToInstall(', 'function RegressionPrepareToInstall(', 1)
    source = source.replace('procedure CurStepChanged(', 'procedure RegressionCurStepChanged(', 1)
    source += r'''

procedure InitializeWizard;
begin
  try
    RegressionInitializeWizard;
    Log('REGRESSION_INITIALIZE_COMPLETE');
  except
    Log('REGRESSION_INITIALIZE_ERROR: ' + GetExceptionMessage);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  Log('REGRESSION_PAGE_REACHED: ' + IntToStr(CurPageID));
  if CurPageID = ClientPage.ID then
  begin
    try
      RegressionCurPageChanged(CurPageID);
      Log('REGRESSION_CLIENT_PAGE_COMPLETE');
      Log('REGRESSION_CLIENT_SELECTION: ' + ClientPage.Values[0]);
    except
      Log('REGRESSION_CLIENT_PAGE_ERROR: ' + GetExceptionMessage);
    end;
    PostMessage(WizardForm.Handle, $0010, 0, 0);
  end
  else if CurPageID = wpWelcome then
    WizardForm.NextButton.OnClick(WizardForm.NextButton)
  else
  begin
    Log('REGRESSION_UNEXPECTED_PAGE');
    PostMessage(WizardForm.Handle, $0010, 0, 0);
  end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  Cancel := True;
  Confirm := False;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := 'REGRESSION_GUARD_NO_INSTALLATION_ALLOWED';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    RaiseException('REGRESSION_GUARD_NO_INSTALLATION_ALLOWED');
end;
'''
    return source


def run_case(label: str, source: str, output: Path, compiler: Path, extra_args=()) -> dict:
    case = output / label
    case.mkdir()
    (case / 'LICENSE').write_text('Test-only installer. Never installs files.\n', encoding='utf-8')
    script = case / 'test.iss'
    script.write_text(instrument(source), encoding='utf-8-sig')
    compiled = subprocess.run([str(compiler), '/Q', '/DPayloadDir=' + str(case),
        '/DReleaseDir=' + str(case), str(script)], capture_output=True, text=True,
        encoding='utf-8', errors='replace', creationflags=0x08000000)
    (case / 'compile.log').write_text(compiled.stdout + compiled.stderr, encoding='utf-8')
    if compiled.returncode:
        raise RuntimeError(label + ' test compilation failed: ' + compiled.stdout + compiled.stderr)
    exe = case / 'default-startup-regression.exe'
    log = case / 'startup.log'
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    # Deliberately no client/directory or silent option. Wrapper self-cancels.
    process = subprocess.Popen([str(exe), '/CURRENTUSER', '/LOG=' + str(log), *extra_args],
        startupinfo=startup, creationflags=0x08000000)
    try:
        exit_code = process.wait(25)
    except subprocess.TimeoutExpired:
        # Only this still-live Popen-owned test setup and its child process.
        subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
            capture_output=True, creationflags=0x08000000)
        process.wait(5)
        raise RuntimeError(label + ' self-closing test timed out')
    text = log.read_text(encoding='utf-8-sig', errors='replace')
    result = {'label': label, 'sourceSha256': hashlib.sha256(source.encode()).hexdigest(),
        'exitCode': exit_code, 'initializeComplete': 'REGRESSION_INITIALIZE_COMPLETE' in text,
        'clientPageComplete': 'REGRESSION_CLIENT_PAGE_COMPLETE' in text,
        'initializationError': next((line.split('REGRESSION_INITIALIZE_ERROR:', 1)[1].strip()
            for line in text.splitlines() if 'REGRESSION_INITIALIZE_ERROR:' in line), None),
        'clientPageError': next((line.split('REGRESSION_CLIENT_PAGE_ERROR:', 1)[1].strip()
            for line in text.splitlines() if 'REGRESSION_CLIENT_PAGE_ERROR:' in line), None),
        'clientSelection': next((line.split('REGRESSION_CLIENT_SELECTION:', 1)[1].strip()
            for line in text.splitlines() if 'REGRESSION_CLIENT_SELECTION:' in line), None),
        'installationStarted': 'REGRESSION_GUARD_NO_INSTALLATION_ALLOWED' in text or 'Installation process succeeded' in text,
        'unexpectedPage': 'REGRESSION_UNEXPECTED_PAGE' in text}
    print(json.dumps(result), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1],
                        help='Project root; defaults to the parent of tests/')
    parser.add_argument('--compiler', type=Path,
                        help='ISCC.exe; defaults to REPO/.build/InnoSetup/ISCC.exe')
    parser.add_argument('--output', type=Path,
                        help='Generated test root; defaults to REPO/.build/installer-startup')
    parser.add_argument('--baseline', type=Path,
                        help='Optional earlier source zip expected to reproduce the initialization error')
    args = parser.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    compiler = (args.compiler or repo / '.build/InnoSetup/ISCC.exe').expanduser().resolve()
    output_root = (args.output or repo / '.build/installer-startup').expanduser().resolve()
    if not compiler.is_file():
        parser.error('Inno Setup compiler not found; pass --compiler or run scripts/build.ps1 first')
    after = (repo / 'installer/setup.iss').read_text(encoding='utf-8-sig')
    output = output_root / ('run-' + str(time.time_ns()))
    output.mkdir(parents=True)
    old_result = None
    if args.baseline:
        with zipfile.ZipFile(args.baseline) as archive:
            candidates = [name for name in archive.namelist()
                          if name == 'installer/setup.iss' or name.endswith('/installer/setup.iss')]
            if len(candidates) != 1:
                raise ValueError('Baseline archive must contain exactly one installer/setup.iss')
            before = archive.read(candidates[0]).decode('utf-8-sig')
        old_result = run_case('before', before, output, compiler)
        assert old_result['initializationError'] and 'before it was initialized' in old_result['initializationError']
        assert not old_result['initializeComplete']
    new_result = run_case('current-default', after, output, compiler)
    assert new_result['initializeComplete'] and new_result['clientPageComplete']
    assert not new_result['initializationError'] and not new_result['clientPageError']
    assert not new_result['installationStarted'] and not new_result['unexpectedPage']
    # These data-only fixture clients are never launched or installed into.
    previous_dir = output / 'previous install 中文'
    previous_dir.mkdir()
    previous_client = output / 'previous client 中文' / 'cloudmusic.exe'
    previous_client.parent.mkdir()
    previous_client.write_bytes(b'never executed client fixture')
    explicit_client = output / 'manual client 中文' / 'cloudmusic.exe'
    explicit_client.parent.mkdir()
    explicit_client.write_bytes(b'never executed client fixture')
    (previous_dir / 'installed-client.ini').write_text('[NcmWasapiExtension]\nSchema=1\nClientPath=' + str(previous_client) + '\n', encoding='utf-8-sig')
    prior_result = run_case('previous-association', after, output, compiler, ['/DIR=' + str(previous_dir)])
    explicit_result = run_case('explicit-priority', after, output, compiler,
        ['/DIR=' + str(previous_dir), '/NCMCLIENT=' + str(explicit_client)])
    for result in (prior_result, explicit_result):
        assert result['initializeComplete'] and result['clientPageComplete']
        assert not result['initializationError'] and not result['clientPageError']
        assert not result['installationStarted'] and not result['unexpectedPage']
    assert prior_result['clientSelection'] == str(previous_client)
    assert explicit_result['clientSelection'] == str(explicit_client)
    (output / 'result.json').write_text(json.dumps({'before': old_result, 'after': new_result,
        'previousAssociation': prior_result, 'explicitPriority': explicit_result}, indent=2), encoding='utf-8')
    print('PASS: default interactive startup and deferred client page, no installation', flush=True)
    print(str(output), flush=True)


if __name__ == '__main__':
    main()
