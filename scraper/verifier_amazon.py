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

Offres retirées
---------------
Une offre Amazon publiée doit correspondre à une fiche vérifiée. Elle est donc
retirée du fichier d'offres quand :

- le lien est mort ou le titre non conforme (avant, la marketplace quittait le
  cache d'ASIN mais son ancienne offre restait publiée) ;
- la fiche vit mais n'affiche aucun prix à payer (bloc panier vide : Amazon ne
  vend pas l'article sur ce site) — garder l'ancien montant reviendrait à
  publier un prix que plus personne ne peut payer ;
- **purge**, à chaque sauvegarde : sa marketplace n'a plus d'ASIN dans le cache
  (`None`/`False`), ou son URL ne pointe pas vers l'ASIN actuel.

Seules les offres de la filière Amazon (boutique `amazon.*` ET lien vers
Amazon) sont concernées : les offres `amazon.fr` de gputracker pointent vers
une redirection gputracker.eu et restent l'affaire de leur source.

Ordre, budget et reprise
------------------------
Le catalogue compte quelque 2 400 couples (composant, marketplace), bien plus
qu'une étape de 30 minutes n'en lit d'une traite. Avant, l'ordre des catégories
était figé (gpus, cpus…) et rien n'était écrit avant la fin d'une catégorie :
l'étape tuée par son timeout perdait son travail en cours, et les dernières
catégories (refroidissements, ventilateurs) n'étaient plus jamais atteintes.

- **Le plus anciennement vérifié d'abord**, toutes catégories confondues. La
  date de la dernière vérification de chaque couple est mémorisée dans
  `amazon_verif.json` (`{cat: {id: {marketplace: iso}}}`, à la racine du dépôt
  de données), même quand la fiche n'affiche aucun prix. Un couple jamais
  vérifié passe en tête ; à défaut de date mémorisée, celle de l'offre publiée
  en tient lieu.
- **Sauvegarde incrémentale** toutes les `--sauver-tous` fiches, et à l'arrêt
  (fin, budget, SIGTERM ou Ctrl+C). Chaque fichier est remplacé d'un bloc :
  une écriture interrompue laisse la version précédente, valide.
- **Budget** (`--budget-min`) : passé ce délai, plus aucune fiche n'est ouverte
  ; celles en cours se terminent, puis tout est écrit.
- **Parallélisme** (`--paralleles`) : plusieurs marketplaces lues à la fois.
  Les domaines sont distincts, et une marketplace n'est jamais servie par deux
  fils en même temps : la pause par domaine (`--pause`) est conservée.

Usage :
    python scraper/verifier_amazon.py --data-repo <clone> [--items-dir catalog]
        [--only gpus,cpus] [--limit N] [--pause 1.0] [--frais-h 12]
        [--budget-min 25] [--paralleles 4] [--sauver-tous 25]
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from collections import Counter, deque, namedtuple
from datetime import datetime, timezone
from functools import lru_cache
from html import unescape
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import SORTIES  # noqa: E402
from gen_amazon_links import FICHIERS, TAGS, lien  # noqa: E402
from resolve_amazon_asins import _jetons, definir_catalogue, valide  # noqa: E402
from sources.amazon_mobile import AmazonMobileSource  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Dates de vérification par couple, à la racine du dépôt de données.
FICHIER_VERIF = "amazon_verif.json"

# Devise par défaut d'une marketplace, quand la chaîne de prix ne porte pas de
# symbole exploitable.
DEVISES = {
    "amazon.fr": "EUR", "amazon.de": "EUR", "amazon.es": "EUR",
    "amazon.it": "EUR", "amazon.co.uk": "GBP", "amazon.com": "USD",
    "amazon.com.au": "AUD",
}

# Blocs porteurs du prix du PANIER, dans l'ordre de préférence. Les prix qui
# traînent ailleurs dans la page appartiennent aux carrousels « articles
# similaires », aux garanties ou aux accessoires — les prendre reviendrait à
# afficher le prix d'un autre produit.
BLOCS_PRIX = ("corePriceDisplay_desktop_feature_div", "corePrice_feature_div",
              "corePriceDisplay_mobile_feature_div", "corePrice_mobile_feature_div",
              "apex_desktop", "price_feature_div")
# Dernier recours : le panier lui-même. Il héberge aussi des widgets annexes
# (protection, accessoires) : seul un prix explicitement « à payer » y compte.
BLOCS_STRICTS = ("buybox",)

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
_SPAN_CLASSE = re.compile(r'<span\b[^>]*?\bclass="([^"]*)"[^>]*>', re.I)
_OFFSCREEN = re.compile(r'<span\b[^>]*\bclass="a-offscreen"[^>]*>([^<]*)</span>', re.I)
_LIBELLE_A_PAYER = re.compile(
    r'<span\b[^>]*\bid="apex-pricetopay-accessibility-label"[^>]*>([^<]*)</span>', re.I)
_WIDGET = re.compile(r'<div\b[^>]*?(?<![\w-])id="[^"]+_feature_div"', re.I)
_URL_DP = re.compile(
    r"^https?://(?:www\.)?(amazon\.[a-z.]+)/(?:[^?#]*/)?dp/([A-Z0-9]{10})", re.I)

# Classes qui désignent le prix À PAYER — et non un prix barré, un prix
# unitaire ou le prix d'une option.
_A_PAYER = {"pricetopay", "apexpricetopay", "apex-pricetopay-value"}

# Un couple à vérifier : composant [cid] de la catégorie [cat], sur [marche].
Tache = namedtuple("Tache", "cat cid marche asin")


def _texte(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _maintenant():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _horodatage(iso):
    """Timestamp UTC d'une date `AAAA-MM-JJTHH:MM:SSZ`, ou None si illisible."""
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


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


# --- lecture du prix ---------------------------------------------------------

@lru_cache(maxsize=None)
def _motif_element(balise):
    # Scripts, styles et commentaires sont sautés d'un bloc : leur texte peut
    # contenir « <div » sans rien ouvrir.
    return re.compile(
        r"(?P<ignore><script\b.*?</script\s*>|<style\b.*?</style\s*>|<!--.*?-->)"
        r"|<(?P<ferme>/?)%s\b[^>]*?(?P<auto>/?)>" % balise, re.S | re.I)


def _fin_element(html, debut, balise, maxi):
    """Position de la balise fermante qui équilibre l'élément [balise] ouvert
    juste avant [debut], ou None si elle n'arrive pas dans les [maxi]
    caractères suivants (page tronquée)."""
    profondeur = 1
    for m in _motif_element(balise).finditer(html, debut,
                                             min(len(html), debut + maxi)):
        if m.group("ignore"):
            continue
        if m.group("ferme"):
            profondeur -= 1
            if profondeur == 0:
                return m.start()
        elif not m.group("auto"):
            profondeur += 1
    return None


def contenu_bloc(html, ident, maxi=100_000):
    """Contenu RÉEL de l'élément d'identifiant [ident] — jusqu'à sa balise
    fermante, pas au-delà —, ou None s'il est absent de la page.

    Lire « les 20 000 caractères qui suivent le début du bloc », comme avant,
    débordait sur les widgets voisins : quand le bloc panier est vide (article
    non vendu par Amazon sur ce site, cas fréquent sur amazon.es vu des
    États-Unis), le premier prix venu était celui d'une garantie ou d'un
    accessoire — « RTX 5080 à 67,42 € ». Si la page est tronquée avant la
    fermeture, le bloc s'arrête au widget suivant.
    """
    # « data-csa-c-slot-id="X" » contient aussi « id="X" » : seul l'attribut
    # `id` lui-même compte. (`str.find` plutôt qu'une expression régulière :
    # sur une page de 1,2 Mo, c'est 30 ms de moins par bloc absent.)
    cle = f'id="{ident}"'
    i = html.find(cle)
    while i > 0 and (html[i - 1].isalnum() or html[i - 1] in "-_"):
        i = html.find(cle, i + 1)
    if i < 0:
        return None
    ouverture = html.rfind("<", 0, i)
    fin_balise = html.find(">", i + len(cle))
    nom = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", html[ouverture:ouverture + 20]) \
        if ouverture >= 0 else None
    if not nom or fin_balise < 0:
        return None
    if html[fin_balise - 1] == "/":
        return ""  # élément auto-fermant : vide par construction
    debut = fin_balise + 1
    fin = _fin_element(html, debut, nom.group(1).lower(), maxi)
    if fin is None:
        w = _WIDGET.search(html, debut, min(len(html), debut + maxi))
        fin = w.start() if w else min(len(html), debut + maxi)
    return html[debut:fin]


def _propre(texte):
    t = unescape(texte).replace("\xa0", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", t).strip()


def _texte_prix(interieur):
    """Montant affiché par un élément `a-price` : son texte « hors écran » s'il
    est rempli, sinon ses morceaux visibles (`a-price-symbol`, `-whole`,
    `-fraction`). Le rendu mobile actuel d'amazon.fr laisse `a-offscreen` VIDE
    et ne donne le prix qu'en morceaux : l'ancienne lecture n'y voyait rien."""
    m = _OFFSCREEN.search(interieur)
    if m and re.search(r"\d", m.group(1)):
        return _propre(m.group(1))
    visible = _propre(re.sub(r"<[^>]+>", "", _OFFSCREEN.sub("", interieur)))
    return visible if re.search(r"\d", visible) else ""


def _prix_du_bloc(contenu, strict=False):
    """Chaîne du prix À PAYER dans le contenu d'un bloc, ou ''.

    Ordre de préférence :
    1. un `a-price` marqué « à payer » (`priceToPay`, `apexPriceToPay`) ;
    2. le libellé d'accessibilité de ce même prix ;
    3. hors mode [strict], le premier `a-price` ordinaire du bloc — jamais un
       prix barré (prix conseillé, plus bas des 30 jours) ni un `a-text-price`
       (prix unitaire, ancien prix).
    """
    ordinaires = []
    for m in _SPAN_CLASSE.finditer(contenu):
        classes = m.group(1).split()
        if "a-price" not in classes or 'data-a-strike="true"' in m.group(0):
            continue
        fin = _fin_element(contenu, m.end(), "span", 5000)
        brut = _texte_prix(contenu[m.end():fin if fin is not None else m.end() + 2000])
        if not brut:
            continue
        if {c.lower() for c in classes} & _A_PAYER:
            return brut
        if "a-text-price" not in classes:
            ordinaires.append(brut)
    m = _LIBELLE_A_PAYER.search(contenu)
    if m and re.search(r"\d", m.group(1)):
        return _propre(m.group(1))
    if not strict and ordinaires:
        return ordinaires[0]
    return ""


def prix(html, domaine):
    """(montant, devise, chaîne brute) du bloc panier, ou (None, None, '').

    Seul le contenu réel des blocs de prix est lu (`contenu_bloc`) : un bloc
    présent mais vide donne « pas de prix », jamais celui du widget voisin.
    """
    blocs = [(b, False) for b in BLOCS_PRIX] + [(b, True) for b in BLOCS_STRICTS]
    for bloc, strict in blocs:
        contenu = contenu_bloc(html, bloc)
        if not contenu:
            continue
        brut = _prix_du_bloc(contenu, strict)
        if not brut:
            continue
        v = _nombre(brut)
        if v and v > 0:
            return v, devise(brut, domaine), brut
    return None, None, ""


# --- offres, dates, purge ----------------------------------------------------

def _est_amazon(offre):
    """Offre de la filière Amazon (ce script ou la PA-API) : boutique
    `amazon.*` ET lien vers Amazon. Les offres `amazon.fr` de gputracker
    pointent vers gputracker.eu : elles appartiennent à leur source, qui les
    purge elle-même."""
    shop = (offre.get("shop") or "").lower()
    hote = urlparse(offre.get("url") or "").netloc.lower()
    return "amazon." in shop and "amazon." in hote


def date_offre(entree, marche):
    """`lastSeen` de l'offre Amazon de [marche] dans [entree], ou ''."""
    for p in (entree or {}).get("prices") or []:
        if (p.get("shop") or "").lower() == marche and _est_amazon(p):
            return p.get("lastSeen") or ""
    return ""


def derniere_verif(verif, entree, cat, cid, marche):
    """Date ISO de la dernière vérification du couple, '' s'il ne l'a jamais été.

    `amazon_verif.json` fait foi. À défaut — couples vérifiés avant qu'il
    existe — la date de relevé de l'offre publiée en tient lieu : elle a été
    écrite lors d'une vérification.
    """
    return verif.get(cat, {}).get(cid, {}).get(marche) or date_offre(entree, marche)


def file_attente(cache, verif, offres, items, marches, frais_h=0.0, limite=0,
                 maintenant=None):
    """Couples à vérifier, LE PLUS ANCIENNEMENT VÉRIFIÉ D'ABORD, toutes
    catégories confondues ; un couple jamais vérifié passe en tête.

    [items] = {cat: {id: composant}} et [offres] = {cat: {id: entrée}} : seules
    les catégories chargées sont parcourues. [frais_h] écarte les couples
    vérifiés depuis moins de N heures ; [limite] borne le nombre de COMPOSANTS
    par catégorie (les premiers de la file).
    """
    seuil = None
    if frais_h > 0:
        seuil = (time.time() if maintenant is None else maintenant) - frais_h * 3600
    brutes = []
    for cat, par_id in items.items():
        for cid, par_marche in cache.get(cat, {}).items():
            if cid not in par_id:
                continue
            for m in marches:
                asin = par_marche.get(m)
                if not asin:
                    continue
                date = derniere_verif(verif, offres.get(cat, {}).get(cid), cat, cid, m)
                vu = _horodatage(date) if date else None
                if seuil is not None and vu is not None and vu > seuil:
                    continue
                # Les dates ISO se trient comme du texte ; '' (jamais vérifié)
                # passe devant tout. Le rang garde un ordre stable entre égaux.
                cle = date if vu is not None else ""
                brutes.append((cle, len(brutes), Tache(cat, cid, m, asin)))
    brutes.sort(key=lambda b: (b[0], b[1]))
    taches, retenus = [], {}
    for _, _, t in brutes:
        if limite:
            ids = retenus.setdefault(t.cat, set())
            if t.cid not in ids:
                if len(ids) >= limite:
                    continue
                ids.add(t.cid)
        taches.append(t)
    return taches


def retirer_offre(entree, marche):
    """Retire de [entree] l'offre Amazon de [marche] ; True si elle existait."""
    if not entree:
        return False
    avant = entree.get("prices") or []
    apres = [p for p in avant
             if not ((p.get("shop") or "").lower() == marche and _est_amazon(p))]
    if len(apres) == len(avant):
        return False
    entree["prices"] = apres
    return True


def poser_offre(offres_cat, cid, nom, offre, quand):
    """Publie [offre] pour le composant [cid], en créant son entrée au besoin."""
    e = offres_cat.get(cid)
    if e is None:
        e = {"id": cid, "name": nom, "priceMin": None,
             "prices": [], "image": None, "lastUpdated": quand}
        offres_cat[cid] = e
    # L'offre relevée remplace celles de la MÊME marketplace ; celles des
    # autres marketplaces sont conservées, sans quoi chaque passe partielle
    # effacerait le travail de la précédente.
    e["prices"] = [p for p in e.get("prices", [])
                   if (p.get("shop") or "").lower() != offre["shop"]]
    e["prices"].append(offre)
    e["lastUpdated"] = quand


def purger(cache, offres):
    """Retire les offres Amazon qu'aucun ASIN valide ne justifie plus : leur
    marketplace n'a plus d'ASIN dans le cache (`None`/`False`/absente), ou leur
    URL ne pointe pas vers l'ASIN actuel de cette marketplace.

    Une catégorie absente du cache n'est pas touchée : un cache perdu ne doit
    pas effacer toutes les offres à l'aveugle. Renvoie (offres retirées,
    catégories modifiées).
    """
    n, touchees = 0, set()
    for cat, entrees in offres.items():
        if not cache.get(cat):
            continue
        for cid, e in entrees.items():
            par_marche = cache[cat].get(cid) or {}
            avant = e.get("prices") or []
            gardees = []
            for p in avant:
                if _est_amazon(p):
                    shop = p["shop"].lower()
                    asin = par_marche.get(shop)
                    m = _URL_DP.match(p.get("url") or "")
                    if not (asin and m and m.group(1).lower() == shop
                            and m.group(2).upper() == str(asin).upper()):
                        n += 1
                        continue
                gardees.append(p)
            if len(gardees) != len(avant):
                e["prices"] = gardees
                touchees.add(cat)
    return n, touchees


def oublier_sans_asin(cache, verif):
    """Efface les dates des couples qui n'ont plus d'ASIN : le jour où un
    nouvel ASIN est trouvé, le couple repart « jamais vérifié », donc en tête de
    file. Renvoie True si [verif] a changé."""
    change = False
    for cat in list(verif):
        if cat not in cache:
            continue  # cache incomplet : on ne décide rien
        for cid in list(verif[cat]):
            par_marche = cache[cat].get(cid) or {}
            for m in list(verif[cat][cid]):
                if not par_marche.get(m):
                    del verif[cat][cid][m]
                    change = True
            if not verif[cat][cid]:
                del verif[cat][cid]
                change = True
        if not verif[cat]:
            del verif[cat]
            change = True
    return change


# --- vérification d'une fiche ------------------------------------------------

def examiner(source, cat, it, marche, asin):
    """Ouvre UNE fiche et rend son verdict, sans rien écrire.

    `etat` : `bloque` (page d'attente : ni preuve de vie, ni preuve de mort),
    `mort` (404 ou titre non conforme), `sans_prix` (fiche vivante, aucun prix
    à payer), `devise` (prix converti pour le visiteur) ou `prix`.
    """
    etat, titre, html = source.fiche_complete(asin, essais=1)
    if etat == "bloque":
        return {"etat": "bloque"}
    if etat == "absent" or not valide(cat, it["name"], titre, _jetons(cat, it),
                                      souple=True):
        # Fiche disparue ou produit remplacé : on cesse de publier ce lien, et
        # la prochaine résolution cherchera mieux.
        return {"etat": "mort"}
    dispo, phrase = disponibilite(html)
    montant, dev, brut = prix(html, marche)
    if montant is None:
        return {"etat": "sans_prix", "dispo": dispo, "phrase": phrase}
    if dev != DEVISES.get(marche):
        # Amazon convertit pour le visiteur : depuis l'Europe, `amazon.com`
        # affiche des euros. Ce montant n'est PAS le prix que verra un acheteur
        # américain (frais d'import inclus, offre différente). On garde le
        # lien, qui reste valide, mais on ne publie pas un prix qui serait faux
        # pour l'utilisateur de cette marketplace.
        return {"etat": "devise", "dispo": dispo, "brut": brut}
    return {"etat": "prix", "dispo": dispo, "titre": titre,
            "montant": round(montant, 2), "devise": dev, "brut": brut}


def appliquer(r, t, nom, cache, verif, offres, quand):
    """Reporte le verdict [r] de la tâche [t] dans les données en mémoire.

    Renvoie (ligne de journal, fichiers modifiés) — catégories, `asins` et/ou
    `verif`. Une page d'attente ne change rien, pas même la date : le couple
    reste en tête de file pour la prochaine exécution.
    """
    cat, cid, m = t.cat, t.cid, t.marche
    sales = set()
    etat = r["etat"]
    if (cache.get(cat, {}).get(cid) or {}).get(m) != t.asin:
        # L'entrée a changé depuis la mise en file (composant retiré du cache
        # après la mort de sa dernière fiche, sur une autre marketplace) : ce
        # verdict ne porte plus sur un lien publié.
        return f"  - {cat}/{cid} {m}: entrée retirée entre-temps, ignoré", sales
    if etat == "bloque":
        return f"  ? {cat}/{cid} {m}: page d'attente, revue plus tard", sales
    if etat == "mort":
        cache[cat][cid][m] = None
        sales.add("asins")
        if verif.get(cat, {}).get(cid, {}).pop(m, None) is not None:
            sales.add("verif")
        if retirer_offre(offres[cat].get(cid), m):
            sales.add(cat)
        if not any(cache[cat][cid].values()):
            # Plus AUCUNE fiche valide : l'entrée est retirée du cache (et son
            # titre de référence avec elle). Laissée à {marché: None}, elle
            # ressemblerait à « cherché, rien trouvé », et
            # `resolve_amazon_asins.py` ne la rechercherait plus jamais —
            # le composant perdrait son lien pour toujours au lieu d'en
            # recevoir un meilleur au passage suivant.
            del cache[cat][cid]
            cache.get("_titres", {}).get(cat, {}).pop(cid, None)
            return (f"  x {cat}/{cid} {m}: lien mort ({t.asin}) — plus aucune "
                    f"fiche, composant remis à rechercher"), sales
        return f"  x {cat}/{cid} {m}: lien mort ({t.asin})", sales
    verif.setdefault(cat, {}).setdefault(cid, {})[m] = quand
    sales.add("verif")
    if etat == "sans_prix":
        retiree = retirer_offre(offres[cat].get(cid), m)
        if retiree:
            sales.add(cat)
        return (f"  . {cat}/{cid} {m}: sans prix"
                f"{'' if r['dispo'] else ' (rupture)'}"
                f"{' — ancienne offre retirée' if retiree else ''}"), sales
    if etat == "devise":
        return f"  $ {cat}/{cid} {m}: devise convertie ({r['brut']}), écarté", sales
    dispo = r["dispo"]
    poser_offre(offres[cat], cid, nom, {
        "shop": m, "price": r["montant"], "currency": r["devise"],
        "url": lien(m, t.asin), "inStock": bool(dispo),
        "lastSeen": quand, "product": r["titre"], "image": None,
    }, quand)
    sales.add(cat)
    return (f"  {'+' if dispo else '~'} {cat}/{cid} {m}: {r['brut']}"
            f"{'' if dispo else ' (rupture)'}"), sales


# --- exécution parallèle -----------------------------------------------------

def executer(taches, traiter, paralleles=1, fin=None, arret=None, sur_arret=None):
    """Lance traiter(tâche) sur chaque tâche avec [paralleles] fils ; renvoie
    (tâches traitées, tâches restantes, raison de l'arrêt).

    Une marketplace n'est JAMAIS servie par deux fils à la fois : chaque source
    garde sa cadence (`--pause`, la politesse par domaine) et sa session HTTP
    n'est jamais utilisée par deux fils en même temps. Parmi les marketplaces
    libres, un fil prend toujours la tâche la plus ancienne en tête de file :
    l'ordre « le plus anciennement vérifié d'abord » est tenu autant que le
    parallélisme le permet.

    Raisons : `fini` ; `budget` ([fin], instant `time.monotonic()`, est passé :
    plus aucune fiche n'est ouverte, celles en cours se terminent) ; `arret`
    ([arret] levé : SIGTERM, Ctrl+C). [sur_arret] est appelé dès que l'arrêt
    est demandé, sans attendre les fiches en cours : c'est la sauvegarde
    d'urgence.
    """
    arret = arret or threading.Event()
    files = {}
    for rang, t in enumerate(taches):
        files.setdefault(t.marche, deque()).append((rang, t))
    occupees = set()
    cond = threading.Condition()
    bilan = {"traitees": 0, "budget": False}

    def prendre():
        with cond:
            while True:
                if arret.is_set():
                    return None
                if fin is not None and time.monotonic() >= fin:
                    bilan["budget"] = True
                    arret.set()
                    return None
                libres = [(f[0][0], m) for m, f in files.items()
                          if f and m not in occupees]
                if libres:
                    m = min(libres)[1]
                    occupees.add(m)
                    return files[m].popleft()[1]
                if not any(files[m] for m in occupees):
                    return None  # plus rien ne se libérera
                cond.wait(0.5)

    def rendre(t):
        with cond:
            occupees.discard(t.marche)
            bilan["traitees"] += 1
            cond.notify_all()

    def fil():
        while True:
            t = prendre()
            if t is None:
                with cond:
                    cond.notify_all()
                return
            try:
                traiter(t)
            except Exception as exc:  # noqa: BLE001 — une fiche n'arrête pas le fil
                print(f"  ! {t.cat}/{t.cid} {t.marche}: {type(exc).__name__}: {exc}",
                      flush=True)
            finally:
                rendre(t)

    fils = [threading.Thread(target=fil, daemon=True, name=f"verif-{i}")
            for i in range(max(1, min(paralleles, len(files))))]
    for f in fils:
        f.start()
    sauve = False
    try:
        while True:
            for f in fils:
                f.join(0.2)
            if arret.is_set() and not sauve and sur_arret:
                sauve = True
                sur_arret()
            if not any(f.is_alive() for f in fils):
                break
    except KeyboardInterrupt:
        arret.set()
        if sur_arret and not sauve:
            sur_arret()
        try:  # laisser quelques secondes aux fiches en cours
            limite = time.monotonic() + 10
            for f in fils:
                f.join(max(0.0, limite - time.monotonic()))
        except KeyboardInterrupt:
            pass
    restantes = sum(len(f) for f in files.values())
    raison = "budget" if bilan["budget"] else ("arret" if arret.is_set() else "fini")
    return bilan["traitees"], restantes, raison


# --- fichiers ----------------------------------------------------------------

def _lire_json(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def ecrire_json(chemin, donnees, sort_keys=False):
    """Écrit [donnees] sans jamais laisser un fichier à moitié écrit : la copie
    temporaire ne remplace l'ancien fichier qu'une fois complète, et une étape
    tuée pendant l'écriture garde la version précédente, valide."""
    tmp = f"{chemin}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=1, sort_keys=sort_keys)
    os.replace(tmp, chemin)


def principal():
    debut = time.monotonic()
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--items-dir", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0,
                    help="N composants au plus par catégorie (les plus "
                         "anciennement vérifiés)")
    ap.add_argument("--pause", type=float, default=1.0,
                    help="délai minimal entre deux requêtes vers une MÊME "
                         "marketplace (politesse)")
    ap.add_argument("--markets", default=",".join(TAGS))
    ap.add_argument("--frais-h", type=float, default=12.0,
                    help="ne pas revérifier un couple composant + marketplace "
                         "vérifié depuis moins de N heures")
    ap.add_argument("--budget-min", type=float, default=0.0,
                    help="arrêt propre avant N minutes : plus aucune fiche "
                         "n'est ouverte passé ce délai, et tout est écrit "
                         "(0 = sans limite)")
    ap.add_argument("--paralleles", type=int, default=1,
                    help="marketplaces lues en même temps, une par fil (la "
                         "pause par domaine est conservée)")
    ap.add_argument("--sauver-tous", type=int, default=25,
                    help="écrire les fichiers toutes les N fiches lues (et à "
                         "l'arrêt)")
    args = ap.parse_args()

    items_dir = args.items_dir or os.path.join(args.data_repo, "catalog")
    # Le contrôle des titres (modèle voisin plus précis…) lit le catalogue
    # vérifié, pas une copie qui pourrait différer.
    definir_catalogue(items_dir)
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    marches = [m.strip() for m in args.markets.split(",") if m.strip() in TAGS]
    fin = debut + args.budget_min * 60 if args.budget_min > 0 else None

    chemin_asins = os.path.join(args.data_repo, "amazon_asins.json")
    with open(chemin_asins, encoding="utf-8") as f:
        cache = json.load(f)
    chemin_verif = os.path.join(args.data_repo, FICHIER_VERIF)
    verif = _lire_json(chemin_verif, {})

    items, offres, sorties = {}, {}, {}
    for cat, fichier in FICHIERS.items():
        if seules and cat not in seules:
            continue
        chemin_items = os.path.join(items_dir, f"{fichier}.json")
        if cat not in cache or not os.path.exists(chemin_items):
            continue
        with open(chemin_items, encoding="utf-8") as f:
            items[cat] = {e["id"]: e for e in json.load(f)}
        sorties[cat] = os.path.join(args.data_repo, SORTIES.get(cat, f"{cat}.json"))
        offres[cat] = {e["id"]: e for e in _lire_json(sorties[cat], [])}

    taches = file_attente(cache, verif, offres, items, marches,
                          args.frais_h, args.limit)
    print(f"{len(taches)} fiches à vérifier · {len(marches)} marketplaces · "
          f"{max(1, args.paralleles)} fil(s) · budget "
          f"{f'{args.budget_min:g} min' if fin else 'illimité'}", flush=True)

    sources = {m: AmazonMobileSource(m, rate_limit=args.pause) for m in marches}
    verrou = threading.RLock()
    n = Counter()
    sales = set()

    def sauver():
        # Sous verrou : aucun fil ne modifie les données pendant l'écriture.
        with verrou:
            retirees, touchees = purger(cache, offres)
            n["purge"] += retirees
            sales.update(touchees)
            if oublier_sans_asin(cache, verif):
                sales.add("verif")
            for cat in sorted(c for c in sales if c in offres):
                ecrire_json(sorties[cat], list(offres[cat].values()))
            if "asins" in sales:
                ecrire_json(chemin_asins, cache, sort_keys=True)
            if "verif" in sales:
                ecrire_json(chemin_verif, verif, sort_keys=True)
            sales.clear()

    def traiter(t):
        it = items[t.cat][t.cid]
        r = examiner(sources[t.marche], t.cat, it, t.marche, t.asin)
        with verrou:
            ligne, modifies = appliquer(r, t, it["name"], cache, verif, offres,
                                        _maintenant())
            sales.update(modifies)
            etat = r["etat"]
            n["lues"] += 1
            n[etat] += 1
            # Mêmes compteurs qu'avant : « prix relevés » = en stock, et une
            # rupture compte qu'elle affiche un prix ou non.
            if etat == "prix" and r["dispo"]:
                n["en_stock"] += 1
            if etat in ("prix", "sans_prix") and not r["dispo"]:
                n["rupture"] += 1
            print(ligne, flush=True)
            if args.sauver_tous > 0 and n["lues"] % args.sauver_tous == 0:
                sauver()

    arret = threading.Event()
    try:
        signal.signal(signal.SIGTERM, lambda *_: arret.set())
    except (ValueError, OSError):
        pass  # hors du fil principal : Ctrl+C reste géré par executer
    traitees, restantes, raison = executer(taches, traiter, args.paralleles,
                                           fin, arret, sur_arret=sauver)
    sauver()

    print(f"\nprix relevés {n['en_stock']} · ruptures {n['rupture']} · sans prix affiché "
          f"{n['sans_prix']} · devise convertie (écarté) {n['devise']} "
          f"· liens morts retirés {n['mort']}")
    print(f"fiches lues {traitees}/{len(taches)} · pages d'attente {n['bloque']} "
          f"· offres Amazon purgées {n['purge']} · arrêt : {raison}"
          f"{f' ({restantes} restantes)' if restantes else ''} "
          f"· {(time.monotonic() - debut) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
