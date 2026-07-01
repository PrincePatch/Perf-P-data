# scraper — prix (gputracker.eu) + benchmarks réels (PassMark)

Scraper Python autonome (item #10) qui publie des JSON de **prix live par magasin**
(gputracker.eu) **fusionnés avec des indices de perf RÉELS** (PassMark : G3D Mark GPU,
CPU Mark). Détails complets : [`docs/DATA_PIPELINE.md`](../docs/DATA_PIPELINE.md).

## Démarrage

```bash
pip install -r requirements.txt
cp .env.example .env            # optionnel

# Démo réelle (GPU + CPU)
python main.py --categories gpus,cpus --max-detail 5 \
  --last-updated 2026-07-01T06:00:00Z --out-dir sample_output

# Photos produit : scraper avec détail complet, puis téléchargement (voir DATA_PIPELINE §2ter)
python main.py --categories gpus --max-detail 0 --out-dir sample_output
python download_images.py --json sample_output/gpus.json --images-dir sample_output/images
```

## Structure

| Fichier | Rôle |
|---|---|
| `main.py` | Orchestrateur (args, config, JSON + `meta.json`, fusion benchmarks). |
| `download_images.py` | Télécharge les photos produit (`image` → `images/<id>.<ext>` + `imageSource`). |
| `sources/gputracker.py` | Fetch poli (robots-aware, rate-limit) + parsing HTML prix + image. |
| `sources/passmark.py` | Benchmarks RÉELS PassMark (G3D/CPU Mark) + repli CSV. |
| `normalize.py` | Brut → schéma Perf P + `prices`/`priceMin` + `merge_benchmarks()`. |
| `config.yaml` | base_url, User-Agent, débit, catégories, section `benchmarks`. |
| `data/benchmarks_*.csv` | Repli hors-ligne : instantané RÉEL PassMark + TDP (sourcé). |

## Règles

- robots.txt respecté **par hôte** (`/si-click` jamais requêté ; `Content-Signal:
  ai-train=no` de PassMark respecté), User-Agent identifiant le bot, 2 s entre
  requêtes, erreurs isolées par catégorie/page.
- gputracker n'expose **aucun benchmark** → `index`/`gamingIndex` viennent de
  **PassMark** (RTX 4070 = 100, Ryzen 7 7800X3D = 100). `null` si non rapproché ;
  **rien n'est inventé**. Repli `data/benchmarks_*.csv` si réseau bloqué.
- Publication : CI (`.github/workflows/data.yml`) pousse sur la branche `data-live`.
- `.env` gitignoré ; aucun secret requis.
