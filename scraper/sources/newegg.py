# -*- coding: utf-8 -*-
"""Source de prix ÉTATS-UNIS : Newegg (newegg.com). Complète gputracker.eu
(zone euro) par des prix RÉELS en DOLLARS pour les utilisateurs américains
(lot 21, item 3). Pages de recherche rendues côté serveur (`/p/pl?d=<requête>`),
parsées avec BeautifulSoup — même pile `requests` + `bs4` que le reste du
scraper, poli (User-Agent descriptif + limitation de débit).

Renvoie des offres au MÊME schéma que `enrich_catalog.offres_pour`, avec une
devise « USD » :
    {"shop": "newegg.com", "price": float, "currency": "USD",
     "url": str, "product": str, "image": str|None}
"""

import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class NeweggSource:
    """Client Newegg poli (limitation de débit, UA navigateur). [domaine] =
    'newegg.com' (USD) ou 'newegg.ca' (CAD) — lot 25, item 1."""

    def __init__(self, rate_limit=1.5, domaine="newegg.com", devise="USD"):
        self.domaine = domaine
        self.devise = devise
        self.base_url = f"https://www.{domaine}"
        self._rate = rate_limit
        self._dernier = 0.0
        self._sess = requests.Session()
        self._sess.headers.update({
            "User-Agent": _UA,
            "Accept-Language": "en-US,en;q=0.9",
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
        """Offres Newegg pour la recherche textuelle [requete]."""
        url = f"{self.base_url}/p/pl?d={quote(requete)}"
        soup = self._get(url)
        out = []
        for cell in soup.select("div.item-cell"):
            a = cell.select_one("a.item-title")
            if not a:
                continue
            nom = a.get_text(strip=True)
            lien = (a.get("href") or "").strip()
            prix = _prix_cellule(cell)
            if not nom or not lien or prix is None or prix <= 0:
                continue
            img = cell.select_one("a.item-img img")
            image = None
            if img:
                image = (img.get("src") or img.get("data-src") or "").strip() or None
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": lien, "product": nom, "image": image})
        return out


def _prix_cellule(cell):
    """Extrait le prix (float, USD) d'une cellule produit Newegg. Le prix est
    « $<strong>549<sup>.99</sup> » — on recompose partie entière + décimales."""
    box = cell.select_one("li.price-current")
    if not box:
        return None
    strong = box.select_one("strong")
    if not strong:
        # repli : premier nombre du texte
        return _parse_usd(box.get_text(" ", strip=True))
    entier = re.sub(r"[^\d]", "", strong.get_text())
    sup = box.select_one("sup")
    dec = re.sub(r"[^\d]", "", sup.get_text()) if sup else ""
    if not entier:
        return None
    try:
        return float(f"{entier}.{dec or '0'}")
    except ValueError:
        return None


def _parse_usd(txt):
    m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", txt.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None
