# -*- coding: utf-8 -*-
"""Enrichit le catalogue de l'app avec les PRIX RÉELS multi-boutiques de
gputracker.eu, pour les catégories au-delà des GPU/CPU : RAM, SSD, cartes
mères, alimentations, boîtiers.

Méthode : pour chaque composant des assets de l'app, une recherche textuelle
sur la page `/en/search/category/<id>/<slug>?textualSearch=...` (produits
individuels, triés du moins cher au plus cher) ; les offres dont le nom ne
correspond pas au composant (capacité/fréquence/modèle différents) sont
écartées par un filtre de jetons. Sortie : `<cat>.json` à la racine du dépôt
de données, structure identique à gpus.json (id APP + prices[] avec nom
produit réel + image), consommée par l'app via une fusion PAR ID.

Usage :
    python scraper/enrich_catalog.py --data-repo <clone de Perf-P-data>
                                     [--only rams,ssds] [--limit N]
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(__file__))
from sources.gputracker import GpuTrackerSource, _parse_price  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")
UA = ("PerfP-DataBot/1.0 (+https://github.com/PrincePatch/Perf-P; "
      "component price aggregation for the Perf P app)")

# Catégories : (fichier assets, id gputracker, slug, builder de requête,
# builder de jetons OBLIGATOIRES dans le nom d'offre).
def _norm(s):
    """Minuscule, sans accents, alphanumérique seul — base des comparaisons."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _cap(size_gb):
    """Jetons acceptés pour une capacité en Go (Go/GB/To/TB, avec ou sans espace)."""
    if size_gb >= 1000 and size_gb % 1000 == 0:
        t = size_gb // 1000
        return [f"{t}tb", f"{t}to", f"{size_gb}gb", f"{size_gb}go"]
    return [f"{size_gb}gb", f"{size_gb}go"]


def q_ram(it):
    return f"{it['kind']} {it['mhz']} {it['sizeGb']}GB CL{it['cl']}"


def t_ram(it):
    return [[_norm(it["kind"])], [str(it["mhz"])], _cap(it["sizeGb"]), [f"cl{it['cl']}"]]


def q_ssd(it):
    # SANS capacité : la recherche du site est un AND strict et « 1TB » ne
    # matche pas « 1 To » — la capacité est filtrée côté correspondance.
    return re.sub(r"\([^)]*\)", "", it["name"]).strip()


def t_ssd(it):
    nom = re.sub(r"\([^)]*\)", "", it["name"])
    jetons = [[_norm(w)] for w in nom.split() if len(_norm(w)) >= 2]
    return jetons + [_cap(it.get("sizeGb") or 1000)]


def q_nom(it):
    return it["name"]


def t_nom(it):
    # Tous les jetons significatifs du nom (les numériques sont discriminants).
    return [[_norm(w)] for w in it["name"].split() if len(_norm(w)) >= 2]


CATEGORIES = {
    "rams": ("rams", 11, "memory-ram", q_ram, t_ram),
    "ssds": ("ssds", 4, "ssd", q_ssd, t_ssd),
    "mobos": ("mobos", 8, "motherboards", q_nom, t_nom),
    "psus": ("psus", 6, "power-supplies", q_nom, t_nom),
    "cases": ("cases", 7, "cases", q_nom, t_nom),
}


def offres_pour(src, cat_id, slug, requete):
    """Offres (triées prix croissant par le site) de la recherche [requete]."""
    url = (f"{src.base_url}/en/search/category/{cat_id}/{slug}"
           f"?textualSearch={quote(requete)}&onlyInStock=on")
    soup = src._get(url)
    out = []
    for a in soup.select("a.tracked-product-click"):
        shop = (a.get("data-shop-name") or "").strip()
        nom = (a.get("data-product-name") or "").strip()
        box = a.select_one("div[class*=h1] span")
        prix = _parse_price(box.get_text() if box else "")
        if not shop or not nom or prix is None or prix <= 0:
            continue
        image = None
        for img in a.find_all("img"):
            srcimg = (img.get("src") or "").strip()
            if "/products/" in srcimg:
                image = srcimg
                break
        out.append({"shop": shop, "price": prix, "url": a.get("href", ""),
                    "product": nom, "image": image})
    return out


def correspond(nom_offre, jetons):
    """Vrai si le nom d'offre contient les jetons du composant. Les groupes
    NUMÉRIQUES (capacité, fréquence, CL, chipset…) sont tous obligatoires —
    ils écartent les modèles voisins ; les groupes alphabétiques tolèrent des
    absences (les marchands omettent marque ou gamme : « Crosshair X670E
    Hero » sans « ASUS ROG »)."""
    n = _norm(nom_offre)
    manquants = [grp for grp in jetons if not any(j in n for j in grp)]
    if any(any(ch.isdigit() for ch in g[0]) for g in manquants):
        return False
    alpha = [g for g in jetons if not any(ch.isdigit() for ch in g[0])]
    alpha_manquants = [g for g in manquants if g in alpha]
    return len(alpha_manquants) <= max(1, len(alpha) // 2)


def telecharger_image(src, url, chemin_sans_ext):
    """Télécharge l'image CDN (signature conservée) → chemin relatif ou None."""
    import requests
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200 or "image" not in r.headers.get("Content-Type", ""):
            return None
        ext = ".png" if "png" in r.headers.get("Content-Type", "") else ".jpg"
        chemin = chemin_sans_ext + ext
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "wb") as f:
            f.write(r.content)
        return chemin
    except requests.RequestException:
        return None


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    quand = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    src = GpuTrackerSource("https://www.gputracker.eu", UA, rate_limit=2.0)

    for cat, (fichier, cat_id, slug, q_build, t_build) in CATEGORIES.items():
        if seules and cat not in seules:
            continue
        with open(os.path.join(RACINE, "assets", "data", f"{fichier}.json"),
                  encoding="utf-8") as f:
            items = json.load(f)
        if args.limit:
            items = items[: args.limit]

        sortie = []
        ok = vide = 0
        for it in items:
            requete = q_build(it)
            jetons = t_build(it)
            try:
                brutes = offres_pour(src, cat_id, slug, requete)
                # Repli : la recherche AND échoue souvent sur la MARQUE (les
                # marchands l'omettent) → nouvelle tentative sans le 1er mot.
                if not brutes and len(requete.split()) > 2:
                    brutes = offres_pour(src, cat_id, slug,
                                         " ".join(requete.split()[1:]))
            except Exception as e:  # noqa: BLE001 — une requête ratée ne tue pas le run
                print(f"  ! {cat}/{it['id']}: {e}", file=sys.stderr)
                continue
            offres = [o for o in brutes if correspond(o["product"], jetons)]
            if not offres:
                vide += 1
                continue
            # Image : celle de l'offre la moins chère qui en porte une.
            image_rel = None
            for o in offres:
                if o["image"]:
                    rel = telecharger_image(
                        src, o["image"],
                        os.path.join(args.data_repo, "images", f"{cat}-{it['id']}"))
                    if rel:
                        image_rel = os.path.relpath(rel, args.data_repo).replace(os.sep, "/")
                    break
            sortie.append({
                "id": it["id"],
                "name": it["name"],
                "priceMin": offres[0]["price"],
                "prices": [{"shop": o["shop"], "price": o["price"], "currency": "EUR",
                            "url": o["url"], "inStock": True, "lastSeen": quand,
                            "product": o["product"]} for o in offres[:15]],
                "image": image_rel,
                "lastUpdated": quand,
            })
            ok += 1
            print(f"  + {cat}/{it['id']}: {len(offres)} offres, min {offres[0]['price']} €")

        chemin = os.path.join(args.data_repo, f"{cat}.json")
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(sortie, f, ensure_ascii=False, indent=1)
        print(f"{cat}: {ok} composants avec offres, {vide} sans correspondance "
              f"-> {os.path.basename(chemin)}")


if __name__ == "__main__":
    principal()
