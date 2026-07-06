# -*- coding: utf-8 -*-
"""Récupère une photo produit pour CHAQUE composant du catalogue de l'app
(CPU, carte mère, RAM, SSD, refroidissement, alimentation, boîtier,
ventilateurs) via la recherche d'images DuckDuckGo (paquet `ddgs`), en
privilégiant les CDN marchands/fabricants (Amazon, LDLC, MSI, Corsair…).

Les images sont téléchargées dans `<data-repo>/catalog/images/<cat>/<id>.<ext>`
et un index `<data-repo>/catalog/images.json` ({catégorie: {id: chemin}}) est
écrit — c'est ce fichier que l'app télécharge pour afficher les photos.

Usage :
    python scraper/fetch_catalog_images.py --data-repo <clone de Perf-P-data>
                                           [--only mobos,psus] [--limit N]

Réexécutable : les images déjà présentes ne sont pas retéléchargées.
"""

import argparse
import json
import os
import sys
import time

import requests

try:
    from ddgs import DDGS
except ImportError:  # ancien nom du paquet
    from duckduckgo_search import DDGS

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Catégories : (fichier assets, mot-clé de recherche).
CATEGORIES = {
    "gpus": ("gpus", "carte graphique"),
    "cpus": ("cpus", "processeur"),
    "mobos": ("mobos", "carte mère"),
    "rams": ("rams", "RAM"),
    "ssds": ("ssds", "SSD"),
    "coolers": ("coolers", "ventirad watercooling"),
    "psus": ("psus", "alimentation PC"),
    "cases": ("cases", "boîtier PC"),
    "fans": ("fans", "ventilateur PC"),
}

# Domaines de confiance, par ordre de préférence (CDN marchands puis fabricants).
DOMAINES = [
    "m.media-amazon.com",
    "media.ldlc.com",
    "ldlc.com",
    "materiel.net",
    "topachat.com",
    "alternate",
    "caseking",
    "bbystatic.com",
    "neweggimages.com",
    "techpowerup.com",
    "msi.com",
    "asus.com",
    "gigabyte.com",
    "asrock.com",
    "corsair.com",
    "gskill.com",
    "kingston.com",
    "crucial",
    "samsung.com",
    "westerndigital.com",
    "seagate.com",
    "noctua.at",
    "bequiet.com",
    "arctic.de",
    "thermalright.com",
    "coolermaster.com",
    "deepcool.com",
    "nzxt.com",
    "lian-li.com",
    "fractal-design.com",
    "phanteks.com",
    "seasonic.com",
    "montech.com",
    "endorfy.com",
    "hyte.com",
]

UA = "PerfP-DataBot/1.0 (+https://github.com/PrincePatch/Perf-P-data)"


def score_domaine(url):
    """Rang de préférence du domaine de [url] (plus petit = mieux), None si
    le domaine n'est pas dans la liste de confiance."""
    u = url.lower()
    for i, d in enumerate(DOMAINES):
        if d in u:
            return i
    return None


def chercher_image(nom, mot_cle):
    """Cherche la meilleure URL d'image produit pour [nom] : premier résultat
    dont le domaine est de confiance (les résultats DuckDuckGo sont déjà triés
    par pertinence, on ne réordonne PAS par domaine au-delà du filtre)."""
    with DDGS() as d:
        resultats = list(d.images(f"{nom} {mot_cle}", max_results=12, region="fr-fr"))
    candidats = []
    for r in resultats:
        url = r.get("image") or ""
        rang = score_domaine(url)
        if rang is not None:
            candidats.append((rang, url))
    if not candidats:
        return None
    # Priorité aux CDN marchands (rang), à pertinence DuckDuckGo décroissante.
    candidats.sort(key=lambda c: c[0])
    return candidats[0][1]


def telecharger(url, chemin_sans_ext):
    """Télécharge [url] vers [chemin_sans_ext].<ext> ; retourne le chemin
    relatif final ou None (contenu non-image ou trop petit = rejet)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200 or len(r.content) < 4000:
            return None
        ctype = r.headers.get("Content-Type", "")
        ext = {"image/png": ".png", "image/webp": ".webp"}.get(ctype.split(";")[0], ".jpg")
        if "image" not in ctype:
            return None
        chemin = chemin_sans_ext + ext
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "wb") as f:
            f.write(r.content)
        return chemin
    except requests.RequestException:
        return None


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True, help="clone local de Perf-P-data")
    ap.add_argument("--only", default="", help="catégories séparées par des virgules")
    ap.add_argument("--limit", type=int, default=0, help="máx d'items par catégorie (0 = tous)")
    args = ap.parse_args()

    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    dossier_images = os.path.join(args.data_repo, "catalog", "images")
    chemin_index = os.path.join(args.data_repo, "catalog", "images.json")
    index = {}
    if os.path.exists(chemin_index):
        with open(chemin_index, encoding="utf-8") as f:
            index = json.load(f)

    for cat, (fichier, mot_cle) in CATEGORIES.items():
        if seules and cat not in seules:
            continue
        with open(os.path.join(RACINE, "assets", "data", f"{fichier}.json"), encoding="utf-8") as f:
            items = json.load(f)
        if args.limit:
            items = items[: args.limit]
        index.setdefault(cat, {})
        ok = manque = 0
        for it in items:
            iid, nom = it["id"], it["name"]
            if iid in index[cat]:
                ok += 1
                continue
            url = None
            try:
                url = chercher_image(nom, mot_cle)
            except Exception as e:  # rate-limit ddgs → on attend et on continue
                print(f"  ! {cat}/{iid}: {e}", file=sys.stderr)
                time.sleep(20)
            if url:
                chemin = telecharger(url, os.path.join(dossier_images, cat, iid))
                if chemin:
                    rel = os.path.relpath(chemin, args.data_repo).replace(os.sep, "/")
                    index[cat][iid] = rel
                    ok += 1
                    print(f"  + {cat}/{iid} <- {url[:80]}")
                else:
                    manque += 1
            else:
                manque += 1
            # Sauvegarde incrémentale + politesse envers le moteur de recherche.
            with open(chemin_index, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=1, sort_keys=True)
            time.sleep(1.5)
        print(f"{cat}: {ok} images, {manque} manquantes")

    with open(chemin_index, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"Index écrit : {chemin_index}")


if __name__ == "__main__":
    principal()
