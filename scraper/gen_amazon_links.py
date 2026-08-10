# -*- coding: utf-8 -*-
"""Génère un LIEN AFFILIÉ AMAZON par composant et par marketplace, à l'avance.

Pourquoi
--------
La PA-API 5.0 (voir `enrich_amazon.py`) est la seule source de *prix* Amazon
fiable, mais Amazon ne l'ouvre qu'après trois ventes qualifiées : tant que les
clés ne sont pas délivrées, l'application n'a **aucune** offre Amazon à
proposer, donc aucun lien d'affiliation à ouvrir — alors que le programme
Associates est déjà actif et que les Partner Tags existent.

Ce script comble ce trou SANS API : pour chaque composant du catalogue de
l'app, il écrit un lien de recherche Amazon **ciblé et déjà tagué**, un par
marketplace disposant d'un compte. Amazon Associates autorise explicitement les
liens vers une page de résultats de recherche, et le cookie d'affiliation est
posé exactement comme sur une fiche produit. Aucun scraping n'est nécessaire :
les URLs sont déterministes.

Le jour où la PA-API s'ouvre, `enrich_amazon.py` écrit un ASIN par composant
et par marketplace dans `amazon_asins.json` ; il suffit alors de relancer ce
script avec `--asins` pour que les liens deviennent des liens produit directs
(`/dp/<ASIN>?tag=…`). Le format de sortie ne change pas : l'application ne voit
que des URLs.

Sortie : `catalog/amazon_links.json` (dépôt data) et/ou
`assets/data/amazon_links.json` (asset embarqué, disponible hors ligne).

Usage :
    python scraper/gen_amazon_links.py --out assets/data/amazon_links.json
    python scraper/gen_amazon_links.py --out <clone>/catalog/amazon_links.json \
        --asins <clone>/amazon_asins.json
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote_plus

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Partner Tag par marketplace — DOIT rester synchronisé avec
# `lib/services/affiliation.dart` (_tagsAmazon). Une marketplace absente de
# cette table n'a pas de compte Associates : aucun lien n'est généré pour elle
# plutôt qu'un lien tagué avec un identifiant invalide.
TAGS = {
    "amazon.fr": "fraym-21",
    "amazon.de": "fraym03-21",
    "amazon.es": "fraym08-21",
    "amazon.it": "fraym06-21",
    "amazon.co.uk": "fraym0a-21",
    "amazon.com": "fraym-20",
    "amazon.com.au": "fraym-22",
}

# Catégorie -> fichier d'assets. `fans` est absent des CATEGORIES du scraper
# (aucune source de prix) mais reste un composant achetable.
FICHIERS = {
    "gpus": "gpus", "cpus": "cpus", "rams": "rams", "ssds": "ssds",
    "mobos": "mobos", "psus": "psus", "cases": "cases", "coolers": "coolers",
    "fans": "fans",
}

# Composants NON achetables : entrées génériques du modèle (refroidisseur
# d'origine, disque dur « générique », absence de ventilateurs). Elles n'ont pas
# de produit Amazon correspondant — les lister sans lien est plus honnête que
# de renvoyer une recherche vide.
GENERIQUES = re.compile(
    r"^(aucun|refroidisseur stock|disque dur \d+ ?tr|sans )", re.I)


def _sans_parentheses(s):
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


def _capacite(gb):
    """« 2000 » -> « 2TB », « 500 » -> « 500GB » (vocabulaire des titres Amazon)."""
    if not gb:
        return ""
    return f"{gb // 1000}TB" if gb >= 1000 and gb % 1000 == 0 else f"{gb}GB"


def _avec_marque(marque, nom):
    """Préfixe [nom] par [marque], sauf si elle y figure déjà."""
    marque = (marque or "").strip()
    if not marque or marque.lower() in nom.lower():
        return nom
    return f"{marque} {nom}"


def requete(cat, it):
    """Termes de recherche Amazon d'un composant.

    Les noms de l'app sont francisés (« 32 Go DDR5-6000 CL30 ») là où les
    titres Amazon sont anglophones et portent la marque : on reconstruit donc
    une requête à partir des CHAMPS, pas du libellé d'affichage.
    """
    nom = _sans_parentheses(it.get("name", ""))
    if cat == "gpus":
        return nom  # « GeForce RTX 5090 » : déjà le terme du marché
    if cat == "rams":
        return (f"{it.get('brand', '')} {it.get('kind', '')}-{it.get('mhz', '')} "
                f"{_capacite(it.get('sizeGb'))} CL{it.get('cl', '')}").strip()
    if cat == "ssds":
        return f"{_avec_marque(it.get('brand'), nom)} {_capacite(it.get('sizeGb'))}".strip()
    return _avec_marque(it.get("brand"), nom)


def lien(marche, q, asin=None):
    """Lien affilié : fiche produit si l'ASIN est connu, recherche ciblée sinon."""
    tag = TAGS[marche]
    if asin:
        return f"https://www.{marche}/dp/{asin}?tag={tag}"
    return f"https://www.{marche}/s?k={quote_plus(q)}&tag={tag}"


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="fichier JSON de sortie")
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    ap.add_argument("--asins", default="",
                    help="amazon_asins.json (PA-API) : produit des liens /dp/ directs")
    ap.add_argument("--markets", default=",".join(TAGS))
    args = ap.parse_args()

    marches = [m.strip() for m in args.markets.split(",") if m.strip() in TAGS]
    if not marches:
        print("Aucune marketplace valide.", file=sys.stderr)
        return 2

    asins = {}
    if args.asins and os.path.exists(args.asins):
        with open(args.asins, encoding="utf-8") as f:
            asins = json.load(f)

    items, ignores = {}, {}
    total_liens = n_asin = 0

    for cat, fichier in FICHIERS.items():
        chemin = os.path.join(args.items_dir, f"{fichier}.json")
        if not os.path.exists(chemin):
            continue
        with open(chemin, encoding="utf-8") as f:
            catalogue = json.load(f)

        par_id, sans = {}, []
        for it in catalogue:
            nom = it.get("name", "")
            if GENERIQUES.match(nom) or (it.get("price") or 0) <= 0:
                sans.append({"id": it["id"], "name": nom,
                             "raison": "generique"})
                continue
            q = requete(cat, it)
            if not q.strip():
                sans.append({"id": it["id"], "name": nom, "raison": "sans_requete"})
                continue
            connus = asins.get(cat, {}).get(it["id"], {})
            liens = {}
            for m in marches:
                a = connus.get(m)
                if a:
                    n_asin += 1
                liens[m] = lien(m, q, a)
            par_id[it["id"]] = {"name": nom, "q": q, "links": liens}
            total_liens += len(liens)

        items[cat] = par_id
        if sans:
            ignores[cat] = sans
        print(f"{cat}: {len(par_id)} composants liés, {len(sans)} sans lien")

    sortie = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "markets": marches,
        "asinLinks": n_asin,
        "note": ("Liens d'affiliation Amazon pré-calculés, utilisés par l'app "
                 "quand aucune offre Amazon vérifiée n'est disponible. "
                 "Recherche ciblée tant que la PA-API n'est pas ouverte ; "
                 "fiche produit /dp/ dès qu'un ASIN est connu."),
        "items": items,
        "skipped": ignores,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out} : {total_liens} liens ({n_asin} par ASIN) "
          f"sur {len(marches)} marketplaces")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
