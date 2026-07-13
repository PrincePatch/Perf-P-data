# -*- coding: utf-8 -*-
"""Passe de NETTOYAGE finale des fichiers de prix id-clés (lot 29b) : retire des
`prices[]` déjà fusionnés les offres qui ne correspondent PAS au composant seul
et qui ont échappé aux filtres par-source (car le fichier agrège plusieurs
passes d'enrichissement, alors que `sans_aberrantes`/`produit_suspect` ne
voyaient qu'une passe à la fois).

Deux règles :
  1. **Accessoires / bundles** : `produit_suspect(nom)` (waterblock, backplate,
     câble, PC monté…) — ex. « Alphacool Core Geforce RTX 5080 » (~220 €)
     retiré de la RTX 5080.
  2. **Aberrations basses** : offre à moins de 0,4 × la MÉDIANE des prix (en
     euros) du composant — filet pour les accessoires non nommés (le waterblock
     est le moins cher, donc invisible pour un filtre « > 2,5× le mini » ; la
     médiane, elle, reste celle des vraies cartes).

Recalcule ensuite `priceMin` (moins cher EN STOCK en euros, sinon toutes
offres). N'ajoute jamais d'offre, ne touche ni au nom ni à l'image. À exécuter
EN DERNIER dans la chaîne.

Usage :
    python scraper/nettoyer_offres.py --data-repo <clone Perf-P-data>
                                      [--only gpus,ssds]
"""

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import prix_eur, produit_suspect  # noqa: E402

# Fichiers id-clés produits par le pipeline (mêmes que enrich_shops).
FICHIERS = {
    "gpus": "gpus_app.json",
    "cpus": "cpus_app.json",
    "rams": "rams.json",
    "ssds": "ssds.json",
    "mobos": "mobos.json",
    "psus": "psus.json",
    "cases": "cases.json",
    "coolers": "coolers.json",
}

# Seuil bas relatif à la médiane : en-dessous = accessoire/erreur de matching.
_SEUIL_MEDIANE = 0.4


def nettoyer_prices(prices, cat=None):
    """Retourne (prices nettoyées, nb accessoires retirés, nb aberrations).
    [cat] pilote le filtre accessoire (les coolers en sont exemptés)."""
    sans_acc = [p for p in prices if not produit_suspect(p.get("product", ""), cat)]
    n_acc = len(prices) - len(sans_acc)
    if len(sans_acc) < 3:  # trop peu d'offres → pas de médiane fiable
        return sans_acc, n_acc, 0
    med = statistics.median(prix_eur(p) for p in sans_acc)
    seuil = med * _SEUIL_MEDIANE
    gardees = [p for p in sans_acc if prix_eur(p) >= seuil]
    return gardees, n_acc, len(sans_acc) - len(gardees)


def _price_min(prices):
    """priceMin EN EUROS : moins cher en stock si possible, sinon toutes offres."""
    if not prices:
        return None
    dispo = [p for p in prices if p.get("inStock", True)]
    pool = dispo or prices
    return round(min(prix_eur(p) for p in pool), 2)


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    seules = {c.strip() for c in args.only.split(",") if c.strip()}

    for cat, fichier in FICHIERS.items():
        if seules and cat not in seules:
            continue
        chemin = os.path.join(args.data_repo, fichier)
        try:
            with open(chemin, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            print(f"{cat}: {fichier} absent, ignoré")
            continue

        tot_acc = tot_ab = 0
        for e in data:
            prices = e.get("prices") or []
            if not prices:
                continue
            gardees, n_acc, n_ab = nettoyer_prices(prices, cat)
            tot_acc += n_acc
            tot_ab += n_ab
            e["prices"] = gardees
            pm = _price_min(gardees)
            if pm is not None:
                e["priceMin"] = pm

        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"{cat}: {tot_acc} accessoires + {tot_ab} aberrations retirés -> {fichier}")


if __name__ == "__main__":
    principal()
