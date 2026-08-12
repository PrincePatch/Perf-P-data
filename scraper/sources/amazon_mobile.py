# -*- coding: utf-8 -*-
"""Recherche Amazon par le rendu MOBILE (`/gp/aw/s`).

Le rendu de bureau (`/s?k=…`) répond 202 avec une page vide aux clients
automatisés ; le rendu mobile, lui, répond normalement. Il fournit pour chaque
résultat l'ASIN (`data-asin`) et le libellé complet du produit (`aria-label`),
c'est-à-dire exactement ce qu'il faut pour construire un lien
`/dp/<ASIN>` sûr, sans passer par un moteur de recherche tiers.

Ce module ne récupère **ni prix ni stock** : uniquement de quoi identifier la
fiche produit d'un composant. Les prix Amazon viendront de la PA-API
(`sources/amazon_paapi.py`) dès que les clés seront délivrées — ce module n'a
alors plus qu'un rôle de repli.

Politesse : un délai minimal entre deux requêtes (`rate_limit`), un seul
processus, aucun parallélisme.
"""

import re
import time
from html import unescape
from urllib.parse import quote_plus

import requests

UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

# Langue d'en-tête par marketplace : le libellé des résultats la suit, et nos
# filtres de correspondance sont indifférents à la langue (jetons techniques).
LANGUES = {
    "amazon.fr": "fr-FR,fr;q=0.9", "amazon.de": "de-DE,de;q=0.9",
    "amazon.es": "es-ES,es;q=0.9", "amazon.it": "it-IT,it;q=0.9",
    "amazon.co.uk": "en-GB,en;q=0.9", "amazon.com": "en-US,en;q=0.9",
    "amazon.com.au": "en-AU,en;q=0.9",
}

_BLOC = re.compile(r'data-asin="(B[A-Z0-9]{9})"')
_LABEL = re.compile(r'aria-label="([^"]{12,400})"')
_ALT = re.compile(r'alt="([^"]{12,400})"')
_TITRE_PAGE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
# Queue/entête « … : Amazon.fr: Informatique » / « Amazon.com: … ».
_QUEUE_PAGE = re.compile(r"\s*[:|-]\s*amazon\..*$", re.I)
_TETE_PAGE = re.compile(r"^\s*amazon(\.[a-z.]+)?\s*[:|-]\s*", re.I)
# « Accédez à la page détaillée pour « X ». … » / « View details for X » : on ne
# garde que X.
_ENROBAGE = re.compile(
    r'^(?:acc[ée]dez? [^«]*«\s*|view details? for\s*|zur detailseite[^„«]*[„«]\s*'
    r'|ver detalles de\s*|vai alla pagina[^«]*«\s*)', re.I)
_FIN = re.compile(r'\s*[»”"]\s*\..*$|\s*\.\s*(?:non\s+)?[ée]ligible.*$', re.I)


# Libellés d'INTERFACE que porte aussi un `aria-label` : ils ne décrivent pas
# le produit et doivent être écartés, sinon le composant serait « validé » sur
# une phrase de navigation.
_UI = re.compile(
    r"^(plus d.articles|articles similaires|ähnliche produkte|similar items?"
    r"|art[íi]culos similares|articoli simili|voir plus|see more|mehr anzeigen"
    r"|sponsoris|sponsored|page suivante|next page|ajouter au panier"
    r"|add to cart|in den einkaufswagen|r[ée]sultat|results?)", re.I)


def _titre(bloc):
    """Libellé produit le plus complet trouvé dans un bloc de résultat.

    On retient le PLUS LONG candidat non ambigu : un titre de produit fait
    plusieurs dizaines de caractères là où les libellés d'interface sont courts
    et répétitifs.
    """
    meilleur = ""
    for c in _LABEL.findall(bloc) + _ALT.findall(bloc):
        t = _FIN.sub("", _ENROBAGE.sub("", unescape(c))).strip(" «».\"")
        if len(t) < 15 or _UI.match(t) or re.match(r"^[\d,.\s/]+$", t):
            continue
        if len(t) > len(meilleur):
            meilleur = t
    return meilleur


class AmazonMobileSource:
    """Résolution d'ASIN par la recherche mobile d'une marketplace."""

    def __init__(self, domaine="amazon.fr", rate_limit=1.0, timeout=25):
        self.domaine = domaine
        self.rate_limit = rate_limit
        self.timeout = timeout
        self._dernier = 0.0
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA_MOBILE,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": LANGUES.get(domaine, "en-US,en;q=0.9"),
        })

    def _attendre(self):
        delta = time.time() - self._dernier
        if delta < self.rate_limit:
            time.sleep(self.rate_limit - delta)
        self._dernier = time.time()

    def resultats(self, requete, maxi=20, essais=1):
        """[(asin, libellé)] pour [requete], dans l'ordre d'affichage."""
        url = f"https://www.{self.domaine}/gp/aw/s?k={quote_plus(requete)}"
        html = ""
        for n in range(essais):
            self._attendre()
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.RequestException:
                time.sleep(2 * (n + 1))
                continue
            # 202/503 ou page tronquée = page d'attente anti-robot.
            if r.status_code == 200 and len(r.text) >= 20_000:
                html = r.text
                break
            time.sleep(2 * (n + 1))
        if not html:
            return []
        out, vus = [], set()
        positions = [(m.group(1), m.end()) for m in _BLOC.finditer(html)]
        for i, (asin, fin) in enumerate(positions):
            if asin in vus:
                continue
            vus.add(asin)
            # Le libellé qui suit un `data-asin` reste INDICATIF : la page
            # mobile imbrique carrousels sponsorisés et vrais résultats, et
            # l'association titre↔ASIN n'y est pas fiable. La preuve vient de
            # [fiche], qui lit la page du produit lui-même.
            borne = positions[i + 1][1] if i + 1 < len(positions) else len(html)
            out.append((asin, _titre(html[fin:min(borne, fin + 6000)])))
            if len(out) >= maxi:
                break
        return out

    def asins(self, requete, maxi=8):
        """ASIN des résultats de [requete], dans l'ordre de pertinence."""
        return [a for a, _ in self.resultats(requete, maxi=maxi)]

    def fiche(self, asin, essais=2):
        """(état, titre) de la FICHE mobile — la seule source qui fait foi.

        `ok` = titre réel du produit · `absent` = 404, l'ASIN n'existe pas sur
        cette marketplace · `bloque` = page d'attente anti-robot (à ne surtout
        pas confondre avec un rejet : ce serait perdre de bons produits).
        """
        etat, titre, _ = self.fiche_complete(asin, essais)
        return etat, titre

    def fiche_complete(self, asin, essais=2):
        """(état, titre, HTML) — variante qui rend aussi la page, pour en tirer
        le prix et la disponibilité sans la retélécharger."""
        url = f"https://www.{self.domaine}/gp/aw/d/{asin}"
        for n in range(essais):
            self._attendre()
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.RequestException:
                time.sleep(1.5 * (n + 1))
                continue
            if r.status_code == 404:
                return "absent", "", ""
            if r.status_code == 200:
                m = _TITRE_PAGE.search(r.text)
                t = re.sub(r"\s+", " ", unescape(m.group(1))).strip() if m else ""
                t = _QUEUE_PAGE.sub("", _TETE_PAGE.sub("", t)).strip()
                if len(t) >= 15:
                    return "ok", t, r.text
            time.sleep(1.5 * (n + 1))
        return "bloque", "", ""
