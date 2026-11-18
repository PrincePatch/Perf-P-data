# -*- coding: utf-8 -*-
"""Source de prix + MODÈLES + IMAGES : LDLC (ldlc.com), grand configurateur/
catalogue PC français (lot 27, item 4). Complète gputracker.eu et Newegg par
une 2e source EUROPÉENNE indépendante — LDLC référence quasiment tous les
modèles achetables (variantes AIB, kits RAM, SSD, cartes mères, alims,
boîtiers) avec leur PHOTO et leur DISPONIBILITÉ réelle. Objectif : plus de
« prix vérifiés » (couverture) et complétion des images/modèles manquants.

Pages de recherche rendues côté serveur (`/recherche/<requête>/`), parsées avec
BeautifulSoup — même pile `requests` + `bs4` que le reste du scraper, polie
(User-Agent navigateur + limitation de débit).

Renvoie des offres au schéma commun de l'enrichissement, avec la DISPONIBILITÉ
réelle (`in_stock`) et la devise EUR :
    {"shop": "ldlc.com", "price": float, "currency": "EUR", "url": str,
     "product": str, "image": str|None, "in_stock": bool}

Trois issues possibles pour une recherche (diagnostic du 18/11/2026) :
- une page de résultats (`li.pdt-item`) ;
- une FICHE PRODUIT : quand la recherche n'a qu'un résultat, LDLC redirige
  (301) vers `/fiche/…` — « Noctua NH-U12A » arrivait ainsi sur sa fiche et
  l'offre était perdue. La fiche est lue par ses données structurées
  schema.org (`offre_fiche_produit`) ;
- une page « 0 résultat » explicite (liste vide, sans erreur).
Toute autre page (défi anti-robot, nouvelle mise en page) lève
`PageNonReconnue` : l'appelant la traite comme un échec et ne retire AUCUNE
ancienne offre sur la foi d'une page qu'il ne sait pas lire.
"""

import html
import json
import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Délais HTTP (connexion, lecture) : une boutique qui coupe la connexion (IP
# bloquée) ne fait plus perdre 25 s avant l'échec de connexion.
DELAIS_HTTP = (10, 25)

# Mots signalant une INDISPONIBILITÉ dans le libellé de stock LDLC (le reste =
# disponible ; l'app suppose « en stock » par défaut, on ne bloque que les
# ruptures explicites).
_RUPTURE = ("rupture", "epuise", "indisponible", "bientot dispo",
            "sur commande", "precommande", "non disponible")

# Page « aucun résultat » des boutiques francophones (LDLC, Materiel.net :
# « 0 résultat pour "…" » ; Alternate : « votre recherche n'a donné aucun
# résultat »).
AUCUN_RESULTAT_FR = re.compile(r"(?<!\d)0 r[ée]sultat|aucun r[ée]sultat|aucun produit pour",
                               re.IGNORECASE)


class PageNonReconnue(Exception):
    """La page reçue n'est ni une liste de résultats, ni une fiche produit, ni
    une page « aucun résultat » explicite : défi anti-robot ou nouvelle mise
    en page. Compte comme un échec (jamais comme « plus d'offre »)."""


def nettoyer_requete(requete):
    """Requête utilisable DANS LE CHEMIN d'une URL (`/recherche/<requête>/`) :
    un « / » y devenait un séparateur de chemin → 404 (« ASRock Z790
    PG-ITX/TB4 », « Disque dur 7200 tr/min » sur Materiel.net, 18/11/2026).
    Remplacé par une espace ; le filtre de correspondance reste celui du nom."""
    return re.sub(r"\s+", " ", requete.replace("/", " ")).strip()


def aucun_resultat(soup, motif=AUCUN_RESULTAT_FR):
    """Vrai si la page annonce explicitement qu'il n'y a aucun résultat."""
    return bool(motif.search(soup.get_text(" ", strip=True)))


def _schema_produit(soup):
    """Objet schema.org `Product` des données structurées JSON-LD, ou None."""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "", strict=False)
        except ValueError:
            continue
        pile = data if isinstance(data, list) else [data]
        while pile:
            obj = pile.pop(0)
            if not isinstance(obj, dict):
                continue
            pile.extend(obj.get("@graph") or [])
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" in types:
                return obj
    return None


# Disponibilités schema.org considérées comme « en stock ».
_DISPO_SCHEMA = ("instock", "limitedavailability", "onlineonly", "instoreonly")


def offre_fiche_produit(soup, domaine, devise, url):
    """Offre d'une FICHE PRODUIT lue dans ses données structurées schema.org
    (nom, prix, disponibilité, état, photo). Renvoie None si la page n'est pas
    une fiche produit, [] si la fiche n'est pas exploitable (pas de prix, autre
    devise, article d'occasion / reconditionné), sinon une liste d'UNE offre."""
    produit = _schema_produit(soup)
    if produit is None:
        return None
    offre = produit.get("offers") or {}
    if isinstance(offre, list):
        offre = next((o for o in offre if isinstance(o, dict) and o.get("price")), {})
    nom = html.unescape(str(produit.get("name") or "")).strip()
    try:
        prix = round(float(str(offre.get("price", "")).replace(",", ".")), 2)
    except ValueError:
        return []
    if not nom or prix <= 0 or (offre.get("priceCurrency") or devise) != devise:
        return []
    etat = str(offre.get("itemCondition") or "NewCondition")
    if not etat.endswith("NewCondition"):
        return []
    dispo = str(offre.get("availability") or "InStock").rsplit("/", 1)[-1].lower()
    image = produit.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    canonique = soup.select_one('link[rel="canonical"][href]')
    lien = canonique["href"] if canonique else url
    return [{"shop": domaine, "price": prix, "currency": devise, "url": lien,
             "product": nom, "image": (image or None), "in_stock": dispo in _DISPO_SCHEMA}]


class LdlcSource:
    """Client LDLC poli (limitation de débit, UA navigateur)."""

    # Unités écrites par la boutique (« 32 Go ») : pilote la requête mémoire
    # vive d'enrich_shops (`requete_boutique`).
    unites = "fr"

    def __init__(self, rate_limit=1.5, domaine="ldlc.com", devise="EUR"):
        self.domaine = domaine
        self.devise = devise
        self.base_url = f"https://www.{domaine}"
        self._rate = rate_limit
        self._dernier = 0.0
        # Compteur de requêtes HTTP réellement envoyées (récapitulatif) et URL
        # finale de la dernière (après redirection vers une fiche produit).
        self.nb_requetes = 0
        self.url_finale = ""
        self._sess = requests.Session()
        self._sess.headers.update({
            "User-Agent": _UA,
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        })

    def _get(self, url):
        ecart = time.time() - self._dernier
        if ecart < self._rate:
            time.sleep(self._rate - ecart)
        self.nb_requetes += 1
        r = self._sess.get(url, timeout=DELAIS_HTTP)
        self._dernier = time.time()
        self.url_finale = r.url or url
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    def offres(self, requete):
        """Offres LDLC pour la recherche textuelle [requete] (produits de la
        1re page de résultats, disponibilité et prix réels)."""
        url = f"{self.base_url}/recherche/{quote(nettoyer_requete(requete))}/"
        soup = self._get(url)
        produits = soup.select("li.pdt-item")
        if not produits:
            if "/recherche/" not in self.url_finale:
                fiche = offre_fiche_produit(soup, self.domaine, self.devise, self.url_finale)
                if fiche is not None:
                    return fiche
            if aucun_resultat(soup):
                return []
            raise PageNonReconnue(f"page non reconnue : {self.url_finale}")
        out = []
        for li in produits:
            a = li.select_one("h3.title-3 a")
            if not a:
                continue
            nom = a.get_text(strip=True)
            href = (a.get("href") or "").strip()
            if not nom or not href:
                continue
            lien = href if href.startswith("http") else self.base_url + href
            prix = _prix_ldlc(li.select_one(".price"))
            if prix is None or prix <= 0:
                continue
            img = li.select_one(".pic img")
            image = None
            if img:
                src = (img.get("src") or img.get("data-src") or "").strip()
                # Ignore le placeholder « pas de photo ».
                if src and "no-photo" not in src:
                    image = src
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": lien, "product": nom, "image": image,
                        "in_stock": _en_stock(li)})
        return out


def _prix_ldlc(node):
    """Prix (float EUR) d'un bloc `.price` LDLC. Format listing : « 749€ 95 »
    (euros avant le €, centimes après) ; gère aussi « 1 099€ 95 » (séparateur
    de milliers) et le repli « 1099,95 € »."""
    if node is None:
        return None
    t = node.get_text(" ", strip=True).replace("\xa0", " ")
    if "€" in t:
        gauche, _, droite = t.partition("€")
        cents = re.sub(r"\D", "", droite)
        if len(cents) >= 2:  # « 749€ 95 » → centimes à droite du €
            euros = re.sub(r"\D", "", gauche)
            return round(int(euros) + int(cents[:2]) / 100, 2) if euros else None
        # « 1099,95 € » → tout à gauche du €
        g = re.sub(r"[^\d.]", "", gauche.replace(" ", "").replace(",", "."))
        try:
            return round(float(g), 2) if g else None
        except ValueError:
            return None
    # Dernier repli : premier nombre décimal du texte.
    m = re.search(r"(\d[\d ]*)[.,](\d{2})", t)
    if m:
        euros = re.sub(r"\D", "", m.group(1))
        return round(int(euros) + int(m.group(2)) / 100, 2) if euros else None
    m = re.search(r"(\d[\d ]{1,})", t)
    if m:
        euros = re.sub(r"\D", "", m.group(1))
        return float(euros) if euros else None
    return None


def _en_stock(li):
    """Disponibilité réelle : False seulement si le libellé de stock contient un
    mot de rupture explicite, True sinon (défaut disponible)."""
    node = li.select_one("div.stock") or li.select_one(".stock-web") or li.select_one(".stock-title")
    if node is None:
        return True
    txt = node.get_text(" ", strip=True).lower()
    txt = txt.encode("ascii", "ignore").decode()  # sans accents pour comparer
    return not any(mot in txt for mot in _RUPTURE)
