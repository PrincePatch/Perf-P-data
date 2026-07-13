# -*- coding: utf-8 -*-
"""Sources de prix DIRECTES pour les boutiques de l'app hors zone gputracker
(lot 28) : Morele (Pologne, PLN) et Canada Computers (Canada, CAD). Chacune
expose `offres(requete)` au schéma commun de l'enrichissement :
    {"shop": <domaine>, "price": float, "currency": str, "url": str,
     "product": str, "image": str|None, "in_stock": bool}

Pages de recherche rendues côté serveur, parsées avec BeautifulSoup — même
pile `requests` + `bs4` que le reste du scraper, polie (UA navigateur +
limitation de débit). Les boutiques testées et NON scrapables (403/DataDome ou
rendu JavaScript) : Caseking, Proshop, Mindfactory, PcComponentes, Coolmod,
Overclockers, Scan, x-kom, Digitec, Azerty, Memory Express, Micro Center,
B&H, Adorama, TopAchat — couvrez-les via gputracker quand il les liste.
"""

import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .ldlc import _RUPTURE, _prix_ldlc

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


class _ClientPoli:
    """Base commune : session HTTP polie (limitation de débit, UA navigateur)."""

    def __init__(self, base_url, rate_limit=1.5, langue="en-US,en;q=0.9"):
        self.base_url = base_url
        self._rate = rate_limit
        self._dernier = 0.0
        self._sess = requests.Session()
        self._sess.headers.update({
            "User-Agent": _UA,
            "Accept-Language": langue,
            "Accept": "text/html,application/xhtml+xml",
        })

    def _get(self, url):
        ecart = time.time() - self._dernier
        if ecart < self._rate:
            time.sleep(self._rate - ecart)
        r = self._sess.get(url, timeout=25)
        self._dernier = time.time()
        # 429 = trop de requêtes (observé sur Morele, lot 29) : attendre le
        # délai demandé (Retry-After, borné) puis retenter UNE fois.
        if r.status_code == 429:
            try:
                attente = min(float(r.headers.get("Retry-After") or 30), 120)
            except ValueError:
                attente = 30.0
            time.sleep(attente)
            r = self._sess.get(url, timeout=25)
            self._dernier = time.time()
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")


class MoreleSource(_ClientPoli):
    """Morele.net (Pologne) — prix en PLN, cartes `.cat-product` portées par
    des attributs data-* propres (nom, prix, lien)."""

    domaine = "morele.net"
    devise = "PLN"

    def __init__(self, rate_limit=1.5):
        super().__init__("https://www.morele.net", rate_limit,
                         langue="pl-PL,pl;q=0.9,en;q=0.8")

    def offres(self, requete):
        soup = self._get(f"{self.base_url}/wyszukiwarka/?q={quote(requete)}")
        out = []
        for card in soup.select("div.cat-product"):
            nom = (card.get("data-product-name") or "").strip()
            prix_txt = (card.get("data-product-price") or "").replace(",", ".")
            lien_node = card.select_one(".productLink[data-link-href-param]")
            href = lien_node.get("data-link-href-param") if lien_node else None
            if not nom or not prix_txt or not href:
                continue
            try:
                prix = float(prix_txt)
            except ValueError:
                continue
            if prix <= 0:
                continue
            img = card.select_one("img.product-image")
            image = (img.get("src") or "").strip() or None if img else None
            # Indisponible : « powiadom o dostępności » (avertir de la dispo) /
            # « wycofany » (retiré) — défaut disponible.
            txt = card.get_text(" ", strip=True).lower()
            in_stock = not any(m in txt for m in
                               ("powiadom o dost", "niedostepny", "niedostępny", "wycofany"))
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": self.base_url + href if href.startswith("/") else href,
                        "product": nom, "image": image, "in_stock": in_stock})
        return out


class CanadaComputersSource(_ClientPoli):
    """Canada Computers (Canada) — prix en CAD, cartes
    `article.product-miniature` (prix dans `data-price`, photo CDN)."""

    domaine = "canadacomputers.com"
    devise = "CAD"

    def __init__(self, rate_limit=1.5):
        super().__init__("https://www.canadacomputers.com", rate_limit,
                         langue="en-CA,en;q=0.9")

    def offres(self, requete):
        soup = self._get(f"{self.base_url}/en/search?s={quote(requete)}")
        out = []
        for art in soup.select("article.product-miniature"):
            a = art.select_one("h2.product-title a") or art.select_one(".product-title a")
            if not a:
                continue
            nom = a.get_text(strip=True)
            href = (a.get("href") or "").strip()
            desc = art.select_one(".product-description")
            prix = _parse_cad((desc.get("data-price") or "") if desc else "")
            if not nom or not href or prix is None or prix <= 0:
                continue
            img = art.select_one("picture img")
            image = None
            if img:
                # data-cc-src = vraie photo (le src est un placeholder) ;
                # data-full-size-image-url = haute résolution.
                image = (img.get("data-full-size-image-url")
                         or img.get("data-cc-src") or "").strip() or None
            txt = art.get_text(" ", strip=True).lower()
            in_stock = not any(m in txt for m in
                               ("out of stock", "sold out", "back order", "special order"))
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": href if href.startswith("http") else self.base_url + href,
                        "product": nom, "image": image, "in_stock": in_stock})
        return out


def _parse_cad(txt):
    """Prix float depuis « $939.99 » (séparateurs de milliers tolérés)."""
    m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", txt.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


class AlternateSource(_ClientPoli):
    """Alternate France (`alternate.fr`) — prix en EUR (« € 641,00 »), cartes
    `.productBox` (le lien EST la carte), stock réel `.delivery-info`
    (lot 29). Source directe : couvre TOUS les modèles du catalogue Alternate,
    au-delà du sous-ensemble relayé par gputracker."""

    domaine = "alternate.fr"
    devise = "EUR"

    def __init__(self, rate_limit=1.5):
        super().__init__("https://www.alternate.fr", rate_limit,
                         langue="fr-FR,fr;q=0.9")

    def offres(self, requete):
        soup = self._get(f"{self.base_url}/listing.xhtml?q={quote(requete)}")
        out = []
        for box in soup.select("a.productBox"):
            nm = box.select_one(".product-name")
            href = (box.get("href") or "").strip()
            prix = _parse_alternate(box.select_one(".price"))
            if not nm or not href or prix is None or prix <= 0:
                continue
            nom = nm.get_text(" ", strip=True)
            img = box.select_one("img.productPicture")
            image = None
            if img:
                src = (img.get("src") or "").strip()
                if src:
                    image = src if src.startswith("http") else self.base_url + src
            livr = box.select_one(".delivery-info")
            txt = (livr.get_text(" ", strip=True).lower() if livr else "")
            txt = txt.encode("ascii", "ignore").decode()
            in_stock = not any(m in txt for m in _RUPTURE)
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": href if href.startswith("http") else self.base_url + href,
                        "product": nom, "image": image, "in_stock": in_stock})
        return out


def _parse_alternate(node):
    """Prix float depuis « € 641,00 » / « € 1.049,00 » (format alternate)."""
    if node is None:
        return None
    t = node.get_text(" ", strip=True)
    m = re.search(r"([0-9][0-9.\s ]*),?([0-9]{2})?", t.replace("€", ""))
    if not m:
        return None
    entier = re.sub(r"\D", "", m.group(1))
    if not entier:
        return None
    try:
        return round(int(entier) + int(m.group(2) or 0) / 100, 2)
    except ValueError:
        return None


class MaterielNetSource(_ClientPoli):
    """Materiel.net (groupe LDLC) — prix au même format que LDLC (« 749€ 95 »),
    cartes `ul.c-products-list li` avec `.c-product__title`, stock réel
    `.o-stock` / disponibilité web (lot 29)."""

    domaine = "materiel.net"
    devise = "EUR"

    def __init__(self, rate_limit=1.5):
        super().__init__("https://www.materiel.net", rate_limit,
                         langue="fr-FR,fr;q=0.9")

    def offres(self, requete):
        soup = self._get(f"{self.base_url}/recherche/{quote(requete)}/")
        out = []
        for li in soup.select("ul.c-products-list li"):
            titre = li.select_one(".c-product__title")
            if titre is None:  # lignes décoratives (reprise, cadeaux…)
                continue
            nom = titre.get_text(strip=True)
            a = li.select_one("a.c-product__link[href]") or li.select_one("a[href]")
            href = (a.get("href") or "").strip() if a else ""
            prix = _prix_ldlc(li.select_one(".o-product__price"))
            if not nom or not href or prix is None or prix <= 0:
                continue
            img = li.select_one("img")
            image = None
            if img:
                src = (img.get("src") or img.get("data-src") or "").strip()
                if src and "no-photo" not in src:
                    image = src if src.startswith("http") else self.base_url + src
            dispo = li.select_one("[class*=availability]") or li.select_one("[class*=stock]")
            txt = (dispo.get_text(" ", strip=True).lower() if dispo else "")
            txt = txt.encode("ascii", "ignore").decode()
            in_stock = not any(m in txt for m in _RUPTURE)
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": href if href.startswith("http") else self.base_url + href,
                        "product": nom, "image": image, "in_stock": in_stock})
        return out
