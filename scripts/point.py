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
        out["restants_division"] = sum(1 for a in amdts if sans_sort(a))
        # division suivante qui a encore des amendements
        nxt = next((q for q in places[idx_place + 1:]
                    if any(sans_sort(a) for a in (q.get("amendements") or []))), None)
        out["division_suivante"] = compact(nxt.get("libelle")) if nxt else None
    elif num:
        out["erreurs"].append(f"amendement {num} absent du dérouleur")

# 4. Horodatage des sorts -> amendements débattus dans la dernière heure
#    sorts_horodatage.json : {numero: [sort, "YYYY-MM-DDTHH:MMZ" | null]}
#    null = sort déjà présent au premier snapshot (heure inconnue, jamais compté).
now = datetime.now(timezone.utc)
stamp = now.strftime("%Y-%m-%dT%H:%MZ")
hpath = d / "sorts_horodatage.json"
try:
    horo = json.loads(hpath.read_text(encoding="utf-8"))
    premier = False
except Exception:  # noqa: BLE001
    horo, premier = {}, True
if sorts_now:
    for k, v in sorts_now.items():
        examine = bool(v) and norm(v) != "aucun"
        if not examine:
            horo.pop(k, None)          # retour à « Aucun » (réserve, rectification…)
            continue
        if k not in horo or horo[k][0] != v:
            horo[k] = [v, None if premier else stamp]
    hpath.write_text(json.dumps(horo, ensure_ascii=False), encoding="utf-8")

    def recent(t):
        if not t:
            return False
        dt = datetime.strptime(t, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() <= 3600
    derniers = [(k, v) for k, (v, t) in horo.items() if recent(t)]
    if premier:
        out["debattus_derniere_heure"] = None
        out["erreurs"].append("premier snapshot horodaté : rythme disponible au prochain passage")
    else:
        out["debattus_derniere_heure"] = len(derniers)
        out["dont_tombes"] = sum(1 for _, v in derniers if norm(v).startswith("tomb"))
        detail = {}
        for _, v in derniers:
            detail[v] = detail.get(v, 0) + 1
        out["detail_derniere_heure"] = detail
        # couverture : depuis quand l'historique existe
        t0 = min((t for _, t in horo.values() if t), default=None)
        out["historique_depuis_utc"] = t0
    out["seance_active_probable"] = bool(out.get("debattus_derniere_heure"))

# 5. Ligne de notification
def nd(x):
    return "n.d." if x is None else str(x)

ligne = (f"{out.get('division') or 'article n.d.'} : "
         f"{nd(out.get('restants_division'))} restants / {nd(out.get('amendements_dans_division'))}")
if out.get("debattus_derniere_heure") is not None:
    ligne += (f" — {out['debattus_derniere_heure']} débattus en 1 h "
              f"(dont {out.get('dont_tombes', 0)} tombés)")
else:
    ligne += " — rythme n.d."
out["ligne"] = ligne

print(json.dumps(out, ensure_ascii=False, indent=2))
