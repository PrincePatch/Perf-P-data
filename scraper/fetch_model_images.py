# -*- coding: utf-8 -*-
"""Récupère une photo pour chaque MODÈLE (variante AIB) vendu qui n'en a pas.

Contexte : les offres gputracker portent parfois une photo (`prices[].image`),
mais beaucoup de modèles n'en ont AUCUNE — l'app affiche alors la photo de la
carte parente pour toutes ses variantes (lot 17, item 3). Ce script :

  1. lit les offres publiées (gpus/gpus_app/rams/ssds.json),
  2. collecte les noms de produits (modèles) dont AUCUNE offre ne porte d'image,
  3. cherche une photo par modèle via DuckDuckGo (CDN marchands/fabricants),
  4. télécharge dans `catalog/images/models/<slug>.<ext>`,
  5. écrit l'index `catalog/model_images.json` : {nomNormalisé: cheminRelatif}.

L'app fusionne cet index par nom de modèle normalisé (`LivePrices.normName`),
chaque variante récupère ainsi SA photo. Incrémental / réexécutable.

Usage :
    python scraper/fetch_model_images.py --data-repo <clone Perf-P-data>
                                         [--limit N] [--only gpus]
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata

import requests

sys.path.insert(0, os.path.dirname(__file__))
from fetch_catalog_images import DOMAINES, chercher_image, telecharger, UA  # noqa: E402

# Fichiers d'offres à balayer + mot-clé de recherche par catégorie.
SOURCES = {
    "gpus": ("gpus", "carte graphique"),
    "gpus_app": ("gpus_app", "carte graphique"),
    "rams": ("rams", "mémoire RAM"),
    "ssds": ("ssds", "SSD"),
}

_MARQUES = re.compile(r"\b(nvidia|amd|intel|geforce|radeon)\b")


def norm_name(s):
    """Réplique EXACTE de LivePrices.normName (côté app) : retire les mots de
    marque puis minuscule sans espace ni ponctuation."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = _MARQUES.sub("", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    seules = {c.strip() for c in args.only.split(",") if c.strip()}

    dossier = os.path.join(args.data_repo, "catalog", "images", "models")
    chemin_index = os.path.join(args.data_repo, "catalog", "model_images.json")
    index = {}
    if os.path.exists(chemin_index):
        with open(chemin_index, encoding="utf-8") as f:
            index = json.load(f)

    # 1-2. Collecte des modèles sans image, dédoublonnés par nom normalisé.
    a_chercher = {}  # normName -> (nom affiché, mot-clé)
    deja_avec = set()
    for cat, (fichier, motcle) in SOURCES.items():
        if seules and cat not in seules:
            continue
        chemin = os.path.join(args.data_repo, f"{fichier}.json")
        if not os.path.exists(chemin):
            continue
        with open(chemin, encoding="utf-8") as f:
            data = json.load(f)
        for comp in data:
            for offre in comp.get("prices") or []:
                nom = (offre.get("product") or "").strip()
                if not nom:
                    continue
                k = norm_name(nom)
                if offre.get("image"):
                    deja_avec.add(k)
                elif k not in a_chercher:
                    a_chercher[k] = (nom, motcle)
    # Ne cherche que ceux vraiment sans aucune image et pas déjà indexés.
    cibles = [(k, v) for k, v in a_chercher.items()
              if k not in deja_avec and k not in index]
    if args.limit:
        cibles = cibles[: args.limit]
    print(f"{len(cibles)} modèles à illustrer (déjà indexés : {len(index)})")

    # 3-5. Recherche + téléchargement + index incrémental.
    ok = manque = 0
    for i, (k, (nom, motcle)) in enumerate(cibles):
        try:
            url = chercher_image(nom, motcle)
        except Exception as e:  # rate-limit ddgs → pause
            print(f"  ! {nom[:40]}: {e}", file=sys.stderr)
            time.sleep(20)
            url = None
        if url:
            chemin = telecharger(url, os.path.join(dossier, slug(nom)))
            if chemin:
                rel = os.path.relpath(chemin, args.data_repo).replace(os.sep, "/")
                index[k] = rel
                ok += 1
                if ok % 20 == 0:
                    print(f"  {ok} images, {manque} manquantes ({i + 1}/{len(cibles)})")
            else:
                manque += 1
        else:
            manque += 1
        with open(chemin_index, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=1, sort_keys=True)
        time.sleep(1.5)

    print(f"Terminé : {ok} nouvelles images, {manque} manquantes. "
          f"Index : {len(index)} modèles → {chemin_index}")


if __name__ == "__main__":
    principal()
