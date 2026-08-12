# -*- coding: utf-8 -*-
"""Retire les offres Amazon dont la DEVISE ne correspond pas à leur marketplace.

Amazon convertit les prix pour le visiteur : consultée depuis l'Europe, une
fiche `amazon.com` affiche des euros. Publier ce montant comme « prix vérifié
sur Amazon US » tromperait l'acheteur américain, à qui la fiche facturera des
dollars et, souvent, des frais d'importation.

`verifier_amazon.py` écarte désormais ces montants à la source ; ce script
nettoie ceux qui ont été enregistrés avant. Purement local : aucune requête.

Usage :
    python scraper/nettoyer_amazon.py --data-repo <clone> [--dry-run]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import SORTIES  # noqa: E402
from gen_amazon_links import FICHIERS  # noqa: E402
from verifier_amazon import DEVISES  # noqa: E402


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total = retires = 0
    for cat in FICHIERS:
        chemin = os.path.join(args.data_repo, SORTIES.get(cat, f"{cat}.json"))
        if not os.path.exists(chemin):
            continue
        with open(chemin, encoding="utf-8") as f:
            entrees = json.load(f)
        touche = False
        for e in entrees:
            gardees = []
            for p in e.get("prices") or []:
                shop = (p.get("shop") or "").lower()
                if "amazon." not in shop:
                    gardees.append(p)
                    continue
                total += 1
                attendue = DEVISES.get(shop)
                if attendue and p.get("currency") != attendue:
                    retires += 1
                    touche = True
                    continue
                gardees.append(p)
            e["prices"] = gardees
        if touche and not args.dry_run:
            with open(chemin, "w", encoding="utf-8") as f:
                json.dump(entrees, f, ensure_ascii=False, indent=1)

    print(f"offres Amazon examinées {total} · retirées (devise convertie) {retires}")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
