# -*- coding: utf-8 -*-
"""Ajoute des PRIX VÉRIFIÉS AMÉRICAINS (Newegg, en $) aux sorties
`gpus_app.json` / `cpus_app.json`, EN PLUS des offres euro de gputracker
(lot 21, item 3). Source complémentaire → plus de prix vérifiés, et surtout des
prix réels pour les utilisateurs américains (l'app filtre par pays de livraison
et convertit les devises).

Additif et sûr : on ne touche PAS aux offres euro existantes ni au `priceMin`
euro (les offres Newegg sont ajoutées à `prices[]` avec `"currency": "USD"`).
Réutilise les filtres de correspondance de `enrich_catalog`.

Usage :
    python scraper/enrich_newegg.py --data-repo <clone Perf-P-data>
                                    [--items-dir catalog] [--only gpus,cpus] [--limit N]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import _norm, correspond, suffixe_ok, t_nom  # noqa: E402
from sources.newegg import NeweggSource  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")

# catégorie app -> (fichier d'items, fichier de sortie enrichi)
CATS = {
    "gpus": ("gpus", "gpus_app.json"),
    "cpus": ("cpus", "cpus_app.json"),
}


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="gpus,cpus")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    args = ap.parse_args()
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    quand = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ng = NeweggSource(rate_limit=1.5)

    for cat, (fichier, sortie_nom) in CATS.items():
        if seules and cat not in seules:
            continue
        with open(os.path.join(args.items_dir, f"{fichier}.json"), encoding="utf-8") as f:
            items = json.load(f)
        if args.limit:
            items = items[: args.limit]

        chemin = os.path.join(args.data_repo, sortie_nom)
        try:
            with open(chemin, encoding="utf-8") as f:
                existant = {e["id"]: e for e in json.load(f)}
        except (OSError, ValueError):
            existant = {}

        ok = vide = 0
        for it in items:
            try:
                brutes = ng.offres(it["name"])
            except Exception as e:  # noqa: BLE001 — une requête ratée ne tue pas le run
                print(f"  ! newegg {cat}/{it['id']}: {e}", file=sys.stderr)
                continue
            jetons = t_nom(it)
            offres = sorted(
                [o for o in brutes
                 if correspond(o["product"], jetons)
                 and suffixe_ok(cat, it["name"], o["product"])],
                key=lambda o: o["price"])[:8]
            if not offres:
                vide += 1
                continue

            usd = [{"shop": o["shop"], "price": o["price"], "currency": "USD",
                    "url": o["url"], "inStock": True, "lastSeen": quand,
                    "product": o["product"], "image": o["image"] or None}
                   for o in offres]

            e = existant.get(it["id"])
            if e is None:
                # Pas d'offre euro : on crée l'entrée avec un priceMin NORMALISÉ
                # en euros (pour ne pas fausser le prix indicatif européen).
                e = {"id": it["id"], "name": it["name"],
                     "priceMin": round(offres[0]["price"] / 1.08, 2),
                     "prices": [], "image": None, "lastUpdated": quand}
                existant[it["id"]] = e
            # Dédoublonnage : on retire d'anciennes offres Newegg puis on rajoute.
            e["prices"] = [p for p in e.get("prices", [])
                           if "newegg" not in (p.get("shop") or "").lower()]
            e["prices"].extend(usd)
            e["lastUpdated"] = quand
            ok += 1
            print(f"  + newegg {cat}/{it['id']}: {len(usd)} offres US, min ${offres[0]['price']}")

        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(list(existant.values()), f, ensure_ascii=False, indent=1)
        print(f"newegg {cat}: {ok} composants enrichis US, {vide} sans correspondance "
              f"-> {sortie_nom}")


if __name__ == "__main__":
    principal()
