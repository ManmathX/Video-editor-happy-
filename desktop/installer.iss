; Installatore Windows dell'app desktop (Inno Setup 6).
; Lo lancia scripts/build_desktop.py --installer, che passa:
;   /DVERSIONE=0.5.3  /DSORGENTE=<cartella costruita da PyInstaller>  /DUSCITA=<dove scrivere>

#define NOME "Topper Mode Video Editor"

[Setup]
AppId={{9C3E7B2A-5D41-4F7E-9A86-2B1D6E0F4C3A}
AppName={#NOME}
AppVersion={#VERSIONE}
AppVerName={#NOME} {#VERSIONE}
AppPublisher=Topper Mode
DefaultDirName={autopf}\{#NOME}
DefaultGroupName={#NOME}
DisableProgramGroupPage=yes
OutputDir={#USCITA}
OutputBaseFilename={#NOME} Setup {#VERSIONE}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#NOME}.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; installa per tutti (amministratore) o solo per l'utente, a scelta di chi installa
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
; l'editor aperto tiene bloccati i suoi file: l'installatore chiede di chiuderlo
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SORGENTE}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#NOME}"; Filename: "{app}\{#NOME}.exe"
Name: "{autodesktop}\{#NOME}"; Filename: "{app}\{#NOME}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#NOME}.exe"; Description: "{cm:LaunchProgram,{#NOME}}"; Flags: nowait postinstall skipifsilent
