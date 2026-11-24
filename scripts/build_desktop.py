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


def _coda(testo: str | bytes, n: int = 3000) -> str:
    if isinstance(testo, bytes):
        testo = testo.decode("utf-8", "replace")
    return testo if len(testo) <= n else "…" + testo[-n:]


def _segnala(titolo: str, dettaglio: str) -> None:
    """Su GitHub Actions l'errore diventa un'annotazione: si legge dalla pagina
    dell'esecuzione senza scaricare i log (che vogliono l'accesso)."""
    print(f"  FAIL {titolo}\n{dettaglio}", flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        testo = dettaglio.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::error title={titolo}::{testo}", flush=True)


def prova() -> None:
    """Ogni controllo gira anche se uno prima fallisce: una sola costruzione dice tutto."""
    cli = cli_app()
    if not cli.is_file():
        raise SystemExit(f"{cli} not found: build first")
    print(f"testing {cli}", flush=True)
    falliti: list[str] = []

    def controllo(titolo: str):
        def deco(f):
            try:
                esito = f()
                _ok(titolo + (f" ({esito})" if esito else ""))
            except Exception as e:  # noqa: BLE001 - raccolto e riportato
                falliti.append(titolo)
                _segnala(titolo, _coda(f"{type(e).__name__}: {e}"))
            return f
        return deco

    def py(codice: str) -> str:
        p = subprocess.run([str(cli), "-c", codice], capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError(f"exit {p.returncode}\n{_coda(p.stdout, 1000)}\n{_coda(p.stderr)}")
        return p.stdout.strip()

    if MAC:
        @controllo("the .app opens the window")
        def _():
            info = plistlib.loads((cli.parents[1] / "Info.plist").read_bytes())
            if info["CFBundleExecutable"] != NOME:
                raise RuntimeError(f"CFBundleExecutable is {info['CFBundleExecutable']!r}")
            return info["CFBundleExecutable"]

    # la stessa via dei lavori figli (trascrizione): sys.executable -c ...
    @controllo("core modules through the -c child-process path")
    def _():
        py("import vedit.api, vedit.mcp_server, vedit.desktop, numpy, PIL, webview")

    for modulo in EXTRA:
        if importlib.util.find_spec(modulo.split(".")[0]) is None:
            continue

        @controllo(f"extra {modulo}")
        def _(modulo=modulo):
            py(f"import {modulo}")

    if importlib.util.find_spec("yt_dlp") is not None:
        @controllo("yt-dlp via -m")
        def _():
            p = subprocess.run([str(cli), "-m", "yt_dlp", "--version"], capture_output=True,
                               text=True, timeout=120)
            if p.returncode != 0 or not p.stdout.strip():
                raise RuntimeError(f"exit {p.returncode}\n{_coda(p.stderr)}")
            return p.stdout.strip()

    # senza ffmpeg /api/state risponde 500: l'app lo scarica al primo avvio, qui
    # si fa lo stesso con la CLI dell'app (sulle macchine di CI non c'e')
    @controllo("ffmpeg found or downloaded by the app (what the first launch does)")
    def _():
        trova = "from vedit import ffmpeg; print(ffmpeg.binary('ffmpeg'), ffmpeg.binary('ffprobe'))"
        try:
            return py(trova)
        except RuntimeError:
            p = subprocess.run([str(cli), "install-ffmpeg"], capture_output=True, text=True,
                               timeout=900)
            if p.returncode != 0:
                raise RuntimeError(f"install-ffmpeg exit {p.returncode}\n{_coda(p.stdout, 1000)}"
                                   f"\n{_coda(p.stderr)}") from None
            # scaricato non basta: deve anche partire su questa macchina
            return "downloaded: " + py(trova + "; import subprocess; subprocess.run("
                                       "[ffmpeg.binary('ffmpeg'), '-version'], check=True, "
                                       "capture_output=True)")

    # interfaccia e MCP su HTTP, come li usa la finestra e Claude Code
    @controllo("server, interface and MCP over HTTP (what the window and Claude Code use)")
    def _():
        import tempfile
        import urllib.error

        porta = 8799
        log = Path(tempfile.mkstemp(prefix="vedit-ui-", suffix=".log")[1])
        with open(log, "w") as out:
            server = subprocess.Popen([str(cli), "ui", "--no-browser", "--port", str(porta)],
                                      stdout=out, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{porta}"
        diretto = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            scadenza = time.time() + 120
            while True:
                try:
                    stato = json.loads(diretto.open(f"{base}/api/state", timeout=5).read())
                    break
                except urllib.error.HTTPError as e:
                    # il server c'e' e ha risposto con un errore: aspettare non serve
                    raise RuntimeError(f"/api/state answered {e.code}: {_coda(e.read(), 500)}\n"
                                       + _coda(log.read_text(errors="replace"))) from None
                except OSError:
                    if server.poll() is not None or time.time() > scadenza:
                        raise RuntimeError(f"the server did not start (exit {server.poll()}):\n"
                                           + _coda(log.read_text(errors="replace")))
                    time.sleep(1)
            if "effects" not in stato:
                raise RuntimeError(f"/api/state: {_coda(json.dumps(stato), 500)}")
            pagina = diretto.open(base + "/", timeout=10).read().decode()
            if '<div id="root">' not in pagina:
                raise RuntimeError(f"interface not served: {pagina[:300]}")
            req = urllib.request.Request(f"{base}/mcp/", data=_richiesta_mcp(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "application/json, text/event-stream"})
            corpo = diretto.open(req, timeout=30).read().decode()
            if "serverInfo" not in corpo:
                raise RuntimeError(f"MCP initialize: {corpo[:300]}")
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()

    # MCP su stdio: `claude mcp add vedit -- <app>/vedit mcp`
    @controllo("MCP over stdio (`vedit mcp`)")
    def _():
        mcp = subprocess.Popen([str(cli), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        risposta: list[bytes] = []
        lettore = threading.Thread(target=lambda: risposta.append(mcp.stdout.readline()),
                                   daemon=True)
        lettore.start()
        mcp.stdin.write(_richiesta_mcp() + b"\n")
        mcp.stdin.flush()
        lettore.join(timeout=120)
        mcp.kill()
        if not risposta or b"serverInfo" not in risposta[0]:
            raise RuntimeError(f"reply {risposta[:1]!r}\n{_coda(mcp.stderr.read())}")

    if falliti:
        raise SystemExit(f"packaged app: {len(falliti)} check(s) failed: {', '.join(falliti)}")
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
