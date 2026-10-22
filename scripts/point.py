#!/usr/bin/env python3
"""Calcule le point d'avancement PLFSS à partir des fichiers Eliasse téléchargés.

Usage : python3 scripts/point.py <dossier data>
Lit statistiques.json, derouleur.json, prochain.txt (et sorts.json du passage précédent),
écrit sorts.json à jour et imprime point.json sur la sortie standard.
"""
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
out = {"genere_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "erreurs": []}


def load_json(name):
    try:
        return json.loads((d / name).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out["erreurs"].append(f"{name}: {e}")
        return None


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def compact(libelle):
    """« Article 5 » -> « Art. 5 », « Après l'article 5 bis » -> « ap5 bis », « Avant l'article 5 » -> « av5 »."""
    if not libelle:
        return None
    n = norm(libelle)
    m = re.match(r"^(apres|avant) l.?article (\w+)(.*)$", n)
    if m:
        return ("ap" if m.group(1) == "apres" else "av") + m.group(2) + m.group(3).rstrip()
    m = re.match(r"^article (\w+)(.*)$", n)
    if m:
        return "Art. " + m.group(1) + m.group(2).rstrip()
    return str(libelle).strip()


def sans_sort(a):
    s = norm(a.get("sort") or a.get("sortEnSeance") or "")
    return s in ("", "aucun", "en discussion", "a discuter", "null", "none")


# 1. Statistiques officielles
stats = load_json("statistiques.json")
if isinstance(stats, dict):
    for k in ("nombreAmendementsTotal", "nombreAmendementsExamines", "nombreAmendementsRestants"):
        out[k] = stats.get(k)

# 2. Amendement appelé
try:
    raw = (d / "prochain.txt").read_text(encoding="utf-8", errors="replace")
    num = re.search(r'"?numAmdt"?\s*[:=>]\s*"?([0-9A-Za-z-]+)', raw)
    bib = re.search(r'"?bibard"?\s*[:=>]\s*"?([0-9]+)', raw)
    out["amendement_appele"] = num.group(1) if num else None
    out["bibard_appele"] = bib.group(1) if bib else None
    out["autre_texte_en_seance"] = bool(bib and bib.group(1) != "3211")
    if not num:
        out["erreurs"].append("prochain.txt: numAmdt introuvable — début: " + raw[:300])
except Exception as e:  # noqa: BLE001
    out["erreurs"].append(f"prochain.txt: {e}")

# 3. Dérouleur
der = load_json("derouleur.json")


def find_places(o):
    if isinstance(o, dict):
        if isinstance(o.get("places"), list):
            return o["places"]
        for v in o.values():
            r = find_places(v)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_places(v)
            if r is not None:
                return r
    return None


places = find_places(der) if der is not None else None
sorts_now = {}
if places is None and der is not None:
    out["erreurs"].append("derouleur.json: clé 'places' introuvable — clés: "
                          + str(list(der.keys())[:20] if isinstance(der, dict) else type(der).__name__))
if places:
    num = str(out.get("amendement_appele") or "")
    idx_place = None
    for i, p in enumerate(places):
        amdts = p.get("amendements") or []
        for a in amdts:
            sorts_now[str(a.get("numero"))] = a.get("sort") or ""
        if idx_place is None and any(str(a.get("numero")) == num for a in amdts):
            idx_place = i
    out["nb_divisions"] = len(places)
    out["restants_calcules_derouleur"] = sum(
        1 for p in places for a in (p.get("amendements") or []) if sans_sort(a))
    if idx_place is not None:
        p = places[idx_place]
        amdts = p.get("amendements") or []
        pos = next(j for j, a in enumerate(amdts) if str(a.get("numero")) == num)
        apres = [a for a in amdts[pos + 1:] if sans_sort(a)]
        out["division"] = compact(p.get("libelle"))
        out["division_libelle"] = p.get("libelle")
        out["restants_avant_changement"] = len(apres)
        out["amendements_dans_division"] = len(amdts)
        # division suivante qui a encore des amendements
        nxt = next((q for q in places[idx_place + 1:]
                    if any(sans_sort(a) for a in (q.get("amendements") or []))), None)
        out["division_suivante"] = compact(nxt.get("libelle")) if nxt else None
    elif num:
        out["erreurs"].append(f"amendement {num} absent du dérouleur")

# 4. Sorts nouveaux depuis le passage précédent
prev = {}
try:
    prev = json.loads((d / "sorts.json").read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001
    pass
if sorts_now:
    nouveaux = {k: v for k, v in sorts_now.items() if v and norm(v) != "aucun" and prev.get(k) != v}
    adoptes = sorted((k for k, v in nouveaux.items() if norm(v).startswith("adopt")),
                     key=lambda x: int(re.sub(r"\D", "", x) or 0))
    out["nouveaux_sorts_depuis_dernier_passage"] = len(nouveaux) if prev else None
    out["adoptes_depuis_dernier_passage"] = adoptes if prev else None
    (d / "sorts.json").write_text(json.dumps(sorts_now, ensure_ascii=False), encoding="utf-8")

# 4bis. Historique des compteurs -> rythme / séance active
hist_path = d / "historique.csv"
ex = out.get("nombreAmendementsExamines")
hist = []
try:
    for l in hist_path.read_text(encoding="utf-8").splitlines():
        t, n = l.split(",")
        hist.append((datetime.strptime(t, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc), int(n)))
except Exception:  # noqa: BLE001
    pass
now = datetime.now(timezone.utc)
if isinstance(ex, int):
    il_y_a_1h = [n for t, n in hist if (now - t).total_seconds() >= 3300]
    if il_y_a_1h:
        out["examines_derniere_heure"] = ex - il_y_a_1h[-1]
    if hist:
        out["seance_active_probable"] = ex != hist[-1][1] or (
            out.get("examines_derniere_heure") or 0) > 0
    with hist_path.open("a", encoding="utf-8") as f:
        f.write(f"{now.strftime('%Y-%m-%dT%H:%MZ')},{ex}\n")

# 5. Ligne de notification
def fmt(n):
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else "non disponible"

parts = [out.get("division") or "division non disponible",
         f"amdt n° {out.get('amendement_appele') or 'non disponible'}"]
if out.get("restants_avant_changement") is not None:
    parts.append(f"{out['restants_avant_changement']} restants avant "
                 f"{out.get('division_suivante') or 'fin du texte'}")
parts.append(f"{fmt(out.get('nombreAmendementsRestants'))} restants sur {fmt(out.get('nombreAmendementsTotal'))}")
if out.get("examines_derniere_heure") is not None:
    parts.append(f"{out['examines_derniere_heure']} examinés en 1 h")
out["ligne"] = " — ".join(parts)

print(json.dumps(out, ensure_ascii=False, indent=2))
