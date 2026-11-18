# -*- coding: utf-8 -*-
"""Passe de NETTOYAGE finale des fichiers de prix id-clés (lot 29b) : retire des
`prices[]` déjà fusionnés les offres PÉRIMÉES et celles qui ne correspondent
PAS au composant seul et qui ont échappé aux filtres par-source (car le fichier
agrège plusieurs passes d'enrichissement, alors que
`sans_aberrantes`/`produit_suspect` ne voyaient qu'une passe à la fois).

Quatre règles, dans cet ordre :
  1. **Offres périmées** (`--age-max-h`, 72 h par défaut) : offre dont le
     `lastSeen` date de plus de N heures, ou sans `lastSeen` lisible (rien ne
     prouve alors que le prix vaut encore). L'app ne regarde jamais cette
     date : une offre qu'aucune source n'a revue depuis des semaines
     (refroidissement vu il y a 128 jours, ventilateurs vus il y a 56 à 98
     jours) s'affichait comme un prix actuel. Elle sort EN PREMIER pour ne pas
     servir de référence aux règles suivantes : trois caches « Noctua NA-HC4 »
     à ~27 €, vus 56 jours plus tôt, faisaient passer les deux vraies offres
     du NH-D15S (~100 €) pour des aberrations hautes.
  2. **Accessoires / bundles / occasion** : `produit_suspect(nom)` (waterblock,
     backplate, câble, PC monté… et, depuis le lot 39, occasion /
     reconditionné / B-stock / used / gebraucht / usado…) — ex. « Alphacool
     Core Geforce RTX 5080 » (~220 €) ou « MSI … RTX 5080 … - Occasion »
     (1 772,96 €) retirés de la RTX 5080.
  3. **Aberrations basses** : offre à moins de 0,4 × la MÉDIANE des prix (en
     euros) du composant — filet pour les accessoires non nommés (le waterblock
     est le moins cher, donc invisible pour un filtre « > 2,5× le mini » ; la
     médiane, elle, reste celle des vraies cartes).
  4. **Aberrations hautes** : offre à plus de 2,5 × la médiane des AUTRES
     offres du composant (aberrations basses exclues), quand il en reste au
     moins 2 — PC complet au nom du composant (« Lenovo ThinkStation P5 … RTX
     5000 Ada », 19 717 C$ ; « ASUS TUF Gaming TM500 » à 1 310 € sur le Ryzen
     5 2600), produit sans rapport (« FRITZ! Mesh Wi-Fi Set 1700 » sur le
     Ryzen 7 1700), prix-bouche-trou d'un vendeur tiers (DDR5-7600 à 4 276 €
     sur amazon.fr, ~1 300 € ailleurs ; Arc A750 à 836 € face à ~220 €).
     La médiane des AUTRES : l'offre jugée ne tire pas sa propre référence
     vers le haut. Limite : un composant dont TOUTES les offres sont gonflées
     (RTX 3070 Ti à 815-1 047 € sur quatre Amazon) ou dont l'écart reste
     sous 2,5 × (RX 6500 XT à 289 / 364 / 462 €) n'a pas de référence saine
     dans ses offres — rien n'y est retiré.

Les règles 3 et 4 comparent EN EUROS, avec les taux figés d'`enrich_catalog`
(`prix_eur`, mêmes taux que l'app : USD, GBP, CHF, PLN, CAD). Une offre dans
une autre devise (dollar australien d'amazon.com.au…) n'est ni jugée ni prise
comme référence : `prix_eur` la compterait 1:1, donc 1,65 fois trop chère.
L'app l'ignore de toute façon (`LivePrices._devisesConnues`).

Recalcule ensuite `priceMin` (moins cher EN STOCK en euros, sinon toutes
offres), **null** quand il ne reste aucune offre : l'app afficherait sinon ce
`priceMin` orphelin comme prix indicatif (`LivePrices.prixRealiste` le rend tel
quel pour une liste d'offres vide) ; elle garde à la place le prix de son
catalogue. L'entrée reste (son image sert de repli). N'ajoute jamais d'offre,
ne touche ni au nom ni à l'image, n'écrit un fichier que s'il change. À
exécuter EN DERNIER dans la chaîne.

`gpus.json` / `cpus.json` (gputracker, « par modèle ») ne sont pas traités
ici : `retirer_prix_perimes.py` les vide d'un bloc au-delà de 30 h, car leur
source unique les réécrit en entier à chaque collecte réussie.

Usage :
    python scraper/nettoyer_offres.py --data-repo <clone Perf-P-data>
                                      [--only gpus,ssds] [--age-max-h 72]
"""

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import _TAUX_EUR, prix_eur, produit_suspect  # noqa: E402
from retirer_prix_perimes import lire_date  # noqa: E402

# Fichiers id-clés produits par le pipeline (ceux d'enrich_shops + ventilateurs).
FICHIERS = {
    "gpus": "gpus_app.json",
    "cpus": "cpus_app.json",
    "rams": "rams.json",
    "ssds": "ssds.json",
    "mobos": "mobos.json",
    "psus": "psus.json",
    "cases": "cases.json",
    "coolers": "coolers.json",
    # Ventilateurs : offres Amazon seulement (verifier_amazon.py). L'app ne lit
    # pas encore ce fichier, mais il ne doit pas garder d'offres périmées.
    "fans": "fans.json",
}

# Âge maximal d'une offre, en heures : la valeur du cron. Les runs ont lieu
# toutes les 5 à 14 h (cron « 0 */6 » retardé par GitHub : ~9 h en médiane sur
# les 40 runs du 04 au 18/11/2026), et une étape coupée par son budget laisse
# ses dernières catégories sans relevé (les offres boutiques des RAM dataient
# de 35 h au run du 18/11). 72 h laissent au moins 5 runs à chaque source pour
# revoir une offre, même aux plus longs intervalles constatés ; 30 h (le seuil
# de retirer_prix_perimes) effaceraient des offres valides après un seul run
# raté. Peu sensible : 312 offres retirées à 48 h, 301 à 72 h, 290 à 120 h sur
# les données du 18/11 — l'essentiel n'avait été revu depuis plus d'une semaine.
AGE_MAX_H_CONSEILLE = 72.0

# Seuil bas relatif à la médiane : en-dessous = accessoire/erreur de matching.
_SEUIL_MEDIANE = 0.4

# Seuil HAUT relatif à la médiane des AUTRES offres (même facteur 2,5 que
# `sans_aberrantes`), et nombre minimal de ces autres offres pour qu'elle fasse
# référence : face à une seule, impossible de dire laquelle a tort ; dès deux,
# la règle tranche à la majorité. Mesuré sur les données des 24/10, 09/11 et
# 18/11 (1 900 à 6 800 offres) : 2 offres de référence au lieu de 3 n'ajoutent
# que de vraies aberrations (PC complet, Arc A750 à 836 €) ; un facteur 2,0
# retirerait en revanche des offres réelles de grandes enseignes en pleine
# pénurie de mémoire (DDR4 32 Go à 600 € chez Materiel.net, 2,3 × la médiane).
_SEUIL_HAUT = 2.5
_MIN_AUTRES = 2

# Devises ramenées en euros par `prix_eur` : l'euro et celles dont il a le taux.
_DEVISES_CONNUES = {"EUR", *_TAUX_EUR}


def offre_perimee(offre, maintenant, age_max_h):
    """Vrai si l'offre n'a pas été revue depuis plus de [age_max_h] heures, ou
    si son `lastSeen` manque ou est illisible."""
    vu = lire_date(offre.get("lastSeen")) if offre.get("lastSeen") else None
    return vu is None or (maintenant - vu).total_seconds() / 3600 > age_max_h


def _comparable(offre):
    """Vrai si le prix de l'offre se ramène en euros (devise connue de `prix_eur`)."""
    return offre.get("currency", "EUR") in _DEVISES_CONNUES


def aberrations(prices):
    """Règles 3 et 4 : retourne (prices gardées, nb aberrations basses, nb
    aberrations hautes). Seules les offres de devise connue sont jugées et
    servent de référence ; les autres sont gardées telles quelles."""
    jugees = [i for i, p in enumerate(prices) if _comparable(p)]
    if len(jugees) < 3:  # trop peu d'offres → pas de médiane fiable
        return list(prices), 0, 0
    eur = {i: prix_eur(prices[i]) for i in jugees}
    seuil_bas = statistics.median(eur.values()) * _SEUIL_MEDIANE
    basses = {i for i in jugees if eur[i] < seuil_bas}
    hautes = set()
    for i in jugees:
        if i in basses:
            continue
        # Référence : les AUTRES offres, sans les accessoires déjà repérés
        # (ils tireraient la médiane vers le bas).
        autres = [eur[j] for j in jugees if j != i and j not in basses]
        if len(autres) >= _MIN_AUTRES and eur[i] > _SEUIL_HAUT * statistics.median(autres):
            hautes.add(i)
    gardees = [p for i, p in enumerate(prices) if i not in basses and i not in hautes]
    return gardees, len(basses), len(hautes)


def nettoyer_prices(prices, cat=None, maintenant=None, age_max_h=0):
    """Applique les quatre règles (voir l'en-tête) à une liste `prices[]`.
    Retourne (prices nettoyées, compteurs {perimees, accessoires, basses,
    hautes}). [cat] pilote le filtre accessoire (refroidissements et
    ventilateurs en sont exemptés) ; [age_max_h] nul désactive la règle 1."""
    n = {}
    fraiches = list(prices)
    if age_max_h and age_max_h > 0:
        maintenant = maintenant or datetime.now(timezone.utc)
        fraiches = [p for p in prices if not offre_perimee(p, maintenant, age_max_h)]
    n["perimees"] = len(prices) - len(fraiches)
    # Un ventilateur se vend « avec câble PWM », « avec adaptateur » : mêmes
    # exemptions que les refroidissements (lot 30) ; bundles et occasion restent
    # filtrés.
    cat_filtre = "coolers" if cat == "fans" else cat
    sans_acc = [p for p in fraiches if not produit_suspect(p.get("product") or "", cat_filtre)]
    n["accessoires"] = len(fraiches) - len(sans_acc)
    gardees, n["basses"], n["hautes"] = aberrations(sans_acc)
    return gardees, n


def _price_min(prices):
    """priceMin EN EUROS : moins cher en stock si possible, sinon toutes offres."""
    if not prices:
        return None
    dispo = [p for p in prices if p.get("inStock", True)]
    pool = dispo or prices
    return round(min(prix_eur(p) for p in pool), 2)


def principal(argv=None, maintenant=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--age-max-h", type=float, default=AGE_MAX_H_CONSEILLE,
                    help="retire les offres dont le lastSeen date de plus de N heures "
                         "(ou sans lastSeen) ; 0 = pas de purge par âge "
                         f"(défaut {AGE_MAX_H_CONSEILLE:g})")
    args = ap.parse_args(argv)
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    maintenant = maintenant or datetime.now(timezone.utc)
    if args.age_max_h > 0:
        limite = (maintenant - timedelta(hours=args.age_max_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"offres périmées : lastSeen absent ou avant {limite} (> {args.age_max_h:g} h)")

    for cat, fichier in FICHIERS.items():
        if seules and cat not in seules:
            continue
        chemin = os.path.join(args.data_repo, fichier)
        try:
            with open(chemin, encoding="utf-8") as f:
                texte = f.read()
            data = json.loads(texte)
        except (OSError, ValueError):
            print(f"{cat}: {fichier} absent, ignoré")
            continue

        tot = {"perimees": 0, "accessoires": 0, "basses": 0, "hautes": 0}
        vides = 0
        for e in data:
            prices = e.get("prices") or []
            gardees, n = nettoyer_prices(prices, cat, maintenant, args.age_max_h)
            for cle, nb in n.items():
                tot[cle] += nb
            if prices and not gardees:
                vides += 1
            e["prices"] = gardees
            # Toujours recalculé : null sans offre (voir l'en-tête).
            e["priceMin"] = _price_min(gardees)

        sortie = json.dumps(data, ensure_ascii=False, indent=1)
        if sortie != texte:
            with open(chemin, "w", encoding="utf-8") as f:
                f.write(sortie)
        print(f"{cat}: {tot['perimees']} périmées + {tot['accessoires']} accessoires + "
              f"{tot['basses']} aberrations basses + {tot['hautes']} hautes retirées, "
              f"{vides} composant(s) sans offre restante -> {fichier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
