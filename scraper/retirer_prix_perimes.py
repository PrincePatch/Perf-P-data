# -*- coding: utf-8 -*-
"""Retire les prix PÉRIMÉS des fichiers « par modèle » gpus.json / cpus.json.

Ces deux fichiers viennent du seul gputracker.eu (main.py). Quand gputracker
ne répond plus (403 Cloudflare sur les serveurs de GitHub, constaté depuis le
25/10/2026), main.py les laisse en place : leurs prix vieillissent. Or l'app
les PRÉFÈRE aux offres par composant (gpus_app.json / cpus_app.json, rafraîchies
par les boutiques directes, Newegg, Amazon) : elle affichait donc des prix
figés. Au-delà de `--age-max-h` heures, ce script publie ces fichiers SANS
prix (`price`, `priceMin` à null, `prices` vide) ; nom, image, indices et
caractéristiques restent. L'app retombe alors d'elle-même sur les offres par
composant (`avecLiveIdDoux` dans dataset_repository.dart), à défaut sur le
prix de son catalogue. Une collecte gputracker réussie réécrit les fichiers
avec des prix frais : rien à défaire.

Idempotent : un fichier déjà sans prix n'est pas réécrit (aucun commit vide).

Usage :
    python scraper/retirer_prix_perimes.py --data-repo . [--age-max-h 30]
"""

import argparse
import json
import os
from datetime import datetime, timezone

FICHIERS = ("gpus.json", "cpus.json")


def lire_date(valeur):
    """Date ISO 8601 (« Z » accepté) en UTC ; None si absente ou illisible."""
    try:
        date = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return date if date.tzinfo else date.replace(tzinfo=timezone.utc)


def date_collecte(items):
    """Date de la dernière collecte d'un fichier : le `lastUpdated` le plus récent."""
    dates = [d for d in (lire_date(it.get("lastUpdated")) for it in items) if d]
    return max(dates) if dates else None


def a_des_prix(item):
    return bool(item.get("prices")) or item.get("priceMin") is not None or item.get("price") is not None


def retirer_prix(items):
    """Vide les champs de prix de chaque élément ; retourne le nombre d'éléments modifiés."""
    modifies = 0
    for it in items:
        if a_des_prix(it):
            it["price"] = None
            it["priceMin"] = None
            it["prices"] = []
            modifies += 1
    return modifies


def traiter_fichier(chemin, age_max_h, maintenant):
    """Retire les prix d'un fichier s'ils sont plus vieux que `age_max_h`. Retourne (âge en h, éléments modifiés)."""
    with open(chemin, encoding="utf-8") as f:
        texte = f.read()
    items = json.loads(texte)
    collecte = date_collecte(items)
    if collecte is None:
        # Pas de date : on ne sait pas juger, on ne touche à rien.
        return None, 0
    age_h = (maintenant - collecte).total_seconds() / 3600
    if age_h <= age_max_h:
        return age_h, 0
    modifies = retirer_prix(items)
    if modifies:
        # Même indentation que le fichier d'origine : un diff limité aux prix.
        indent = 1 if texte.startswith("[\n {") else 2
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=indent)
    return age_h, modifies


def marquer_meta(chemin_meta, fichiers_retires, maintenant):
    """Note dans meta.json que les prix sont retirés (une seule fois : pas de diff à chaque run)."""
    try:
        with open(chemin_meta, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return
    if meta.get("pricesWithdrawn"):
        return
    meta["pricesWithdrawn"] = {
        "since": maintenant.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "files": fichiers_retires,
        "reason": "gputracker.eu indisponible : prix plus vieux que le seuil retires, "
                  "l'app utilise les offres par composant (gpus_app.json, cpus_app.json)",
    }
    with open(chemin_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def principal(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-repo", default=".")
    ap.add_argument("--age-max-h", type=float, default=30.0)
    args = ap.parse_args(argv)
    maintenant = datetime.now(timezone.utc)

    retires = []
    for nom in FICHIERS:
        chemin = os.path.join(args.data_repo, nom)
        if not os.path.exists(chemin):
            print(f"{nom}: absent, ignoré")
            continue
        age_h, modifies = traiter_fichier(chemin, args.age_max_h, maintenant)
        if age_h is None:
            print(f"{nom}: aucune date de collecte, laissé tel quel")
        elif age_h <= args.age_max_h:
            print(f"{nom}: prix de il y a {age_h:.0f} h, gardés")
        else:
            retires.append(nom)
            print(f"{nom}: prix vieux de {age_h / 24:.1f} j (> {args.age_max_h:.0f} h), "
                  f"{modifies} élément(s) publiés sans prix")
    if retires:
        marquer_meta(os.path.join(args.data_repo, "meta.json"), retires, maintenant)
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
