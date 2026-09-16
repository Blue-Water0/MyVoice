; MyVoice Windows installer (Inno Setup 6.x script).
;
; Build (from the repository root, after the PyInstaller one-folder build
; has produced "dist\MyVoice\"):
;
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" myvoice_win\setup.iss
;
; Produces: dist\installer\MyVoice-Setup-1.0.0.exe
;
; Per design doc "docs/superpowers/plans/2026-07-13-windows-port-plan.md"
; section "## 3. Packaging", "Installer Contents" table:
;   - Per-user install, no Administrator privileges required.
;   - Install location: %LOCALAPPDATA%\MyVoice
;   - Start Menu shortcut under a "MyVoice" folder.
;   - Uninstaller registered in Windows Apps & Features.
;   - Autostart: optional checkbox, UNCHECKED by default.
;   - Desktop shortcut: optional, UNCHECKED by default.
;
; Scope note (disclosed, not silently dropped): the design doc's table also
; lists an admin/system-wide "%ProgramFiles%\MyVoice" install mode as
; "optional". This script implements only the per-user mode the task brief
; asks for (PrivilegesRequired=lowest, single fixed install root). Adding
; the dual per-user/per-machine toggle Inno Setup supports via
; PrivilegesRequiredOverridesAllowed would also change the *shape* of
; DefaultDirName (Inno's {autopf} macro resolves to
; "{localappdata}\Programs\MyVoice" for the per-user case, not the bare
; "{localappdata}\MyVoice" this table specifies) -- reconciling that exactly
; needs Pascal Script this project has no way to test in this sandbox, so
; it is left as a follow-up rather than guessed at. See task-13-report.md.

#define MyAppName "MyVoice"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "MyVoice"
#define MyAppExeName "MyVoice.exe"

[Setup]
; Fixed GUID for this application -- generated once, must never change
; across releases (Inno/Windows use it to recognize "this is the same app"
; across versions for upgrade/uninstall purposes). Note the doubled opening
; brace: that is Inno's literal-brace escape ("{{" -> "{"), the standard,
; slightly odd-looking convention every Inno-generated script uses for
; AppId -- NOT a typo. Do not "fix" it down to a single brace, that would
; make Inno's compiler try to resolve {BBF261E0-...} as a constant
; reference and fail with an "unknown constant" error.
AppId={{BBF261E0-7F25-420B-ABD7-6B7760A40FB5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
; Per-user, no-admin install (design doc: "no Administrator required").
PrivilegesRequired=lowest
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Only one shortcut folder is ever created ("MyVoice") -- no need to ask
; the user to pick/rename a Start Menu group.
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
OutputDir=..\dist\installer
OutputBaseFilename=MyVoice-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Inno Setup >= 6.3 syntax. If building with an older Inno Setup 6.x that
; predates x64compatible, replace with: ArchitecturesInstallIn64BitMode=x64
ArchitecturesInstallIn64BitMode=x64compatible
; Prepared for Authenticode re-signing after build (design doc: "Code
; signing | Prepared for Authenticode signing"). No SignTool= directive is
; set here -- signing is done as a separate post-build step per the design
; doc's Build Process step 6 (signtool sign ... MyVoice-Setup-1.0.0.exe),
; not by Inno Setup itself.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Both optional, both UNCHECKED by default per design doc / task brief.
Name: "autostart"; Description: "Start {#MyAppName} automatically when I sign in"; Flags: unchecked
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
; The full PyInstaller one-folder output tree (MyVoice.exe + myvoice_win/,
; myvoice/, PySide6/, faster_whisper/, ctranslate2/, onnxruntime/, numpy/,
; sounddevice/, webrtcvad/, and every supporting DLL PyInstaller collected
; -- see myvoice_win/myvoice.spec). "ignoreversion" because most of this
; payload is Python bytecode / data files with no meaningful Win32 version
; resource to compare against.
Source: "..\dist\MyVoice\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Optional autostart entry, written only if the "autostart" task is
; checked. Same HKCU Run key + value name myvoice_win/app.py's own
; _apply_autostart() uses (Software\Microsoft\Windows\CurrentVersion\Run,
; value "MyVoice") so the installer-time checkbox and the in-app Settings
; toggle both read/write the exact same registry entry rather than two
; independent ones that could drift out of sync. uninsdeletevalue removes
; it again on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "MyVoice"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

; Deliberately NO [UninstallDelete] entry forcing removal of "{app}" itself.
; myvoice_win/paths.py's cache_dir() is "%LOCALAPPDATA%\MyVoice\cache" --
; the SAME top-level folder this installer uses as {app} -- so downloaded
; Whisper model weights (hundreds of MB to a few GB) end up living inside
; the installed application's own directory tree, not a separate user-data
; location. Inno Setup's default uninstall behavior already prompts the
; user ("Do you want to completely remove {app} and all of its contents?")
; when it finds files left over after removing everything it explicitly
; installed -- that prompt is what protects the model cache from being
; silently deleted on uninstall, so no additional [UninstallDelete] directive
; is added here. See task-13-report.md for the cross-task note on this.
