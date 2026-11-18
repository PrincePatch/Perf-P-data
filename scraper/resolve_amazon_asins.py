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
_BUNDLE_AMZ_RAM_SSD = tuple(m for m in _BUNDLE_AMZ
                            if m not in ("memory pc", "pc de bureau"))

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


# « be quiet! System Power 10 » est un NOM de gamme d'alimentations : le mot
# « system » du filtre partagé y voyait un PC complet, et ces alimentations ne
# pouvaient jamais être validées.
_GAMMES_OK = re.compile(r"\bsystem power\b", re.I)


def _neutraliser(titre):
    """Retire les tournures « desktop … » qui décrivent un composant seul, et
    les noms de gamme qui ressemblent à un mot de bundle."""
    return _GAMMES_OK.sub(" ", _DESKTOP_OK.sub(" ", titre))


def suspect_amazon(cat, titre):
    """Bundle ou machine montée déguisé en composant, dans un titre Amazon."""
    plat = _norm_espace(titre)
    # Mémoire et SSD : « Mémoire pour PC de bureau », « Desktop Memory PC »
    # disent la machine cible, pas un PC monté (faux rejets du 18/11/2026).
    mots = _BUNDLE_AMZ_RAM_SSD if cat in ("rams", "ssds") else _BUNDLE_AMZ
    if any(m in plat for m in mots):
        return True
    # … mais « PC de bureau » non précédé de « pour » reste un PC monté
    # (« PC de bureau gaming Ryzen 7 avec 32 Go DDR5 »).
    if (cat in ("rams", "ssds") and "pc de bureau" in plat
            and not re.search(r"\b(?:pour|for|fur|per|para)\s+(?:(?:un|le|les|la)\s+)?pc de bureau", plat)):
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
    # Un kit n'annonce parfois QUE sa composition : « Ripjaws M5 2x16gb » est
    # un kit de 32 Go, alors que la seule capacité lisible est « 16gb ».
    trouves += [int(n) * int(v) for n, v in _KIT.findall(titre)]
    return not trouves or max(trouves) <= cible


# Composition d'un kit mémoire : « 2x16GB », « (2 x 16 Go) », « 2X 16 Go ».
_KIT = re.compile(r"\b([2-8])\s?x\s?(\d{1,3})\s?g[bo]\b", re.I)


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


# ===========================================================================
# IDENTITÉ EXACTE du produit
# ===========================================================================
# Les garde-fous précédents (jetons, suffixe, mots de bundle et d'accessoire)
# laissaient passer des produits VOISINS, réellement retenus à la découverte
# puis relevés à l'audit des titres :
#   · « Fractal Design Meshify 3 » pour le Meshify 2, « be quiet! Dark Rock 5 »
#     pour le Dark Rock 4 : un numéro d'un seul chiffre n'est pas un jeton
#     (`t_nom` ignore les mots d'une lettre) ;
#   · « GIGABYTE B550M AORUS Elite » pour la B550 Aorus Elite : le chipset
#     « B550 », répété plus loin dans le titre, satisfaisait le contrôle de
#     suffixe ;
#   · « Lian Li O11 Dynamic Evo », « MONTECH King 95 Pro », « Fractal Torrent
#     Compact » : une variante plus précise du même modèle ;
#   · « Noctua NA-HC4 … pour NH-D15S » : un accessoire qui nomme le modèle ;
#   · « Fractal Ridge, Gigabyte A520I AC Motherboard » : un lot ;
#   · « Case NZXT Compatible H1 v2 » : une contrefaçon « compatible ».
# Chaque règle ci-dessous répond à l'un de ces cas. Elles travaillent sur les
# MOTS (`_norm_espace`) et non sur le texte collé de `_norm`, où « co » se
# trouve dans « cooler » et « 8000 » dans « PC5-48000 ».

# --- Catalogue, chargé à la demande ------------------------------------------
# `valide` ne reçoit qu'un nom, mais plusieurs règles ont besoin de la FICHE
# du composant (mémoire d'une carte graphique, label 80 PLUS d'une
# alimentation, type de disque) et des AUTRES composants de sa catégorie
# (variante plus précise). `principal` y pointe le dossier `--items-dir` ; à
# défaut, le dossier `catalog/` du dépôt de données (exécution par le cron),
# puis les assets de l'application.
_DOSSIER_CATALOGUE = None
_CATALOGUES = {}


def definir_catalogue(dossier):
    """Désigne le dossier des listes de composants (`--items-dir`)."""
    global _DOSSIER_CATALOGUE
    if dossier != _DOSSIER_CATALOGUE:
        _DOSSIER_CATALOGUE = dossier
        _CATALOGUES.clear()


def _catalogue(cat):
    """Composants de [cat] ; liste vide si aucun fichier n'est lisible."""
    if cat not in _CATALOGUES:
        _CATALOGUES[cat] = []
        fichier = f"{FICHIERS.get(cat, cat)}.json"
        for dossier in (_DOSSIER_CATALOGUE, os.path.join(RACINE, "catalog"),
                        os.path.join(RACINE, "assets", "data")):
            if not dossier:
                continue
            try:
                with open(os.path.join(dossier, fichier), encoding="utf-8") as f:
                    _CATALOGUES[cat] = json.load(f)
                break
            except (OSError, ValueError):
                continue
    return _CATALOGUES[cat]


def _fiche(nom_app, catalogue):
    """Entrée du catalogue qui porte exactement ce nom (ou {})."""
    return next((it for it in catalogue if it.get("name") == nom_app), {})


# --- Outils communs ---------------------------------------------------------
_CAPACITE_NOM = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:go|to|gb|tb)\b", re.I)


def _mots(texte):
    # « ™ » se décompose en « TM » et collerait au mot : « Ryzen™ » → « ryzentm ».
    return _norm_espace((texte or "").replace("™", " ")).split()


def _nom_modele(nom_app):
    """Désignation du modèle seule : sans parenthèses ni capacité.

    « Arctic P12 (×3) » → « Arctic P12 », « Fractal Ridge (ITX) » → « Fractal
    Ridge », « Crucial T500 1 To » → « Crucial T500 ». Le lot, le format et la
    capacité sont contrôlés par des règles dédiées.
    """
    return _CAPACITE_NOM.sub(" ", re.sub(r"\([^)]*\)", " ", nom_app or ""))


# --- 1. Numéros de modèle, quelle que soit leur longueur -----------------------
# Chiffres romains de génération : « Lancool III », « Galahad II », « WiFi II ».
_ROMAINS = {"ii", "iii", "iv", "vi", "vii", "viii"}
_UNITES_NOM = _UNITES | {"tr"}  # « 7200 tr/min »


def _mot_court(mot):
    """Mot qui prolonge un numéro : « 500 FX », « 120 SE », « 12 M », « 360 RS ».
    « RGB » n'en est pas un : c'est l'éclairage, que les vendeurs placent où
    ils veulent (« F120 RGB Core » / « F120 Core RGB »)."""
    return mot.isalpha() and len(mot) <= 3 and mot not in _UNITES_NOM and mot != "rgb"


def _sans_unite(coeur, lettres):
    """Lettres collées au numéro, l'UNITÉ retirée (« 850W » → « », « 120mm »
    → « »). Le « x » n'est une unité qu'après un seul chiffre (« 2x ») : après
    « 3500 » ou « RM850 », c'est une lettre de modèle."""
    if lettres == "x":
        return "" if len(coeur) == 1 else "x"
    return "" if coeur.isdigit() and lettres in _UNITES_NOM else lettres


def _designations(nom_app):
    """(cœur, suffixe attendu, mot précédent, mot suivant) de chaque numéro.

    Le cœur va jusqu'au dernier chiffre du mot (« b550 », « d15 », « 360r2 ») ;
    le suffixe est la lettre de modèle collée (« m », « s », « dx ») ou, à
    défaut, le mot court qui suit (« Pure Base 500 FX » → « fx », « Pure
    Power 12 M » → « m »).
    """
    mots = _mots(_nom_modele(nom_app))
    out = []
    for i, mot in enumerate(mots):
        suivant = i + 1
        if mot in _ROMAINS:
            coeur, suf = mot, ""
        else:
            r = re.fullmatch(r"([a-z0-9]*\d)([a-z]*)", mot)
            if not r:
                continue
            coeur, suf = r.group(1), _sans_unite(r.group(1), r.group(2))
            if not suf and suivant < len(mots) and _mot_court(mots[suivant]):
                suf = mots[suivant]
                suivant += 1
        out.append((coeur, suf, mots[i - 1] if i else "",
                    mots[suivant] if suivant < len(mots) else ""))
    return out


def _occurrences(mots_t, coeur):
    """[(formes, précédent, suivant)] de chaque mot du titre portant [coeur].

    [formes] = suffixes plausibles de l'occurrence : lettres collées (chiffres
    éventuels compris — « H2OA5 » n'est pas « H2O »), ou mot court suivant.
    """
    out = []
    for k, mot in enumerate(mots_t):
        prec_mot = mots_t[k - 1] if k else ""
        if coeur in _ROMAINS:
            if mot == coeur:
                out.append(({""}, prec_mot, mots_t[k + 1] if k + 1 < len(mots_t) else ""))
            continue
        j = mot.find(coeur)
        while j >= 0:
            colle = mot[:j]
            reste = mot[j + len(coeur):]
            # Frontière : pas de chiffre collé de part et d'autre (« 1200 » ne
            # porte pas « 200 »), et un numéro court (« H1 », « 36 ») commence
            # le mot. Un cœur long peut être collé à sa gamme : « AssassinX120 ».
            if not (colle[-1:].isdigit() or reste[:1].isdigit()
                    or (len(coeur) <= 2 and colle)):
                lettres = re.match(r"[a-z]*", reste).group(0)
                chiffres = re.match(r"\d*", reste[len(lettres):]).group(0)
                formes = {_sans_unite(coeur, lettres) + chiffres}
                suivant = k + 1
                if not reste and suivant < len(mots_t) and _mot_court(mots_t[suivant]):
                    formes.add(mots_t[suivant])
                out.append((formes, colle or prec_mot,
                            mots_t[suivant] if suivant < len(mots_t) else ""))
            j = mot.find(coeur, j + 1)
    return out


def numeros_coherents(nom_app, titre):
    """Chaque numéro du nom se retrouve dans le titre avec la MÊME lettre.

    Généralise `suffixe_generique_ok` à toutes les longueurs de numéro :
    · un numéro de 1 ou 2 chiffres, ou un chiffre romain, est OBLIGATOIRE et
      doit suivre le même mot que dans le nom (« Meshify 2 », « Dark Rock 4 »,
      « Lancool III ») — « Dark Rock 5 … Silent Wings 4 » ne vaut pas Dark
      Rock 4 ;
    · un numéro plus long est comparé d'abord à ses occurrences ANCRÉES
      (entourées des mêmes mots que dans le nom) : le chipset « B550 » cité
      plus loin ne rachète plus « GIGABYTE B550M AORUS Elite ».
    """
    mots_t = _mots(titre)
    for coeur, attendu, prec, suiv in _designations(nom_app):
        occ = _occurrences(mots_t, coeur)
        ancrees = [f for f, p, n in occ if (prec and p == prec) or (suiv and n == suiv)]
        if coeur in _ROMAINS or (coeur.isdigit() and len(coeur) <= 2):
            if not any(attendu in f for f in ancrees):
                return False
        elif occ and not any(attendu in f for f in (ancrees or [f for f, _, _ in occ])):
            return False
    return True


# --- 2. Mots courts du nom présents comme MOTS ---------------------------------
# `correspond_strict` cherche les jetons dans le texte collé : « CO » (Arctic
# Freezer 36 CO) s'y trouvait dans « cooler », « M » (Pure Power 12 M) et « R »
# (Assassin X120 R) n'étaient même pas des jetons. Au-delà de deux lettres, le
# texte collé suffit (« OCF » s'écrit aussi « OC Formula »).
_FRAGMENTS_MARQUE = {"be", "li"}  # « be quiet! », « Lian Li »


def mots_courts_presents(nom_app, titre):
    """Chaque mot alphabétique de 1 ou 2 lettres du nom figure comme mot."""
    texte = " ".join(_mots(titre))
    for mot in _mots(_nom_modele(nom_app)):
        if mot.isalpha() and len(mot) <= 2 and mot not in _FRAGMENTS_MARQUE:
            if not re.search(rf"(?<![a-z]){mot}(?![a-z])", texte):
                return False
    return True


# --- 3. Qualificatif de variante absent du nom, collé au modèle ----------------
# « King 95 Pro », « Torrent Compact », « O11 Dynamic Evo », « 5000D Core
# Airflow », « Y70 Touch », « Z890 AORUS Master AI Top », « Nautilus 360 RS
# LCD »… sont d'autres produits. Les RÉVISIONS (V2, R2.0, 2024) restent
# acceptées : même produit, catalogue sans révision (choix de l'audit).
# Une couleur ou un éclairage différent (RGB, ARGB, White) aussi.
_VARIANTES = {
    "evo", "compact", "mini", "micro", "xl", "xxl", "elite", "pro", "plus",
    "lite", "se", "g2", "g3", "max", "touch", "core", "shift", "encore", "ai",
    "top", "co", "lcd", "d4",
}
# « Mini-ITX », « Compact ATX Mid-Tower » décrivent un FORMAT, « 80 PLUS » un
# label, « AI Overclocking » une fonction : seul « AI Top » est un modèle.
_FORMATS = {"itx", "dtx", "atx", "matx", "eatx", "tower", "tour", "mid", "midi",
            "pc", "case", "boitier", "gehause", "chassis"}


def sans_variante_voisine(nom_app, titre):
    """Faux si un qualificatif de variante absent du nom touche le modèle."""
    app = set(_mots(_nom_modele(nom_app)))
    codes = {m for m in app if len(m) >= 3 and re.search(r"\d", m) and re.search(r"[a-z]", m)}

    def du_modele(mot):
        # Mot du nom, ou mot collé qui contient un code de modèle du nom
        # (« assassinx120 » contient « x120 »). « 280mm » n'est pas « 280 ».
        return mot in app or any(c in mot for c in codes)

    mots_t = _mots(titre)
    for k, mot in enumerate(mots_t):
        if mot not in _VARIANTES or mot in app:
            continue
        prec = mots_t[k - 1] if k else ""
        suiv = mots_t[k + 1] if k + 1 < len(mots_t) else ""
        if mot in ("mini", "micro", "compact") and suiv in _FORMATS:
            continue
        if (mot == "plus" and prec == "80") or (mot == "ai" and suiv != "top") \
                or (mot == "top" and prec != "ai"):
            continue
        voisins = mots_t[max(0, k - 1):k] + mots_t[k + 1:k + 2]
        if any(du_modele(v) for v in voisins):
            return False
    return True


# --- 4. Variante plus précise présente au catalogue -----------------------------
def _mots_identite(nom):
    """Mots d'un nom, mémoire normalisée (« 16GB », « 16 Go », « 16G » → « 16g »)."""
    return {re.sub(r"^(\d{1,3})g[bo]?$", r"\1g", m) for m in _mots(_nom_modele(nom))}


def variante_plus_precise(nom_app, titre, catalogue):
    """Nom d'un AUTRE composant du catalogue que le titre désigne mieux.

    « Lian Li O11 Dynamic EVO » contient tous les mots de « Lian Li O11
    Dynamic » et un de plus ; si ce mot (« evo ») est dans le titre, le titre
    décrit l'EVO, pas le modèle de base. Idem « Meshify 2 Compact », « NH-D15
    G2 », « Kraken Elite 360 », « RTX 3080 12G »…
    """
    base = _mots_identite(nom_app)
    mots_t = _mots_identite(titre)
    for it in catalogue:
        autre = it.get("name") or ""
        if not autre or autre == nom_app:
            continue
        precis = _mots_identite(autre)
        if base < precis and (precis - base) <= mots_t:
            return autre
    return None


# --- 5. Accessoire ou contrefaçon : le modèle ne vient qu'après « pour » --------
_PREPOSITIONS = {"pour", "for", "fur", "per", "para", "voor"}
_COMPATIBLE = {
    "compatible", "compatibles", "compatibile", "compatibili", "kompatibel",
    "apto", "apta", "adatto", "adatta", "adatti", "passend", "geeignet",
}


def _ancre(nom_app):
    """Mot le plus distinctif du nom : le numéro le plus long (sans unité)."""
    mots = _mots(_nom_modele(nom_app))
    nums = [re.match(r"[a-z]*\d+", m).group(0) for m in mots
            if re.match(r"[a-z]*\d", m) and m not in _ROMAINS]
    if nums:
        return max(nums, key=lambda n: sum(c.isdigit() for c in n))
    return mots[-1] if mots else ""


def _code_modele(mot):
    """Code de modèle (« hc4 », « sl140 ») : lettres ET chiffres, pas une mesure."""
    return (len(mot) >= 3 and re.search(r"[a-z]", mot) and re.search(r"\d", mot)
            and not re.fullmatch(r"\d+[a-z]{1,5}", mot))


def sans_renvoi_vers_modele(cat, nom_app, titre):
    """Faux si le modèle n'apparaît qu'APRÈS « pour / for / compatible ».

    « Case NZXT Compatible H1 v2 », « Ventilateur … pour MSI GeForce GTX 1650
    Super », « Genuine Intel Cooler for Core i5-10600K », « Noctua NA-HC4 …
    pour NH-D15S » : le titre vend autre chose qui VISE le modèle. Pour un
    boîtier ou un refroidissement, « Refroidisseur pour processeur Arctic
    Freezer 36 » reste correct : il y faut en plus un AUTRE code de modèle
    avant la préposition (« NA-HC4 »). Les GPU et CPU, eux, commencent
    toujours par leur marque et leur modèle.
    """
    ancre = _ancre(nom_app)
    mots_t = _mots(titre)
    # Une ancre courte (« xr ») doit être un mot entier ; une longue peut être
    # collée (« rtx5090 », « nhd15s »).
    pos = next((k for k, m in enumerate(mots_t)
                if ancre and (m == ancre or (len(ancre) >= 3 and ancre in m))), None)
    if pos is None:
        return True
    avant = mots_t[:pos]
    if any(m in _COMPATIBLE for m in avant):
        return False
    prep = next((k for k, m in enumerate(avant) if m in _PREPOSITIONS), None)
    if prep is None:
        return True
    if cat in ("gpus", "cpus"):
        return False
    app = set(_mots(_nom_modele(nom_app)))
    return not any(_code_modele(m) and m not in app for m in avant[:prep])


# Accessoires propres aux refroidissements, boîtiers, ventilateurs et
# alimentations, que le filtre partagé ne couvre pas (et qu'il n'applique pas
# du tout aux refroidissements).
_ACCESSOIRE_AMZ = re.compile(
    r"\b(habillage|cover|abdeckung|cubierta|copertura|mounting kit|"
    r"kit de montage|kit de fixation|montagekit|replacement fan|"
    r"ventilateur de rechange|ventilateur de remplacement|ersatzlufter|"
    r"without controller)\b")


# --- 6. Lots ------------------------------------------------------------------
# « Lot de 2 disques SSD », « 2er-Pack », « pack of 2 » : le prix est celui de
# plusieurs pièces. Les ventilateurs (vendus en lot au catalogue) et la mémoire
# (kits) ont leurs propres règles.
_LOT = re.compile(
    r"\b(?:lot|pack|set|kit|paquete|confezione)\s(?:de|of|da)\s[2-9]\b"
    r"|\b[2-9]\s?(?:er\s)?(?:pack|pcs|pieces|stuck|unidades|pezzi)\b")
# Processeur précis cité par un refroidissement ou une carte mère : « Ryzen 7
# 7800X3D », « Core i5-14600K ». Les gammes (« Ryzen 7000 ») restent permises.
_CPU_PRECIS = re.compile(
    r"\bryzen [3579] \d{4}|\bcore i[3579] \d{4,5}|\bcore ultra [579] \d{3}")
_CHIPSET = re.compile(r"\b[abhxz][3-9]\d0[a-z]?\b")
_MOT_CARTE_MERE = re.compile(
    r"\b(motherboard|mainboard|carte mere|placa base|scheda madre|hauptplatine)\b")


def est_un_lot(cat, titre):
    """Le titre vend-il plusieurs pièces, ou la pièce avec une autre ?"""
    t = " ".join(_mots(titre))
    if cat not in ("rams", "fans") and _LOT.search(t):
        return True
    if cat in ("coolers", "mobos") and _CPU_PRECIS.search(t):
        return True
    # Boîtier + carte mère : « Fractal Design Ridge mITX Case, Gigabyte A520I
    # AC Motherboard ». « Compatible avec la carte mère » seul reste permis.
    return cat == "cases" and bool(_CHIPSET.search(t) and _MOT_CARTE_MERE.search(t))


# --- 7. Règles par catégorie ------------------------------------------------------
# Ventilateurs : le catalogue les vend en LOT (« Arctic P12 (×3) ») ; une fiche
# à l'unité n'a ni le même prix ni le même contenu.
def lot_de_ventilateurs(nom_app, titre):
    m = re.search(r"\(\s*[x×]\s?(\d)\s*\)", nom_app or "", re.I)
    if not m:
        return True
    n = m.group(1)
    bas = titre.lower()
    # « (×3) », « x3 », « 3x 120 mm » — mais pas « max 3 ».
    if re.search(rf"(?<![a-z])[x×]\s?{n}\b|\b{n}\s?[x×](?![a-z0-9])", bas):
        return True
    t = " ".join(_mots(titre))
    motifs = [
        rf"\b{n}\s?(?:er\s)?(?:pack|pcs|pieces|stuck|unidades|pezzi|"
        rf"ventilateurs|ventiladores|ventole|fans|lufter)\b",
        rf"\b(?:pack|lot|kit|set|paquete|confezione)\s(?:de\s|of\s|da\s)?{n}\b",
    ]
    motifs.append({"3": r"\btri(?:ple)?\s?pack\b", "2": r"\b(?:dual|twin|duo)\s?pack\b"}
                  .get(n, r"$^"))
    return any(re.search(mo, t) for mo in motifs)


# Cartes mères Intel 600/700 : chaque modèle existe en DDR4 et en DDR5, et le
# catalogue nomme explicitement la version DDR4 (« … DDR4 », « …/D4 »).
_CHIPSET_DDR_MIXTE = re.compile(r"[bhz][67]\d0m?")


def carte_mere_coherente(nom_app, titre):
    nom = _mots(nom_app)
    t = _mots(titre)
    if any(_CHIPSET_DDR_MIXTE.fullmatch(m) for m in nom):
        d4_nom = "ddr4" in nom or "d4" in nom
        d4_tit = "ddr4" in t or "d4" in t
        if d4_tit != d4_nom and (d4_tit or "ddr5" in t):
            return False
    # « X570-Plus (Wi-Fi) », « B550-PLUS WiFi II », ou « Wi-Fi » cité parmi
    # les caractéristiques : la version WiFi d'une carte que le catalogue
    # nomme sans WiFi. En revanche « WiFi 7 », « WIFI6E » décrivent la norme,
    # et « B650E PG-ITX WiFi », « Z890-I GAMING WIFI » (WiFi à deux mots au
    # plus du nom) désignent une carte qui n'existe qu'en WiFi : le vendeur
    # reprend son nom officiel.
    if "wifi" not in nom:
        if re.search(r"\(\s*wi-?\s?fi\s*\)|\bwi-?\s?fi\s+ii\b", titre.lower()):
            return False
        app = set(_mots(_nom_modele(nom_app)))
        t = " ".join(t).replace("wi fi", "wifi").split()
        for k, mot in enumerate(t):
            if mot != "wifi":
                continue  # « wifi6e », « wifi7 » : la norme seule
            norme = k + 1 < len(t) and re.fullmatch(r"\d[a-z]?", t[k + 1])
            if not norme and not any(m in app for m in t[max(0, k - 2):k]):
                return False
    return True


# Alimentations : le label 80 PLUS annoncé doit être celui du catalogue
# (« HX850 80+ Gold » de 2012 ≠ HX850 Platinum ; « ROG Strix 1000W Platinum »
# ≠ ROG Strix 1000W Gold).
_LABELS = {"bronze": "bronze", "silver": "silver", "argent": "silver",
           "gold": "gold", "or": "gold", "platinum": "platinum",
           "platine": "platinum", "titanium": "titanium", "titane": "titanium"}
_NOMS_LABEL = "|".join(_LABELS)
_LABEL_80 = re.compile(
    rf"80\s*-?\s*(?:\+|plus)\s*[-®]?\s*({_NOMS_LABEL})\b"
    rf"|\b({_NOMS_LABEL})\s*-?\s*80\s*(?:\+|plus)", re.I)


# Label cité SANS « 80 PLUS » devant : « ASUS ROG Strix - 1000W Platinum ».
# « Or » et « argent » restent exclus (trop courants hors label), et un label
# Cybenetics (« Cybenetics Platinum ») n'est pas un label 80 PLUS.
_LABELS_SEULS = {"bronze", "silver", "gold", "platinum", "platine", "titanium", "titane"}


def label_coherent(fiche, titre):
    voulu = (fiche.get("rating") or "").lower().replace("80+", "").strip()
    if not voulu:
        return True
    trouves = {_LABELS[(a or b).lower()] for a, b in _LABEL_80.findall(titre)}
    if not trouves:
        mots = _mots(titre)
        trouves = {_LABELS[m] for k, m in enumerate(mots)
                   if m in _LABELS_SEULS and not (k and mots[k - 1] == "cybenetics")}
    return not trouves or voulu in trouves


# Cartes graphiques : la mémoire annoncée doit être celle du catalogue (« RX
# 570 Armor 8G » ≠ RX 570 4 Go), et les mots de GÉNÉRATION du nom sont exigés
# (« RTX 6000 Ada » ≠ « Quadro RTX 6000 » de 2018, « RTX 4000 Ada » ≠ « RTX
# A4000 »). La tolérance de `correspond` reste pour les noms de gamme que les
# vendeurs omettent (« GeForce », « Radeon »).
# La mémoire s'écrit aussi collée au code du modèle : « RX570-O8G » (O = OC),
# « 08G », « 8G ».
# « SFF » désigne une VERSION compacte (« RTX 4000 SFF Ada », 70 W) ; « SFF-
# Ready » ne fait que dire qu'une carte normale entre dans un petit boîtier.
_GPU_FACULTATIFS = {"geforce", "radeon", "arc", "nvidia", "amd", "intel"}
_MEMOIRE_GPU = re.compile(r"(?<![a-z0-9])o?0?(\d{1,2})\s?(?:gb|go|g)\b")
_GPU_COMPACTE = re.compile(r"\b(?:sff|small form factor)\b(?! ready)")


def carte_graphique_coherente(nom_app, titre, fiche):
    texte = " ".join(_mots(titre))
    nom = _mots(nom_app)
    for mot in nom:
        if mot.isalpha() and len(mot) >= 3 and mot not in _GPU_FACULTATIFS:
            if not re.search(rf"(?<![a-z]){mot}(?![a-z])", texte):
                return False
    if "sff" not in nom and _GPU_COMPACTE.search(texte):
        return False
    vram = fiche.get("vramGb")
    if vram:
        trouves = {int(x) for x in _MEMOIRE_GPU.findall(texte)}
        if trouves and int(vram) not in trouves:
            return False
    return True


# Processeurs : un PC complet ou un kit annonce de la mémoire ou du stockage
# (« Acer Veriton X2720G i5-14500 16GB 512GBSSD »), et la gamme du nom — ou
# à défaut le fabricant — est exigée (« ARCTIC Senza 5700G » est un PC passif,
# sans « Ryzen » ni « AMD »). Les vendeurs omettent souvent « Core » (« Intel
# i5-12600K Tray ») et Amazon écrit parfois « Ryzensets » : le fabricant et le
# début de mot suffisent.
_GAMMES_CPU = {"ryzen": "amd", "threadripper": "amd", "core": "intel", "xeon": "intel"}


def processeur_coherent(nom_app, titre):
    texte = " ".join(_mots(titre))
    if re.search(r"\b\d{1,4}\s?(?:gb|go|tb|to)|\bssd\b", texte):
        return False
    gamme = next((m for m in _mots(nom_app) if m in _GAMMES_CPU), None)
    return gamme is None or bool(re.search(
        rf"(?<![a-z])(?:{gamme}|{_GAMMES_CPU[gamme]}(?![a-z]))", texte))


# Mémoire : la fréquence PRINCIPALE du titre (la première annoncée) doit être
# celle du kit — « 5600MHz … (ou 5200MHz / 4800MHz) » n'est pas un kit
# DDR5-4800, ni « 6000 Mhz / Pc5-48000 » un kit DDR5-8000 (le texte collé y
# trouvait « 8000 ») —, et une barrette de portable (SO-DIMM, iMac) n'est pas
# une barrette de bureau.
_RAM_PORTABLE = re.compile(
    r"\b(so dimm|sodimm|imac|macbook|mac mini|laptop|notebook|portable|"
    r"portatile?|nuc)\b")


def memoire_coherente(nom_app, titre):
    voulu = re.search(r"ddr\d[- ]?(\d{4})", nom_app or "", re.I)
    texte = " ".join(_mots(titre))
    if _RAM_PORTABLE.search(texte):
        return False
    if not voulu:
        return True
    premiere = re.search(r"\b(\d{4})\s?(?:mhz|mt s|mts|mt)\b|\bddr\d (\d{4})\b", texte)
    return not premiere or int(premiere.group(1) or premiere.group(2)) == int(voulu.group(1))


# Stockage : disque INTERNE du type du catalogue, à la bonne vitesse de
# rotation (« WD Blue 2 To Mobile … 5400 RPM », « WD20EZAZ … 5400 tours/min »
# ≠ WD Blue 7200 tr/min ; un disque dur externe USB-C ou un SSD NVMe ne
# remplacent pas un disque dur).
_EXTERNE = re.compile(r"\b(externe?|external|esterno|externo|usb c|usb 3)\b")
# Un disque dur de 2,5 pouces est un disque de portable (5400 tr/min) : le
# « FireCuda 2 To » à 7200 tr/min du catalogue est le 3,5 pouces. Cherché sur
# le titre BRUT, où le guillemet des pouces existe encore.
_FORMAT_25 = re.compile(
    r"\b2[.,]5\s?(?:\"|''|”|″|pouces?|inch|in\b|zoll|pollici|pulgadas|-inch)", re.I)


def stockage_coherent(nom_app, titre, fiche):
    texte = " ".join(_mots(titre))
    if _EXTERNE.search(texte):
        return False
    if (fiche.get("kind") or "").upper() == "HDD" and re.search(r"\b(ssd|nvme)\b", texte):
        return False
    voulu = re.search(r"\b(5400|5900|7200|10000)\b", nom_app or "")
    if voulu:
        # « 7 200 tr/min » s'écrit avec une espace de milliers.
        texte = re.sub(r"\b(\d) (\d{3})\b", r"\1\2", texte)
        trouves = set(re.findall(
            r"\b(\d{4,5})\s?(?:rpm|tr|tours?|u min|upm|giri)\b", texte))
        if trouves and voulu.group(1) not in trouves:
            return False
        if voulu.group(1) == "7200" and _FORMAT_25.search(titre):
            return False
    return True


def valide(cat, nom_app, titre, jetons, souple=False, catalogue=None):
    """Le titre RÉEL de la fiche désigne-t-il EXACTEMENT le composant ?

    [souple] autorise l'absence des jetons trop variables (la latence mémoire).
    Type, fréquence et capacité, eux, restent obligatoires : on ne peut pas se
    tromper de barrette, seulement de timings.

    [catalogue] = composants de la catégorie ; chargé depuis le disque quand il
    n'est pas fourni (voir `definir_catalogue`).
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
    if catalogue is None:
        catalogue = _catalogue(cat)
    fiche = _fiche(nom_app, catalogue)

    if cat == "rams":
        if not suffixe_generique_ok(nom_app, titre) or not memoire_coherente(nom_app, titre):
            return False
    elif cat not in ("gpus", "cpus"):
        if not (numeros_coherents(nom_app, titre)
                and mots_courts_presents(nom_app, titre)
                and sans_variante_voisine(nom_app, titre)):
            return False
        if cat != "ssds" and _ACCESSOIRE_AMZ.search(" ".join(_mots(titre))):
            return False
    if cat != "rams":
        if not sans_renvoi_vers_modele(cat, nom_app, titre) or est_un_lot(cat, titre):
            return False
        if variante_plus_precise(nom_app, titre, catalogue):
            return False

    regles = {
        "gpus": lambda: carte_graphique_coherente(nom_app, titre, fiche),
        "cpus": lambda: processeur_coherent(nom_app, titre),
        "ssds": lambda: stockage_coherent(nom_app, titre, fiche),
        "mobos": lambda: carte_mere_coherente(nom_app, titre),
        "psus": lambda: label_coherent(fiche, titre),
        "fans": lambda: lot_de_ventilateurs(nom_app, titre),
    }
    if cat in regles and not regles[cat]():
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
    ap.add_argument("--par-marche", action="store_true",
                    help="chercher une fiche PROPRE à chaque marketplace pour "
                         "les pays où l'ASIN commun n'existe pas : le même "
                         "produit y est souvent vendu sous un autre "
                         "identifiant, et le lien cesse alors de rebondir "
                         "vers la France")
    ap.add_argument("--completer", action="store_true",
                    help="compléter les marketplaces manquantes des composants "
                         "DÉJÀ résolus (rend les liens natifs au lieu de "
                         "passer par OneLink) — aucune recherche, uniquement "
                         "la lecture des fiches")
    args = ap.parse_args()
    # Les garde-fous de `valide` lisent le MÊME catalogue que celui qu'on résout.
    definir_catalogue(args.items_dir)

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

            # --- mode PAR MARCHÉ : une fiche propre à chaque pays -----------
            # Le mode complétion se contente de tester l'ASIN déjà connu sur
            # les autres marketplaces. Quand il n'y existe pas, le produit y
            # est pourtant souvent vendu sous un AUTRE identifiant — c'est le
            # cas courant des ventirads et des alimentations, référencés
            # séparément dans chaque pays. Faute de le chercher, tous ces
            # liens rebondissaient vers la France.
            if args.par_marche:
                if not connus or not any(connus.values()):
                    n_saute += 1
                    continue
                manquantes = [m for m in marches if not connus.get(m)]
                if not manquantes:
                    n_saute += 1
                    continue
                q = requete(cat, it)
                simple = requete_simple(cat, it)
                jetons = _jetons(cat, it)
                gagnees = []
                for m in manquantes:
                    src = sources[m]
                    trouve = None
                    for req in [q] + ([simple] if simple and simple != q else []):
                        for asin in src.asins(req, maxi=args.candidats):
                            etat, t = src.fiche(asin, essais=1)
                            if etat == "ok" and valide(cat, nom, t, jetons,
                                                       souple=True):
                                trouve = asin
                                break
                        if trouve:
                            break
                    connus[m] = trouve or False
                    if trouve:
                        gagnees.append(m)
                if gagnees:
                    n_ok += 1
                    print(f"  + {cat}/{cid}: fiches propres sur "
                          f"{', '.join(m.split('.', 1)[1] for m in gagnees)}",
                          flush=True)
                else:
                    n_ko += 1
                ecrire()
                continue

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
                    # Deux essais : une page d'attente laisse la marketplace
                    # « à vérifier », donc une passe entière peut se terminer
                    # sans le moindre progrès si le site bride. Un second essai
                    # coûte quelques secondes et débloque ces catégories.
                    etat, t = sources[m].fiche(asin, essais=2)
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

