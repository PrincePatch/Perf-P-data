# -*- coding: utf-8 -*-
"""Ajoute des PRIX VÉRIFIÉS + MODÈLES + IMAGES des boutiques à SOURCE DIRECTE
aux sorties id-clées de l'enrichissement : `gpus_app.json`, `cpus_app.json`,
`rams/ssds/mobos/psus/cases/coolers.json` (lots 27-30). Runner GÉNÉRIQUE
multi-boutiques (remplace l'ancien enrich_ldlc.py mono-boutique) :

- **LDLC** (`ldlc.com`), **Materiel.net** et **Alternate** (France, EUR) —
  quasi tous les modèles achetables, photos haute résolution, stock réel ;
- **Morele** (`morele.net`, Pologne, PLN) — couverture des utilisateurs PL ;
- **Canada Computers** (`canadacomputers.com`, Canada, CAD) — couverture CA.

Les ventilateurs (`fans.json`) ne font PAS partie de l'étape : le catalogue les
vend par lots (« Arctic P12 (×3) ») quand les boutiques vendent l'unité, et
l'app ne lit pas d'offres par id pour eux.

REMPLACEMENT PAR BOUTIQUE (18/11/2026). Quand une boutique RÉPOND pour un
composant (liste de résultats, fiche produit ou « aucun résultat » explicite),
ses anciennes offres pour ce composant sont REMPLACÉES par les nouvelles :
celles qui ne sont plus trouvées disparaissent. Une boutique en échec (403,
429, délai, page non reconnue), coupée ou non visitée faute de temps GARDE ses
anciennes offres : c'est la purge par âge de `nettoyer_offres` qui les
retire. Avant, un composant sans nouvelle offre gardait TOUTES ses anciennes
offres (Morele et Canada Computers du 13/07 dans coolers.json), et une
boutique coupée perdait les siennes dès qu'une autre boutique trouvait quelque
chose. `priceMin` est recalculé avec la règle de `nettoyer_offres` : null
quand il ne reste aucune offre (l'entrée reste, son image sert de repli).

CORRESPONDANCE STRICTE : filtres partagés avec `enrich_catalog` (jetons,
suffixes de modèle, bundle / accessoire / occasion, écart de prix aberrant),
plus quatre règles nées d'erreurs réelles des fichiers du 18/11/2026 :
- capacité TOTALE des kits mémoire (« 2 x 16 Go (32 Go) » ≠ 16 Go ; 28 offres
  mémoire sur 157 portaient une autre capacité) ;
- composant PLUS PRÉCIS du catalogue : une offre « RTX 3050 6G » ou « NH-D15S »
  revient à ce composant-là, jamais à la « RTX 3050 » ou au « NH-D15 » ;
- refroidissements et boîtiers : numéro de génération (« Pure Rock 2 » ≠
  « Pure Rock 3 »), mots du modèle (« Shadow Rock 3 » ≠ « Pure Wings 3 »),
  mots de variante (« Slim », « Pro », « SE », « XL », « Compact »…) ;
- composant sans achat (« Refroidisseur stock », prix 0) jamais recherché.
Les offres DÉJÀ publiées par ces boutiques sont repassées dans ces filtres au
chargement (`revalider_existant`, sans réseau).

TEMPS MAÎTRISÉ (18/11/2026 — l'étape était tuée à 45 min, avant les alims,
boîtiers et refroidissements) :
- un fil par boutique (`--paralleles`), chacun gardant sa propre politesse ;
  les écritures se font sous verrou ;
- ROTATION : chaque boutique visite d'abord les composants qu'elle a
  rafraîchis le plus anciennement (`enrich_shops_etat.json` à la racine du
  dépôt de données, publié avec les données) — un passage coupé n'affame plus
  les dernières catégories ;
- `--budget-min` : plus aucun composant n'est commencé quand l'échéance
  approche, et tout est écrit AVANT elle ;
- sauvegarde INCRÉMENTALE (toutes les 30 s, écriture atomique) : un arrêt
  brutal ne perd que les dernières secondes ;
- récapitulatif par boutique en fin de log : requêtes, correspondances,
  échecs HTTP.

À exécuter APRÈS `enrich_catalog` (et `enrich_newegg`) pour compléter les
fichiers existants ; exécuté seul, il crée des fichiers ne contenant que ses
offres (toujours valides).

Usage :
    python scraper/enrich_shops.py --data-repo <clone Perf-P-data>
                                   [--items-dir catalog] [--only gpus,rams]
                                   [--shops ldlc,materielnet,alternate,morele,canadacomputers]
                                   [--limit N] [--budget-min 35] [--paralleles 5]
                                   [--etat <fichier d'état>]
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(__file__))
from enrich_catalog import (  # noqa: E402
    CATEGORIES, SORTIES, _norm, _norm_espace, _prix_min, correspond,
    produit_suspect, sans_aberrantes, suffixe_ok, telecharger_image)
from sources.boutiques import (  # noqa: E402
    AlternateSource, CanadaComputersSource, MaterielNetSource, MoreleSource)
from sources.ldlc import LdlcSource, PageNonReconnue  # noqa: E402

RACINE = os.path.join(os.path.dirname(__file__), "..")

# Boutiques disponibles : id → fabrique de source. Chaque source expose
# `offres(requete)`, `domaine`, `devise`, `unites` et `nb_requetes`.
BOUTIQUES = {
    "ldlc": lambda: LdlcSource(rate_limit=1.5),
    # Morele limite vite (429) : cadence 4 s + backoff Retry-After (lot 29).
    "morele": lambda: MoreleSource(rate_limit=4.0),
    "canadacomputers": lambda: CanadaComputersSource(rate_limit=1.5),
    # Lot 29 : deux sources EUROPÉENNES de plus (stock réel + tout le catalogue,
    # au-delà du sous-ensemble gputracker).
    "alternate": lambda: AlternateSource(rate_limit=1.5),
    "materielnet": lambda: MaterielNetSource(rate_limit=1.5),
}

# Échecs d'affilée au-delà desquels une boutique est mise en pause, puis
# ignorée pour le reste du passage si la seconde chance échoue aussi (voir
# `parcourir_boutique`).
SEUIL_COUPURE = 6
# Pause après un échec : PAUSE_ECHEC_S × nombre d'échecs d'affilée, plafonnée.
# Laisse au pare-feu d'une boutique le temps de relâcher l'adresse au lieu de
# brûler les 6 essais en quelques secondes.
PAUSE_ECHEC_S = 10
PAUSE_ECHEC_MAX_S = 60
# Seconde chance d'une boutique coupée : LDLC et Materiel.net bloquent par
# périodes (403 puis délais dépassés) au milieu du passage du 18/11/2026.
PAUSE_REPRISE_S = 300
REPRISES_MAX = 1
# Sauvegarde incrémentale : au plus une écriture des fichiers modifiés toutes
# les SAUVEGARDE_S secondes (et toujours à la fin).
SAUVEGARDE_S = 30
# Marge avant l'échéance du budget sous laquelle aucun composant n'est plus
# commencé (le pire composant : 2 requêtes à 25 s de lecture). Plafonnée à
# 25 % du budget pour les essais courts.
MARGE_MAX_S = 60
# Offres retenues par boutique et par composant (en stock d'abord, puis prix).
OFFRES_PAR_BOUTIQUE = 8
# État de la rotation (dernière visite réussie par boutique et par composant),
# à la racine du dépôt de données.
FICHIER_ETAT = "enrich_shops_etat.json"

# Catégories où le modèle se lit dans les MOTS du nom (pas dans un numéro de
# modèle unique) : règles de `modele_coherent`.
CATEGORIES_MODELE_MOTS = ("coolers", "cases")
# Mots signalant une AUTRE version du produit quand le composant ne les porte
# pas (« Pure Rock Slim 3 », « Peerless Assassin 120 SE », « Define 7 XL »,
# « Meshify 2 Compact », « Liquid Freezer III Pro 240 »…).
_MOTS_VARIANTE = frozenset({
    "slim", "pro", "lp", "elite", "lx", "mini", "max", "plus", "se", "co", "xl",
    "ultra", "lite", "duo", "compact", "evo",
})
# Unités qui suivent un nombre à un chiffre sans en faire un numéro de modèle.
_UNITES = frozenset({"mm", "cm", "w", "go", "gb", "to", "tb"})
# Format de boîtier écrit en description (« boîtier mini tower », « Mini-ITX »
# chez Alternate) : ce « mini » n'est pas le modèle « O11 Dynamic Mini ».
_FORMAT_BOITIER = re.compile(r"\b(?:mini|midi|big|full|micro)[\s-]*(?:tower|tour|itx|atx)\b",
                             re.IGNORECASE)
# Articles de retour / carton ouvert, absents de la liste « occasion »
# d'enrich_catalog : « (Open Box) » chez Canada Computers, « [outlet] » chez
# Morele. Même règle que l'occasion : leur prix n'est pas celui du neuf.
_RE_RETOUR = re.compile(r"open[\s-]?box|\boutlet\b", re.IGNORECASE)
# Mémoire d'ordinateur PORTABLE (SO-DIMM, « mémoire PC portable » chez
# Alternate) : jamais une barrette du catalogue.
_RE_SODIMM = re.compile(r"\bso[\s-]?dimm\b|\bpc portable\b", re.IGNORECASE)
# Une carte graphique dont l'offre nomme un PROCESSEUR (et inversement) est un
# PC monté : « ARMOURY AMD Ryzen 7 5700 Radeon RX 9060 XT 16GB RAM 1TB SSD »
# passait pour une Radeon RX 5700 chez Canada Computers.
_AUTRE_COMPOSANT = {
    "gpus": re.compile(r"\bryzen\b|\bcore\s?(?:i[3579]|ultra)\b|\bxeon\b|\bthreadripper\b",
                       re.IGNORECASE),
    "cpus": re.compile(r"\b[rg]tx\s?\d{3,4}\b|\bradeon\s?rx\b", re.IGNORECASE),
}

_journal_verrou = threading.Lock()


def journal(message, erreur=False):
    """Écrit une ligne de log tout de suite (plusieurs fils écrivent ; une
    sortie mise en tampon était perdue quand l'étape était tuée)."""
    with _journal_verrou:
        print(message, file=sys.stderr if erreur else sys.stdout, flush=True)


def maintenant():
    """Horodatage UTC au format des fichiers de données."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _de_la_boutique(offre, domaine):
    return domaine in (offre.get("shop") or "").lower()


# ---------------------------------------------------------------------------
# Requête et correspondance
# ---------------------------------------------------------------------------

def requete_boutique(cat, it, unites="en", requete_memoire=None):
    """Requête de recherche du composant [it] pour une boutique qui écrit ses
    unités en [unites]. Mémoire vive des boutiques françaises : « DDR5 32 Go
    6000 MHz CL30 » — le « 32GB » de la requête standard ne renvoie plus rien
    chez Materiel.net depuis le 17/11/2026 (0 composant mémoire enrichi), et
    LDLC ne trouve rien sans « Go » ni « MHz ». Une source peut imposer son
    propre format mémoire ([requete_memoire], ex. Alternate)."""
    if cat == "rams" and requete_memoire is not None:
        return requete_memoire(it)
    if cat == "rams" and unites == "fr":
        return f"{it['kind']} {it['sizeGb']} Go {it['mhz']} MHz CL{it['cl']}"
    return CATEGORIES[cat][3](it)


_RE_KIT_RAM = re.compile(r"(\d+)\s*x\s*(\d+)\s*(?:go|gb)\b")
_RE_CAP_RAM = re.compile(r"(\d+)\s*(?:go|gb)\b")


def capacite_ram_go(nom):
    """Capacité TOTALE en Go d'un kit mémoire lue dans son nom : « 2 x 16 Go
    (32 Go) » → 32, « 64GB (2x32GB) » → 64, « 16 Go » → 16 ; None si absente."""
    bas = (nom or "").lower().replace("×", "x")
    kit = _RE_KIT_RAM.search(bas)
    if kit:
        return int(kit.group(1)) * int(kit.group(2))
    caps = [int(c) for c in _RE_CAP_RAM.findall(bas)]
    return max(caps) if caps else None


def capacite_ram_ok(nom, size_gb):
    """Vrai si le kit [nom] a la capacité totale [size_gb]. Le filtre par
    jetons cherchait seulement « 32go » DANS le nom : un kit « 2 x 32 Go
    (64 Go) » passait pour le 32 Go, « 2 x 16 Go (32 Go) » pour le 16 Go."""
    cap = capacite_ram_go(nom)
    return cap is None or cap == int(size_gb)


def _jeton_present(groupe, compact, mots):
    """Un groupe de jetons est présent dans un nom : par sous-chaîne s'il porte
    un chiffre (comme `correspond`) ou s'il est long, comme MOT ENTIER s'il est
    court (« co » ne doit pas se trouver dans « compatible »)."""
    for j in groupe:
        if any(ch.isdigit() for ch in j) or len(j) >= 4:
            if j in compact:
                return True
        elif j in mots:
            return True
    return False


def rivaux_specifiques(cat, items):
    """Pour chaque composant, les composants PLUS PRÉCIS du même catalogue :
    ceux dont le nom contient tous ses jetons et en ajoute (« RTX 3050 6G »
    pour la « RTX 3050 », « NH-D15S » et « NH-D15 G2 » pour le « NH-D15 »,
    « Hyper 212 Halo » pour l'« Hyper 212 »). → {id: [(nom, jetons en plus)]}"""
    t_build = CATEGORIES[cat][4]
    fiches = [(it, t_build(it), _norm(it["name"])) for it in items]
    rivaux = {}
    for a, ja, na in fiches:
        liste = []
        for b, jb, nb in fiches:
            if b["id"] == a["id"] or nb == na:
                continue
            # Un mot d'unité (« To » de « WD Black SN850X 1 To ») ne rend pas un
            # composant plus précis : la capacité est déjà contrôlée à part.
            extras = [g for g in jb if g not in ja and not set(g) <= _UNITES]
            if extras and all(g in jb or any(j in nb for j in g) for g in ja):
                liste.append((b["name"], extras))
        if liste:
            rivaux[a["id"]] = liste
    return rivaux


def appartient_a_rival(cat, nom_offre, rivaux):
    """Vrai si l'offre désigne un composant plus précis du catalogue (tous ses
    jetons en plus sont dans le nom, même suffixe de modèle)."""
    compact = _norm(nom_offre)
    mots = set(_norm_espace(nom_offre).split())
    return any(all(_jeton_present(g, compact, mots) for g in extras)
               and suffixe_ok(cat, nom_b, nom_offre)
               for nom_b, extras in rivaux)


def numeros_isoles(nom_item, nom_offre, jetons):
    """Un numéro de modèle du composant ne doit pas se prolonger par un autre
    CHIFFRE dans l'offre : `correspond` cherche une sous-chaîne, et « p3 »
    se trouvait dans « P310 » (Crucial P3 ≠ P310), « 550 » dans « 5500 »
    (RX 550 ≠ RX 5500 XT). Vérifié pour les jetons à chiffre qui sont un mot du
    nom du composant, dans l'offre écrite avec ses espaces puis collée."""
    mots_item = set(_norm_espace(nom_item).split())
    espace, compact = _norm_espace(nom_offre), _norm(nom_offre)
    for groupe in jetons:
        if len(groupe) != 1 or groupe[0] not in mots_item:
            continue
        jeton = groupe[0]
        if not any(ch.isdigit() for ch in jeton):
            continue
        motif = re.compile(rf"(?<!\d){re.escape(jeton)}(?!\d)")
        if not (motif.search(espace) or motif.search(compact)):
            return False
    return True


def modele_coherent(nom_item, nom_offre):
    """Refroidissements et boîtiers : le modèle est dans les MOTS du nom, que
    `correspond` tolère absents jusqu'à la moitié (« be quiet! Shadow Rock 3 »
    recevait des ventilateurs « be quiet! Pure Wings 3 »). Trois règles :
    - un numéro de génération à un chiffre doit figurer tel quel (« Pure Rock
      2 » ≠ « Pure Rock 3 ») ;
    - seuls les deux premiers mots (la marque) peuvent manquer ;
    - aucun mot de variante absent du composant (« Slim », « Pro », « SE »…).
    Les précisions entre parenthèses du catalogue (« Fractal Terra (ITX) ») et
    les formats de boîtier écrits en description sont ignorés."""
    nom_item = _FORMAT_BOITIER.sub(" ", re.sub(r"\([^)]*\)", " ", nom_item))
    nom_offre = _FORMAT_BOITIER.sub(" ", nom_offre)
    mots_item = _norm_espace(nom_item).split()
    mots_offre = set(_norm_espace(nom_offre).split())
    compact = _norm(nom_offre)
    for i, m in enumerate(mots_item):
        suivant = mots_item[i + 1] if i + 1 < len(mots_item) else ""
        if len(m) == 1 and m.isdigit():
            if suivant not in _UNITES and m not in mots_offre:
                return False
        elif (i >= 2 and len(m) >= 2 and not any(ch.isdigit() for ch in m)
              and not _jeton_present([m], compact, mots_offre)):
            return False
    return not ((mots_offre & _MOTS_VARIANTE) - set(mots_item))


def garder_offre(cat, it, jetons, nom_offre, rivaux=()):
    """Vrai si l'offre [nom_offre] est bien le composant [it] — toutes les
    règles de correspondance, pour une offre neuve comme pour une offre déjà
    publiée (`revalider_existant`)."""
    return (correspond(nom_offre, jetons)
            and numeros_isoles(it["name"], nom_offre, jetons)
            and suffixe_ok(cat, it["name"], nom_offre)
            and not produit_suspect(nom_offre, cat)
            and not _RE_RETOUR.search(nom_offre)
            and not (cat in _AUTRE_COMPOSANT and _AUTRE_COMPOSANT[cat].search(nom_offre))
            and (cat != "rams" or (capacite_ram_ok(nom_offre, it["sizeGb"])
                                   and not _RE_SODIMM.search(nom_offre)))
            and (cat not in CATEGORIES_MODELE_MOTS or modele_coherent(it["name"], nom_offre))
            and not appartient_a_rival(cat, nom_offre, rivaux))


def filtrer_offres(cat, it, jetons, brutes, rivaux=()):
    """Offres brutes d'UNE boutique → offres retenues : correspondance stricte,
    en stock d'abord puis prix croissant, plafond par boutique, écart aberrant."""
    gardees = sorted([o for o in brutes if garder_offre(cat, it, jetons, o["product"], rivaux)],
                     key=lambda o: (not o.get("in_stock", True), o["price"]))
    return sans_aberrantes(gardees[:OFFRES_PAR_BOUTIQUE])


def sans_achat(it):
    """Composant qui ne s'achète pas (« Refroidisseur stock », livré avec le
    processeur, prix 0) : jamais recherché — sa recherche ramenait n'importe
    quel ventirad « Refroidisseur CPU »."""
    return "price" in it and not it["price"]


# ---------------------------------------------------------------------------
# Fusion dans les fichiers par composant
# ---------------------------------------------------------------------------

def fusionner_offres(entree, domaine, nouvelles, quand, domaines_directs):
    """REMPLACE les offres de la boutique [domaine] de [entree] par
    [nouvelles] (offres brutes déjà filtrées), en gardant celles des autres
    sources. Une nouvelle offre à plus de 2,5× la moins chère des boutiques
    directes (celles déjà publiées comprises) est écartée. Recalcule
    `priceMin`, **null** quand il ne reste aucune offre (même règle que
    `nettoyer_offres` : l'entrée reste, son image sert de repli, et l'app garde
    le prix de son catalogue). → (nombre d'offres ajoutées, nombre d'anciennes
    offres de la boutique retirées)."""
    anciennes = entree.get("prices") or []
    autres = [p for p in anciennes if not _de_la_boutique(p, domaine)]
    retirees = len(anciennes) - len(autres)
    if not nouvelles and not retirees:  # rien à changer
        return 0, 0
    directes = [p for p in autres if any(_de_la_boutique(p, d) for d in domaines_directs)]
    if nouvelles and directes:
        survivantes = {id(o) for o in sans_aberrantes(list(nouvelles) + directes)}
        nouvelles = [o for o in nouvelles if id(o) in survivantes]
    offres = [{"shop": o["shop"], "price": o["price"],
               "currency": o.get("currency", "EUR"),
               "url": o["url"], "inStock": bool(o.get("in_stock", True)),
               "lastSeen": quand, "product": o["product"],
               "image": o.get("image") or None}
              for o in nouvelles]
    prices = autres + offres
    entree["prices"] = prices
    entree["priceMin"] = _prix_min(prices) if prices else None
    entree["lastUpdated"] = quand
    return len(offres), retirees


def revalider_existant(cat, items, existant, rivaux, domaines_directs):
    """Repasse les offres DÉJÀ publiées par les boutiques directes dans les
    filtres actuels (sans réseau) : une offre qu'un passage n'accepterait plus
    est retirée tout de suite, sans attendre que sa boutique soit revisitée
    (Morele et Canada Computers, bloqués sur les serveurs de GitHub, ne le
    sont presque jamais). Les offres des autres sources (Amazon, Newegg) ne
    sont pas touchées. → (offres retirées, composants restés sans offre)."""
    t_build = CATEGORIES[cat][4]
    retirees = vides = 0
    for it in items:
        e = existant.get(it["id"])
        if not e or not e.get("prices"):
            continue
        jetons = t_build(it)
        rv = rivaux.get(it["id"], ())
        gardees = [p for p in e["prices"]
                   if not any(_de_la_boutique(p, d) for d in domaines_directs)
                   or (not sans_achat(it)
                       and garder_offre(cat, it, jetons, p.get("product") or "", rv))]
        if len(gardees) == len(e["prices"]):
            continue
        retirees += len(e["prices"]) - len(gardees)
        e["prices"] = gardees
        e["priceMin"] = _prix_min(gardees) if gardees else None
        vides += not gardees
    return retirees, vides


def ecrire_json(chemin, donnees, **options):
    """Écriture ATOMIQUE (fichier temporaire puis remplacement) : un arrêt
    brutal pendant l'écriture laisse l'ancien fichier, jamais un JSON tronqué."""
    tmp = chemin + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(donnees, f, ensure_ascii=False, indent=1, **options)
        os.replace(tmp, chemin)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def charger_etat(chemin):
    """État de la rotation : {"version": 1, "visites": {boutique: {"cat/id":
    horodatage de la dernière visite réussie}}} (vide si absent ou illisible)."""
    try:
        with open(chemin, encoding="utf-8") as f:
            etat = json.load(f)
    except (OSError, ValueError):
        etat = {}
    if not isinstance(etat, dict) or not isinstance(etat.get("visites"), dict):
        etat = {"version": 1, "visites": {}}
    return etat


def ordre_rotation(taches, boutique, domaine, visites, existants):
    """Ordre de passage d'une boutique : le composant visité le plus
    anciennement d'abord (jamais visité = tout premier), à égalité l'ordre du
    catalogue. Sans état connu, la date de visite est celle de la plus récente
    offre publiée de la boutique (`lastSeen`). [taches] : (rang de catégorie,
    rang dans la catégorie, cat, composant)."""
    vues = visites.get(boutique, {})

    def cle(tache):
        rang_cat, rang_it, cat, it = tache
        vu = vues.get(f"{cat}/{it['id']}")
        if vu is None:
            e = existants.get(cat, {}).get(it["id"]) or {}
            dates = [p.get("lastSeen") or "" for p in e.get("prices") or []
                     if _de_la_boutique(p, domaine)]
            vu = max(dates) if dates else ""
        return (vu, rang_cat, rang_it)

    return sorted(taches, key=cle)


class Collecte:
    """État partagé du passage : fichiers par composant en mémoire, état de la
    rotation et statistiques, sous UN verrou ; sauvegarde incrémentale."""

    def __init__(self, data_repo, chemin_etat, quand):
        self.data_repo = data_repo
        self.chemin_etat = chemin_etat
        self.quand = quand
        self.verrou = threading.Lock()
        self.fichiers = {}  # cat → (chemin, {id: entrée})
        self.stats = {}  # cat → Counter
        self.maj = {}  # cat → ids modifiés
        self.sales = set()
        self.etat = charger_etat(chemin_etat)
        self.etat_sale = False
        self.derniere_sauvegarde = time.time()
        self.fermee = False

    def charger(self, cat):
        """Charge le fichier de sortie de [cat] (vide s'il n'existe pas)."""
        chemin = os.path.join(self.data_repo, SORTIES.get(cat, f"{cat}.json"))
        if os.path.exists(chemin + ".tmp"):  # reste d'un arrêt brutal
            os.remove(chemin + ".tmp")
        try:
            with open(chemin, encoding="utf-8") as f:
                existant = {e["id"]: e for e in json.load(f)}
        except (OSError, ValueError):
            existant = {}
        self.fichiers[cat] = (chemin, existant)
        self.stats[cat] = Counter()
        self.maj[cat] = set()
        return existant

    def existants(self):
        return {cat: ex for cat, (_c, ex) in self.fichiers.items()}

    def marquer_sale(self, cat):
        with self.verrou:
            self.sales.add(cat)

    def appliquer(self, boutique, domaine, cat, it, nouvelles, domaines_directs):
        """Visite RÉUSSIE de [boutique] pour [it] : remplace ses offres, note la
        visite. → (ajoutées, retirées, ajoutées en stock, besoin d'image) ou
        None si le passage est clos."""
        with self.verrou:
            if self.fermee:
                return None
            _chemin, existant = self.fichiers[cat]
            stats = self.stats[cat]
            e = existant.get(it["id"])
            ajoutees = retirees = dispo = 0
            besoin_image = False
            if e is None and nouvelles:
                e = {"id": it["id"], "name": it["name"], "priceMin": None,
                     "prices": [], "image": None, "lastUpdated": self.quand}
                existant[it["id"]] = e
            if e is not None:
                ajoutees, retirees = fusionner_offres(
                    e, domaine, nouvelles, self.quand, domaines_directs)
                besoin_image = not e.get("image")
                dispo = sum(1 for p in e["prices"] if _de_la_boutique(p, domaine)
                            and p.get("lastSeen") == self.quand and p.get("inStock"))
                if ajoutees or retirees:
                    self.sales.add(cat)
                    self.maj[cat].add(it["id"])
                    stats["vides"] += not e["prices"]
            stats["ajoutees"] += ajoutees
            stats["retirees"] += retirees
            self._noter_visite(boutique, cat, it["id"])
            self._enregistrer()
            return ajoutees, retirees, dispo, besoin_image

    def marquer_visite(self, boutique, cat, ident):
        """Visite sans lecture possible mais sans blocage (404) : notée pour la
        rotation, aucune offre touchée."""
        with self.verrou:
            if not self.fermee:
                self._noter_visite(boutique, cat, ident)

    def poser_image(self, cat, ident, chemin_rel):
        with self.verrou:
            e = self.fichiers[cat][1].get(ident)
            if self.fermee or e is None or e.get("image"):
                return False
            e["image"] = chemin_rel
            self.sales.add(cat)
            self.stats[cat]["images"] += 1
            return True

    def _noter_visite(self, boutique, cat, ident):
        self.etat["visites"].setdefault(boutique, {})[f"{cat}/{ident}"] = maintenant()
        self.etat_sale = True

    def _enregistrer(self, force=False):
        if not force and time.time() - self.derniere_sauvegarde < SAUVEGARDE_S:
            return
        for cat in sorted(self.sales):
            chemin, existant = self.fichiers[cat]
            ecrire_json(chemin, list(existant.values()))
        self.sales.clear()
        if self.etat_sale:
            ecrire_json(self.chemin_etat, self.etat, sort_keys=True)
            self.etat_sale = False
        self.derniere_sauvegarde = time.time()

    def fermer(self):
        """Écriture finale de tout ce qui a changé ; plus aucune modification
        acceptée ensuite (un fil encore en vol est ignoré)."""
        with self.verrou:
            self._enregistrer(force=True)
            self.fermee = True


# ---------------------------------------------------------------------------
# Un fil par boutique
# ---------------------------------------------------------------------------

class Pilotage:
    """Commandes partagées par les fils : échéance du budget, marge, nombre de
    boutiques interrogées en même temps, signal d'arrêt, données de filtrage."""

    def __init__(self, echeance, marge, paralleles, domaines_directs, rivaux):
        self.echeance = echeance
        self.marge = marge
        self.semaphore = threading.BoundedSemaphore(max(1, paralleles))
        self.arret = threading.Event()
        self.domaines_directs = domaines_directs
        self.rivaux = rivaux

    def temps_restant(self):
        return float("inf") if self.echeance is None else self.echeance - time.time()

    def peut_commencer(self):
        """Un nouveau composant peut-il être commencé ?"""
        return not self.arret.is_set() and self.temps_restant() > self.marge

    def attendre(self, secondes):
        """Pause interrompue par l'arrêt et bornée par l'échéance. → vrai si le
        passage peut continuer."""
        self.arret.wait(min(secondes, max(0.0, self.temps_restant() - self.marge)))
        return self.peut_commencer()


def nouveau_recap():
    return {"visites": 0, "avec_offres": 0, "sans_correspondance": 0,
            "offres": 0, "retirees": 0, "images": 0, "echecs": Counter(),
            "coupee": False, "reprises": 0, "a_faire": 0, "traites": 0}


def _motif_echec(e):
    """Libellé court d'un échec pour le récapitulatif."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    if isinstance(e, PageNonReconnue):
        return "page non reconnue"
    if isinstance(e, requests.Timeout):
        return "délai dépassé"
    if isinstance(e, requests.ConnectionError):
        return "connexion"
    return type(e).__name__


def parcourir_boutique(nom, src, taches, collecte, recap, pilote):
    """Fil d'UNE boutique : visite les composants dans l'ordre de la rotation,
    à sa propre cadence, jusqu'à la fin de la liste, l'échéance ou la coupure.

    COUPE-CIRCUIT : une boutique qui bloque les robots (403, 429, connexion
    coupée) échoue sur CHAQUE composant ; ce seul entêtement faisait durer
    l'étape plus de deux heures en septembre 2026. Après SEUIL_COUPURE échecs
    d'affilée (avec une pause croissante entre eux), la boutique se met en
    pause PAUSE_REPRISE_S, puis a une seconde chance : un nouvel échec la
    coupe pour le reste du passage."""
    src.echeance = None if pilote.echeance is None else pilote.echeance - pilote.marge
    recap["a_faire"] = len(taches)
    echecs_suivis = 0
    for _rc, _ri, cat, it in taches:
        if not pilote.peut_commencer():
            break
        recap["traites"] += 1
        requete = requete_boutique(cat, it, getattr(src, "unites", "en"),
                                   getattr(src, "requete_memoire", None))
        try:
            with pilote.semaphore:
                brutes = src.offres(requete)
                # Repli : la recherche échoue souvent sur la MARQUE (souvent
                # omise) → nouvelle tentative sans le 1er mot.
                if not brutes and len(requete.split()) > 2:
                    brutes = src.offres(" ".join(requete.split()[1:]))
        except Exception as e:  # noqa: BLE001 — une requête ratée ne tue pas le passage
            motif = _motif_echec(e)
            recap["echecs"][motif] += 1
            journal(f"  ! {nom} {cat}/{it['id']}: {e}", erreur=True)
            if motif == "HTTP 404":
                # Page absente : rien à lire mais pas un blocage. Visite notée
                # (la rotation avance), anciennes offres gardées.
                collecte.marquer_visite(nom, cat, it["id"])
                continue
            echecs_suivis += 1
            if echecs_suivis >= SEUIL_COUPURE:
                if recap["reprises"] >= REPRISES_MAX:
                    recap["coupee"] = True
                    journal(f"  ✂ {nom} ignorée pour la fin du passage : {SEUIL_COUPURE} échecs "
                            f"d'affilée (blocage anti-robot probable)", erreur=True)
                    break
                recap["reprises"] += 1
                journal(f"  ✂ {nom} en pause {PAUSE_REPRISE_S // 60} min après {SEUIL_COUPURE} "
                        f"échecs d'affilée (blocage anti-robot probable), puis seconde chance",
                        erreur=True)
                echecs_suivis = SEUIL_COUPURE - 1
                if not pilote.attendre(PAUSE_REPRISE_S):
                    break
                continue
            if not pilote.attendre(min(PAUSE_ECHEC_MAX_S, PAUSE_ECHEC_S * echecs_suivis)):
                break
            continue
        echecs_suivis = 0
        try:
            jetons = CATEGORIES[cat][4](it)
            nouvelles = filtrer_offres(cat, it, jetons, brutes,
                                       pilote.rivaux.get(cat, {}).get(it["id"], ()))
            resultat = collecte.appliquer(nom, src.domaine, cat, it, nouvelles,
                                          pilote.domaines_directs)
        except Exception as e:  # noqa: BLE001 — un composant mal formé ne tue pas le fil
            recap["echecs"]["erreur interne"] += 1
            journal(f"  ! {nom} {cat}/{it['id']}: erreur interne {type(e).__name__}: {e}",
                    erreur=True)
            continue
        if resultat is None:  # passage clos pendant la requête
            break
        ajoutees, retirees, dispo, besoin_image = resultat
        recap["visites"] += 1
        recap["offres"] += ajoutees
        recap["retirees"] += retirees
        if ajoutees:
            recap["avec_offres"] += 1
            journal(f"  + {nom} {cat}/{it['id']}: {ajoutees} offres ({dispo} en stock)")
        else:
            recap["sans_correspondance"] += 1
            if retirees:
                journal(f"  - {nom} {cat}/{it['id']}: {retirees} ancienne(s) offre(s) "
                        f"retirée(s), plus trouvée(s)")
        # Complétion d'IMAGE (lot 27, item 4) : si l'entrée n'a pas de photo,
        # on télécharge celle du meilleur modèle LDLC en haute résolution
        # (media.ldlc.com : /r150/ → /r1600/).
        if besoin_image:
            for o in nouvelles:
                if o["shop"] == "ldlc.com" and o.get("image"):
                    grande = o["image"].replace("/r150/", "/r1600/")
                    rel = telecharger_image(
                        src, grande, os.path.join(collecte.data_repo, "images", f"{cat}-{it['id']}"))
                    if rel and collecte.poser_image(
                            cat, it["id"],
                            os.path.relpath(rel, collecte.data_repo).replace(os.sep, "/")):
                        recap["images"] += 1
                    break


def afficher_recapitulatif(sources, recaps, collecte):
    """Récapitulatif de fin de log : par boutique (requêtes, correspondances,
    échecs HTTP, état) puis par catégorie."""
    journal("Récapitulatif par boutique :")
    for nom, src in sources:
        r = recaps[nom]
        echecs = ", ".join(f"{m} ×{n}" for m, n in r["echecs"].most_common()) or "aucun"
        restants = r["a_faire"] - r["traites"]
        if r["coupee"]:
            fin = "coupée (blocage probable)"
        elif restants:
            fin = "arrêtée par le budget"
        else:
            fin = "terminée"
        if r["reprises"] and not r["coupee"]:
            fin += " après une pause"
        journal(f"  {nom}: {src.nb_requetes} requêtes, {r['visites']} composants lus "
                f"({r['avec_offres']} avec correspondance, {r['sans_correspondance']} sans), "
                f"{r['offres']} offres ajoutées, {r['retirees']} retirées, {r['images']} images ; "
                f"échecs : {echecs} ; {fin}, {restants} composant(s) non visité(s) "
                f"sur {r['a_faire']}")
    for cat, (chemin, _ex) in collecte.fichiers.items():
        s = collecte.stats[cat]
        journal(f"shops {cat}: {len(collecte.maj[cat])} composants mis à jour, "
                f"{s['ajoutees']} offres ajoutées, {s['retirees']} retirées, "
                f"{s['vides']} composants restés sans offre, {s['images']} images "
                f"ajoutées -> {os.path.basename(chemin)}")


def principal(argv=None):
    debut = time.time()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--shops", default=",".join(BOUTIQUES))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--items-dir", default=os.path.join(RACINE, "assets", "data"))
    ap.add_argument("--budget-min", type=float, default=0,
                    help="durée maximale du passage en minutes, écriture comprise "
                         "(0 = sans limite)")
    ap.add_argument("--paralleles", type=int, default=0,
                    help="boutiques interrogées en même temps (défaut : toutes)")
    ap.add_argument("--etat", default="",
                    help=f"fichier d'état de la rotation (défaut : <data-repo>/{FICHIER_ETAT})")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # sortie remplacée (tests)
        pass
    seules = {c.strip() for c in args.only.split(",") if c.strip()}
    demandees = [b.strip() for b in args.shops.split(",") if b.strip()]
    for b in demandees:
        if b not in BOUTIQUES:
            journal(f"boutique inconnue ignorée : {b}", erreur=True)
    boutiques = [b for b in demandees if b in BOUTIQUES]
    quand = maintenant()
    budget_s = max(0.0, args.budget_min * 60)
    echeance = debut + budget_s if budget_s else None
    marge = min(MARGE_MAX_S, budget_s * 0.25)

    collecte = Collecte(args.data_repo, args.etat or os.path.join(args.data_repo, FICHIER_ETAT),
                        quand)
    domaines_directs = tuple(fabrique().domaine for fabrique in BOUTIQUES.values())
    taches, rivaux = [], {}
    for rang_cat, (cat, (fichier, _cat_id, _slug, _q, _t)) in enumerate(CATEGORIES.items()):
        if seules and cat not in seules:
            continue
        try:
            with open(os.path.join(args.items_dir, f"{fichier}.json"), encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, ValueError) as e:
            journal(f"shops {cat}: liste de composants illisible ({e}), catégorie ignorée",
                    erreur=True)
            continue
        rivaux[cat] = rivaux_specifiques(cat, items)
        existant = collecte.charger(cat)
        retirees, vides = revalider_existant(cat, items, existant, rivaux[cat],
                                             domaines_directs)
        if retirees:
            collecte.marquer_sale(cat)
            collecte.stats[cat]["retirees"] += retirees
            collecte.stats[cat]["vides"] += vides
            journal(f"shops {cat}: {retirees} offre(s) déjà publiée(s) écartée(s) par les filtres "
                    f"actuels ({vides} composant(s) resté(s) sans offre)")
        if args.limit:
            items = items[: args.limit]
        taches.extend((rang_cat, rang_it, cat, it) for rang_it, it in enumerate(items)
                      if not sans_achat(it))

    sources = [(b, BOUTIQUES[b]()) for b in boutiques]
    paralleles = args.paralleles if 0 < args.paralleles < len(sources) else len(sources)
    pilote = Pilotage(echeance, marge, paralleles, domaines_directs, rivaux)
    recaps = {b: nouveau_recap() for b, _src in sources}
    existants = collecte.existants()
    fils = [threading.Thread(
        target=parcourir_boutique,
        args=(b, src, ordre_rotation(taches, b, src.domaine, collecte.etat["visites"], existants),
              collecte, recaps[b], pilote),
        name=f"boutique-{b}", daemon=True) for b, src in sources]
    journal(f"shops : {len(taches)} composants × {len(sources)} boutiques, "
            f"{paralleles} en même temps"
            + (f", budget {args.budget_min:g} min" if echeance else ", sans budget"))

    # SIGTERM (arrêt demandé par le runner) → même sortie propre que Ctrl+C.
    def _sur_signal(_signum, _frame):
        raise KeyboardInterrupt
    try:
        ancien = signal.signal(signal.SIGTERM, _sur_signal)
    except ValueError:  # hors du fil principal
        ancien = None

    for t in fils:
        t.start()
    garde = min(10.0, budget_s * 0.05)
    try:
        while True:
            vivants = [t for t in fils if t.is_alive()]
            if not vivants:
                break
            if echeance is not None and time.time() > echeance - garde:
                journal("shops : échéance du budget, arrêt des fils et écriture", erreur=True)
                break
            vivants[0].join(0.5)
    except KeyboardInterrupt:
        journal("shops : interruption, écriture de ce qui a été collecté", erreur=True)
    finally:
        pilote.arret.set()
        # Quelques secondes pour finir le composant en cours, sans dépasser
        # l'échéance ; un fil encore en vol ensuite est ignoré (`fermer`).
        attente = 30.0 if echeance is None else min(5.0, max(0.0, echeance - time.time() - 1))
        fin = time.time() + attente
        for t in fils:
            t.join(max(0.0, fin - time.time()))
        collecte.fermer()
        if ancien is not None:
            signal.signal(signal.SIGTERM, ancien)
    afficher_recapitulatif(sources, recaps, collecte)
    journal(f"shops : terminé en {(time.time() - debut) / 60:.1f} min")


if __name__ == "__main__":
    principal()
