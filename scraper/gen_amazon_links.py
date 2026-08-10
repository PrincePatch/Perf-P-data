# -*- coding: utf-8 -*-
"""Génère un LIEN AFFILIÉ AMAZON **vers la fiche produit**, par composant et par
marketplace.

Pourquoi
--------
La PA-API 5.0 (voir `enrich_amazon.py`) est la seule source de *prix* Amazon
fiable, mais Amazon ne l'ouvre qu'après trois ventes qualifiées : tant que les
clés ne sont pas délivrées, l'application n'a **aucune** offre Amazon à
proposer, donc aucun lien d'affiliation à ouvrir — alors que le programme
Associates est déjà actif et que les Partner Tags existent.

Ce script comble ce trou SANS API, en s'appuyant sur les ASIN résolus par
`resolve_amazon_asins.py` (découverte via moteur de recherche, **validation sur
la fiche produit Amazon elle-même**). Chaque lien produit ici mène donc
DIRECTEMENT à un produit :

    https://www.amazon.fr/dp/B0DT6SN14V?tag=fraym-21

Les pages de résultats de recherche ne sont plus utilisées : elles obligeaient
l'utilisateur à choisir lui-même, sans garantie de tomber sur le bon composant.

Marketplace sans ASIN validé
----------------------------
Un ASIN européen est souvent valable sur `.fr`, `.de`, `.es` et `.it` à la fois,
mais pas toujours. Pour une marketplace où la fiche n'existe pas, on publie le
lien d'une marketplace voisine où elle EXISTE (avec le Partner Tag de
celle-ci) : **OneLink** redirige ensuite l'acheteur vers son magasin local.
L'utilisateur atterrit dans tous les cas sur une fiche produit.

Un composant sans aucun ASIN validé n'est PAS publié : il figure dans
`skipped`, et l'application garde son repli habituel plutôt que d'afficher un
lien qui ne mènerait pas au bon produit.

Le jour où la PA-API s'ouvre, `enrich_amazon.py` écrit ses propres ASIN dans le
même `amazon_asins.json` : ce script les reprend sans modification.

Sortie : `catalog/amazon_links.json` (dépôt data) et/ou
`assets/data/amazon_links.json` (asset embarqué, disponible hors ligne).

Usage :
    python scraper/gen_amazon_links.py --out assets/data/amazon_links.json \
        --asins <clone>/amazon_asins.json
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

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


def lien(marche, asin):
    """Lien affilié vers la FICHE PRODUIT [asin] sur [marche]."""
    return f"https://www.{marche}/dp/{asin}?tag={TAGS[marche]}"


# Marketplace de repli, par ordre de proximité (catalogue, langue, logistique),
# quand la fiche n'existe pas sur celle du pays. OneLink prend ensuite le relais.
VOISINES = {
    "amazon.fr": ["amazon.de", "amazon.es", "amazon.it", "amazon.co.uk", "amazon.com"],
    "amazon.de": ["amazon.fr", "amazon.it", "amazon.es", "amazon.co.uk", "amazon.com"],
    "amazon.es": ["amazon.fr", "amazon.it", "amazon.de", "amazon.co.uk", "amazon.com"],
    "amazon.it": ["amazon.de", "amazon.fr", "amazon.es", "amazon.co.uk", "amazon.com"],
    "amazon.co.uk": ["amazon.de", "amazon.fr", "amazon.com", "amazon.es", "amazon.it"],
    "amazon.com": ["amazon.com.au", "amazon.co.uk", "amazon.de", "amazon.fr"],
    "amazon.com.au": ["amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr"],
}


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="fichier JSON de sortie")
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    ap.add_argument("--asins", required=True,
                    help="amazon_asins.json — ASIN validés (resolve_amazon_asins / PA-API)")
    ap.add_argument("--markets", default=",".join(TAGS))
    args = ap.parse_args()

    marches = [m.strip() for m in args.markets.split(",") if m.strip() in TAGS]
    if not marches:
        print("Aucune marketplace valide.", file=sys.stderr)
        return 2

    if not os.path.exists(args.asins):
        print(f"ASIN introuvables : {args.asins}. Lancer d'abord "
              f"resolve_amazon_asins.py.", file=sys.stderr)
        return 2
    with open(args.asins, encoding="utf-8") as f:
        asins = json.load(f)
    titres = asins.get("_titres", {})

    items, ignores = {}, {}
    total_liens = n_natif = n_onelink = 0

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
                sans.append({"id": it["id"], "name": nom, "raison": "generique"})
                continue

            connus = asins.get(cat, {}).get(it["id"]) or {}
            valides = {m: a for m, a in connus.items() if a and m in marches}
            if not valides:
                # Pas de fiche produit sûre : ne RIEN publier vaut mieux qu'un
                # lien vers un autre produit ou vers une page de recherche.
                sans.append({"id": it["id"], "name": nom, "raison": "sans_fiche"})
                continue

            liens, natifs = {}, []
            for m in marches:
                if m in valides:
                    liens[m] = lien(m, valides[m])
                    natifs.append(m)
                    n_natif += 1
                    continue
                # Repli OneLink : la marketplace voisine la plus proche où la
                # fiche existe réellement.
                repli = next((v for v in VOISINES.get(m, []) if v in valides), None)
                repli = repli or next(iter(valides))
                liens[m] = lien(repli, valides[repli])
                n_onelink += 1
            total_liens += len(liens)

            par_id[it["id"]] = {
                "name": nom,
                "q": requete(cat, it),
                "asin": valides.get(natifs[0]) if natifs else next(iter(valides.values())),
                "product": titres.get(cat, {}).get(it["id"], ""),
                "native": natifs,
                "links": liens,
            }

        items[cat] = par_id
        if sans:
            ignores[cat] = sans
        print(f"{cat}: {len(par_id)} composants liés, {len(sans)} sans lien")

    sortie = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "markets": marches,
        "nativeLinks": n_natif,
        "onelinkLinks": n_onelink,
        "note": ("Liens d'affiliation Amazon pre-calcules vers la FICHE PRODUIT "
                 "(/dp/<ASIN>), utilises par l'app quand aucune offre Amazon "
                 "verifiee n'est disponible. ASIN valides sur la fiche Amazon "
                 "elle-meme ; les marketplaces sans fiche recoivent le lien "
                 "d'une marketplace voisine, OneLink redirigeant l'acheteur."),
        "items": items,
        "skipped": ignores,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out} : {total_liens} liens produit sur {len(marches)} "
          f"marketplaces ({n_natif} natifs, {n_onelink} via OneLink)")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
