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
    # GPU/CPU par ID d'app (lot 13, item 5) : complète le fichier « par modèle »
    # du scraper principal — sorties gpus_app.json / cpus_app.json.
    "gpus": ("gpus", 1, "graphics-cards", q_nom, t_nom),
    "cpus": ("cpus", 2, "processors", q_nom, t_nom),
    "rams": ("rams", 11, "memory-ram", q_ram, t_ram),
    "ssds": ("ssds", 4, "ssd", q_ssd, t_ssd),
    "mobos": ("mobos", 8, "motherboards", q_nom, t_nom),
    "psus": ("psus", 6, "power-supplies", q_nom, t_nom),
    "cases": ("cases", 7, "cases", q_nom, t_nom),
}

# Fichier de sortie par catégorie (GPU/CPU ne doivent PAS écraser les fichiers
# « par modèle » gpus.json / cpus.json produits par main.py).
SORTIES = {"gpus": "gpus_app.json", "cpus": "cpus_app.json"}

# Suffixes de modèle discriminants : une offre « RTX 4070 Ti Super » ne doit
# JAMAIS matcher le composant « RTX 4070 » (et « 7600X » ≠ « 7600 »).
_SUFFIXES = {
    "gpus": ["tisuper", "ti", "super", "xtx", "xt", "gre"],
    "cpus": ["x3d", "ks", "kf", "x", "k", "f"],
}


def _norm_espace(s):
    """Norme qui PRÉSERVE les frontières de mots (séparateurs → espace) —
    permet de savoir si un chiffre suivait le suffixe SANS séparateur."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower())


def _suffixe_apres(norm, espace, num, suffixes):
    """Suffixe de modèle collé à [num] dans [norm] (ex. '4070tisuper' → 'tisuper').
    Un CHIFFRE collé derrière le suffixe DANS LE NOM BRUT en fait un AUTRE
    modèle (lot 28 : « 9950X3D2 Dual Edition » ≠ « 9950X3D ») — il est inclus
    dans le suffixe rendu pour faire échouer la comparaison. La contiguïté est
    vérifiée sur [espace] : « 9950X3D (4.3 GHz) » donne bien 'x3d' (le 4 est
    séparé par une parenthèse), « 9950X3D2 » donne 'x3d2'."""
    i = norm.find(num)
    if i < 0:
        return None
    reste = norm[i + len(num):]
    for suf in suffixes:  # liste ordonnée du plus long au plus court
        if reste.startswith(suf):
            fin = reste[len(suf):]
            if fin[:1].isdigit() and (num + suf + fin[:1]) in espace:
                return suf + fin[:1]
            return suf
    return ""


def suffixe_ok(cat, nom_app, nom_offre):
    """Vrai si l'offre porte EXACTEMENT le même suffixe de modèle que le
    composant de l'app après chaque numéro de modèle (4070 vs 4070 Ti…)."""
    suffixes = _SUFFIXES.get(cat)
    if suffixes is None:
        return True
    napp, noff = _norm(nom_app), _norm(nom_offre)
    eapp, eoff = _norm_espace(nom_app), _norm_espace(nom_offre)
    for num in re.findall(r"\d{3,5}", nom_app):
        attendu = _suffixe_apres(napp, eapp, num, suffixes)
        trouve = _suffixe_apres(noff, eoff, num, suffixes)
        if trouve is not None and trouve != attendu:
            return False
    return True


# Requête SPÉCIFIQUE pour la passe « grandes enseignes » quand la requête
# standard contient un jeton que les boutiques FR n'écrivent pas pareil
# (LDLC écrit « 32 Go », pas « 32GB ») — la capacité est déjà contrôlée par
# le filtre de correspondance.
PRIO_QUERY = {
    "rams": lambda it: f"{it['kind']} {it['mhz']} CL{it['cl']}",
}

# Grandes enseignes prioritaires (ids fv_shop gputracker) : leurs offres sont
# recherchées EN PLUS, même hors du top-20 par prix — l'app affiche ainsi le
# prix vérifié LDLC/Amazon/Materiel.net/Alternate presque à chaque fois
# (lot 16, item 9).
FV_SHOPS_PRIORITAIRES = ["3", "4", "2", "18"]  # ldlc, amazon.fr, materiel.net, alternate.fr
# 2e vague (lot 17, item 6 — élargie lot 28) : TOUTES les autres enseignes du
# filtre gputracker suivies par l'app. Le paramètre fv_shop est un OU côté
# site (vérifié) → une seule requête pour les 10.
FV_SHOPS_SECONDAIRES = [
    "42", "78", "9", "195", "5",     # grosbill, cybertek, rueducommerce, topbiz, cdiscount
    "27", "65", "109", "171", "6",   # reichelt, compumsa, bpm-power, codima.be, amazon.de
]

# Mots signalant un PRODUIT COMPOSÉ (PC monté, bundle CPU+carte mère, kit
# d'upgrade…) dont le prix ne correspond PAS au composant seul : ces offres
# passent parfois le filtre de jetons (le nom du bundle contient le nom du
# composant) — lot 28. Comparaison sur le nom BRUT en minuscules (pas _norm,
# pour garder les séparateurs : « pc » seul matcherait « PCIe »).
_MOTS_BUNDLE = (
    " + ", "combo", "bundle", "komputer", "zestaw", "gaming pc", "pc gamer",
    "desktop", "laptop", "notebook", "ordinateur", "barebone", "mini pc",
    "upgrade kit", "kit upgrade", "kit évolution", "system", "all-in-one",
)

# ACCESSOIRES portant le nom d'un composant (waterblock, backplate, support
# anti-sag, riser, adaptateur…) — lot 29b : « Alphacool Core Geforce RTX 5080 »
# est un waterblock, PAS une carte ; son prix (~220 €) polluait la 5080.
# NB : « waterforce » (Gigabyte AORUS WATERFORCE = vraie carte AIO) et
# « founders » restent volontairement absents.
_MOTS_ACCESSOIRE = (
    "alphacool", "waterblock", "water block", "water-block", "vga block",
    "gpu block", "backplate", "back plate", "anti-sag", "anti sag",
    "support gpu", "gpu support", "gpu holder", "gpu bracket", "riser",
    "bykski", "kryographics", "ek-quantum", "ek quantum", "ek water",
    "thermal pad", "pâte thermique", "thermal paste", "adaptateur", "adapter",
    "câble", "cable", "rgb strip", "watercooling gpu",
)


def produit_suspect(nom_offre):
    """Vrai si l'offre ressemble à un bundle / PC complet OU à un ACCESSOIRE
    (waterblock, backplate, câble…) plutôt qu'au composant seul — son prix
    polluerait les « prix vérifiés » (lot 28-29b)."""
    bas = f" {nom_offre.lower()} "
    return any(m in bas for m in _MOTS_BUNDLE) or any(m in bas for m in _MOTS_ACCESSOIRE)


# Taux €→devise pour comparer des offres multi-devises (mêmes taux figés que
# le modèle Dart de l'app).
_TAUX_EUR = {"USD": 1.08, "GBP": 0.86, "CHF": 0.94, "PLN": 4.30, "CAD": 1.47}


def prix_eur(offre):
    """Prix d'une offre ramené en euros (repli 1:1 pour l'EUR/devise inconnue)."""
    return offre["price"] / _TAUX_EUR.get(offre.get("currency", "EUR"), 1.0)


def sans_aberrantes(offres):
    """Écarte les offres à plus de 2,5× la moins chère (en euros) : ce sont
    presque toujours des bundles/éditions spéciales qui ont échappé aux mots
    de bundle — le composant seul ne varie jamais autant entre boutiques."""
    if len(offres) < 2:
        return offres
    mini = min(prix_eur(o) for o in offres)
    return [o for o in offres if prix_eur(o) <= mini * 2.5]


def plafonner_offres(offres, cap=40):
    """Plafonne [offres] (triées prix croissant) en PRÉSERVANT la diversité de
    boutiques : d'abord la moins chère de CHAQUE boutique (une offre forcée
    Amazon/LDLC chère ne tombe plus hors du top — lot 28), puis les variantes
    restantes par prix jusqu'à [cap]."""
    vues = set()
    tetes, restes = [], []
    for o in offres:
        if o["shop"] not in vues:
            vues.add(o["shop"])
            tetes.append(o)
        else:
            restes.append(o)
    return (tetes + restes)[:cap]


def offres_pour(src, cat_id, slug, requete, fv_shops=None):
    """Offres (triées prix croissant par le site) de la recherche [requete].
    [fv_shops] restreint la recherche aux boutiques données (ids gputracker)."""
    filtres = "".join(f"&fv_shop={s}" for s in (fv_shops or []))
    url = (f"{src.base_url}/en/search/category/{cat_id}/{slug}"
           f"?textualSearch={quote(requete)}&onlyInStock=on{filtres}")
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
    # Dossier des listes de composants. Défaut : assets de l'app (exécution
    # depuis le repo app) ; le CRON du dépôt de données passe `catalog/`
    # (copie synchronisée des assets) → pipeline AUTONOME, prix à jour à
    # chaque exécution planifiée (lot 13, item 5).
    ap.add_argument("--items-dir",
                    default=os.path.join(RACINE, "assets", "data"))
    args = ap.parse_args()
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    quand = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    src = GpuTrackerSource("https://www.gputracker.eu", UA, rate_limit=2.0)

    for cat, (fichier, cat_id, slug, q_build, t_build) in CATEGORIES.items():
        if seules and cat not in seules:
            continue
        with open(os.path.join(args.items_dir, f"{fichier}.json"),
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
                requete_ok = requete
                if not brutes and len(requete.split()) > 2:
                    requete_ok = " ".join(requete.split()[1:])
                    brutes = offres_pour(src, cat_id, slug, requete_ok)
                # Grandes enseignes : offres ajoutées même hors du top-20 prix
                # (lot 16, item 9) — dédoublonnées par (boutique, prix).
                if brutes:
                    vues = {(o["shop"], o["price"]) for o in brutes}
                    q_prio = PRIO_QUERY.get(cat, lambda x: requete_ok)(it)
                    for lot_shops in (FV_SHOPS_PRIORITAIRES, FV_SHOPS_SECONDAIRES):
                        for o in offres_pour(src, cat_id, slug, q_prio,
                                             fv_shops=lot_shops):
                            if (o["shop"], o["price"]) not in vues:
                                vues.add((o["shop"], o["price"]))
                                brutes.append(o)
            except Exception as e:  # noqa: BLE001 — une requête ratée ne tue pas le run
                print(f"  ! {cat}/{it['id']}: {e}", file=sys.stderr)
                continue
            offres = sorted(
                [o for o in brutes
                 if correspond(o["product"], jetons)
                 and suffixe_ok(cat, it["name"], o["product"])
                 and not produit_suspect(o["product"])],
                key=lambda o: o["price"])
            offres = sans_aberrantes(offres)
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
                # Cap PRÉSERVANT une offre par boutique (les offres forcées des
                # grandes enseignes ne tombent plus hors du top-15) — lot 28.
                "prices": [{"shop": o["shop"], "price": o["price"], "currency": "EUR",
                            "url": o["url"], "inStock": True, "lastSeen": quand,
                            "product": o["product"],
                            "image": o["image"] or None}
                           for o in plafonner_offres(offres)],
                "image": image_rel,
                "lastUpdated": quand,
            })
            ok += 1
            print(f"  + {cat}/{it['id']}: {len(offres)} offres, min {offres[0]['price']} €")

        chemin = os.path.join(args.data_repo, SORTIES.get(cat, f"{cat}.json"))
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(sortie, f, ensure_ascii=False, indent=1)
        print(f"{cat}: {ok} composants avec offres, {vide} sans correspondance "
              f"-> {os.path.basename(chemin)}")


if __name__ == "__main__":
    principal()
