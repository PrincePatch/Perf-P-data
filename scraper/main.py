"""Orchestrateur du pipeline de scraping gputracker.eu (item #10).

Usage local :
    python scraper/main.py --out-dir scraper/sample_output
    python scraper/main.py --categories gpus,cpus --max-detail 5

En CI (voir .github/workflows/data.yml) :
    python scraper/main.py --out-dir public_data --last-updated "2026-07-01T06:00:00Z"

Regles : robots.txt respecte, User-Agent identifie, debit limite, chaque categorie
isolee (une erreur reseau ne fait pas planter tout le run).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

import yaml

# Permet `python scraper/main.py` depuis la racine du repo comme depuis scraper/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize import merge_benchmarks, normalize  # noqa: E402
from sources.gputracker import CategorySpec, GpuTrackerSource  # noqa: E402
from sources.passmark import Benchmarks, PassMarkSource, load_curated, load_tdp_table  # noqa: E402

log = logging.getLogger("perfp.scraper")


def _load_dotenv() -> None:
    """Charge .env si present (sans dependance dure a python-dotenv)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent / ".env")
    except Exception:  # pragma: no cover - optionnel
        pass


def _resolve_last_updated(cli_value: str | None) -> str:
    """Date ISO reproductible : arg CLI > env PERFP_LAST_UPDATED > maintenant (UTC)."""
    value = cli_value or os.getenv("PERFP_LAST_UPDATED")
    if value:
        return value
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _category_specs(config: dict, only: set[str] | None) -> list[CategorySpec]:
    specs: list[CategorySpec] = []
    for entry in config.get("categories", []):
        key = entry["key"]
        if only is not None:
            if key not in only:
                continue
        elif not entry.get("enabled", False):
            continue
        specs.append(
            CategorySpec(
                key=key,
                cat_id=entry["id"],
                slug=entry["slug"],
                schema=entry["schema"],
                max_products=entry.get("max_products", 0),
                max_detail_pages=entry.get("max_detail_pages", 0),
            )
        )
    return specs


def scrape_category(
    source: GpuTrackerSource, spec: CategorySpec, last_updated: str, max_detail_override: int | None
) -> list[dict]:
    """Collecte + normalise une categorie. Renvoie la liste d'items JSON."""
    products = source.list_products(spec)
    detail_budget = spec.max_detail_pages if max_detail_override is None else max_detail_override
    for i, product in enumerate(products):
        if detail_budget and i >= detail_budget:
            break
        try:
            source.fetch_offers(product)
        except Exception as exc:  # isolation : on garde l'offre de la page liste
            log.warning("[%s] detail KO pour %s : %s", spec.key, product.name, exc)
    items = [normalize(spec.schema, p, last_updated) for p in products if p.offers]
    return items


def build_benchmarks(config: dict, site: dict, data_dir: Path) -> Benchmarks:
    """Charge les scores PassMark (live) avec repli CSV bundle si reseau bloque.

    Le TDP (absent de PassMark) est toujours surimpose depuis la table constructeur
    bundlee (`benchmarks_*.csv`), y compris en mode live.
    """
    bench_cfg = config.get("benchmarks", {})
    bench: Benchmarks | None = None
    if bench_cfg.get("enabled", True):
        try:
            source = PassMarkSource(
                user_agent=site["user_agent"],
                rate_limit=float(bench_cfg.get("rate_limit_seconds", site.get("rate_limit_seconds", 2.0))),
                timeout=int(site.get("timeout_seconds", 30)),
                max_retries=int(site.get("max_retries", 3)),
            )
            bench = source.fetch()
            log.info("Benchmarks PassMark (live) : %s", bench.coverage())
        except Exception as exc:  # repli hors-ligne
            log.warning("PassMark live indisponible (%s) : repli sur CSV bundle", exc)
    if bench is None or not bench.coverage()["gpuMarks"]:
        bench = load_curated(data_dir)
    # TDP : toujours depuis la table bundlee (PassMark ne le fournit pas).
    bench.tdp.update(load_tdp_table(data_dir))
    return bench


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scraper gputracker.eu -> JSON Perf P")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.yaml"))
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "sample_output"))
    parser.add_argument("--categories", help="liste separee par des virgules (ex. gpus,cpus)")
    parser.add_argument("--last-updated", help="date ISO injectee dans lastUpdated")
    parser.add_argument("--max-detail", type=int, help="override du nb de pages detail par categorie")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _load_dotenv()

    config = _load_config(Path(args.config))
    site = config["site"]
    last_updated = _resolve_last_updated(args.last_updated)
    only = {c.strip() for c in args.categories.split(",")} if args.categories else None
    specs = _category_specs(config, only)
    if not specs:
        log.error("Aucune categorie a collecter (verifier config/--categories).")
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    source = GpuTrackerSource(
        base_url=site["base_url"],
        user_agent=site["user_agent"],
        rate_limit=float(site.get("rate_limit_seconds", 2.0)),
        timeout=int(site.get("timeout_seconds", 30)),
        max_retries=int(site.get("max_retries", 3)),
    )

    # Benchmarks REELS (PassMark) charges une seule fois si GPU/CPU sont demandes.
    config_dir = Path(args.config).resolve().parent
    data_dir = Path(config.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = config_dir / data_dir
    bench = None
    if any(spec.schema in ("gpu", "cpu") for spec in specs):
        bench = build_benchmarks(config, site, data_dir)

    summary: dict[str, int] = {}
    coverage: dict[str, dict] = {}
    for spec in specs:
        try:
            items = scrape_category(source, spec, last_updated, args.max_detail)
        except Exception as exc:  # isolation par categorie
            log.error("[%s] categorie en echec : %s", spec.key, exc)
            summary[spec.key] = 0
            continue
        if bench is not None and spec.schema in ("gpu", "cpu"):
            cov = merge_benchmarks(items, spec.schema, bench)
            coverage[spec.key] = cov
            log.info("[%s] benchmarks : %d/%d rapproches (ref=%s)",
                     spec.key, cov["matched"], cov["total"], cov.get("reference"))
        out_file = out_dir / f"{spec.key}.json"
        out_file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        summary[spec.key] = len(items)
        log.info("[%s] ecrit %d items -> %s", spec.key, len(items), out_file)

    meta = {
        "source": "gputracker.eu",
        "benchmarkSource": "PassMark (videocardbenchmark.net / cpubenchmark.net)",
        "benchmarkOrigin": bench.origin if bench is not None else None,
        "references": {"gpuIndex": "GeForce RTX 4070 = 100", "cpuGamingIndex": "AMD Ryzen 7 7800X3D = 100"},
        "lastUpdated": last_updated,
        "categories": summary,
        "benchmarkCoverage": coverage,
        "note": "Prix live agreges (gputracker.eu). index/gamingIndex = scores PassMark reels "
                "(null si non rapproche). tdp = fiche constructeur bundlee.",
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Termine. Resume: %s", summary)
    return 0 if any(summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
