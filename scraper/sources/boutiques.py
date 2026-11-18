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

Comme `LdlcSource`, chaque source distingue une liste de résultats, une page
« aucun résultat » explicite (liste vide) et une page qu'elle ne sait pas lire
(`PageNonReconnue`, traitée comme un échec par enrich_shops).
"""

import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .ldlc import (AUCUN_RESULTAT_FR, DELAIS_HTTP, PageNonReconnue, _RUPTURE,
                   _prix_ldlc, aucun_resultat, nettoyer_requete, offre_fiche_produit)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


class _ClientPoli:
    """Base commune : session HTTP polie (limitation de débit, UA navigateur)."""

    # Unités écrites par la boutique dans ses noms de produits (« 32GB » par
    # défaut, « 32 Go » pour les boutiques françaises) — enrich_shops en
    # déduit la requête mémoire vive.
    unites = "en"

    def __init__(self, base_url, rate_limit=1.5, langue="en-US,en;q=0.9"):
        self.base_url = base_url
        self._rate = rate_limit
        self._dernier = 0.0
        # Compteur de requêtes HTTP réellement envoyées (récapitulatif du log),
        # URL finale de la dernière (redirections) et ÉCHÉANCE facultative
        # (horodatage) posée par enrich_shops : une attente « Retry-After »
        # qui la dépasserait n'est pas faite.
        self.nb_requetes = 0
        self.url_finale = ""
        self.echeance = None
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
        self.nb_requetes += 1
        r = self._sess.get(url, timeout=DELAIS_HTTP)
        self._dernier = time.time()
        # 429 = trop de requêtes (observé sur Morele, lot 29) : attendre le
        # délai demandé (Retry-After, borné) puis retenter UNE fois — sauf si
        # l'attente dépasserait l'échéance du passage (budget de temps).
        if r.status_code == 429:
            try:
                attente = min(float(r.headers.get("Retry-After") or 30), 120)
            except ValueError:
                attente = 30.0
            if self.echeance is None or time.time() + attente < self.echeance:
                time.sleep(attente)
                self.nb_requetes += 1
                r = self._sess.get(url, timeout=DELAIS_HTTP)
                self._dernier = time.time()
        self.url_finale = r.url or url
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")


def _parse_pln(txt):
    """Prix float depuis « 4 504,36 zł » / « 4 399 zł » (espaces de milliers,
    insécables comprises, virgule décimale)."""
    t = re.sub(r"[\s\xa0 ]", "", txt or "")
    m = re.search(r"\d+(?:[.,]\d{1,2})?", t)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


# Morele : « Brak wyników » = aucun résultat.
_AUCUN_RESULTAT_PL = re.compile(r"brak wynik", re.IGNORECASE)
# Morele : « powiadom o dostępności » (avertir de la dispo) / « wycofany »
# (retiré) = indisponible.
_INDISPO_PL = ("powiadom o dost", "niedostepny", "niedostępny", "wycofany")


class MoreleSource(_ClientPoli):
    """Morele.net (Pologne, place de marché) — prix en PLN. Cartes
    `.product-card` (mise en page constatée le 18/11/2026 : l'ancienne
    `div.cat-product` à attributs data-* ne renvoyait plus rien), ancienne
    mise en page gardée en repli."""

    domaine = "morele.net"
    devise = "PLN"

    def __init__(self, rate_limit=1.5):
        super().__init__("https://www.morele.net", rate_limit,
                         langue="pl-PL,pl;q=0.9,en;q=0.8")

    def offres(self, requete):
        soup = self._get(f"{self.base_url}/wyszukiwarka/?q={quote(requete)}")
        cartes = soup.select("div.product-card")
        anciennes = soup.select("div.cat-product")
        if not cartes and not anciennes:
            if aucun_resultat(soup, _AUCUN_RESULTAT_PL):
                return []
            raise PageNonReconnue(f"page non reconnue : {self.url_finale}")
        out = []
        for card in cartes:
            a = card.select_one("a.product-card__name-link[href]")
            prix = _parse_pln((card.select_one(".product-card__price-current")
                               or card).get_text(" ", strip=True)) if a else None
            if not a or prix is None or prix <= 0:
                continue
            nom = a.get_text(" ", strip=True)
            href = a["href"].strip()
            txt = card.get_text(" ", strip=True).lower()
            # « W Outlecie » (déclinaison polonaise d'« outlet ») = article de
            # l'outlet (retour, carton ouvert) : même règle que l'occasion, son
            # prix n'est pas celui du neuf.
            if "outlet" in txt or "outlec" in txt:
                continue
            img = card.select_one("img.product-card__img")
            image = ((img.get("src") or img.get("data-src") or "").strip() or None) if img else None
            # Morele masque par défaut les produits indisponibles : une carte
            # sans bouton « Do koszyka » (ajout au panier) est hors stock.
            in_stock = (card.select_one(".product-card__add-to-cart") is not None
                        and not any(m in txt for m in _INDISPO_PL))
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": self.base_url + href if href.startswith("/") else href,
                        "product": nom, "image": image, "in_stock": in_stock})
        for card in anciennes:
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
            in_stock = not any(m in txt for m in _INDISPO_PL)
            out.append({"shop": self.domaine, "price": prix, "currency": self.devise,
                        "url": self.base_url + href if href.startswith("/") else href,
                        "product": nom, "image": image, "in_stock": in_stock})
        return out


# Canada Computers : sa recherche est approximative (une requête absurde
# renvoie encore « 510 results ») ; ce motif ne sert qu'au cas improbable d'une
# page vide.
_AUCUN_RESULTAT_EN = re.compile(r"\b0 results?\b|no results|did not match", re.IGNORECASE)


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
        articles = soup.select("article.product-miniature")
        if not articles:
            if aucun_resultat(soup, _AUCUN_RESULTAT_EN):
                return []
            raise PageNonReconnue(f"page non reconnue : {self.url_finale}")
        out = []
        for art in articles:
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


# Alternate : la latence de la mémoire vive n'est pas dans le nom (« Patriot
# 32 Go DDR5-6000 (2x 16 Go) mémoire vive ») mais dans la fiche résumée de la
# carte (« Timing: CL30 40-40-76 »).
_TIMING_CL = re.compile(r"Timing\s*:\s*CL\s*(\d{1,2})\b", re.IGNORECASE)
_CL_DANS_NOM = re.compile(r"\bCL\s?\d", re.IGNORECASE)


class AlternateSource(_ClientPoli):
    """Alternate France (`alternate.fr`) — prix en EUR (« € 641,00 »), cartes
    `.productBox` (le lien EST la carte), stock réel `.delivery-info`
    (lot 29). Source directe : couvre TOUS les modèles du catalogue Alternate,
    au-delà du sous-ensemble relayé par gputracker."""

    domaine = "alternate.fr"
    devise = "EUR"
    unites = "fr"

    def __init__(self, rate_limit=1.5):
        super().__init__("https://www.alternate.fr", rate_limit,
                         langue="fr-FR,fr;q=0.9")

    @staticmethod
    def requete_memoire(it):
        """Recherche mémoire vive au format des noms Alternate (« 16 Go
        DDR4-3200 ») : « DDR4 16 Go 3200 MHz CL16 » ramenait des PC montés
        (18/11/2026). La latence est contrôlée ensuite via `Timing`."""
        return f"{it['sizeGb']} Go {it['kind']}-{it['mhz']}"

    def offres(self, requete):
        soup = self._get(f"{self.base_url}/listing.xhtml?q={quote(requete)}")
        boites = soup.select("a.productBox")
        if not boites:
            if aucun_resultat(soup, AUCUN_RESULTAT_FR):
                return []
            raise PageNonReconnue(f"page non reconnue : {self.url_finale}")
        out = []
        for box in boites:
            nm = box.select_one(".product-name")
            href = (box.get("href") or "").strip()
            prix = _parse_alternate(box.select_one(".price"))
            if not nm or not href or prix is None or prix <= 0:
                continue
            nom = nm.get_text(" ", strip=True)
            # Mémoire vive : la latence lue dans la fiche résumée complète le
            # nom (sans elle, le filtre strict « CL30 obligatoire » écartait
            # TOUTES les barrettes Alternate — diagnostic du 18/11/2026).
            timing = _TIMING_CL.search(box.get_text(" ", strip=True))
            if timing and not _CL_DANS_NOM.search(nom):
                nom = f"{nom} CL{timing.group(1)}"
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
    `.o-stock` / disponibilité web (lot 29). Comme LDLC, une recherche à
    résultat unique redirige vers la fiche `/produit/…`, lue par ses données
    structurées schema.org."""

    domaine = "materiel.net"
    devise = "EUR"
    unites = "fr"

    def __init__(self, rate_limit=1.5):
        super().__init__("https://www.materiel.net", rate_limit,
                         langue="fr-FR,fr;q=0.9")

    def offres(self, requete):
        soup = self._get(f"{self.base_url}/recherche/{quote(nettoyer_requete(requete))}/")
        lignes = [li for li in soup.select("ul.c-products-list li")
                  if li.select_one(".c-product__title") is not None]
        if not lignes:
            if "/recherche/" not in self.url_finale:
                fiche = offre_fiche_produit(soup, self.domaine, self.devise, self.url_finale)
                if fiche is not None:
                    return fiche
            if aucun_resultat(soup, AUCUN_RESULTAT_FR):
                return []
            raise PageNonReconnue(f"page non reconnue : {self.url_finale}")
        out = []
        for li in lignes:
            titre = li.select_one(".c-product__title")
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
