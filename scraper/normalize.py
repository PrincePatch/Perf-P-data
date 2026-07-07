"""Mapping des donnees brutes gputracker -> schema JSON de Perf P.

Perf P attend des formes precises (voir assets/data/*.json). On les respecte et on
AJOUTE le pricing live sans casser les champs existants :
  - `prices` : [{shop, price, currency, url, inStock, lastSeen}]
  - `priceMin` : nombre (min des offres)
  - `sourceUrl`, `lastUpdated` : tracabilite

IMPORTANT : gputracker.eu est un agregateur de PRIX. Il n'expose ni indice de
performance ni TDP structure. Les champs de perf (`index`, `gamingIndex`, `tdp`,
`cores`, `socket`) sont donc emis a `null` : rien n'est invente. L'app fusionnera
ces valeurs depuis ses propres donnees (voir docs/DATA_PIPELINE.md).
"""
from __future__ import annotations

import re
from collections import Counter

from sources.gputracker import Offer, Product
from sources.passmark import Benchmarks

_VRAM_RE = re.compile(r"(\d{1,2})\s?GB", re.IGNORECASE)
_RAM_SIZE_RE = re.compile(r"(\d{2,3})\s?GB", re.IGNORECASE)
_MHZ_RE = re.compile(r"(\d{4,5})\s?MHz", re.IGNORECASE)
_CL_RE = re.compile(r"\bCL\s?(\d{2})\b", re.IGNORECASE)
_SIZE_TB_RE = re.compile(r"(\d(?:[.,]\d)?)\s?TB", re.IGNORECASE)
_SIZE_GB_RE = re.compile(r"(\d{3,4})\s?GB", re.IGNORECASE)


def slug_id(name: str) -> str:
    """Identifiant stable et minuscule a partir d'un nom de modele."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def guess_brand(name: str) -> str | None:
    upper = name.upper()
    if "NVIDIA" in upper or "GEFORCE" in upper or upper.strip().startswith("RTX") or "GTX" in upper:
        return "NVIDIA"
    if "AMD" in upper or "RADEON" in upper or "RYZEN" in upper or upper.strip().startswith("RX "):
        return "AMD"
    if "INTEL" in upper or "CORE" in upper or "ARC" in upper:
        return "Intel"
    return None


def _price_min(offers: list[Offer]) -> float | None:
    prices = [o.price for o in offers if o.price is not None]
    return min(prices) if prices else None


def _pick_image(offers: list[Offer]) -> str | None:
    """Photo produit representative : celle de l'offre la moins chere qui en a une.

    Les magasins vendent des variantes AIB differentes (donc des photos
    differentes) ; on prend l'image de l'offre la plus abordable disponible.
    URL absolue (CDN gputracker) ; sera reecrite en chemin relatif par
    `download_images.py`. `None` si aucune offre ne porte d'image (rien inventé).
    """
    for o in sorted(offers, key=lambda x: x.price):
        if o.image:
            return o.image
    return None


def _offers_payload(offers: list[Offer], last_updated: str) -> list[dict]:
    seen: set[tuple[str, float]] = set()
    payload: list[dict] = []
    for o in sorted(offers, key=lambda x: x.price):
        key = (o.shop, o.price)
        if key in seen:
            continue
        seen.add(key)
        payload.append(
            {
                "shop": o.shop,
                "price": o.price,
                "currency": o.currency,
                "url": o.url,
                "inStock": o.in_stock,
                "lastSeen": last_updated,
                # Nom RÉEL du produit vendu (variante AIB : « ASUS TUF RTX 4070
                # OC »…) — alimente le sélecteur de modèles de l'app (lot 10).
                "product": o.product_name or None,
                # Photo de la VARIANTE vendue (CDN gputracker, URL absolue) —
                # chaque modèle du sélecteur a sa propre image (lot 15, item 9).
                "image": o.image or None,
            }
        )
    return payload


def _most_common(values: list[int]) -> int | None:
    return Counter(values).most_common(1)[0][0] if values else None


def _base(product: Product, last_updated: str) -> dict:
    offers = product.offers
    return {
        "id": slug_id(product.name),
        "name": product.name,
        "priceMin": _price_min(offers),
        "prices": _offers_payload(offers, last_updated),
        "image": _pick_image(offers),
        "sourceUrl": product.detail_url,
        "lastUpdated": last_updated,
    }


def to_gpu(product: Product, last_updated: str) -> dict:
    vram = _most_common([int(m) for o in product.offers for m in _VRAM_RE.findall(o.product_name)])
    base = _base(product, last_updated)
    return {
        "id": base["id"],
        "brand": guess_brand(product.name),
        "name": product.name,
        "index": None,          # non fourni par gputracker (agregateur de prix)
        "vramGb": vram,
        "price": base["priceMin"],
        "tdp": None,            # non fourni
        "priceMin": base["priceMin"],
        "prices": base["prices"],
        "image": base["image"],  # photo produit (CDN gputracker) ou None
        "sourceUrl": base["sourceUrl"],
        "lastUpdated": last_updated,
    }


def to_cpu(product: Product, last_updated: str) -> dict:
    base = _base(product, last_updated)
    return {
        "id": base["id"],
        "brand": guess_brand(product.name),
        "name": product.name,
        "gamingIndex": None,    # non fourni par gputracker
        "cores": None,          # non fourni
        "socket": None,         # non fourni
        "price": base["priceMin"],
        "tdp": None,            # non fourni
        "priceMin": base["priceMin"],
        "prices": base["prices"],
        "sourceUrl": base["sourceUrl"],
        "lastUpdated": last_updated,
    }


def to_ssd(product: Product, last_updated: str) -> dict:
    text = " ".join(o.product_name for o in product.offers)
    size_gb = None
    tb = _SIZE_TB_RE.search(text)
    gb = _SIZE_GB_RE.search(text)
    if tb:
        size_gb = int(float(tb.group(1).replace(",", ".")) * 1000)
    elif gb:
        size_gb = int(gb.group(1))
    base = _base(product, last_updated)
    return {
        "id": base["id"],
        "name": product.name,
        "kind": None,           # non fourni de facon structuree
        "readMBps": None,       # non fourni
        "writeMBps": None,      # non fourni
        "sizeGb": size_gb,
        "price": base["priceMin"],
        "priceMin": base["priceMin"],
        "prices": base["prices"],
        "image": base["image"],  # photo produit (CDN gputracker) ou None
        "sourceUrl": base["sourceUrl"],
        "lastUpdated": last_updated,
    }


def to_ram(product: Product, last_updated: str) -> dict:
    text = " ".join([product.name] + [o.product_name for o in product.offers])
    size = _RAM_SIZE_RE.search(text)
    mhz = _MHZ_RE.search(text)
    cl = _CL_RE.search(text)
    kind = "DDR5" if "DDR5" in text.upper() else ("DDR4" if "DDR4" in text.upper() else None)
    base = _base(product, last_updated)
    return {
        "id": base["id"],
        "name": product.name,
        "sizeGb": int(size.group(1)) if size else None,
        "mhz": int(mhz.group(1)) if mhz else None,
        "cl": int(cl.group(1)) if cl else None,
        "kind": kind,
        "price": base["priceMin"],
        "priceMin": base["priceMin"],
        "prices": base["prices"],
        "image": base["image"],  # photo produit (CDN gputracker) ou None
        "sourceUrl": base["sourceUrl"],
        "lastUpdated": last_updated,
    }


NORMALIZERS = {
    "gpu": to_gpu,
    "cpu": to_cpu,
    "ssd": to_ssd,
    "ram": to_ram,
}


def normalize(schema: str, product: Product, last_updated: str) -> dict:
    fn = NORMALIZERS.get(schema)
    if fn is None:
        raise ValueError(f"schema inconnu : {schema}")
    return fn(product, last_updated)


# -- Fusion des benchmarks REELS (PassMark) -----------------------------------
#
# Echelle relative de Perf P : GeForce RTX 4070 (G3D) = 100 pour `index`,
# Ryzen 7 7800X3D (CPU Mark) = 100 pour `gamingIndex`. Un composant non rapproche
# garde `null` (rien n'est extrapole). Le `tdp` vient de la table constructeur
# bundlee (PassMark ne le fournit pas), sinon reste `null`.

def merge_benchmarks(items: list[dict], schema: str, bench: Benchmarks) -> dict[str, int]:
    """Injecte index/gamingIndex/tdp REELS dans les items ; renvoie la couverture."""
    if schema == "gpu":
        return _merge_gpu(items, bench)
    if schema == "cpu":
        return _merge_cpu(items, bench)
    return {"matched": 0, "total": len(items)}


def _merge_gpu(items: list[dict], bench: Benchmarks) -> dict[str, int]:
    ref = bench.gpu_reference()
    matched = 0
    for item in items:
        hit = bench.match_gpu(item["name"], item.get("vramGb"))
        if hit and ref:
            item["index"] = round(hit.g3d / ref * 100, 1)
            matched += 1
        tdp = bench.tdp_for("gpu", item["name"])
        if tdp is not None:
            item["tdp"] = tdp
    return {"matched": matched, "total": len(items), "reference": ref or 0}


def _merge_cpu(items: list[dict], bench: Benchmarks) -> dict[str, int]:
    ref = bench.cpu_reference()
    matched = 0
    for item in items:
        hit = bench.match_cpu(item["name"])
        if hit and ref:
            item["gamingIndex"] = round(hit.cpu_mark / ref * 100, 1)
            matched += 1
        tdp = bench.tdp_for("cpu", item["name"])
        if tdp is not None:
            item["tdp"] = tdp
    return {"matched": matched, "total": len(items), "reference": ref or 0}
