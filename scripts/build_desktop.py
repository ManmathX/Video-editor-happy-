"""Costruisce l'app desktop: finestra + CLI `vedit`, con Python e dipendenze dentro.

    python scripts/build_desktop.py               # compila la UI se serve e impacchetta
    python scripts/build_desktop.py --no-ui       # riusa frontend/dist com'e'
    python scripts/build_desktop.py --installer   # Windows: anche l'installatore (Inno Setup)
    python scripts/build_desktop.py --leggera     # senza gli extra pesanti
    python scripts/build_desktop.py --prova       # solo la prova dell'app gia' costruita

Si costruisce sul sistema di destinazione: l'.exe per Windows su Windows, il .app
su macOS (PyInstaller non incrocia). Li fa tutti e due .github/workflows/desktop.yml.

Prima: pip install -e ".[chat,desktop,transcribe,html,web,plugin]" pyinstaller
(gli extra che mancano restano fuori dall'app, come da `pip install`).

Esce in dist-desktop/:
  macOS    "Topper Mode Video Editor.app" (la CLI e' Contents/MacOS/vedit)
  Windows  "Topper Mode Video Editor/" con i due .exe; con --installer anche
           "Topper Mode Video Editor Setup <versione>.exe"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
NOME = "Topper Mode Video Editor"
SPEC = RADICE / "desktop" / "vedit-desktop.spec"
USCITA = RADICE / "dist-desktop"
LAVORO = RADICE / "build" / "desktop"
FRONT = RADICE / "frontend"
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"

# gli extra che, se installati qui, devono funzionare anche dentro l'app
EXTRA = ["anthropic", "faster_whisper", "ctranslate2", "yt_dlp", "playwright.sync_api",
         "pedalboard", "ultralytics"]


def versione() -> str:
    testo = (RADICE / "backend" / "vedit" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', testo).group(1)


def esegui(cmd: list[str], cwd: Path = RADICE, env: dict | None = None) -> None:
    print(f"  $ {' '.join(cmd)}", flush=True)
    if subprocess.run(cmd, cwd=cwd, env=env, shell=WIN and cmd[0] == "npm").returncode != 0:
        raise SystemExit(f"failed: {' '.join(cmd)}")


def compila_ui() -> None:
    if not (FRONT / "node_modules").is_dir():
        esegui(["npm", "ci"], FRONT)
    esegui(["npm", "run", "build"], FRONT)


def impacchetta(leggera: bool) -> None:
    for modulo, come in (("PyInstaller", "pyinstaller"), ("webview", "'vedit-mcp[desktop]'")):
        if importlib.util.find_spec(modulo) is None:
            raise SystemExit(f"{modulo} is missing: pip install {come}")
    env = {**os.environ, "VEDIT_DESKTOP_LEGGERA": "1" if leggera else "0"}
    esegui([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean",
            "--distpath", str(USCITA), "--workpath", str(LAVORO)], env=env)
    if MAC:
        # il .app contiene gia' tutto: la cartella intermedia e' un doppione da centinaia di MB
        shutil.rmtree(USCITA / NOME, ignore_errors=True)


def iscc() -> str | None:
    if exe := shutil.which("iscc"):
        return exe
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (p := Path(base) / "Inno Setup 6" / "ISCC.exe").is_file():
            return str(p)
    return None


def installatore() -> Path:
    if not WIN:
        raise SystemExit("the installer is for Windows: on macOS the .app is the deliverable")
    exe = iscc()
    if not exe:
        raise SystemExit("Inno Setup 6 is missing: choco install innosetup "
                         "(or winget install JRSoftware.InnoSetup)")
    v = versione()
    esegui([exe, f"/DVERSIONE={v}", f"/DSORGENTE={USCITA / NOME}", f"/DUSCITA={USCITA}",
            str(RADICE / "desktop" / "installer.iss")])
    return USCITA / f"{NOME} Setup {v}.exe"


# --------------------------------------------------------------------------
# prova: l'app costruita si avvia, serve la UI e parla MCP (HTTP e stdio)
# --------------------------------------------------------------------------


def cli_app() -> Path:
    if MAC:
        return USCITA / f"{NOME}.app" / "Contents" / "MacOS" / "vedit"
    return USCITA / NOME / ("vedit.exe" if WIN else "vedit")


def _ok(cosa: str) -> None:
    print(f"  ok  {cosa}", flush=True)


def _richiesta_mcp() -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "build-desktop", "version": "0"}}}).encode()


def prova() -> None:
    cli = cli_app()
    if not cli.is_file():
        raise SystemExit(f"{cli} not found: build first")
    print(f"testing {cli}", flush=True)

    if MAC:
        info = plistlib.loads((cli.parents[1] / "Info.plist").read_bytes())
        assert info["CFBundleExecutable"] == NOME, info["CFBundleExecutable"]
        _ok(f"the .app opens the window ({info['CFBundleExecutable']})")

    def py(codice: str) -> str:
        p = subprocess.run([str(cli), "-c", codice], capture_output=True, text=True, timeout=300)
        assert p.returncode == 0, f"{codice}\n{p.stdout}\n{p.stderr}"
        return p.stdout.strip()

    # la stessa via dei lavori figli (trascrizione): sys.executable -c ...
    py("import vedit.api, vedit.mcp_server, vedit.desktop, numpy, PIL, webview")
    _ok("core modules and the -c child-process path")
    presenti = [m for m in EXTRA if importlib.util.find_spec(m.split(".")[0]) is not None]
    if presenti:
        py("import importlib\nfor m in %r: importlib.import_module(m)" % presenti)
        _ok("extras: " + ", ".join(presenti))
    if "yt_dlp" in presenti:
        p = subprocess.run([str(cli), "-m", "yt_dlp", "--version"], capture_output=True,
                           text=True, timeout=120)
        assert p.returncode == 0 and p.stdout.strip(), p.stderr
        _ok(f"yt-dlp via -m ({p.stdout.strip()})")

    # interfaccia e MCP su HTTP, come li usa la finestra e Claude Code
    porta = 8799
    server = subprocess.Popen([str(cli), "ui", "--no-browser", "--port", str(porta)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{porta}"
    diretto = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        scadenza = time.time() + 120
        while True:
            try:
                stato = json.loads(diretto.open(f"{base}/api/state", timeout=2).read())
                break
            except OSError:
                if server.poll() is not None or time.time() > scadenza:
                    raise SystemExit("the packaged server did not start:\n"
                                     + (server.stdout.read() if server.poll() is not None else ""))
                time.sleep(1)
        assert "effects" in stato
        pagina = diretto.open(base + "/", timeout=10).read().decode()
        assert '<div id="root">' in pagina, pagina[:300]
        _ok("server, /api/state and the compiled interface")
        req = urllib.request.Request(f"{base}/mcp/", data=_richiesta_mcp(), method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        corpo = diretto.open(req, timeout=30).read().decode()
        assert "serverInfo" in corpo, corpo[:300]
        _ok("MCP over HTTP (/mcp/, what the window and Claude Code use)")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()

    # MCP su stdio: `claude mcp add vedit -- <app>/vedit mcp`
    mcp = subprocess.Popen([str(cli), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    risposta: list[bytes] = []
    lettore = threading.Thread(target=lambda: risposta.append(mcp.stdout.readline()), daemon=True)
    lettore.start()
    mcp.stdin.write(_richiesta_mcp() + b"\n")
    mcp.stdin.flush()
    lettore.join(timeout=120)
    mcp.kill()
    assert risposta and b"serverInfo" in risposta[0], (risposta, mcp.stderr.read()[-2000:])
    _ok("MCP over stdio (`vedit mcp`)")
    print("packaged app OK", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-ui", action="store_true", help="reuse frontend/dist as it is")
    ap.add_argument("--installer", action="store_true", help="Windows: build the setup .exe too")
    ap.add_argument("--leggera", action="store_true",
                    help="leave out transcription, web download, html render, plugins, YOLO")
    ap.add_argument("--prova", action="store_true", help="only test the app already built")
    a = ap.parse_args()

    if a.prova:
        prova()
        return
    if not (a.no_ui and (FRONT / "dist" / "index.html").is_file()):
        compila_ui()
    impacchetta(a.leggera)
    fatto = [USCITA / (f"{NOME}.app" if MAC else NOME)]
    if a.installer:
        fatto.append(installatore())
    print("\nbuilt:")
    for f in fatto:
        print(f"  {f}")


if __name__ == "__main__":
    main()
