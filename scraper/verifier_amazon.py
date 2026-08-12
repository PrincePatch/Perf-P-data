# -*- coding: utf-8 -*-
"""Vérifie CHAQUE lien d'affiliation Amazon et relève son PRIX, marketplace par
marketplace (lot 34).

Un lien publié n'est utile que s'il mène toujours au bon produit et qu'on sait
à quel prix. Ce script fait les deux en une passe, pour tous les composants et
toutes les marketplaces où une fiche a été validée :

1. **le lien vit-il encore ?** — un ASIN retiré du catalogue renvoie un 404, et
   une fiche dont le titre ne correspond plus au composant (produit remplacé,
   ASIN recyclé) est tout aussi inutilisable. Dans les deux cas la marketplace
   est effacée du cache d'ASIN : `gen_amazon_links.py` cessera de la publier et
   `resolve_amazon_asins.py` lui cherchera un remplaçant à la passe suivante ;
2. **quel est le prix ?** — lu dans le bloc panier de la fiche, avec sa devise
   réelle (une fiche `amazon.co.uk` peut afficher des euros) ;
3. **est-il achetable ?** — « Actuellement indisponible », « cannot be shipped
   to your selected delivery location »… sont des ruptures : le prix est alors
   absent ou trompeur, et l'offre est publiée `inStock: false`.

Les prix relevés sont écrits dans les fichiers id-clés du dépôt de données
(`gpus_app.json`, `cpus_app.json`, `rams.json`…) au même format que les autres
sources, avec l'URL **affiliée** : l'application les affiche donc comme des
« prix vérifiés », et le récapitulatif ouvre le lien qui rapporte.

Usage :
    python scraper/verifier_amazon.py --data-repo <clone> [--items-dir catalog]
        [--only gpus,cpus] [--limit N] [--pause 1.0]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import SORTIES  # noqa: E402
from gen_amazon_links import FICHIERS, TAGS, lien  # noqa: E402
from resolve_amazon_asins import _jetons, valide  # noqa: E402
from sources.amazon_mobile import AmazonMobileSource  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Devise par défaut d'une marketplace, quand la chaîne de prix ne porte pas de
# symbole exploitable.
DEVISES = {
    "amazon.fr": "EUR", "amazon.de": "EUR", "amazon.es": "EUR",
    "amazon.it": "EUR", "amazon.co.uk": "GBP", "amazon.com": "USD",
    "amazon.com.au": "AUD",
}

# Blocs porteurs du prix du PANIER, dans l'ordre de préférence. Les prix qui
# traînent ailleurs dans la page appartiennent aux carrousels « articles
# similaires » — les prendre reviendrait à afficher le prix d'un autre produit.
BLOCS_PRIX = ("corePrice_feature_div", "corePrice_mobile_feature_div",
              "corePriceDisplay_mobile_feature_div", "price_feature_div",
              "buybox")

# Formulations de RUPTURE ou d'indisponibilité, par marketplace. Cherchées dans
# le seul bloc `availability`, jamais dans la page entière : les mêmes mots
# apparaissent dans les suggestions et feraient passer tout le catalogue pour
# indisponible.
RUPTURE = (
    "actuellement indisponible", "currently unavailable", "temporairement en rupture",
    "derzeit nicht verfügbar", "nicht auf lager", "no disponible", "no está disponible",
    "attualmente non disponibile", "non disponibile", "out of stock",
    "cannot be shipped", "ne peut pas être expédié", "temporarily out of stock",
    "actualmente no disponible", "momentaneamente non disponibile",
)

_AVAIL = re.compile(r'id="availability"[^>]*>(.{0,700}?)</div>', re.S | re.I)
_OFFSCREEN = re.compile(r'class="a-offscreen">([^<]{2,40})</span>')


def _texte(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _recent(entree, heures):
    """Vrai si les offres Amazon de ce composant datent de moins de [heures]."""
    if not entree or heures <= 0:
        return False
    vus = [p.get("lastSeen") for p in entree.get("prices", [])
           if "amazon." in (p.get("shop") or "").lower() and p.get("lastSeen")]
    if not vus:
        return False
    try:
        dernier = max(datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc) for v in vus)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dernier).total_seconds() < heures * 3600


def disponibilite(html):
    """(disponible, phrase) d'après le bloc `availability` de la fiche."""
    for m in _AVAIL.finditer(html):
        t = _texte(m.group(1))
        if not t:
            continue  # premier bloc = conteneur vide
        bas = t.lower()
        return (not any(r in bas for r in RUPTURE)), t[:120]
    return True, ""  # pas de bloc : produit vendu et expédié normalement


def _nombre(brut):
    """« 1 499,99 » / « 1,499.99 » / « 224,00 » → float.

    Le dernier séparateur rencontré est le séparateur DÉCIMAL : c'est la seule
    règle qui marche à la fois pour les marketplaces européennes et anglo-
    saxonnes sans avoir à deviner la locale.
    """
    s = re.sub(r"[^\d,.]", "", brut)
    if not s:
        return None
    dernier = max(s.rfind(","), s.rfind("."))
    if dernier >= 0 and len(s) - dernier - 1 in (1, 2):
        entier = re.sub(r"[,.]", "", s[:dernier])
        return float(f"{entier}.{s[dernier + 1:]}")
    return float(re.sub(r"[,.]", "", s))


def devise(brut, domaine):
    """Devise réellement affichée sur la fiche (elle ne suit pas le domaine :
    une fiche britannique peut afficher des euros pour un produit importé)."""
    b = brut.upper()
    if "A$" in b or "AUD" in b:
        return "AUD"
    if "C$" in b or "CAD" in b:
        return "CAD"
    if "€" in b or "EUR" in b:
        return "EUR"
    if "£" in b or "GBP" in b:
        return "GBP"
    if "US$" in b or "USD" in b or "$" in b:
        return "USD"
    return DEVISES.get(domaine, "EUR")


def prix(html, domaine):
    """(montant, devise, chaîne brute) du bloc panier, ou (None, None, '')."""
    for bloc in BLOCS_PRIX:
        i = html.find(f'id="{bloc}"')
        if i < 0:
            continue
        m = _OFFSCREEN.search(html, i, i + 20000)
        if not m:
            continue
        brut = m.group(1).replace(" ", " ").replace("\xa0", " ")
        v = _nombre(brut)
        if v and v > 0:
            return v, devise(brut, domaine), brut.strip()
    return None, None, ""


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--items-dir", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--markets", default=",".join(TAGS))
    ap.add_argument("--frais-h", type=float, default=12.0,
                    help="ne pas revérifier un composant dont les offres Amazon "
                         "datent de moins de N heures (reprise après coupure)")
    args = ap.parse_args()

    items_dir = args.items_dir or os.path.join(args.data_repo, "catalog")
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    marches = [m.strip() for m in args.markets.split(",") if m.strip() in TAGS]
    quand = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    chemin_asins = os.path.join(args.data_repo, "amazon_asins.json")
    with open(chemin_asins, encoding="utf-8") as f:
        cache = json.load(f)

    sources = {m: AmazonMobileSource(m, rate_limit=args.pause) for m in marches}
    n_prix = n_rupture = n_mort = n_sans = n_devise = 0

    for cat, fichier in FICHIERS.items():
        if seules and cat not in seules:
            continue
        chemin_items = os.path.join(items_dir, f"{fichier}.json")
        if cat not in cache or not os.path.exists(chemin_items):
            continue
        with open(chemin_items, encoding="utf-8") as f:
            items = {e["id"]: e for e in json.load(f)}

        sortie = os.path.join(args.data_repo, SORTIES.get(cat, f"{cat}.json"))
        try:
            with open(sortie, encoding="utf-8") as f:
                existant = {e["id"]: e for e in json.load(f)}
        except (OSError, ValueError):
            existant = {}

        traites = 0
        for cid, par_marche in cache[cat].items():
            it = items.get(cid)
            if it is None:
                continue
            if args.limit and traites >= args.limit:
                break
            actifs = {m: a for m, a in par_marche.items() if a and m in marches}
            if not actifs:
                continue
            # Reprise : un composant dont les offres Amazon viennent d'être
            # relevées est sauté. Sans cela, chaque exécution repartait du début
            # du catalogue et revérifiait éternellement les mêmes composants.
            if _recent(existant.get(cid), args.frais_h):
                continue
            traites += 1
            jetons = _jetons(cat, it)
            offres = []

            for m, asin in actifs.items():
                etat, titre, html = sources[m].fiche_complete(asin, essais=1)
                if etat == "bloque":
                    continue  # page d'attente : ni preuve de vie, ni preuve de mort
                if etat == "absent" or not valide(cat, it["name"], titre, jetons,
                                                  souple=True):
                    # Fiche disparue ou produit remplacé : on cesse de publier
                    # ce lien, et la prochaine résolution cherchera mieux.
                    par_marche[m] = None
                    n_mort += 1
                    print(f"  x {cat}/{cid} {m}: lien mort ({asin})", flush=True)
                    continue
                dispo, phrase = disponibilite(html)
                montant, dev, brut = prix(html, m)
                if montant is None:
                    n_sans += 1
                    if not dispo:
                        n_rupture += 1
                    continue
                if dev != DEVISES.get(m):
                    # Amazon convertit pour le visiteur : depuis l'Europe,
                    # `amazon.com` affiche des euros. Ce montant n'est PAS le
                    # prix que verra un acheteur américain (frais d'import
                    # inclus, offre différente). On garde le lien, qui reste
                    # valide, mais on ne publie pas un prix qui serait faux
                    # pour l'utilisateur de cette marketplace.
                    n_devise += 1
                    continue
                if dispo:
                    n_prix += 1
                else:
                    n_rupture += 1
                offres.append({
                    "shop": m, "price": round(montant, 2), "currency": dev,
                    "url": lien(m, asin), "inStock": bool(dispo),
                    "lastSeen": quand, "product": titre, "image": None,
                })
                print(f"  {'+' if dispo else '~'} {cat}/{cid} {m}: {brut}"
                      f"{'' if dispo else ' (rupture)'}", flush=True)

            if not offres:
                continue
            e = existant.get(cid)
            if e is None:
                e = {"id": cid, "name": it["name"], "priceMin": None,
                     "prices": [], "image": None, "lastUpdated": quand}
                existant[cid] = e
            # Les offres Amazon relevées ici remplacent les précédentes : elles
            # portent le lien affilié et un prix du jour.
            e["prices"] = [p for p in e.get("prices", [])
                           if "amazon." not in (p.get("shop") or "").lower()]
            e["prices"].extend(offres)
            e["lastUpdated"] = quand

        with open(sortie, "w", encoding="utf-8") as f:
            json.dump(list(existant.values()), f, ensure_ascii=False, indent=1)
        with open(chemin_asins, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"\nprix relevés {n_prix} · ruptures {n_rupture} · sans prix affiché "
          f"{n_sans} · devise convertie (écarté) {n_devise} "
          f"· liens morts retirés {n_mort}")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
