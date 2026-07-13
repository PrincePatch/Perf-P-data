# -*- coding: utf-8 -*-
"""Ajoute des PRIX VÉRIFIÉS + MODÈLES + IMAGES des boutiques à SOURCE DIRECTE
aux sorties id-clées de l'enrichissement : `gpus_app.json`, `cpus_app.json`,
`rams/ssds/mobos/psus/cases.json` (lot 27-28). Runner GÉNÉRIQUE multi-boutiques
(remplace l'ancien enrich_ldlc.py mono-boutique) :

- **LDLC** (`ldlc.com`, France/Europe, EUR) — quasi tous les modèles achetables,
  photos haute résolution, disponibilité réelle ;
- **Morele** (`morele.net`, Pologne, PLN) — couverture des utilisateurs PL ;
- **Canada Computers** (`canadacomputers.com`, Canada, CAD) — couverture CA.

Additif et sûr (même principe que `enrich_newegg`) : les offres de chaque
boutique sont AJOUTÉES à `prices[]` (avec `inStock` réel + `currency`) après
dédoublonnage de ses anciennes offres ; le `priceMin` d'une entrée existante
n'est pas modifié. Si une entrée n'a AUCUNE image, la photo LDLC haute
résolution du meilleur modèle est téléchargée. Filtres partagés avec
`enrich_catalog` : correspondance par jetons, suffixes de modèle, mots de
bundle (`produit_suspect`) et écart de prix aberrant (`sans_aberrantes`).

À exécuter APRÈS `enrich_catalog` (et `enrich_newegg`) pour compléter les
fichiers existants ; exécuté seul, il crée des fichiers ne contenant que ses
offres (toujours valides).

Usage :
    python scraper/enrich_shops.py --data-repo <clone Perf-P-data>
                                   [--items-dir catalog] [--only gpus,rams]
                                   [--shops ldlc,morele,canadacomputers] [--limit N]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import (  # noqa: E402
    CATEGORIES, SORTIES, correspond, produit_suspect, sans_aberrantes,
    suffixe_ok, telecharger_image, prix_eur)
from sources.boutiques import (  # noqa: E402
    AlternateSource, CanadaComputersSource, MaterielNetSource, MoreleSource)
from sources.ldlc import LdlcSource  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Boutiques disponibles : id → fabrique de source. Chaque source expose
# `offres(requete)`, `domaine` et `devise`.
BOUTIQUES = {
    "ldlc": lambda: LdlcSource(rate_limit=1.5),
    # Morele limite vite (429) : cadence 4 s + backoff Retry-After (lot 29).
    "morele": lambda: MoreleSource(rate_limit=4.0),
    "canadacomputers": lambda: CanadaComputersSource(rate_limit=1.5),
    # Lot 29 : deux sources EUROPÉENNES de plus (stock réel + tout le catalogue,
    # au-delà du sous-ensemble gputracker).
    "alternate": lambda: AlternateSource(rate_limit=1.5),
    "materielnet": lambda: MaterielNetSource(rate_limit=1.5),
}


def _min_eur(offres):
    """Prix mini EN EUROS pour le priceMin d'une NOUVELLE entrée : le moins
    cher parmi les offres EN STOCK si possible, sinon toutes offres."""
    dispo = [o for o in offres if o.get("in_stock", True)]
    pool = dispo or offres
    return round(min(prix_eur(o) for o in pool), 2)


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--shops", default=",".join(BOUTIQUES))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    args = ap.parse_args()
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    boutiques = [b.strip() for b in args.shops.split(",") if b.strip() in BOUTIQUES]
    quand = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    sources = [(b, BOUTIQUES[b]()) for b in boutiques]

    for cat, (fichier, _cat_id, _slug, q_build, t_build) in CATEGORIES.items():
        if seules and cat not in seules:
            continue
        with open(os.path.join(args.items_dir, f"{fichier}.json"),
                  encoding="utf-8") as f:
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

        ok = vide = imgs = 0
        for it in items:
            requete = q_build(it)
            jetons = t_build(it)
            nouvelles = []  # offres retenues, toutes boutiques directes
            for nom_src, src in sources:
                try:
                    brutes = src.offres(requete)
                    # Repli : la recherche échoue souvent sur la MARQUE
                    # (souvent omise) → nouvelle tentative sans le 1er mot.
                    if not brutes and len(requete.split()) > 2:
                        brutes = src.offres(" ".join(requete.split()[1:]))
                except Exception as e:  # noqa: BLE001 — une requête ratée ne tue pas le run
                    print(f"  ! {nom_src} {cat}/{it['id']}: {e}", file=sys.stderr)
                    continue
                filtrees = sorted(
                    [o for o in brutes
                     if correspond(o["product"], jetons)
                     and suffixe_ok(cat, it["name"], o["product"])
                     and not produit_suspect(o["product"])],
                    key=lambda o: (not o.get("in_stock", True), o["price"]))[:8]
                nouvelles.extend(filtrees)
            nouvelles = sans_aberrantes(nouvelles)
            if not nouvelles:
                vide += 1
                continue

            offres = [{"shop": o["shop"], "price": o["price"],
                       "currency": o.get("currency", "EUR"),
                       "url": o["url"], "inStock": bool(o.get("in_stock", True)),
                       "lastSeen": quand, "product": o["product"],
                       "image": o["image"] or None}
                      for o in nouvelles]

            e = existant.get(it["id"])
            if e is None:
                e = {"id": it["id"], "name": it["name"],
                     "priceMin": _min_eur(nouvelles),
                     "prices": [], "image": None, "lastUpdated": quand}
                existant[it["id"]] = e
            # Dédoublonnage : retire les anciennes offres des boutiques
            # re-scrapées, puis rajoute les fraîches.
            domaines = tuple(s.domaine for _n, s in sources)
            e["prices"] = [p for p in e.get("prices", [])
                           if not any(d in (p.get("shop") or "").lower() for d in domaines)]
            e["prices"].extend(offres)

            # Complétion d'IMAGE (lot 27, item 4) : si l'entrée n'a pas de
            # photo, on télécharge celle du meilleur modèle LDLC en haute
            # résolution (media.ldlc.com : /r150/ → /r1600/).
            if not e.get("image"):
                for o in nouvelles:
                    if o["shop"] == "ldlc.com" and o["image"]:
                        grande = o["image"].replace("/r150/", "/r1600/")
                        rel = telecharger_image(
                            sources[0][1], grande,
                            os.path.join(args.data_repo, "images", f"{cat}-{it['id']}"))
                        if rel:
                            e["image"] = os.path.relpath(rel, args.data_repo).replace(os.sep, "/")
                            imgs += 1
                        break

            e["lastUpdated"] = quand
            ok += 1
            dispo = sum(1 for o in nouvelles if o.get("in_stock", True))
            print(f"  + shops {cat}/{it['id']}: {len(offres)} offres ({dispo} en stock)")

        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(list(existant.values()), f, ensure_ascii=False, indent=1)
        print(f"shops {cat}: {ok} composants enrichis, {imgs} images ajoutées, "
              f"{vide} sans correspondance -> {sortie_nom}")


if __name__ == "__main__":
    principal()
