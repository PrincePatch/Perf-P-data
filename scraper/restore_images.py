# -*- coding: utf-8 -*-
"""Restaure les références d'images de gpus.json après un re-scrape : main.py
régénère le fichier et perd le champ `image` des modèles dont les offres du
jour ne portent pas de photo, alors que le fichier existe toujours dans
`images/`. À exécuter après download_images.py (CI et local).

Usage : python scraper/restore_images.py [--data-repo .]
"""

import argparse
import glob
import json
import os


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", default=".")
    args = ap.parse_args()

    chemin = os.path.join(args.data_repo, "gpus.json")
    with open(chemin, encoding="utf-8") as f:
        data = json.load(f)
    dispo = {os.path.splitext(os.path.basename(f))[0]: "images/" + os.path.basename(f)
             for f in glob.glob(os.path.join(args.data_repo, "images", "*.*"))}
    fixes = 0
    for g in data:
        img = g.get("image")
        if (not img or img.startswith("http")) and g["id"] in dispo:
            g["image"] = dispo[g["id"]]
            fixes += 1
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"références d'images restaurées : {fixes}")


if __name__ == "__main__":
    principal()
