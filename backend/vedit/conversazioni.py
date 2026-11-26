"""La chat di ogni progetto, salvata accanto al progetto.

Riaprire un progetto e trovare l'assistente senza memoria di quello che si e'
gia' deciso costringe a rispiegare tutto. Qui la conversazione vive in
``<progetto>.chat.json``, accanto al file del progetto come le cartelle
``_voice`` e ``_web``: due cose insieme.

- ``vista``: i turni come li mostra la chat (testo, passi, errori);
- ``stato``: quello che serve al modello per riprendere — la cronologia dei
  modelli via API, l'id della sessione di Claude Code (``--resume``).

La cronologia su disco e' ripulita: niente immagini (pesano e si rigenerano
con preview_frame), niente blocchi di ragionamento (valgono solo nella
conversazione che li ha prodotti), solo gli ultimi messaggi. Se il modello non
puo' riprendere la cronologia, la ``vista`` diventa testo di contesto
(``testo``): il contesto non riparte mai vuoto.

Ricominciare non cancella: la conversazione aperta va nell'archivio del
progetto, ``<progetto>.chat-archivio.json``, da dove si riapre (``riprendi``)
con modello, sessione di Claude Code e turni come li aveva lasciati. Ogni
conversazione ha una ``meta`` (id, titolo dato dall'utente, quando e' nata):
l'id fa si' che una conversazione ripresa e poi lasciata torni al suo posto
nell'archivio invece di comparire due volte. La meta sta fuori da ``stato``,
che il cambio di modello svuota (``chat.run``).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

MAX_MESSAGGI = 80
MAX_TURNI = 300
# conversazioni passate per progetto: oltre, esce la piu' vecchia
MAX_ARCHIVIO = 100
_VERSIONE = 1


def percorso(progetto: str | None) -> Path | None:
    if not progetto:
        return None
    p = Path(progetto)
    return p.with_name(f"{p.stem}.chat.json")


def percorso_archivio(progetto: str | None) -> Path | None:
    if not progetto:
        return None
    p = Path(progetto)
    return p.with_name(f"{p.stem}.chat-archivio.json")


def nuova_meta() -> dict:
    return {"id": uuid.uuid4().hex[:12], "creata": time.time()}


def _blocco(b: Any) -> dict | None:
    """Un blocco di contenuto in forma JSON, o None se non va salvato."""
    if not isinstance(b, dict):
        tipo = getattr(b, "type", None)
        if tipo == "text":
            return {"type": "text", "text": b.text}
        if tipo == "tool_use":
            return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
        return None   # ragionamento, blocchi dei server: non si rimandano
    tipo = b.get("type")
    if tipo in ("image", "document"):
        return {"type": "text", "text": "(immagine)" if tipo == "image" else "(documento)"}
    if tipo == "image_url":
        return {"type": "text", "text": "(immagine)"}
    if tipo in ("thinking", "redacted_thinking"):
        return None
    if tipo == "tool_result":
        c = b.get("content")
        if isinstance(c, list):
            c = "\n".join(x.get("text", "(immagine)") if isinstance(x, dict) else str(x) for x in c)
        out = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "content": str(c or "")}
        if b.get("is_error"):
            out["is_error"] = True
        return out
    return json.loads(json.dumps(b, default=str))


def _messaggio(m: dict) -> dict:
    c = m.get("content")
    if isinstance(c, list):
        blocchi = [x for x in (_blocco(b) for b in c) if x is not None]
        if not blocchi:
            blocchi = [{"type": "text", "text": "…"}]
        return {**{k: v for k, v in m.items() if k != "content"}, "content": blocchi}
    return json.loads(json.dumps(m, default=str))


def _inizio_valido(m: dict) -> bool:
    """Un messaggio da cui una cronologia puo' cominciare: l'utente che scrive,
    non il risultato di uno strumento chiesto in un messaggio tagliato via."""
    if m.get("role") != "user":
        return False
    c = m.get("content")
    return not (isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                            for b in c))


def pulisci(messaggi: list) -> list[dict]:
    msgs = [_messaggio(m) for m in messaggi]
    if len(msgs) > MAX_MESSAGGI:
        da = len(msgs) - MAX_MESSAGGI
        while da < len(msgs) and not _inizio_valido(msgs[da]):
            da += 1
        msgs = msgs[da:]
    return msgs


def _ripulita(stato: dict, vista: list[dict]) -> dict:
    return {"stato": {**{k: v for k, v in stato.items() if k != "messaggi"},
                      "messaggi": pulisci(stato.get("messaggi") or [])},
            "vista": vista[-MAX_TURNI:]}


def _scrivi(f: Path, dati: dict) -> None:
    tmp = f.with_name(f"~{f.name}")
    tmp.write_text(json.dumps(dati, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, f)


def _leggi(f: Path | None) -> dict:
    if f is None or not f.is_file():
        return {}
    try:
        dati = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dati if isinstance(dati, dict) else {}


def salva(progetto: str | None, stato: dict, vista: list[dict], meta: dict | None = None) -> None:
    f = percorso(progetto)
    if f is None:
        return
    if not vista and not stato.get("messaggi") and not stato.get("sessione"):
        f.unlink(missing_ok=True)
        return
    dati = {"versione": _VERSIONE, **_ripulita(stato, vista)}
    if meta:
        dati["meta"] = meta
    _scrivi(f, dati)


def apri(progetto: str | None) -> tuple[dict, list[dict], dict]:
    """La conversazione aperta del progetto: stato, vista e meta (vuota se non c'era)."""
    dati = _leggi(percorso(progetto))
    stato = dati.get("stato") if isinstance(dati.get("stato"), dict) else {}
    vista = dati.get("vista") if isinstance(dati.get("vista"), list) else []
    meta = dati.get("meta") if isinstance(dati.get("meta"), dict) else {}
    return stato, vista, meta


def carica(progetto: str | None) -> tuple[dict, list[dict]]:
    stato, vista, _ = apri(progetto)
    return stato, vista


# --------------------------------------------------------------------------
# archivio: le conversazioni passate del progetto
# --------------------------------------------------------------------------


def titolo_da(vista: list[dict]) -> str:
    """Il titolo di una conversazione senza nome: la prima cosa chiesta."""
    for t in vista:
        testo = " ".join(str(t.get("text") or "").split())
        if t.get("role") == "user" and testo:
            return testo if len(testo) <= 60 else testo[:59].rstrip() + "…"
    return "Untitled chat"


def riassunto(voce: dict) -> dict:
    """Quello che la UI mostra di una conversazione: mai i turni interi."""
    vista = voce.get("vista") or []
    return {"id": voce.get("id"), "titolo": voce.get("titolo") or titolo_da(vista),
            "rinominata": bool(voce.get("titolo")), "creata": voce.get("creata"),
            "aggiornata": voce.get("aggiornata"),
            "turni": sum(1 for t in vista if t.get("role") == "user")}


def _archivio(progetto: str | None) -> list[dict]:
    voci = _leggi(percorso_archivio(progetto)).get("chat")
    return [v for v in voci or [] if isinstance(v, dict) and v.get("id")
            and isinstance(v.get("vista"), list)]


def _salva_archivio(progetto: str | None, voci: list[dict]) -> None:
    f = percorso_archivio(progetto)
    if f is None:
        return
    if not voci:
        f.unlink(missing_ok=True)
        return
    _scrivi(f, {"versione": _VERSIONE, "chat": voci[:MAX_ARCHIVIO]})


def elenco(progetto: str | None) -> list[dict]:
    """Le conversazioni passate, dall'ultima lasciata."""
    return [riassunto(v) for v in _archivio(progetto)]


def archivia(progetto: str | None, stato: dict, vista: list[dict],
             meta: dict | None) -> dict | None:
    """Mette la conversazione nell'archivio invece di perderla. Una vuota non entra;
    una che c'era gia' (stesso id, ripresa e lasciata di nuovo) prende il suo posto."""
    if not vista or percorso_archivio(progetto) is None:
        return None
    meta = {k: v for k, v in (meta or {}).items() if k in ("id", "titolo", "creata")}
    voce = {**nuova_meta(), **meta, "aggiornata": time.time(), **_ripulita(stato, vista)}
    altre = [v for v in _archivio(progetto) if v["id"] != voce["id"]]
    _salva_archivio(progetto, [voce, *altre])
    return riassunto(voce)


def riprendi(progetto: str | None, cid: str) -> tuple[dict, list[dict], dict]:
    """Toglie una conversazione dall'archivio e la restituisce come stato, vista, meta.

    KeyError se non c'e'. Chi la riapre deve prima archiviare quella aperta.
    """
    voci = _archivio(progetto)
    voce = next((v for v in voci if v["id"] == cid), None)
    if voce is None:
        raise KeyError(cid)
    _salva_archivio(progetto, [v for v in voci if v is not voce])
    meta = {k: voce[k] for k in ("id", "titolo", "creata") if voce.get(k)}
    stato = voce.get("stato") if isinstance(voce.get("stato"), dict) else {}
    return stato, voce["vista"], meta


def rinomina(progetto: str | None, cid: str, titolo: str) -> bool:
    """Titolo vuoto = torna quello automatico (la prima domanda)."""
    voci = _archivio(progetto)
    for v in voci:
        if v["id"] == cid:
            if titolo:
                v["titolo"] = titolo
            else:
                v.pop("titolo", None)
            _salva_archivio(progetto, voci)
            return True
    return False


def elimina(progetto: str | None, cid: str) -> bool:
    voci = _archivio(progetto)
    resto = [v for v in voci if v["id"] != cid]
    if len(resto) == len(voci):
        return False
    _salva_archivio(progetto, resto)
    return True


def testo(vista: list[dict], limite: int = 8000) -> str:
    """La conversazione in parole, dalla fine: contesto per un modello che non
    puo' riprendere la cronologia vera."""
    righe: list[str] = []
    for t in vista:
        if t.get("role") == "user":
            righe.append(f"Utente: {t.get('text', '')}")
        else:
            passi = ", ".join(x.get("name", "") for x in t.get("tools") or []
                              if not x.get("utente"))
            riga = f"Assistente: {t.get('text', '')}".rstrip()
            if passi:
                riga += f" [strumenti: {passi}]"
            righe.append(riga)
            righe += [f"Utente (durante il turno): {x['utente']}"
                      for x in t.get("tools") or [] if x.get("utente")]
    out, tot = [], 0
    for r in reversed(righe):
        r = r[:1500]
        if tot + len(r) > limite:
            break
        out.append(r)
        tot += len(r)
    return "\n".join(reversed(out))
