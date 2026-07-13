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
"""

import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Mots signalant une INDISPONIBILITÉ dans le libellé de stock LDLC (le reste =
# disponible ; l'app suppose « en stock » par défaut, on ne bloque que les
# ruptures explicites).
_RUPTURE = ("rupture", "epuise", "indisponible", "bientot dispo",
            "sur commande", "precommande", "non disponible")


class LdlcSource:
    """Client LDLC poli (limitation de débit, UA navigateur)."""

    def __init__(self, rate_limit=1.5, domaine="ldlc.com", devise="EUR"):
        self.domaine = domaine
        self.devise = devise
        self.base_url = f"https://www.{domaine}"
        self._rate = rate_limit
        self._dernier = 0.0
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
        r = self._sess.get(url, timeout=25)
        self._dernier = time.time()
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    def offres(self, requete):
        """Offres LDLC pour la recherche textuelle [requete] (produits de la
        1re page de résultats, disponibilité et prix réels)."""
        url = f"{self.base_url}/recherche/{quote(requete)}/"
        soup = self._get(url)
        out = []
        for li in soup.select("li.pdt-item"):
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
