"""Le chat passate di un progetto: una chat nuova non cancella quella di prima.

Prima "ricomincia" toglieva la conversazione dal disco. Ora va nell'archivio
del progetto (``<progetto>.chat-archivio.json``) e si riapre com'era: turni,
cronologia del modello, sessione di Claude Code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vedit import api as api_mod
from vedit import chat, conversazioni, llm, progetti
from vedit.store import Store


def _vista(*domande: str) -> list[dict]:
    out = []
    for d in domande:
        out += [{"role": "user", "text": d, "refs": []},
                {"role": "assistant", "text": f"fatto: {d}", "tools": []}]
    return out


# --------------------------------------------------------------------------
# conversazioni.py
# --------------------------------------------------------------------------


def test_archivia_e_riprendi_com_era(tmp_path):
    p = str(tmp_path / "a.json")
    stato = {"provider": "claude_code", "sessione": "sess-1", "messaggi": []}
    meta = conversazioni.nuova_meta()
    r = conversazioni.archivia(p, stato, _vista("metti un titolo", "piu' grande"), meta)
    assert r["titolo"] == "metti un titolo" and r["turni"] == 2 and not r["rinominata"]
    assert (tmp_path / "a.chat-archivio.json").is_file()

    st, vista, m = conversazioni.riprendi(p, meta["id"])
    assert st["sessione"] == "sess-1" and vista == _vista("metti un titolo", "piu' grande")
    assert m["id"] == meta["id"]
    # ripresa, esce dall'archivio (vuoto: il file se ne va)
    assert conversazioni.elenco(p) == [] and not (tmp_path / "a.chat-archivio.json").exists()


def test_una_chat_vuota_non_entra(tmp_path):
    p = str(tmp_path / "a.json")
    assert conversazioni.archivia(p, {}, [], conversazioni.nuova_meta()) is None
    assert conversazioni.elenco(p) == []


def test_ripresa_e_lasciata_torna_al_suo_posto_senza_doppioni(tmp_path):
    p = str(tmp_path / "a.json")
    m1, m2 = conversazioni.nuova_meta(), conversazioni.nuova_meta()
    conversazioni.archivia(p, {}, _vista("uno"), m1)
    conversazioni.archivia(p, {}, _vista("due"), m2)
    assert [c["titolo"] for c in conversazioni.elenco(p)] == ["due", "uno"]  # l'ultima prima
    st, vista, m = conversazioni.riprendi(p, m1["id"])
    conversazioni.archivia(p, st, vista + _vista("tre"), m)
    elenco = conversazioni.elenco(p)
    assert [c["titolo"] for c in elenco] == ["uno", "due"]
    assert elenco[0]["id"] == m1["id"] and elenco[0]["turni"] == 2


def test_rinomina_e_titolo_automatico(tmp_path):
    p = str(tmp_path / "a.json")
    m = conversazioni.nuova_meta()
    conversazioni.archivia(p, {}, _vista("x" * 100), m)
    assert conversazioni.elenco(p)[0]["titolo"] == "x" * 59 + "…"
    assert conversazioni.rinomina(p, m["id"], "Intro del video")
    assert conversazioni.elenco(p)[0]["titolo"] == "Intro del video"
    assert conversazioni.elenco(p)[0]["rinominata"]
    assert conversazioni.rinomina(p, m["id"], "")          # vuoto: torna la prima domanda
    assert conversazioni.elenco(p)[0]["titolo"].startswith("xxx")
    assert not conversazioni.rinomina(p, "nessuno", "x")


def test_elimina(tmp_path):
    p = str(tmp_path / "a.json")
    m1, m2 = conversazioni.nuova_meta(), conversazioni.nuova_meta()
    conversazioni.archivia(p, {}, _vista("uno"), m1)
    conversazioni.archivia(p, {}, _vista("due"), m2)
    assert conversazioni.elimina(p, m1["id"]) and not conversazioni.elimina(p, m1["id"])
    assert [c["id"] for c in conversazioni.elenco(p)] == [m2["id"]]


def test_l_archivio_ha_un_limite(tmp_path, monkeypatch):
    monkeypatch.setattr(conversazioni, "MAX_ARCHIVIO", 3)
    p = str(tmp_path / "a.json")
    for i in range(5):
        conversazioni.archivia(p, {}, _vista(f"chat {i}"), conversazioni.nuova_meta())
    assert [c["titolo"] for c in conversazioni.elenco(p)] == ["chat 4", "chat 3", "chat 2"]


def test_la_meta_si_salva_con_la_chat_aperta(tmp_path):
    p = str(tmp_path / "a.json")
    meta = {**conversazioni.nuova_meta(), "titolo": "Montaggio"}
    conversazioni.salva(p, {"sessione": "s"}, _vista("ciao"), meta)
    _, vista, letta = conversazioni.apri(p)
    assert letta == meta and len(vista) == 2
    assert conversazioni.carica(p)[1] == vista   # la forma vecchia resta


def test_l_archivio_segue_il_progetto(tmp_path):
    f = tmp_path / "Bozza.json"
    Store.create("Bozza", path=str(f))
    conversazioni.archivia(str(f), {}, _vista("uno"), conversazioni.nuova_meta())
    out = progetti.rinomina(str(f), "Vacanze")
    assert Path(out["path"]).with_name("Vacanze.chat-archivio.json").is_file()
    assert not (tmp_path / "Bozza.chat-archivio.json").exists()
    copia = progetti.duplica(out["path"])
    assert not Path(copia["path"]).with_name(Path(copia["path"]).stem + ".chat-archivio.json").exists()


# --------------------------------------------------------------------------
# API, come la usa la chat della UI
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "claude_exe", lambda: None)
    f = llm._file()
    f.unlink(missing_ok=True)
    giri = {"n": 0}

    def finto_run(store, stato, domanda, run_op, refs=None, mcp_url=None, fermo=None,
                  stile=None, precedente=""):
        giri["n"] += 1
        stato.setdefault("messaggi", []).append({"role": "user", "content": domanda})
        stato["sessione"] = f"sess-{giri['n']}"
        yield {"type": "text", "text": f"ok {domanda}"}
        yield {"type": "done"}

    monkeypatch.setattr(chat, "run", finto_run)
    api_mod.S.store = None
    try:
        with TestClient(api_mod.app) as c:
            c.post("/api/project/create", json={"path": str(tmp_path / "a.json"), "preset": "720p"})
            yield c
    finally:
        api_mod.S.store = None
        f.unlink(missing_ok=True)


def test_chat_nuova_mette_la_vecchia_nella_cronologia(client, tmp_path):
    client.post("/api/chat", json={"message": "taglia l'intro"})
    client.post("/api/chat/reset")
    assert client.get("/api/chat/storia").json()["vista"] == []
    a = client.get("/api/chat/archivio").json()
    assert a["attuale"]["turni"] == 0
    assert [c["titolo"] for c in a["chat"]] == ["taglia l'intro"]
    assert (tmp_path / "a.chat-archivio.json").is_file()


def test_riprendi_rimette_turni_e_sessione(client):
    client.post("/api/chat", json={"message": "prima chat"})
    client.post("/api/chat/reset")
    client.post("/api/chat", json={"message": "seconda chat"})
    vecchia = client.get("/api/chat/archivio").json()["chat"][0]

    r = client.post("/api/chat/riprendi", json={"id": vecchia["id"]}).json()
    assert r["vista"][0]["text"] == "prima chat"
    assert api_mod.S.chat["sessione"] == "sess-1"      # Claude Code riprende la sua sessione
    assert r["attuale"]["id"] == vecchia["id"]
    # quella che era aperta adesso e' nella cronologia, al posto della ripresa
    assert [c["titolo"] for c in r["chat"]] == ["seconda chat"]
    # e lasciata di nuovo, torna una voce sola
    client.post("/api/chat/reset")
    assert sorted(c["titolo"] for c in client.get("/api/chat/archivio").json()["chat"]) == \
        ["prima chat", "seconda chat"]


def test_il_cambio_di_modello_non_perde_l_identita(client):
    """chat.run svuota lo stato quando cambia il motore: l'id della chat vive fuori."""
    client.post("/api/chat", json={"message": "uno"})
    cid = client.get("/api/chat/archivio").json()["attuale"]["id"]
    api_mod.S.chat.clear()
    client.post("/api/chat/reset")
    assert client.get("/api/chat/archivio").json()["chat"][0]["id"] == cid


def test_rinomina_ed_elimina(client):
    client.post("/api/chat", json={"message": "vecchia"})
    client.post("/api/chat/reset")
    client.post("/api/chat", json={"message": "aperta"})
    a = client.get("/api/chat/archivio").json()
    aperta, vecchia = a["attuale"]["id"], a["chat"][0]["id"]

    r = client.post("/api/chat/rinomina", json={"id": aperta, "titolo": "  Titoli   finali "}).json()
    assert r["attuale"]["titolo"] == "Titoli finali"
    r = client.post("/api/chat/rinomina", json={"id": vecchia, "titolo": "Intro"}).json()
    assert r["chat"][0]["titolo"] == "Intro"

    assert client.post("/api/chat/elimina", json={"id": aperta}).status_code == 400
    assert client.post("/api/chat/elimina", json={"id": vecchia}).json()["chat"] == []
    assert client.post("/api/chat/elimina", json={"id": vecchia}).status_code == 404


def test_non_si_cambia_chat_mentre_l_assistente_risponde(client):
    client.post("/api/chat", json={"message": "uno"})
    client.post("/api/chat/reset")
    cid = client.get("/api/chat/archivio").json()["chat"][0]["id"]
    api_mod.S.in_turno = True
    try:
        assert client.post("/api/chat/riprendi", json={"id": cid}).status_code == 409
    finally:
        api_mod.S.in_turno = False
    assert client.post("/api/chat/riprendi", json={"id": "nessuna"}).status_code == 404


def test_riaprendo_il_progetto_la_chat_aperta_tiene_il_nome(client, tmp_path):
    client.post("/api/chat", json={"message": "uno"})
    cid = client.get("/api/chat/archivio").json()["attuale"]["id"]
    client.post("/api/chat/rinomina", json={"id": cid, "titolo": "Montaggio"})
    client.post("/api/project/create", json={"path": str(tmp_path / "b.json"), "preset": "720p"})
    client.post("/api/project/open", json={"path": str(tmp_path / "a.json")})
    a = client.get("/api/chat/archivio").json()["attuale"]
    assert a["id"] == cid and a["titolo"] == "Montaggio"
