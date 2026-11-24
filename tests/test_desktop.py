"""App desktop: finestra, app impacchettata e collegamento con Claude Code.

La finestra vera (pywebview) non si apre nei test: qui si prova cio' che le sta
intorno e che si rompe senza far rumore — l'eseguibile che fa da python per i
lavori figli, il riconoscimento di un editor gia' aperto, la registrazione in
Claude Code, lo smistamento fra finestra e riga di comando.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from vedit import desktop


@pytest.fixture(autouse=True)
def _cache_propria(tmp_path, monkeypatch):
    """desktop.json (la scelta su Claude Code) non deve passare da un test all'altro."""
    monkeypatch.setenv("VEDIT_CACHE", str(tmp_path / "cache"))


# --------------------------------------------------------------------------
# l'app impacchettata fa da interprete per i lavori figli
# --------------------------------------------------------------------------


def test_meno_c_esegue_il_codice_con_gli_argomenti(tmp_path, monkeypatch):
    """Come `python -c codice arg`: e' cosi' che analyze lancia la trascrizione."""
    monkeypatch.setattr(sys, "argv", ["app"])
    out = tmp_path / "out.txt"
    codice = "import sys; open(sys.argv[1], 'w').write(sys.argv[0] + '|' + sys.argv[2])"
    assert desktop.come_interprete(["-c", codice, str(out), "x"]) == 0
    assert out.read_text() == "-c|x"


def test_meno_c_riporta_il_codice_di_uscita(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app"])
    assert desktop.come_interprete(["-c", "raise SystemExit(3)"]) == 3
    assert desktop.come_interprete(["-c", "import sys; sys.exit()"]) == 0


def test_meno_m_esegue_il_modulo(tmp_path, monkeypatch):
    """Come `python -m yt_dlp ...` di web.py."""
    (tmp_path / "modulo_prova_desktop.py").write_text(
        "import sys\nopen(sys.argv[1], 'w').write('ok')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["app"])
    out = tmp_path / "out.txt"
    assert desktop.come_interprete(["-m", "modulo_prova_desktop", str(out)]) == 0
    assert out.read_text() == "ok"


@pytest.mark.parametrize("argv", [[], ["--port", "8770"], ["mcp"], ["-c"]])
def test_il_resto_non_e_per_l_interprete(argv):
    assert desktop.come_interprete(argv) is None


# --------------------------------------------------------------------------
# finestra o riga di comando
# --------------------------------------------------------------------------


def test_un_comando_va_alla_cli(monkeypatch):
    """`vedit mcp` dentro l'app: e' la riga che si da' a Claude Code."""
    from vedit import cli

    chiamate = []
    monkeypatch.setattr(cli, "main", lambda argv: chiamate.append(argv) or 0)
    monkeypatch.setattr(desktop, "apri", lambda *a: pytest.fail("non doveva aprire la finestra"))
    assert desktop.principale(["mcp"]) == 0
    assert chiamate == [["mcp"]]


def test_senza_comando_apre_la_finestra(monkeypatch):
    aperte = []
    monkeypatch.setattr(desktop, "apri", lambda *a: aperte.append(a) or 0)
    assert desktop.principale(["--port", "8771", "-psn_0_42"]) == 0
    assert aperte == [(8771, None, False)]


def test_la_cli_ha_il_comando_desktop():
    from vedit import cli

    a = cli.build_parser().parse_args(["desktop", "--port", "8772", "--connect-claude"])
    assert a.func is cli.cmd_desktop and a.port == 8772 and a.connect_claude


def test_path_completo_tiene_la_shell_prima_e_senza_doppioni(tmp_path, monkeypatch):
    uno, due = tmp_path / "uno", tmp_path / "due"
    uno.mkdir()
    due.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(due), str(uno)]))
    voci = desktop.path_completo(shell_path=os.pathsep.join([str(uno), str(tmp_path / "manca")]))
    voci = voci.split(os.pathsep)
    assert voci[:2] == [str(uno), str(due)]
    assert str(tmp_path / "manca") not in voci  # le cartelle che non esistono restano fuori


def test_l_exe_della_finestra_sta_accanto_alla_cli(tmp_path, monkeypatch):
    nome = desktop.NOME_APP + (".exe" if os.name == "nt" else "")
    (tmp_path / nome).write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "vedit"))
    assert desktop._exe_finestra() == [str(tmp_path / nome)]


# --------------------------------------------------------------------------
# un editor gia' aperto
# --------------------------------------------------------------------------


def _servi(risposta: dict | None) -> tuple[http.server.HTTPServer, str]:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            corpo = json.dumps(risposta).encode()
            self.send_response(200 if risposta is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_riconosce_un_editor_vedit():
    srv, url = _servi({"project": None, "revision": "0", "seq": 0})
    try:
        assert desktop.editor_attivo(url)
    finally:
        srv.shutdown()


def test_un_altro_programma_sulla_porta_non_e_un_editor():
    srv, url = _servi({"status": "ok"})
    try:
        assert not desktop.editor_attivo(url)
    finally:
        srv.shutdown()


def test_porta_chiusa_non_e_un_editor():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    assert not desktop.editor_attivo(f"http://127.0.0.1:{porta}", timeout=0.5)


def test_risponde_il_server_vero():
    """Con l'API vera: /api/state ha i campi che editor_attivo cerca."""
    from fastapi.testclient import TestClient

    from vedit import api

    dati = TestClient(api.app).get("/api/state").json()
    assert "project" in dati and "revision" in dati


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


def test_collega_claude_registra_il_server_http(monkeypatch):
    from vedit import llm

    comandi = []

    class Esito:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(llm, "claude_exe", lambda: "/bin/claude")
    monkeypatch.setattr(desktop, "_esegui", lambda cmd: comandi.append(cmd) or Esito())
    r = desktop.collega_claude("http://127.0.0.1:8760/mcp/")
    assert r["ok"]
    assert comandi == [
        ["/bin/claude", "mcp", "remove", "--scope", "user", "vedit-live"],
        ["/bin/claude", "mcp", "add", "--scope", "user", "--transport", "http",
         "vedit-live", "http://127.0.0.1:8760/mcp/"],
    ]
    assert desktop.pref()["claude"] == "http://127.0.0.1:8760/mcp/"


def test_collega_claude_senza_claude_dice_come_installarlo(monkeypatch):
    from vedit import llm

    monkeypatch.setattr(llm, "claude_exe", lambda: None)
    r = desktop.collega_claude("http://127.0.0.1:8760/mcp/")
    assert not r["ok"] and "npm install -g @anthropic-ai/claude-code" in r["messaggio"]


def test_collega_claude_riporta_l_errore(monkeypatch):
    from vedit import llm

    class Esito:
        returncode, stdout, stderr = 1, "", "boom"

    monkeypatch.setattr(llm, "claude_exe", lambda: "/bin/claude")
    monkeypatch.setattr(desktop, "_esegui", lambda cmd: Esito())
    r = desktop.collega_claude("http://127.0.0.1:8760/mcp/")
    assert not r["ok"] and r["messaggio"] == "boom"
    assert "claude" not in desktop.pref()


class _Finestra:
    def __init__(self, si: bool):
        self.si, self.domande = si, []

    def create_confirmation_dialog(self, titolo, testo):
        self.domande.append(titolo)
        return self.si


def test_primo_avvio_chiede_una_volta(monkeypatch):
    from vedit import llm

    monkeypatch.setattr(llm, "claude_exe", lambda: "/bin/claude")
    collegati = []
    monkeypatch.setattr(desktop, "collega_claude",
                        lambda url: collegati.append(url) or {"ok": True, "messaggio": ""})
    f = _Finestra(si=False)
    desktop._proponi_claude(f, "http://127.0.0.1:8760/mcp/")
    desktop._proponi_claude(f, "http://127.0.0.1:8760/mcp/")
    assert f.domande == ["Claude Code"] and collegati == []  # un no resta un no


def test_se_cambia_porta_il_collegamento_la_segue(monkeypatch):
    from vedit import llm

    monkeypatch.setattr(llm, "claude_exe", lambda: "/bin/claude")
    desktop._salva_pref(claude="http://127.0.0.1:8760/mcp/")
    collegati = []
    monkeypatch.setattr(desktop, "collega_claude",
                        lambda url: collegati.append(url) or {"ok": True, "messaggio": ""})
    f = _Finestra(si=True)
    desktop._proponi_claude(f, "http://127.0.0.1:8761/mcp/")
    assert collegati == ["http://127.0.0.1:8761/mcp/"] and f.domande == []


def test_senza_pywebview_dice_cosa_installare(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "webview", None)
    assert desktop.apri() == 1
    assert "vedit-mcp[desktop]" in capsys.readouterr().err


def test_l_app_impacchettata_ha_icone_e_installatore():
    radice = Path(__file__).resolve().parents[1] / "desktop"
    for f in ("entry.py", "vedit-desktop.spec", "installer.iss", "icon.png", "icon.ico",
              "icon.icns"):
        assert (radice / f).is_file(), f
    assert desktop.NOME_APP in (radice / "vedit-desktop.spec").read_text()
    assert desktop.NOME_APP in (radice / "installer.iss").read_text()
