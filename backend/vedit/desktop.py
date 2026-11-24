"""vedit in una finestra sua invece che in una scheda del browser.

``vedit desktop`` (e l'app impacchettata da ``scripts/build_desktop.py``) avvia
lo stesso server di ``vedit ui`` in un thread di questo processo
(``api.serve_background``) e lo mostra nella webview di sistema: WebKit su
macOS, Edge WebView2 su Windows, GTK o Qt su Linux. Chiudere la finestra ferma
il server e gli export che aveva avviato.

Claude Code lavora sul progetto della finestra dal server MCP che il server
espone gia' su ``/mcp/``: lo usa l'assistente dentro l'app, e una sessione
esterna di Claude Code ci arriva una volta registrato con
``claude mcp add --transport http vedit-live <url>/mcp/`` — l'app lo propone al
primo avvio e dal menu "Claude Code".

Un progetto alla volta per file: se sulla porta risponde gia' un editor (un'altra
finestra, ``vedit ui``, ``open_ui`` da una sessione di Claude Code) la finestra si
apre su quello invece di avviare un secondo server. Due ``Store`` sullo stesso
``.json`` si sovrascriverebbero (AGENTS.md, sezione 4).
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

NOME_APP = "Topper Mode Video Editor"
PORTA = 8760
MCP_NOME = "vedit-live"
SFONDO = "#05060d"  # --bg dell'interfaccia: niente lampo bianco mentre carica
INSTALLA = "pip install 'vedit-mcp[desktop]'"

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# dove npm, Homebrew & co. mettono `claude` e `ffmpeg` quando la shell non lo dice
_DIR_COMUNI = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.claude/local",
               "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin", "~/.nvm/versions/node/*/bin")


def congelato() -> bool:
    """Vero dentro l'app impacchettata da PyInstaller."""
    return bool(getattr(sys, "frozen", False))


# --------------------------------------------------------------------------
# app impacchettata: l'eseguibile non e' python
# --------------------------------------------------------------------------


def come_interprete(argv: list[str]) -> int | None:
    """Esegue ``-c codice ...`` / ``-m modulo ...`` come farebbe python; None per il resto.

    Nell'app impacchettata ``sys.executable`` e' l'app stessa, ma vedit avvia
    ancora lavori figli con ``sys.executable -c ...`` (trascrizione, vedi
    ``analyze._asr``) e ``sys.executable -m yt_dlp`` (download dal web). Senza
    questo aprirebbero una seconda finestra dell'editor invece di fare il lavoro.
    """
    if len(argv) < 2 or argv[0] not in ("-c", "-m"):
        return None
    try:
        if argv[0] == "-c":
            sys.argv = ["-c", *argv[2:]]
            exec(compile(argv[1], "<string>", "exec"), {"__name__": "__main__"})
        else:
            import runpy

            sys.argv = [argv[1], *argv[2:]]
            runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            return e.code or 0
        print(e.code, file=sys.stderr)
        return 1
    return 0


def _stdio_sicuri() -> None:
    """Un'app a finestra su Windows non ha console: sys.stdout/stderr sono None.

    Il primo ``print`` (di vedit o di uvicorn) solleverebbe un'eccezione. Vanno
    in un file di log, che e' anche dove guardare quando l'app si comporta male.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .proxy import cache_dir

    log = cache_dir() / "desktop.log"
    if log.is_file() and log.stat().st_size > 2_000_000:
        log.unlink()
    f = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - vive quanto l'app
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f


def path_completo(shell_path: str | None = None) -> str:
    """Il PATH della shell dell'utente, non quello spoglio che il Dock da' alle app.

    Aperta dal Finder un'app macOS riceve ``/usr/bin:/bin:/usr/sbin:/sbin``:
    niente ffmpeg di Homebrew e niente ``claude`` (npm, spesso via nvm), quindi
    l'assistente direbbe che Claude Code non c'e' anche quando c'e'. La shell si
    interroga una volta (interattiva e di login, cosi' conta anche nvm nel
    .zshrc); le cartelle comuni coprono una shell che non risponde.
    ``shell_path`` serve ai test.
    """
    voci: list[str] = []
    if shell_path is None and os.name != "nt":
        shell = os.environ.get("SHELL") or "/bin/zsh"
        try:
            out = subprocess.run(
                [shell, "-ilc", 'printf "\\n__VEDIT_PATH__%s__VEDIT_PATH__\\n" "$PATH"'],
                capture_output=True, text=True, timeout=8,
                # in `vedit mcp` lo stdin e' il canale MCP: la shell non deve leggerlo
                stdin=subprocess.DEVNULL).stdout
            m = re.search(r"__VEDIT_PATH__(.*?)__VEDIT_PATH__", out)
            shell_path = m.group(1) if m else ""
        except (OSError, subprocess.SubprocessError):
            shell_path = ""
    voci += (shell_path or "").split(os.pathsep)
    voci += os.environ.get("PATH", "").split(os.pathsep)
    if os.name != "nt":
        for d in _DIR_COMUNI:
            voci += sorted(glob.glob(os.path.expanduser(d)), reverse=True)  # node piu' nuovo prima
    visti: set[str] = set()
    nuovo = []
    for d in voci:
        if d and d not in visti and os.path.isdir(d):
            visti.add(d)
            nuovo.append(d)
    return os.pathsep.join(nuovo)


def _exe_finestra() -> list[str]:
    """L'eseguibile a finestra dell'app impacchettata (accanto a quello della CLI ``vedit``)."""
    exe = Path(sys.executable)
    for nome in (NOME_APP + ".exe", NOME_APP):
        c = exe.with_name(nome)
        if c.is_file() and c != exe:
            return [str(c)]
    return [str(exe)]


def apri_in_finestra(url: str) -> None:
    """``open_ui`` / ``vedit ui`` dalla CLI impacchettata: una finestra, non una scheda.

    La finestra trova l'editor che risponde gia' su quella porta e mostra quello,
    quindi agente e finestra lavorano sullo stesso progetto in memoria.
    """
    porta = urllib.parse.urlsplit(url).port or PORTA
    subprocess.Popen([*_exe_finestra(), "--port", str(porta)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)


# --------------------------------------------------------------------------
# l'editor dietro la finestra
# --------------------------------------------------------------------------


def editor_attivo(url: str, timeout: float = 1.0) -> bool:
    """Vero se a ``url`` risponde un editor vedit (non un programma qualsiasi sulla porta)."""
    diretto = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with diretto.open(f"{url}/api/state", timeout=timeout) as r:
            dati = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(dati, dict) and "project" in dati and "revision" in dati


def _assicura_ffmpeg(finestra: Any) -> None:
    """La prima volta scarica ffmpeg nella cache di vedit, con la barra di avanzamento."""
    from . import ffbin, ffmpeg

    try:
        ffmpeg.binary("ffmpeg")
        ffmpeg.binary("ffprobe")
        return
    except FileNotFoundError:
        pass
    finestra.load_html(_pagina("Downloading ffmpeg",
                               "One time only: the editor needs ffmpeg to play and export video.",
                               barra=True))
    ultimo = [-1]

    def avanza(f: float) -> None:
        if int(f * 100) != ultimo[0]:
            ultimo[0] = int(f * 100)
            try:
                finestra.evaluate_js(f"window.avanza && avanza({f:.3f})")
            except Exception:  # noqa: BLE001 - la barra e' solo estetica
                pass

    ffbin.install(progress=avanza)
    ffmpeg.forget()


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


def _pref_file() -> Path:
    from .proxy import cache_dir

    return cache_dir() / "desktop.json"


def pref() -> dict:
    try:
        return json.loads(_pref_file().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _salva_pref(**kv: Any) -> None:
    _pref_file().write_text(json.dumps({**pref(), **kv}, indent=2), "utf-8")


def _esegui(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW)


def collega_claude(url_mcp: str) -> dict:
    """Registra il server MCP di questo editor in Claude Code, per ogni cartella (scope user).

    Da li' ogni sessione di Claude Code ha gli strumenti di vedit e lavora sul
    progetto aperto nella finestra, finche' la finestra e' aperta.
    """
    from . import llm

    exe = llm.claude_exe()
    if not exe:
        return {"ok": False, "messaggio": (
            "Claude Code is not installed on this computer: "
            "npm install -g @anthropic-ai/claude-code, then run `claude` once to sign in.")}
    try:
        # aggiungere sopra una voce che c'e' gia' fallisce: prima si toglie
        # (se non c'e' e' un errore, ignorato)
        _esegui([exe, "mcp", "remove", "--scope", "user", MCP_NOME])
        p = _esegui([exe, "mcp", "add", "--scope", "user", "--transport", "http",
                     MCP_NOME, url_mcp])
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "messaggio": f"could not run Claude Code: {e}"}
    if p.returncode != 0:
        return {"ok": False, "messaggio": (p.stderr or p.stdout).strip()
                or f"claude mcp add exited with code {p.returncode}"}
    _salva_pref(claude=url_mcp)
    return {"ok": True, "messaggio": (
        f"Claude Code is connected. In any Claude Code session the '{MCP_NOME}' tools edit "
        f"the project open in this window ({url_mcp}). Sessions already running need a "
        "restart to see them.")}


def _proponi_claude(finestra: Any, url_mcp: str, forza: bool = False) -> None:
    """Al primo avvio chiede una volta se collegare Claude Code; dopo segue la porta."""
    from . import llm

    if forza:
        _avvisa(finestra, collega_claude(url_mcp))
        return
    gia = pref().get("claude")
    if gia in (url_mcp, "no") or not llm.claude_exe():
        return
    if gia:  # collegato prima su un altro indirizzo (la porta era occupata): lo segue
        collega_claude(url_mcp)
        return
    if finestra.create_confirmation_dialog(
            "Claude Code",
            "Connect Claude Code to this editor?\n\nClaude Code sessions will get the video "
            "editing tools and work on the project open in this window. You can do it later "
            "from the Claude Code menu."):
        _avvisa(finestra, collega_claude(url_mcp))
    else:
        _salva_pref(claude="no")


def _avvisa(finestra: Any, esito: dict) -> None:
    titolo = "Claude Code connected" if esito["ok"] else "Claude Code not connected"
    try:
        finestra.create_confirmation_dialog(titolo, esito["messaggio"])
    except Exception:  # noqa: BLE001 - finestra gia' chiusa
        print(esito["messaggio"], file=sys.stderr)


# --------------------------------------------------------------------------
# finestra
# --------------------------------------------------------------------------


def _pagina(titolo: str, testo: str, barra: bool = False) -> str:
    extra = ('<div class="b"><i id="i"></i></div><script>function avanza(f)'
             '{document.getElementById("i").style.width=(f*100)+"%"}</script>') if barra else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;height:100%;background:{SFONDO};color:#e8e9f3;
font:15px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif}}
main{{height:100%;display:grid;place-content:center;text-align:center;gap:6px;padding:24px}}
h1{{font-size:18px;font-weight:600;margin:0}}
p{{margin:0;color:#9aa0b8;max-width:560px;white-space:pre-wrap}}
.b{{margin:14px auto 0;width:280px;height:4px;border-radius:2px;background:#1d2033;overflow:hidden}}
.b i{{display:block;height:100%;width:0;background:#ff7a4d;transition:width .2s}}
</style></head><body><main><h1>{html.escape(titolo)}</h1><p>{html.escape(testo)}</p>{extra}
</main></body></html>"""


def apri(port: int = PORTA, project: str | None = None, collega: bool = False) -> int:
    """Apre l'editor in una finestra; torna quando la finestra si chiude."""
    try:
        import webview
        from webview.menu import Menu, MenuAction
    except ImportError:
        print(f"the desktop window needs pywebview: {INSTALLA}", file=sys.stderr)
        return 1
    from . import api, figli
    from .proxy import cache_dir

    figli.lega_figli()
    base = f"http://127.0.0.1:{port}"
    ospite = editor_attivo(base)
    if ospite and project:
        print(f"an editor is already open on {base}: showing that one, --project ignored",
              file=sys.stderr)
    stato: dict[str, Any] = {"url": base if ospite else None}
    ferma = api.stop_background

    webview.settings["ALLOW_DOWNLOADS"] = True  # copertine e file che la UI offre da salvare
    finestra = webview.create_window(
        NOME_APP, html=_pagina(NOME_APP, "Starting…"), width=1440, height=900,
        min_size=(1100, 680), maximized=True, background_color=SFONDO,
        text_select=True)  # pywebview di serie impedisce di selezionare il testo; un browser no

    def davanti(_url: str | None = None) -> None:
        # open_ui da un agente collegato a questo editor: e' gia' aperto, lo mostra
        try:
            finestra.restore()
            finestra.show()
        except Exception:  # noqa: BLE001
            pass

    def avvio() -> None:
        try:
            if not ospite:
                _assicura_ffmpeg(finestra)
                store = None
                if project:
                    from .store import Store

                    store = Store.open(project)
                info = api.serve_background(store, "127.0.0.1", port, open_browser=False)
                stato["url"] = info["url"]
                figli.apri_nel_browser = davanti
                # close_ui da un agente non deve togliere il server da sotto la
                # finestra: la finestra e' dell'utente, la chiude lui
                api.stop_background = lambda: False
            finestra.load_url(stato["url"])
            _proponi_claude(finestra, stato["url"] + "/mcp/", forza=collega)
        except Exception as e:  # noqa: BLE001 - mostrato nella finestra, non perso in un thread
            traceback.print_exc()
            finestra.load_html(_pagina("The editor could not start", f"{type(e).__name__}: {e}"))

    def connetti() -> None:
        if stato["url"]:
            _avvisa(finestra, collega_claude(stato["url"] + "/mcp/"))

    def nel_browser() -> None:
        if stato["url"]:
            webbrowser.open(stato["url"])

    menu = [Menu("Claude Code", [
        MenuAction("Connect Claude Code to this editor", connetti),
        MenuAction("Open the editor in the browser", nel_browser),
    ])]
    webview.start(avvio, private_mode=False, storage_path=str(cache_dir("desktop")), menu=menu)

    if not ospite:
        ferma()
    # un thread di render ancora vivo terrebbe in piedi un processo invisibile (e su
    # Windows il .exe bloccato): chiudere la finestra chiude l'editor, come `vedit ui`
    for flusso in (sys.stdout, sys.stderr):
        if flusso is not None:
            flusso.flush()
    os._exit(0)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=NOME_APP,
        description="Video editor in a desktop window. The full command line ships with it: "
                    "`vedit mcp` (MCP server on stdio), `vedit doctor`, `vedit render`... "
                    "— run `vedit --help` from the app folder.")
    p.add_argument("--port", type=int, default=PORTA)
    p.add_argument("--project", help="project to open at start")
    p.add_argument("--connect-claude", action="store_true",
                   help="register this editor in Claude Code (MCP over HTTP)")
    return p


def principale(argv: list[str] | None = None) -> int:
    """Punto d'ingresso dell'app impacchettata (``desktop/entry.py``): finestra o riga di comando."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if congelato():
        codice = come_interprete(argv)
        if codice is not None:
            return codice
        _stdio_sicuri()
        os.environ["PATH"] = path_completo()
    # macOS passava -psn_0_1234 alle app aperte dal Finder
    argv = [a for a in argv if not a.startswith("-psn_")]
    if argv and not argv[0].startswith("-"):
        # `vedit mcp`, `vedit doctor`, `vedit ui`...: tutta la CLI viaggia nell'app
        from . import cli, figli

        if congelato():
            figli.apri_nel_browser = apri_in_finestra
        return cli.main(argv)
    a = _parser().parse_args(argv)
    return apri(a.port, a.project, a.connect_claude)
