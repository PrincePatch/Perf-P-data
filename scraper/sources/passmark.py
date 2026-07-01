"""Source de benchmarks REELS : PassMark (item #10, enrichissement perf).

gputracker.eu n'expose aucun indice de performance. On les recupere donc depuis
**PassMark**, qui publie des scores mesures et largement reconnus :

- GPU : `videocardbenchmark.net/gpu_list.php` -> **G3D Mark** (tableau HTML complet,
  une ligne par carte, score en 2e colonne).
- CPU : `cpubenchmark.net/{high_end,mid_range,midlow_range,low_end}_cpus.html` ->
  **CPU Mark** (multithread). NB : la liste `cpu_list.php` ne rend que des CPU Intel
  anciens cote serveur ; les pages « charts » par gamme contiennent AMD **et** Intel
  avec leur score inline -> ce sont elles qu'on lit.

Politesse (identique a gputracker.py) : User-Agent descriptif, verification
`robots.txt` (`can_fetch`) **par hote** avant chaque GET, debit limite, retries,
isolation des erreurs. robots.txt de PassMark autorise ces pages pour `*` (seuls
`/shared/ /cgi-bin/ /search/ /baselines/ ...` sont interdits ; cpubenchmark porte
un `Content-Signal: ai-train=no` que l'on respecte : on ne fait qu'agreger des
scores, aucun entrainement de modele).

Repli hors-ligne : si le reseau est bloque (CI/offline), `load_curated()` recharge
un instantane REEL et date des memes scores depuis `scraper/data/benchmarks_*.csv`
(chaque valeur sourcee dans la colonne `source`). Rien n'est jamais invente.
"""
from __future__ import annotations

import csv
import logging
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("passmark")

GPU_LIST_URL = "https://www.videocardbenchmark.net/gpu_list.php"
CPU_CHART_URLS = [
    "https://www.cpubenchmark.net/high_end_cpus.html",
    "https://www.cpubenchmark.net/mid_range_cpus.html",
    "https://www.cpubenchmark.net/midlow_range_cpus.html",
    "https://www.cpubenchmark.net/low_end_cpus.html",
]

# Parts de reference qui fixent l'echelle relative de Perf P (= 100).
GPU_REFERENCE_NAME = "GeForce RTX 4070"
CPU_REFERENCE_NAME = "AMD Ryzen 7 7800X3D"

_MEM_RE = re.compile(r"\b(\d{1,2})\s?gb\b")
_CORES_RE = re.compile(r"\b\d{1,3}[\s-]?cores?\b")
_CLOCK_RE = re.compile(r"@.*$")

# Mots retires avant comparaison de noms (marques, gammes, suffixes marketing).
_STOP_GPU = {
    "nvidia", "geforce", "amd", "radeon", "intel", "arc", "quadro",
    "ada", "generation", "edition", "series", "laptop", "mobile",
}
_STOP_CPU = {
    "amd", "ryzen", "threadripper", "intel", "core", "processor", "cpu",
    "with", "radeon", "graphics", "pro", "apu",
}
# Suffixes AMD (APU « G », sans-iGPU « F »…) testes quand le nom du dataset est
# tronque (ex. « Ryzen 5 3400 » -> PassMark « Ryzen 5 3400G »).
_CPU_SUFFIXES = ("", "g", "f", "ge", "gt")


def normalize_gpu_name(name: str) -> str:
    """Cle de rapprochement GPU : minuscule, sans marque/gamme/taille memoire."""
    s = _MEM_RE.sub(" ", name.lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return "".join(tok for tok in s.split() if tok not in _STOP_GPU)


def normalize_cpu_name(name: str) -> str:
    """Cle de rapprochement CPU : minuscule, sans marque, horloge ni nb de coeurs."""
    s = _CLOCK_RE.sub("", name.lower())
    s = _CORES_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return "".join(tok for tok in s.split() if tok not in _STOP_CPU)


@dataclass
class GpuMark:
    """Un score G3D pour une carte (le nom porte la taille memoire eventuelle)."""

    source_name: str
    g3d: int
    vram_gb: int | None = None


@dataclass
class CpuMark:
    """Un score CPU Mark (multithread) pour un processeur."""

    source_name: str
    cpu_mark: int


@dataclass
class Benchmarks:
    """Scores normalises + table TDP optionnelle + provenance (live / repli CSV)."""

    gpu: dict[str, list[GpuMark]] = field(default_factory=dict)
    cpu: dict[str, CpuMark] = field(default_factory=dict)
    tdp: dict[str, int] = field(default_factory=dict)  # normkey -> watts (curated)
    origin: str = "passmark-live"

    # -- rapprochement --------------------------------------------------------
    def match_gpu(self, name: str, vram_gb: int | None) -> GpuMark | None:
        """Rapproche un GPU du dataset ; departage les variantes 8/16 Go par VRAM."""
        cands = self.gpu.get(normalize_gpu_name(name))
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        if vram_gb is not None:
            exact = [c for c in cands if c.vram_gb == vram_gb]
            if exact:
                return max(exact, key=lambda c: c.g3d)
        return max(cands, key=lambda c: c.g3d)

    def match_cpu(self, name: str) -> CpuMark | None:
        """Rapproche un CPU ; teste les suffixes AMD si le nom est tronque."""
        key = normalize_cpu_name(name)
        for suffix in _CPU_SUFFIXES:
            hit = self.cpu.get(key + suffix)
            if hit:
                return hit
        return None

    def tdp_for(self, schema: str, name: str) -> int | None:
        key = normalize_gpu_name(name) if schema == "gpu" else normalize_cpu_name(name)
        if schema == "cpu":
            for suffix in _CPU_SUFFIXES:
                if key + suffix in self.tdp:
                    return self.tdp[key + suffix]
            return None
        return self.tdp.get(key)

    def gpu_reference(self) -> int | None:
        ref = self.match_gpu(GPU_REFERENCE_NAME, None)
        return ref.g3d if ref else None

    def cpu_reference(self) -> int | None:
        ref = self.match_cpu(CPU_REFERENCE_NAME)
        return ref.cpu_mark if ref else None

    def add_gpu(self, mark: GpuMark) -> None:
        self.gpu.setdefault(normalize_gpu_name(mark.source_name), []).append(mark)

    def add_cpu(self, mark: CpuMark) -> None:
        # premier arrive gagne : les pages « charts » sont deja triees par score.
        self.cpu.setdefault(normalize_cpu_name(mark.source_name), mark)

    def coverage(self) -> dict[str, int]:
        return {
            "gpuMarks": sum(len(v) for v in self.gpu.values()),
            "cpuMarks": len(self.cpu),
        }


class PassMarkSource:
    """Client HTTP poli pour PassMark (robots.txt verifie par hote)."""

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
        self._session.headers.update({"User-Agent": user_agent, "Accept-Language": "en"})
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    # -- robots.txt (un par hote) --------------------------------------------
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        host = urlsplit(url).netloc
        rp = self._robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{urlsplit(url).scheme}://{host}/robots.txt"
            try:
                resp = self._session.get(robots_url, timeout=self.timeout)
                resp.raise_for_status()
                rp.parse(resp.text.splitlines())
                log.info("robots.txt charge pour %s", host)
            except requests.RequestException as exc:  # pragma: no cover - reseau
                log.warning("robots.txt injoignable (%s) : hote %s en mode prudent", exc, host)
                rp.disallow_all = True
            self._robots[host] = rp
        return rp

    def _allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.user_agent, url)

    # -- HTTP poli ------------------------------------------------------------
    def _sleep_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)

    def _get(self, url: str) -> str:
        if not self._allowed(url):
            raise RuntimeError(f"robots.txt interdit : {url}")
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._sleep_if_needed()
            try:
                resp = self._session.get(url, timeout=self.timeout)
                self._last_request = time.monotonic()
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
            except requests.RequestException as exc:  # pragma: no cover - reseau
                last_exc = exc
                log.warning("GET %s echec (essai %d/%d) : %s", url, attempt, self.max_retries, exc)
                time.sleep(self.rate_limit * attempt)
        raise RuntimeError(f"GET impossible apres {self.max_retries} essais : {url}") from last_exc

    # -- parsing --------------------------------------------------------------
    def fetch_gpu(self, bench: Benchmarks) -> None:
        """Remplit bench.gpu depuis le tableau G3D Mark (cartes desktop seules)."""
        soup = BeautifulSoup(self._get(GPU_LIST_URL), "lxml")
        added = 0
        for row in soup.select("tr[id^=gpu]"):
            anchor = row.find("a")
            cells = row.find_all("td")
            if not anchor or len(cells) < 2:
                continue
            name = anchor.get_text(strip=True)
            low = name.lower()
            if "laptop" in low or "mobile" in low or "max-q" in low:
                continue  # dataset = cartes desktop uniquement
            mark = _to_int(cells[1].get_text())
            if mark is None:
                continue
            vm = _MEM_RE.search(low)
            bench.add_gpu(GpuMark(name, mark, int(vm.group(1)) if vm else None))
            added += 1
        log.info("PassMark GPU : %d scores G3D charges", added)

    def fetch_cpu(self, bench: Benchmarks) -> None:
        """Remplit bench.cpu depuis les pages « charts » par gamme (AMD + Intel)."""
        total = 0
        for url in CPU_CHART_URLS:
            try:
                soup = BeautifulSoup(self._get(url), "lxml")
            except Exception as exc:  # isolation par page
                log.warning("PassMark CPU : page %s ignoree (%s)", url, exc)
                continue
            for li in soup.select("li[id^=rk]"):
                anchor = li.find("a", href=True)
                count = li.find("span", class_="count")
                if not anchor or not count or "cpu.php" not in anchor["href"]:
                    continue
                name = _name_from_href(anchor["href"]) or anchor.get_text(strip=True)
                mark = _to_int(count.get_text())
                if mark is None:
                    continue
                before = len(bench.cpu)
                bench.add_cpu(CpuMark(name, mark))
                total += len(bench.cpu) - before
        log.info("PassMark CPU : %d scores CPU Mark charges", total)

    def fetch(self, *, gpu: bool = True, cpu: bool = True) -> Benchmarks:
        bench = Benchmarks(origin="passmark-live")
        if gpu:
            self.fetch_gpu(bench)
        if cpu:
            self.fetch_cpu(bench)
        return bench


def _to_int(text: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


def _name_from_href(href: str) -> str | None:
    m = re.search(r"cpu=([^&]+)", href)
    return urllib.parse.unquote_plus(m.group(1)) if m else None


# -- repli hors-ligne : instantane REEL date, sourcé par ligne -----------------
def load_curated(data_dir: Path) -> Benchmarks:
    """Recharge les scores depuis les CSV bundles (repli si reseau bloque).

    Colonnes GPU : name,g3d_mark,vram_gb,tdp_watts,source
    Colonnes CPU : name,cpu_mark,tdp_watts,source
    Les valeurs sont un instantane REEL de PassMark (date dans `source`).
    """
    bench = Benchmarks(origin="curated-csv")
    gpu_csv = data_dir / "benchmarks_gpus.csv"
    cpu_csv = data_dir / "benchmarks_cpus.csv"
    if gpu_csv.exists():
        for r in _read_csv(gpu_csv):
            mark = _to_int(r.get("g3d_mark", ""))
            if mark is None:
                continue
            vram = _to_int(r.get("vram_gb", ""))
            bench.add_gpu(GpuMark(r["name"], mark, vram))
            _add_tdp(bench, normalize_gpu_name(r["name"]), r.get("tdp_watts", ""))
    if cpu_csv.exists():
        for r in _read_csv(cpu_csv):
            mark = _to_int(r.get("cpu_mark", ""))
            if mark is None:
                continue
            bench.add_cpu(CpuMark(r["name"], mark))
            _add_tdp(bench, normalize_cpu_name(r["name"]), r.get("tdp_watts", ""))
    log.info("Repli curated charge : %s", bench.coverage())
    return bench


def load_tdp_table(data_dir: Path) -> dict[str, int]:
    """Table TDP (watts) issue des CSV bundles, indexee par cle normalisee.

    PassMark ne fournit pas le TDP ; on l'ajoute depuis les fiches constructeur
    (colonne `source` des CSV). Sert a remplir `tdp` meme en mode live.
    """
    tdp: dict[str, int] = {}
    specs = [
        (data_dir / "benchmarks_gpus.csv", normalize_gpu_name),
        (data_dir / "benchmarks_cpus.csv", normalize_cpu_name),
    ]
    for path, norm in specs:
        if not path.exists():
            continue
        for r in _read_csv(path):
            watts = _to_int(r.get("tdp_watts", ""))
            if watts is not None:
                tdp.setdefault(norm(r["name"]), watts)
    return tdp


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _add_tdp(bench: Benchmarks, key: str, raw: str) -> None:
    watts = _to_int(raw)
    if watts is not None:
        bench.tdp.setdefault(key, watts)
