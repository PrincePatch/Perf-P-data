"""Recuperation des donnees composants depuis gputracker.eu.

Analyse du site (voir docs/DATA_PIPELINE.md) :
- HTML rendu cote serveur (Bootstrap), aucun JS requis, pas d'API JSON publique.
- Les pages `/en/category/{id}/{slug}` listent une carte par modele
  (`div.filtered-overview-entry`) avec le meilleur prix + le magasin le moins cher.
- Les pages `/en/search/.../facet/.../{modele}` listent toutes les offres par
  magasin (`a.tracked-product-click`) : nom produit, magasin, prix, stock.
- Les liens d'achat sont des redirections `/si-click/...` que robots.txt INTERDIT :
  on les enregistre comme URL d'offre mais on ne les requete JAMAIS.

Politesse : User-Agent identifie, verification robots.txt, pause entre requetes,
retries, isolation des erreurs par page.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

log = logging.getLogger("gputracker")

_PRICE_RE = re.compile(r"[\d][\d.\s]*[\d]|\d")


@dataclass
class Offer:
    """Une offre d'un magasin pour un modele donne."""

    shop: str
    price: float
    currency: str
    url: str
    in_stock: bool
    product_name: str
    image: str | None = None  # photo produit (CDN gputracker), absolue ; None si absente


@dataclass
class Product:
    """Un modele (ex. "AMD RX 7600") et ses offres collectees."""

    name: str
    detail_url: str
    category_key: str
    offers: list[Offer] = field(default_factory=list)


@dataclass
class CategorySpec:
    """Une categorie a collecter (issue de config.yaml)."""

    key: str
    cat_id: int
    slug: str
    schema: str
    max_products: int = 0
    max_detail_pages: int = 0


class RobotsDisallowed(RuntimeError):
    """Levee quand une URL est interdite par robots.txt."""


def _parse_price(text: str) -> float | None:
    """Extrait un prix numerique d'un texte type "1 154" / "253" / "1.299,00"."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    m = _PRICE_RE.search(cleaned)
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


class GpuTrackerSource:
    """Client HTTP poli pour gputracker.eu."""

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        *,
        rate_limit: float = 2.0,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.rate_limit = rate_limit
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent, "Accept-Language": "en"}
        )
        self._robots = self._load_robots()

    # -- robots.txt -------------------------------------------------------
    def _load_robots(self) -> urllib.robotparser.RobotFileParser:
        rp = urllib.robotparser.RobotFileParser()
        url = f"{self.base_url}/robots.txt"
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            rp.parse(resp.text.splitlines())
            log.info("robots.txt charge (%d octets)", len(resp.text))
        except requests.RequestException as exc:  # pragma: no cover - reseau
            log.warning("robots.txt injoignable (%s) : mode prudent (tout interdit)", exc)
            rp.disallow_all = True
        return rp

    def _allowed(self, url: str) -> bool:
        return self._robots.can_fetch(self.user_agent, url)

    # -- HTTP poli --------------------------------------------------------
    def _sleep_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)

    def _get(self, url: str) -> BeautifulSoup:
        if not self._allowed(url):
            raise RobotsDisallowed(url)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._sleep_if_needed()
            try:
                resp = self._session.get(url, timeout=self.timeout)
                self._last_request = time.monotonic()
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                return BeautifulSoup(resp.text, "lxml")
            except requests.RequestException as exc:  # pragma: no cover - reseau
                last_exc = exc
                wait = self.rate_limit * attempt
                log.warning("GET %s echec (essai %d/%d) : %s", url, attempt, self.max_retries, exc)
                time.sleep(wait)
        raise RuntimeError(f"GET impossible apres {self.max_retries} essais : {url}") from last_exc

    # -- API haut niveau --------------------------------------------------
    def category_url(self, spec: CategorySpec) -> str:
        return f"{self.base_url}/{{lang}}/category/{spec.cat_id}/{spec.slug}".format(
            lang="en"
        )

    def list_products(self, spec: CategorySpec) -> list[Product]:
        """Liste les modeles d'une categorie + leur meilleure offre (page liste)."""
        soup = self._get(self.category_url(spec))
        products: list[Product] = []
        for card in soup.select("div.filtered-overview-entry"):
            heading = card.select_one("h2 a[href]")
            if not heading:
                continue
            name = (card.get("data-text-value") or heading.get_text(strip=True)).strip()
            detail_url = urljoin(self.base_url, heading["href"])
            product = Product(name=name, detail_url=detail_url, category_key=spec.key)
            best = self._parse_offer_card(card)
            if best:
                product.offers.append(best)
            products.append(product)
            if spec.max_products and len(products) >= spec.max_products:
                break
        log.info("[%s] %d modeles listes", spec.key, len(products))
        return products

    def fetch_offers(self, product: Product) -> None:
        """Remplit product.offers avec toutes les offres par magasin (page detail)."""
        soup = self._get(product.detail_url)
        offers: list[Offer] = []
        for anchor in soup.select("a.tracked-product-click"):
            offer = self._parse_offer_detail(anchor)
            if offer:
                offers.append(offer)
        if offers:
            product.offers = offers
        log.info("[%s] %d offres pour %s", product.category_key, len(offers), product.name)

    # -- parsing interne --------------------------------------------------
    def _parse_offer_card(self, card: Tag) -> Offer | None:
        """Meilleure offre affichee sur la carte de la page liste."""
        anchor = card.select_one("a.tracked-product-click")
        if not anchor:
            return None
        strong = anchor.find("strong")
        price = _parse_price(strong.get_text() if strong else "")
        shop = (anchor.get("data-shop-name") or "").strip()
        if price is None or not shop:
            return None
        return Offer(
            shop=shop,
            price=price,
            currency="EUR",
            url=urljoin(self.base_url, anchor.get("href", "")),
            in_stock=True,  # la carte n'affiche que le meilleur prix disponible
            product_name=(anchor.get("data-product-name") or "").strip(),
            image=self._extract_image(anchor),  # la carte liste n'en porte pas (=> None)
        )

    def _parse_offer_detail(self, anchor: Tag) -> Offer | None:
        """Offre d'un magasin sur la page detail (prix + stock precis)."""
        shop = (anchor.get("data-shop-name") or "").strip()
        if not shop:
            return None
        price_box = anchor.find("div", class_=lambda c: bool(c) and "h1" in c.split())
        span = price_box.find("span") if price_box else None
        price = _parse_price(span.get_text() if span else "")
        if price is None:
            return None
        stock_div = anchor.find(
            "div",
            class_=lambda c: bool(c) and ("text-success" in c.split() or "text-danger" in c.split()),
        )
        in_stock = bool(stock_div and "text-success" in (stock_div.get("class") or []))
        return Offer(
            shop=shop,
            price=price,
            currency="EUR",
            url=urljoin(self.base_url, anchor.get("href", "")),
            in_stock=in_stock,
            product_name=(anchor.get("data-product-name") or "").strip(),
            image=self._extract_image(anchor),
        )

    def _extract_image(self, anchor: Tag) -> str | None:
        """Photo produit portee par une offre (page detail).

        Chaque offre `a.tracked-product-click` de la page detail contient un
        `<picture><img class="img-fluid" src="https://img-cdn-aws.gputracker.eu/
        fit-in/150x150/products/.../....png?signature=..."></picture>`. On ne
        retient que les URL dont le chemin contient `/products/` (exclut logo,
        banniere, pixel Facebook). URL deja absolue ; `?signature=` conserve
        (le CDN l'exige pour servir l'image).
        """
        for img in anchor.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src:
                continue
            absolute = urljoin(self.base_url, src)
            if "/products/" in absolute:
                return absolute
        return None
