# -*- coding: utf-8 -*-
"""Ajoute des PRIX VÉRIFIÉS **AMAZON** (toutes marketplaces) aux sorties
id-clées de l'enrichissement : `gpus_app.json`, `cpus_app.json`,
`rams/ssds/mobos/psus/cases/coolers.json` (lot 31, partie 2 items 2 et 3).

Pourquoi une source dédiée : Amazon n'est pas scrapable en direct (anti-bot),
et les rares offres Amazon récupérées via gputracker ne couvraient que ~8 % du
catalogue. Or Amazon est le **lien d'affiliation principal** de l'app : il faut
un prix Amazon pour chaque composant qui existe chez eux. La PA-API 5.0 du
programme Associates est la seule source fiable — voir `sources/amazon_paapi.py`
et `docs/AMAZON_AFFILIATION.md` (clés à fournir).

Stratégie en deux temps, pour tenir le quota PA-API (1 req/s, ~8 640 req/jour
au départ) :

1. **Résolution d'ASIN** — pour un composant dont l'ASIN est inconnu sur une
   marketplace, un `SearchItems` (1 requête) trouve le meilleur produit
   correspondant. L'ASIN est mémorisé dans `amazon_asins.json` (dépôt data).
2. **Rafraîchissement** — les ASIN connus sont relus par paquets de 10 via
   `GetItems` : 10 fois moins de requêtes, donc un cron qui repasse sur tout le
   catalogue sans exploser le quota.

Le résultat est ADDITIF comme les autres enrichissements : les offres Amazon
sont ajoutées à `prices[]` (avec `inStock` et `currency` réels) après
dédoublonnage des anciennes offres Amazon ; l'`url` vient de la PA-API et
contient **déjà le Partner Tag** de la marketplace.

Usage :
    python scraper/enrich_amazon.py --data-repo <clone Perf-P-data>
        [--items-dir catalog] [--only gpus,cpus]
        [--markets amazon.fr,amazon.de,amazon.com] [--limit N] [--resolve-max N]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import (  # noqa: E402
    CATEGORIES, SORTIES, correspond, produit_suspect, suffixe_ok)
from sources.amazon_paapi import (  # noqa: E402
    MARKETPLACES, AmazonPaapiError, AmazonPaapiSource)

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Ordre de repli des marketplaces (item 3 des consignes) : la France d'abord
# — marché principal —, puis les autres pays couverts par un Partner Tag.
# L'app choisira ensuite CELLE qui correspond au pays de livraison, et se
# rabattra sur une autre si le composant n'y est pas.
ORDRE_DEFAUT = ["amazon.fr", "amazon.de", "amazon.es", "amazon.it",
                "amazon.co.uk", "amazon.com", "amazon.com.au"]

# Fichier de cache des ASIN, versionné dans le dépôt data :
#   {"gpus": {"rtx-5080": {"amazon.fr": "B0XXXX", "amazon.de": null}}}
# Un `null` mémorise une recherche INFRUCTUEUSE : on ne la retente que si
# --recheck est passé, pour ne pas gaspiller le quota à chaque run.
CACHE_ASIN = "amazon_asins.json"


def charger_cache(data_repo):
    try:
        with open(os.path.join(data_repo, CACHE_ASIN), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ecrire_cache(data_repo, cache):
    with open(os.path.join(data_repo, CACHE_ASIN), "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)


def meilleure_offre(offres, cat, nom_app, jetons):
    """Meilleure offre Amazon pour un composant, ou None.

    Mêmes garde-fous que les autres sources : correspondance par jetons,
    suffixe de modèle (« 4070 » ≠ « 4070 Ti »), rejet des bundles/accessoires.
    À correspondance égale, on préfère une offre EN STOCK puis la moins chère.
    """
    valides = [o for o in offres
               if correspond(o["product"], jetons)
               and suffixe_ok(cat, nom_app, o["product"])
               and not produit_suspect(o["product"], cat)]
    if not valides:
        return None
    valides.sort(key=lambda o: (not o.get("in_stock", True), o["price"]))
    return valides[0]


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--markets", default=",".join(ORDRE_DEFAUT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    ap.add_argument("--resolve-max", type=int, default=400,
                    help="nb max de recherches d'ASIN par run (quota PA-API)")
    ap.add_argument("--recheck", action="store_true",
                    help="retenter les composants marqués introuvables")
    args = ap.parse_args()

    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    marches = [m.strip() for m in args.markets.split(",") if m.strip() in MARKETPLACES]
    quand = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    sources = {}
    for m in marches:
        src = AmazonPaapiSource(domaine=m)
        if not src.configure:
            print("Clés PA-API absentes (AMAZON_ACCESS_KEY / AMAZON_SECRET_KEY) — "
                  "rien à faire. Voir docs/AMAZON_AFFILIATION.md.", file=sys.stderr)
            return 2
        sources[m] = src

    cache = charger_cache(args.data_repo)
    budget = args.resolve_max
    total_ok = total_offres = 0

    for cat, (fichier, _cat_id, _slug, q_build, t_build) in CATEGORIES.items():
        if seules and cat not in seules:
            continue
        with open(os.path.join(args.items_dir, f"{fichier}.json"), encoding="utf-8") as f:
            items = json.load(f)
        if args.limit:
            items = items[: args.limit]

        sortie_nom = SORTIES.get(cat, f"{cat}.json")
        chemin = os.path.join(args.data_repo, sortie_nom)
        try:
            with open(chemin, encoding="utf-8") as f:
                existant = {e["id"]: e for e in json.load(f)}
        except (OSError, ValueError):
            existant = {}

        cache_cat = cache.setdefault(cat, {})
        ok = vide = 0

        for it in items:
            requete = q_build(it)
            jetons = t_build(it)
            connus = cache_cat.setdefault(it["id"], {})
            trouvees = []          # offres retenues, une par marketplace au plus
            a_relire = []          # (marketplace, asin) déjà connus

            # --- 1. marketplaces dont l'ASIN est déjà connu -> GetItems ------
            for m in marches:
                asin = connus.get(m)
                if asin:
                    a_relire.append((m, asin))

            for m, asin in a_relire:
                try:
                    offres = sources[m].offres_par_asin([asin])
                except AmazonPaapiError as e:
                    print(f"  ! {m} {cat}/{it['id']}: {e}", file=sys.stderr)
                    continue
                if offres:
                    trouvees.append(offres[0])
                else:
                    # L'ASIN ne renvoie plus d'offre (produit retiré) : on
                    # l'oublie pour qu'un prochain run le recherche à nouveau.
                    connus.pop(m, None)

            # --- 2. marketplaces sans ASIN -> SearchItems (coûteux) ----------
            for m in marches:
                if m in connus and (connus[m] or not args.recheck):
                    continue  # ASIN connu (traité ci-dessus) ou échec mémorisé
                if budget <= 0:
                    break
                budget -= 1
                try:
                    brutes = sources[m].offres(requete)
                    if not brutes and len(requete.split()) > 2:
                        # Repli : la marque est souvent absente du titre Amazon.
                        brutes = sources[m].offres(" ".join(requete.split()[1:]))
                except AmazonPaapiError as e:
                    print(f"  ! {m} {cat}/{it['id']}: {e}", file=sys.stderr)
                    continue
                choix = meilleure_offre(brutes, cat, it["name"], jetons)
                connus[m] = choix.get("asin") if choix else None
                if choix:
                    trouvees.append(choix)

            if not trouvees:
                vide += 1
                continue

            offres = [{"shop": o["shop"], "price": o["price"],
                       "currency": o.get("currency", "EUR"),
                       "url": o["url"], "inStock": bool(o.get("in_stock", True)),
                       "lastSeen": quand, "product": o["product"],
                       "image": o["image"] or None}
                      for o in trouvees]

            e = existant.get(it["id"])
            if e is None:
                e = {"id": it["id"], "name": it["name"],
                     "priceMin": None, "prices": [], "image": None,
                     "lastUpdated": quand}
                existant[it["id"]] = e
            # Dédoublonnage : on retire toutes les anciennes offres Amazon
            # (y compris celles récupérées via gputracker) puis on met les
            # fraîches, qui portent le lien affilié.
            e["prices"] = [p for p in e.get("prices", [])
                           if "amazon." not in (p.get("shop") or "").lower()]
            e["prices"].extend(offres)
            e["lastUpdated"] = quand
            ok += 1
            total_offres += len(offres)
            print(f"  + amazon {cat}/{it['id']}: {len(offres)} marketplaces")

        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(list(existant.values()), f, ensure_ascii=False, indent=1)
        ecrire_cache(args.data_repo, cache)
        total_ok += ok
        print(f"amazon {cat}: {ok} composants avec offre, {vide} sans -> {sortie_nom}")

    print(f"amazon: {total_ok} composants, {total_offres} offres, "
          f"{args.resolve_max - budget} recherches d'ASIN consommées")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
