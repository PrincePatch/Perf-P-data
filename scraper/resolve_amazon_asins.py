# -*- coding: utf-8 -*-
"""Résout un ASIN Amazon RÉEL par composant et par marketplace, sans PA-API.

Pourquoi
--------
Les liens de recherche (`/s?k=…`) posent bien le cookie d'affiliation, mais ils
déposent l'utilisateur sur une page de résultats : mauvaise expérience, et le
produit trouvé n'est pas forcément celui de la configuration. Il faut un lien
`/dp/<ASIN>` par composant.

La PA-API n'étant pas encore ouverte, l'ASIN est obtenu en deux temps, via le
rendu **mobile** d'Amazon (`sources/amazon_mobile.py`) : le rendu de bureau
répond 202 avec une page vide aux clients automatisés, le rendu mobile répond
normalement.

1. **Découverte** — la recherche mobile donne les ASIN des résultats, dans
   l'ordre de pertinence. Leurs libellés ne sont PAS retenus : la page imbrique
   carrousels sponsorisés et vrais résultats, l'association titre↔ASIN n'y est
   pas fiable.
2. **Validation** — la **fiche produit** de chaque candidat est ouverte, et son
   titre réel passe les mêmes garde-fous que les prix vérifiés : jetons
   obligatoires, suffixe de modèle (« 4070 » ≠ « 4070 Ti »), rejet des
   accessoires et des ensembles montés. Un ASIN inexistant renvoie un 404 franc.
   Le premier candidat qui passe est retenu ; s'il n'y en a pas, le composant
   n'aura pas de lien — mieux vaut aucun lien qu'un mauvais produit.

La fiche est vérifiée sur la marketplace de découverte, puis sur `amazon.com`
pour que les acheteurs américains aient eux aussi un lien natif. Les autres
marketplaces sont couvertes par `gen_amazon_links.py` via le lien d'une
marketplace validée, **OneLink** redirigeant l'acheteur vers son magasin local.

Sortie : `amazon_asins.json` — même format que celui qu'écrira `enrich_amazon`
quand la PA-API sera ouverte, donc parfaitement interopérable :

    {"gpus": {"rtx5090": {"amazon.fr": "B0DYDY8KSC", "amazon.pl": null}},
     "_titres": {"gpus": {"rtx5090": "Nvidia GeForce RTX 5090 Founders Edition"}}}

Le fichier est écrit au fil de l'eau : l'exécution est **reprenable**.

Usage :
    python scraper/resolve_amazon_asins.py --asins <clone>/amazon_asins.json
        [--items-dir catalog] [--only gpus,cpus] [--limit N] [--recheck]
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import (  # noqa: E402
    CATEGORIES, _norm, _norm_espace, correspond, produit_suspect, suffixe_ok)
from gen_amazon_links import FICHIERS, TAGS, requete  # noqa: E402
from sources.amazon_mobile import AmazonMobileSource  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Marketplace interrogée en premier par le moteur de recherche. Le catalogue
# européen est le plus proche du nôtre (composants vendus en France) ; les
# États-Unis servent de second filet.
ORDRE_DECOUVERTE = ["amazon.fr", "amazon.de", "amazon.com"]

# Jetons de correspondance par catégorie, réutilisés depuis enrich_catalog
# (mêmes règles que pour les prix vérifiés).
JETONS = {cat: CATEGORIES[cat][4] for cat in CATEGORIES}

_ASIN_URL = re.compile(r"amazon\.[a-z.]+/(?:[^?#]*/)?(?:dp|gp/product)/(B[A-Z0-9]{9})")

# Moteur de secours pour la DÉCOUVERTE. Amazon limite ses pages de RECHERCHE
# bien plus durement que ses fiches produit : après quelques dizaines de
# requêtes, la recherche mobile renvoie une page vide alors que les fiches
# continuent de répondre. Un moteur généraliste prend alors le relais — la
# validation, elle, reste faite sur la fiche Amazon.
MOTEURS = ["bing", "auto"]


def _interroger_moteur(q, domaine, moteur):
    from ddgs import DDGS
    out = []
    with DDGS() as d:
        for r in d.text(f"site:{domaine} {q}", max_results=10, backend=moteur):
            m = _ASIN_URL.search(r.get("href") or "")
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


def candidats_moteur(q, pause, domaine="amazon.fr", delai=20):
    """ASIN plausibles pour [q], vus par un moteur de recherche généraliste.

    [domaine] restreint la recherche à une marketplace : un composant absent du
    catalogue français existe souvent sur `.de` ou `.com`, et l'ASIN trouvé là
    vaut ensuite pour toutes les marketplaces où la fiche existe.

    L'appel est **borné dans le temps** : la bibliothèque de recherche peut
    rester bloquée plusieurs minutes sur une requête, et une exécution entière
    s'y perdait sans produire la moindre ligne.
    """
    for moteur in MOTEURS:
        out = []
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                out = ex.submit(_interroger_moteur, q, domaine, moteur).result(delai)
        except Exception:
            continue  # moteur muet, saturé, ou trop lent
        finally:
            time.sleep(pause)
        if out:
            return out
    return []


def requete_simple(cat, it):
    """Variante ALLÉGÉE de la requête, essayée quand la précise ne rend rien.

    Le moteur d'Amazon est un ET strict : « Corsair DDR4-3200 32GB CL16 » ne
    ramène rien si le vendeur écrit « CL16-20-20-38 » ou omet la latence. On
    retire donc les qualificatifs les plus fragiles — la validation sur la
    fiche produit, elle, reste aussi stricte.
    """
    marque = (it.get("brand") or "").strip()
    if cat == "rams":
        return f"{marque} {it.get('kind', '')}-{it.get('mhz', '')} " \
               f"{it.get('sizeGb', '')}GB".strip()
    nom = re.sub(r"\s*\([^)]*\)", "", it.get("name", "")).strip()
    # Ponctuation de marque qui casse la recherche : « be quiet! », « G.Skill ».
    nom = nom.replace("!", " ").replace(".", " ")
    if marque and marque.lower() not in nom.lower():
        nom = f"{marque} {nom}"
    # Trois premiers mots : marque + gamme + modèle, sans les mentions de
    # finition (« WiFi », « Black », « Edition »…) qui varient d'un vendeur à
    # l'autre.
    mots = nom.split()
    return " ".join(mots[:3]) if len(mots) > 3 else nom


def _jetons(cat, it):
    build = JETONS.get(cat)
    if build is None:  # catégories hors pipeline de prix (ventilateurs)
        from enrich_catalog import t_nom
        build = t_nom
    return build(it)


# Bundles propres aux titres AMAZON, que le filtre partagé ne couvre pas : les
# revendeurs allemands et français vendent des « kits d'évolution » (processeur
# + carte mère + mémoire) dont le titre commence par le nom du processeur.
# Comparaison faite sur le titre SANS accents (« kit d'évolution » → « kit d
# evolution »), d'où les variantes ci-dessous.
_BUNDLE_AMZ = (
    "kit d evolution", "kit evolution", "aufrust", "aufruest", "aufrustkit",
    "memory pc", "komplett", "prebuilt", "pc kit", "kit pc", "pc set",
    "cyberpowerpc", "ibuypower", "skytech", "megaport", "dubaro",
    "pc de bureau", "tour gaming", "gaming desktop", "workstation pc",
    "unite centrale", "pc complet", "pc monte",
)

# Un vrai composant ne mentionne pas les caractéristiques d'une AUTRE pièce
# maîtresse : un processeur vendu seul n'annonce ni « RTX » ni « 32 GB DDR5 »,
# une carte graphique seule n'annonce pas « Ryzen ». C'est la signature d'un
# ensemble monté.
_HORS_CAT = {
    # Un processeur vendu seul n'annonce ni carte graphique, ni barrettes, ni
    # CHIPSET de carte mère (B650, X670E, Z790…) : c'est la signature d'un
    # ensemble « processeur + carte mère (+ mémoire) ».
    "cpus": re.compile(
        r"\b(rtx|geforce|radeon rx)\b|\b\d{2,3}\s?g[bo]\s?ddr"
        r"|\b(b[4-8]\d0|x[5-8]\d0e?|z[6-8]90|h[5-8]10)\b"
        r"|\bmotherboard\b|carte m[eè]re", re.I),
    "gpus": re.compile(r"\b(ryzen|core\s?i[3579]|intel core|ddr5|ddr4)\b", re.I),
}


# « Desktop Processor », « Desktop Graphics Card » : formulations STANDARD des
# fabricants pour un composant vendu seul, par opposition à sa version portable.
# Le filtre partagé y voyait un « desktop », donc un PC monté, et rejetait la
# moitié des processeurs Intel et beaucoup de cartes graphiques.
_DESKTOP_OK = re.compile(
    r"\bdesktop[- ](processor|prozessor|cpu|graphics?|gpu|grafikkarte|"
    r"videocard|video card)\w*", re.I)


def _neutraliser(titre):
    """Retire les tournures « desktop … » qui décrivent un composant seul."""
    return _DESKTOP_OK.sub(" ", titre)


def suspect_amazon(cat, titre):
    """Bundle ou machine montée déguisé en composant, dans un titre Amazon."""
    plat = _norm_espace(titre)
    if any(m in plat for m in _BUNDLE_AMZ):
        return True
    motif = _HORS_CAT.get(cat)
    return bool(motif and motif.search(titre))


# Catégories dont le modèle est un NOM LIBRE (« B650 Tomahawk WiFi », « Pure
# Rock 2 », « H5 Flow ») : chaque mot compte, et en omettre un suffisait à
# confondre « MSI B650 Tomahawk WiFi » avec « MSI Pro B650M-A WiFi ».
# Les GPU et CPU, eux, portent un numéro de modèle déjà protégé par
# `suffixe_ok`, et les vendeurs omettent souvent le nom de famille
# (« ASUS ROG Astral RTX 5080 » sans « GeForce ») : la tolérance du filtre
# partagé y reste indispensable.
_STRICT = ("rams", "ssds", "mobos", "psus", "cases", "coolers", "fans")


def correspond_strict(titre, jetons):
    """Comme `correspond`, mais SANS tolérance : chaque groupe doit être là."""
    n = _norm(titre)
    return all(any(j in n for j in groupe) for groupe in jetons)


_CAPACITE = re.compile(r"(\d{1,4})\s?(?:g[bo]|to|tb)\b", re.I)


def capacite_coherente(nom_app, titre):
    """Rejette un produit de capacité SUPÉRIEURE à celle du composant.

    « 16 Go DDR4-3200 CL16 » matchait « CORSAIR Vengeance 32GB (2x16GB) » : le
    jeton « 16gb » figure bien dans le titre… au titre des deux barrettes. On
    compare donc la plus GRANDE capacité annoncée, qui est le total du kit.
    """
    voulu = _CAPACITE.search(nom_app)
    if not voulu:
        return True
    def _go(txt, val):
        return int(val) * 1000 if re.search(rf"{val}\s?(?:to|tb)\b", txt, re.I) else int(val)
    cible = _go(nom_app, voulu.group(1))
    trouves = [_go(titre, m.group(1)) for m in _CAPACITE.finditer(titre)]
    return not trouves or max(trouves) <= cible


_NUM = re.compile(r"\d{3,5}")

# UNITÉS collées au nombre : « 3200 » et « 3200MHz » désignent la même
# fréquence, « 850W » la même puissance. Les confondre avec une lettre de
# modèle rejetait des produits parfaitement corrects.
_UNITES = {
    "", "mhz", "mt", "mts", "hz", "khz", "ghz", "w", "watt", "watts",
    "gb", "go", "tb", "to", "mb", "mo", "mm", "cm", "rpm", "v", "mbps",
    "bit", "bits", "pin", "nm", "x", "cl", "gbps",
}


def _lettres_modele(suffixe):
    """Suffixe débarrassé de son unité : ne reste que la lettre de modèle."""
    return "" if suffixe in _UNITES else suffixe


def suffixe_generique_ok(nom_app, titre):
    """« B550 » ne doit pas matcher « B550M », ni « 850 » « 850X ».

    `suffixe_ok` ne connaît que les suffixes des GPU et CPU. Ailleurs (cartes
    mères, alimentations, boîtiers, refroidissements), on vérifie que le numéro
    de modèle est suivi des mêmes LETTRES DE MODÈLE des deux côtés — les unités
    de mesure étant neutralisées.

    NB : le numéro est cherché sans exiger de frontière de mot À GAUCHE. Avec
    `\\b`, « B550 » n'était jamais trouvé (aucune frontière entre « b » et
    « 5 ») et le contrôle ne s'appliquait pas du tout — « Gigabyte B550 Aorus
    Elite » se laissait résoudre en « GIGABYTE B550**M** AORUS Elite ».
    """
    app = _norm_espace(nom_app)
    tit = _norm_espace(titre)
    for num in set(_NUM.findall(nom_app)):
        m = re.search(rf"(?<![0-9]){num}([a-z]*)", app)
        attendu = _lettres_modele(m.group(1) if m else "")
        trouves = {_lettres_modele(t) for t in re.findall(rf"(?<![0-9]){num}([a-z]*)", tit)}
        if trouves and attendu not in trouves:
            return False
    return True


# Jetons qu'un titre Amazon écrit de trop de façons pour être exigés au premier
# essai. La LATENCE des barrettes en est le cas typique : « CL30 », « C30 »,
# « CL30-36-36-96 », ou simplement absente du titre alors que la fiche la
# précise plus bas. L'exiger faisait échouer les trois quarts de la mémoire.
_JETON_SOUPLE = re.compile(r"^cl\d{1,2}$")


def _sans_jetons_souples(jetons):
    return [g for g in jetons if not all(_JETON_SOUPLE.match(j) for j in g)]


def valide(cat, nom_app, titre, jetons, souple=False):
    """Le titre RÉEL de la fiche correspond-il au composant ?

    [souple] autorise l'absence des jetons trop variables (la latence mémoire).
    Type, fréquence et capacité, eux, restent obligatoires : on ne peut pas se
    tromper de barrette, seulement de timings.
    """
    if souple:
        jetons = _sans_jetons_souples(jetons)
    titre = unescape(titre or "")
    if len(titre) < 8:
        return False
    sain = _neutraliser(titre)
    if produit_suspect(sain, cat) or suspect_amazon(cat, sain):
        return False
    if not suffixe_ok(cat, nom_app, titre):
        return False
    if cat in ("rams", "ssds") and not capacite_coherente(nom_app, titre):
        return False
    if cat not in ("gpus", "cpus") and not suffixe_generique_ok(nom_app, titre):
        return False
    return (correspond_strict if cat in _STRICT else correspond)(titre, jetons)


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asins", required=True)
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--markets", default=",".join(TAGS))
    ap.add_argument("--pause", type=float, default=1.0,
                    help="délai minimal entre deux requêtes Amazon (politesse)")
    ap.add_argument("--candidats", type=int, default=5,
                    help="fiches consultées au plus par marketplace")
    ap.add_argument("--recheck", action="store_true",
                    help="retenter les composants marqués introuvables")
    ap.add_argument("--amazon-dabord", action="store_true",
                    help="interroger le moteur d'Amazon AVANT le moteur "
                         "généraliste : bien plus pertinent sur les références "
                         "de niche, mais Amazon bride ses recherches — à "
                         "réserver aux rattrapages de quelques dizaines "
                         "d'articles")
    ap.add_argument("--completer", action="store_true",
                    help="compléter les marketplaces manquantes des composants "
                         "DÉJÀ résolus (rend les liens natifs au lieu de "
                         "passer par OneLink) — aucune recherche, uniquement "
                         "la lecture des fiches")
    args = ap.parse_args()

    marches = [m.strip() for m in args.markets.split(",") if m.strip() in TAGS]
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    # Une session par marketplace : cookies, langue et cadence propres.
    sources = {m: AmazonMobileSource(m, rate_limit=args.pause)
               for m in dict.fromkeys(ORDRE_DECOUVERTE + marches)}

    try:
        with open(args.asins, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    titres = cache.setdefault("_titres", {})

    n_ok = n_ko = n_saute = 0

    def ecrire():
        with open(args.asins, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)

    for cat, fichier in FICHIERS.items():
        if seules and cat not in seules:
            continue
        chemin = os.path.join(args.items_dir, f"{fichier}.json")
        if not os.path.exists(chemin):
            continue
        with open(chemin, encoding="utf-8") as f:
            items = json.load(f)
        if args.limit:
            items = items[: args.limit]

        cat_cache = cache.setdefault(cat, {})
        cat_titres = titres.setdefault(cat, {})
        print(f"=== {cat} ({len(items)} composants)")

        for it in items:
            cid, nom = it["id"], it.get("name", "")
            connus = cat_cache.get(cid)

            # --- mode COMPLÉTION : on ne cherche rien de neuf, on vérifie
            # seulement si l'ASIN déjà retenu existe AUSSI sur les marketplaces
            # restées vides. Chaque succès transforme un rebond OneLink en lien
            # natif, avec le Partner Tag du pays.
            if args.completer:
                asin = next((a for a in (connus or {}).values() if a), None)
                # `None` = pas encore vérifiée · `False` = vérifiée, la fiche
                # n'existe pas là-bas. Sans cette distinction, une marketplace
                # sans le produit était re-testée à CHAQUE passe et les vagues
                # tournaient en rond sur les mêmes composants.
                manquantes = [m for m in marches if (connus or {}).get(m) is None]
                if not asin or not manquantes:
                    n_saute += 1
                    continue
                jetons = _jetons(cat, it)
                gagnees = []
                for m in manquantes:
                    # Un seul essai : une marketplace qui bride reste « à
                    # vérifier » et sera reprise à la passe suivante. Réessayer
                    # sur place coûtait 5 s par marketplace, soit une trentaine
                    # de secondes par composant.
                    etat, t = sources[m].fiche(asin, essais=1)
                    if etat == "ok" and valide(cat, nom, t, jetons, souple=True):
                        connus[m] = asin
                        gagnees.append(m)
                    elif etat != "bloque":
                        connus[m] = False  # absence CONSTATÉE, ne plus retester
                if gagnees:
                    n_ok += 1
                    print(f"  + {cat}/{cid}: +{len(gagnees)} natives "
                          f"({', '.join(m.split('.', 1)[1] for m in gagnees)})",
                          flush=True)
                else:
                    n_ko += 1
                ecrire()
                continue

            # Déjà résolu (au moins une marketplace valide) → on ne recommence
            # pas : l'exécution est reprenable et le quota DDG précieux.
            if connus and any(connus.values()) and not args.recheck:
                n_saute += 1
                continue
            if connus is not None and not any(connus.values()) and not args.recheck:
                n_saute += 1
                continue

            q = requete(cat, it)
            jetons = _jetons(cat, it)

            # --- 1. découverte : les résultats de la recherche Amazon --------
            # On ne retient QUE les identifiants : sur la page mobile, les
            # carrousels sponsorisés s'intercalent entre les vrais résultats et
            # l'association titre↔ASIN n'est pas fiable. C'est la fiche produit,
            # à l'étape suivante, qui départage.
            retenu = titre_ref = None
            marche_src = None
            replis = None  # meilleur candidat « souple », faute de mieux
            resultats = {}

            # Requête principale, puis une variante ALLÉGÉE : la requête
            # complète (« Corsair DDR4-3200 32GB CL16 ») est parfois trop
            # précise pour le moteur d'Amazon, qui ne renvoie alors rien.
            simple = requete_simple(cat, it)
            requetes = [q] + ([simple] if simple and simple != q else [])
            # Sources de candidats, dans l'ordre : la recherche Amazon de chaque
            # marketplace, puis — si elle est muette (page d'attente) — un moteur
            # généraliste. La preuve reste toujours la fiche produit.
            # Le moteur généraliste passe EN PREMIER : la recherche Amazon est
            # la partie la plus vite bridée du site, et attendre son verdict
            # avant de basculer coûtait ~40 s par composant.
            # Trois pistes AU PLUS. Les épuiser toutes coûtait ~50 s par
            # composant introuvable, pour un gain marginal : le catalogue
            # entier y passait la nuit.
            plans = [(None, requetes[0], "amazon.fr")]
            plans += [(None, r, "amazon.fr") for r in requetes[1:]]
            plans += [(ORDRE_DECOUVERTE[0], requetes[0], None)]
            if args.amazon_dabord:
                # Sur une référence de niche, le moteur généraliste renvoie des
                # produits sans rapport (un écran pour « DeepCool AK620 »)
                # tandis que le moteur d'Amazon, lui, connaît son catalogue.
                plans = ([(m, r, None) for r in requetes
                          for m in ORDRE_DECOUVERTE[:2]] + plans)
            # Catalogues voisins, uniquement si tout ce qui précède est muet :
            # une référence absente du catalogue français est souvent vendue en
            # Allemagne ou aux États-Unis, et l'ASIN trouvé là vaut ensuite
            # partout où la fiche existe.
            plans += [(None, requetes[0], d) for d in ("amazon.de", "amazon.com")]
            # Catalogues voisins : beaucoup de composants absents d'amazon.fr
            # sont vendus sur .de ou .com, et l'ASIN trouvé là vaut ensuite pour
            # toutes les marketplaces où la fiche existe (mode --completer).
            plans += [(None, requetes[0], d) for d in ("amazon.de", "amazon.com")]

            for marche, req, dom in plans:
                if marche is None:
                    candidats = candidats_moteur(req, args.pause, dom)
                    # La fiche est lue sur la marketplace d'où vient le lien.
                    marche = dom
                    src = sources[marche]
                else:
                    src = sources[marche]
                    candidats = src.asins(req, maxi=args.candidats)
                for asin in candidats[: args.candidats]:
                    # Un seul essai par candidat : ils sont nombreux et la
                    # plupart seront écartés. Réessayer une page lente coûtait
                    # jusqu'à 55 s par candidat, soit des dizaines de minutes
                    # sur un composant finalement introuvable.
                    etat, titre = src.fiche(asin, essais=1)
                    if etat != "ok":
                        continue  # 404 ou page d'attente : candidat suivant
                    if valide(cat, nom, titre, jetons):
                        retenu, titre_ref, marche_src = asin, titre, marche
                        resultats = {marche: asin}
                        break
                    # Second regard, plus souple sur les seuls jetons que les
                    # vendeurs écrivent de dix façons (la latence mémoire) —
                    # gardé de côté et n'est retenu que si rien de mieux ne
                    # se présente.
                    if replis is None and valide(cat, nom, titre, jetons, souple=True):
                        replis = (marche, asin, titre)
                if retenu:
                    break

            # Aucune correspondance exacte : on se rabat sur le candidat
            # « souple » si l'on en a croisé un.
            if not retenu and replis is not None:
                marche_src, retenu, titre_ref = replis
                resultats = {marche_src: retenu}

            # La marketplace américaine sert aux acheteurs US/AU : si la fiche
            # y existe aussi, ils obtiennent un lien natif au lieu d'un rebond.
            if retenu and marche_src != "amazon.com" and "amazon.com" in marches:
                etat_us, titre_us = sources["amazon.com"].fiche(retenu)
                if etat_us == "ok" and valide(cat, nom, titre_us, jetons, souple=True):
                    resultats["amazon.com"] = retenu

            if not retenu:
                cat_cache[cid] = {m: None for m in marches}
                n_ko += 1
                print(f"  - {cat}/{cid}: fiche non concordante ({nom})", flush=True)
            else:
                cat_cache[cid] = {m: resultats.get(m) for m in marches}
                cat_titres[cid] = titre_ref
                n_ok += 1
                ok = [m for m, a in cat_cache[cid].items() if a]
                print(f"  + {cat}/{cid}: {retenu} sur {len(ok)} marketplaces — "
                      f"{(titre_ref or '')[:70]}", flush=True)
            ecrire()

    ecrire()
    print(f"\nASIN résolus: {n_ok} · sans ASIN: {n_ko} · déjà connus: {n_saute}")
    return 0


if __name__ == "__main__":
    sys.exit(principal())

