"""Telechargement des photos produit reperees par le scraper (item #10 — images).

Le scraper ecrit, pour chaque GPU (et SSD/RAM), un champ `image` = URL ABSOLUE de
la photo produit sur le CDN gputracker (`https://img-cdn-aws.gputracker.eu/fit-in/
150x150/products/.../....png?signature=...`). Ce script :

  1. lit les JSON de sortie (ex. sample_output/gpus.json) ;
  2. telecharge chaque `image` (UA identifie, debit limite, retries, skip si echec)
     dans `<images-dir>/<id>.<ext>` ;
  3. reecrit `image` -> chemin RELATIF publie (`images/<id>.<ext>`) et conserve
     l'URL absolue d'origine dans `imageSource` (tracabilite).

L'app consomme alors l'image en RAW :
  https://raw.githubusercontent.com/PrincePatch/Perf-P-data/main/images/<id>.<ext>

Politesse : User-Agent descriptif (repris de config.yaml), pause entre requetes,
retries a back-off, verification robots.txt de l'hote CDN, aucune URL inventee.
Idempotent : un `image` deja relatif (deja telecharge) est ignore.

Usage :
    python scraper/download_images.py                      # gpus.json de sample_output
    python scraper/download_images.py --json a.json,b.json --images-dir out/images
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

log = logging.getLogger("perfp.images")

# Extensions d'images acceptees (l'ext vient de l'URL, repli via Content-Type).
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_CTYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _load_site(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)["site"]


def _ext_for(url: str, content_type: str | None) -> str:
    """Extension du fichier : d'abord l'URL, sinon le Content-Type, sinon .png."""
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in _ALLOWED_EXT:
        return ".jpg" if path_ext == ".jpeg" else path_ext
    if content_type:
        mapped = _CTYPE_EXT.get(content_type.split(";")[0].strip().lower())
        if mapped:
            return mapped
    return ".png"


class ImageDownloader:
    """Client HTTP poli pour telecharger les photos produit (robots-aware)."""

    def __init__(
        self,
        user_agent: str,
        *,
        rate_limit: float = 2.0,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.user_agent = user_agent
        self.rate_limit = rate_limit
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    # -- robots.txt (par hote CDN) ---------------------------------------
    def _allowed(self, url: str) -> bool:
        parts = urlparse(url)
        host = parts.netloc
        rp = self._robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                resp = self._session.get(robots_url, timeout=self.timeout)
                # CDN sans robots valide (404 / 4xx JSON) => aucune regle => tout permis.
                if resp.status_code == 200 and "text" in resp.headers.get("content-type", ""):
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])
            except requests.RequestException:
                rp.parse([])  # injoignable => on n'invente pas d'interdiction
            self._robots[host] = rp
        return rp.can_fetch(self.user_agent, url)

    # -- HTTP poli --------------------------------------------------------
    def _sleep_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)

    def fetch(self, url: str) -> requests.Response | None:
        """GET binaire poli avec retries ; None si interdit ou echec definitif."""
        if not self._allowed(url):
            log.warning("robots.txt interdit : %s", url)
            return None
        for attempt in range(1, self.max_retries + 1):
            self._sleep_if_needed()
            try:
                resp = self._session.get(url, timeout=self.timeout)
                self._last_request = time.monotonic()
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                log.warning(
                    "GET image echec (essai %d/%d) : %s (%s)",
                    attempt, self.max_retries, url, exc,
                )
                self._last_request = time.monotonic()
                time.sleep(self.rate_limit * attempt)
        return None


def _process_file(
    json_path: Path,
    images_dir: Path,
    base_path: str,
    downloader: ImageDownloader,
) -> dict[str, int]:
    """Telecharge les images d'un fichier JSON et reecrit ses champs sur place."""
    items = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        log.warning("%s : format inattendu (liste attendue) — ignore", json_path.name)
        return {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    images_dir.mkdir(parents=True, exist_ok=True)
    total = downloaded = skipped = failed = 0
    for item in items:
        url = item.get("image")
        if not url:
            continue
        total += 1
        if not str(url).lower().startswith(("http://", "https://")):
            skipped += 1  # deja relatif (deja telecharge) => idempotent
            continue
        resp = downloader.fetch(url)
        if resp is None:
            failed += 1
            continue
        ext = _ext_for(url, resp.headers.get("content-type"))
        filename = f"{item['id']}{ext}"
        (images_dir / filename).write_bytes(resp.content)
        item["imageSource"] = url                   # tracabilite (URL d'origine)
        item["image"] = f"{base_path}/{filename}"   # chemin publie relatif
        downloaded += 1
        log.info("image OK %s (%d octets) -> %s", item["id"], len(resp.content), filename)

    json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"total": total, "downloaded": downloaded, "skipped": skipped, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Telecharge les photos produit du scraper Perf P")
    parser.add_argument(
        "--json",
        default=str(here / "sample_output" / "gpus.json"),
        help="fichier(s) JSON, separes par des virgules",
    )
    parser.add_argument(
        "--images-dir",
        default=str(here / "sample_output" / "images"),
        help="dossier de sortie des images",
    )
    parser.add_argument(
        "--base-path",
        default="images",
        help="prefixe relatif publie ecrit dans le champ image",
    )
    parser.add_argument("--config", default=str(here / "config.yaml"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    site = _load_site(Path(args.config))
    downloader = ImageDownloader(
        user_agent=site["user_agent"],
        rate_limit=float(site.get("rate_limit_seconds", 2.0)),
        timeout=int(site.get("timeout_seconds", 30)),
        max_retries=int(site.get("max_retries", 3)),
    )

    images_dir = Path(args.images_dir)
    grand: dict[str, int] = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    for raw in args.json.split(","):
        json_path = Path(raw.strip())
        if not json_path.exists():
            log.warning("fichier introuvable : %s", json_path)
            continue
        stats = _process_file(json_path, images_dir, args.base_path.strip("/"), downloader)
        log.info("%s : %s", json_path.name, stats)
        for key in grand:
            grand[key] += stats[key]

    log.info("Termine. Total: %s", grand)
    return 0 if grand["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
