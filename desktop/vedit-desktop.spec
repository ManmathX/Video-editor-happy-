# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: l'editor come app desktop, con Python e dipendenze dentro.

Si lancia da scripts/build_desktop.py, che prima compila l'interfaccia. Esce una
cartella con due eseguibili sullo stesso pacchetto:

  "Topper Mode Video Editor"   la finestra (su macOS e' il .app)
  "vedit"                      la riga di comando, con console: `vedit mcp` per
                               Claude Code su stdio, `vedit doctor`, `vedit render`...

Gli extra (trascrizione, download dal web, clip html, plugin audio, YOLO) entrano
se sono installati nell'ambiente che costruisce: il workflow desktop li installa
tutti, `vision` solo a richiesta. VEDIT_DESKTOP_LEGGERA=1 li lascia fuori.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (collect_all, collect_data_files, collect_submodules,
                                     copy_metadata)

RADICE = Path(SPECPATH).resolve().parent
NOME = "Topper Mode Video Editor"
UI = RADICE / "frontend" / "dist"
VERSIONE = re.search(r'__version__ = "([^"]+)"',
                     (RADICE / "backend" / "vedit" / "__init__.py").read_text()).group(1)

if not (UI / "index.html").is_file():
    raise SystemExit("frontend/dist is missing: run `npm run build` in frontend/ "
                     "(scripts/build_desktop.py does it for you)")

LEGGERA = os.environ.get("VEDIT_DESKTOP_LEGGERA") == "1"
# senza hook propri: moduli caricati per nome, librerie native e dati vanno presi a mano
SENZA_HOOK = ["faster_whisper", "ctranslate2", "tokenizers", "yt_dlp", "yt_dlp_ejs",
              "pedalboard"]
PESANTI = ["faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
           "huggingface_hub", "yt_dlp", "yt_dlp_ejs", "playwright", "pedalboard",
           "ultralytics", "torch", "torchvision"]


def c_e(modulo: str) -> bool:
    return importlib.util.find_spec(modulo) is not None


# l'interfaccia compilata dove la cerca api._frontend_dir, e le skill incluse
datas = [(str(UI), "vedit/webui")]
datas += collect_data_files("vedit", excludes=["webui", "webui/**"])
binaries = []
# vedit importa molti moduli dentro le funzioni; uvicorn sceglie i protocolli per nome
hidden = collect_submodules("vedit") + collect_submodules("uvicorn")

for m in SENZA_HOOK:
    if c_e(m) and not LEGGERA:
        d, b, h = collect_all(m)
        datas += d
        binaries += b
        hidden += h

# chi legge la propria versione da importlib.metadata a runtime
for dist in ("vedit-mcp", "mcp", "fastapi", "starlette", "uvicorn", "pydantic", "anthropic",
             "pywebview", "faster-whisper", "ctranslate2", "tokenizers", "huggingface-hub",
             "yt-dlp", "playwright", "pedalboard", "ultralytics", "torch", "av", "onnxruntime"):
    try:
        datas += copy_metadata(dist)
    except Exception:  # noqa: BLE001 - non installato in questo ambiente
        pass

escludi = ["tkinter", "_tkinter", "pytest", "_pytest", "IPython", "jupyter", "notebook"]
if LEGGERA:
    escludi += PESANTI
elif not c_e("ultralytics"):
    # analyze prova torch solo per sapere se c'e' una GPU: senza YOLO non serve portarlo
    escludi += ["torch", "torchvision"]

a = Analysis(
    [str(RADICE / "desktop" / "entry.py")],
    pathex=[str(RADICE / "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=escludi,
    noarchive=False,
)
pyz = PYZ(a.pure)

icona = str(RADICE / "desktop" / ("icon.ico" if sys.platform == "win32" else "icon.icns"))
comuni = dict(exclude_binaries=True, debug=False, strip=False, upx=False, icon=icona,
              argv_emulation=False, codesign_identity=None, entitlements_file=None)

# la finestra per prima: su macOS il .app lancia il primo eseguibile del COLLECT
finestra = EXE(pyz, a.scripts, [], name=NOME, console=False, **comuni)
riga = EXE(pyz, a.scripts, [], name="vedit", console=True, **comuni)

coll = COLLECT(finestra, riga, a.binaries, a.datas, strip=False, upx=False, name=NOME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NOME}.app",
        icon=icona,
        bundle_identifier="com.toppermode.videoeditor",
        version=VERSIONE,
        info_plist={
            "CFBundleName": NOME,
            "CFBundleDisplayName": NOME,
            "CFBundleShortVersionString": VERSIONE,
            "CFBundleVersion": VERSIONE,
            "LSApplicationCategoryType": "public.app-category.video",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # la webview chiede microfono e camera per registrare la voce: senza
            # queste righe macOS chiude l'app al primo accesso
            "NSMicrophoneUsageDescription": "To record voice-over in the editor.",
            "NSCameraUsageDescription": "To record from the camera in the editor.",
        },
    )
