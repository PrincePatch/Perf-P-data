# -*- coding: utf-8 -*-
"""Re-passe les ASIN déjà résolus dans les filtres COURANTS, hors ligne.

Les titres retenus sont mémorisés dans `amazon_asins.json` (`_titres`). Quand
les règles de correspondance sont durcies — c'est arrivé au lot 32 après avoir
vu « MSI B650 **Tomahawk** WiFi » résolu en « MSI Pro B650M-A WiFi », ou un kit
mémoire de 16 Go résolu en 32 Go —, il serait absurde de re-télécharger tout le
catalogue pour appliquer les nouveaux filtres : le titre suffit.

Ce script relit chaque entrée, la soumet à `valide()` et **supprime** celles qui
ne passent plus. Le composant redevient donc « non résolu » : la prochaine
exécution de `resolve_amazon_asins.py` lui cherchera un meilleur produit.

Usage :
    python scraper/revalider_asins.py --asins <clone>/amazon_asins.json
        [--items-dir catalog] [--dry-run]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from gen_amazon_links import FICHIERS  # noqa: E402
from resolve_amazon_asins import _jetons, valide  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asins", required=True)
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.asins, encoding="utf-8") as f:
        cache = json.load(f)
    titres = cache.get("_titres", {})

    garde = rejet = sans_preuve = 0
    for cat, fichier in FICHIERS.items():
        chemin = os.path.join(args.items_dir, f"{fichier}.json")
        if cat not in cache or not os.path.exists(chemin):
            continue
        with open(chemin, encoding="utf-8") as f:
            items = {e["id"]: e for e in json.load(f)}

        for cid in list(cache[cat]):
            marches = cache[cat][cid]
            if not any(marches.values()):
                continue  # déjà marqué introuvable
            it = items.get(cid)
            titre = titres.get(cat, {}).get(cid, "")
            if it is None:
                continue
            # Pas de titre mémorisé = ASIN retenu sans preuve (ancienne
            # version, page d'attente) : on l'écarte pour re-résolution.
            if not titre:
                sans_preuve += 1
                if not args.dry_run:
                    cache[cat].pop(cid, None)
                print(f"  ? {cat}/{cid}: sans titre mémorisé -> à re-résoudre")
                continue
            jetons = _jetons(cat, it)
            # Même tolérance qu'à la résolution : la latence mémoire s'écrit de
            # trop de façons pour être exigée (« CL30 », « C30 », absente…).
            # Sans cela, la re-validation supprimerait tout ce que le second
            # regard avait justement permis de trouver.
            if (valide(cat, it["name"], titre, jetons)
                    or valide(cat, it["name"], titre, jetons, souple=True)):
                garde += 1
                continue
            rejet += 1
            print(f"  - {cat}/{cid}: « {it['name']} » ≠ « {titre[:70]} »")
            if not args.dry_run:
                cache[cat].pop(cid, None)
                titres.get(cat, {}).pop(cid, None)

    if not args.dry_run:
        with open(args.asins, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"\nconservés: {garde} · rejetés: {rejet} · sans preuve: {sans_preuve}")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
