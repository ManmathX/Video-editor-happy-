"""Tutti i progetti, non solo gli ultimi 12.

``recenti.json`` teneva 12 voci e buttava quelle il cui file non c'era: un
progetto uscito dall'elenco andava ritrovato a mano sul disco. Ora il file li
ricorda tutti (la prima schermata ne mostra sempre 12), l'elenco completo
aggiunge quelli trovati nella cartella Video e nelle raccolte, e quelli spariti
restano segnati finche' l'utente non li toglie.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vedit import api as api_mod
from vedit import progetti, raccolte, versions
from vedit.store import Store


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    cache, video = tmp_path / "cache", tmp_path / "Videos"
    cache.mkdir()
    video.mkdir()
    monkeypatch.setenv("VEDIT_CACHE", str(cache))
    monkeypatch.setattr(raccolte, "cartella_video", lambda: video)
    api_mod.S.store = None
    try:
        with TestClient(api_mod.app) as c:
            yield c, video
    finally:
        api_mod.S.store = None


def _tutti(c) -> list[dict]:
    return c.get("/api/progetti/tutti").json()["progetti"]


def test_la_prima_schermata_ne_mostra_12_l_elenco_li_ricorda_tutti(ambiente, tmp_path):
    c, _ = ambiente
    altrove = tmp_path / "altrove"
    altrove.mkdir()
    for i in range(15):
        c.post("/api/project/create", json={"path": str(altrove / f"p{i}.json"), "preset": "720p"})
    assert len(c.get("/api/recenti").json()) == 12
    tutti = _tutti(c)
    assert [Path(v["path"]).stem for v in tutti] == [f"p{i}" for i in reversed(range(15))]
    assert all(v["esiste"] for v in tutti)


def test_uno_sparito_resta_segnato_finche_non_si_toglie(ambiente, tmp_path):
    c, _ = ambiente
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    c.post("/api/project/create", json={"path": str(a), "preset": "720p"})
    c.post("/api/project/create", json={"path": str(b), "preset": "720p"})
    api_mod.S.store = None
    a.unlink()   # spostato o cancellato fuori dall'editor
    assert [r["path"] for r in c.get("/api/recenti").json()] == [str(b)]
    voce = next(v for v in _tutti(c) if v["path"] == str(a))
    assert voce["esiste"] is False

    # uno che esiste non si "dimentica": si elimina dal suo menu
    assert c.post("/api/progetti/dimentica", json={"path": str(b)}).status_code == 400
    r = c.post("/api/progetti/dimentica", json={"path": str(a)}).json()
    assert [v["path"] for v in r["progetti"]] == [str(b)]


def test_trova_quelli_nella_cartella_video(ambiente):
    c, video = ambiente
    vecchio = video / "Vecchio.json"
    s = Store.create("Vecchio", path=str(vecchio))
    versions.snapshot(s, "prima")                       # Vecchio.versioni/prima.json: no
    (video / "Vecchio.chat.json").write_text('{"vista": []}', encoding="utf-8")
    (video / "Vecchio.chat-archivio.json").write_text('{"chat": []}', encoding="utf-8")
    (video / "altro.json").write_text('{"tracks": []}', encoding="utf-8")   # non un progetto
    profondo = video / "Canale" / "Video 1" / "Video 1.json"
    profondo.parent.mkdir(parents=True)
    Store.create("Video 1", path=str(profondo))

    tutti = _tutti(c)
    assert sorted(v["name"] for v in tutti) == ["Vecchio", "Video 1"]
    # trovati una volta, restano nell'elenco anche senza riscoprirli
    salvati = json.loads((Path(api_mod.proxy.cache_dir()) / "recenti.json").read_text())
    assert {v["name"] for v in salvati} == {"Video 1", "Vecchio"}


def test_mostra_la_raccolta(ambiente, tmp_path):
    c, _ = ambiente
    r = raccolte.crea("Viaggi", str(tmp_path / "viaggi"))
    p = raccolte.percorso_video(r["path"], "Islanda")
    Store.create("Islanda", path=p)
    c.post("/api/project/open", json={"path": p})
    voce = next(v for v in _tutti(c) if v["name"] == "Islanda")
    assert voce["raccolta"] == "Viaggi"


def test_scopri_si_ferma_presto_su_un_disco_pieno(tmp_path, monkeypatch):
    monkeypatch.setattr(progetti, "SCOPERTA_MAX", 10)
    for i in range(50):
        (tmp_path / f"clip{i:02}.mov").write_bytes(b"")
    Store.create("Ultimo", path=str(tmp_path / "zz.json"))
    assert progetti.scopri([tmp_path], set()) == []     # oltre il tetto non si guarda


def test_leggi_progetto_riconosce_solo_i_progetti(tmp_path):
    p = tmp_path / "x.json"
    Store.create("X", path=str(p))
    assert progetti.leggi_progetto(p)["name"] == "X"
    for nome, testo in (("raccolta.json", "{}"), ("x.piano.json", "{}"), ("y.json", "[1]"),
                        ("z.json", "non json")):
        f = tmp_path / nome
        f.write_text(testo, encoding="utf-8")
        assert progetti.leggi_progetto(f) is None
