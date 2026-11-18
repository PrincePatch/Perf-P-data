# -*- coding: utf-8 -*-
"""Tests de `enrich_shops.py` et des sources directes (`sources/ldlc.py`,
`sources/boutiques.py`) : lecture des pages (liste, fiche produit, « aucun
résultat », page non reconnue), correspondance stricte, remplacement des
offres par boutique, rotation, budget de temps, sauvegarde incrémentale et
coupe-circuit.

Sans réseau : sessions HTTP remplacées par des réponses fabriquées, boutiques
remplacées par des sources factices, fichiers dans un dossier temporaire. Les
noms d'offres reprennent des cas réels des données et des pages du 18/11/2026.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import enrich_shops as es  # noqa: E402
from sources import boutiques  # noqa: E402
from sources.ldlc import LdlcSource, PageNonReconnue  # noqa: E402


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

class FausseReponse:
    """Réponse HTTP fabriquée (texte, URL finale, statut, en-têtes)."""

    def __init__(self, texte, url, statut=200, entetes=None):
        self.text, self.url, self.status_code = texte, url, statut
        self.headers = entetes or {}
        self.history = []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


def brancher(src, pages):
    """Remplace la session de [src] : chaque requête reçoit la page suivante de
    [pages] — (texte, url finale ou None, statut facultatif). → URLs demandées."""
    src._rate = 0
    appels = []

    def get(url, timeout=None):
        appels.append(url)
        texte, finale, *reste = pages.pop(0)
        statut = reste[0] if reste else 200
        entetes = reste[1] if len(reste) > 1 else None
        return FausseReponse(texte, finale or url, statut, entetes)

    src._sess.get = get
    return appels


def erreur_http(statut):
    return requests.HTTPError(f"{statut} Client Error",
                              response=FausseReponse("", "https://x", statut))


def offre(produit, prix, shop="boutique-a.fr", stock=True, devise="EUR"):
    """Offre brute au schéma des sources."""
    return {"shop": shop, "price": prix, "currency": devise, "url": f"https://{shop}/p",
            "product": produit, "image": None, "in_stock": stock}


def publiee(produit, prix, shop, vu="2026-11-16T04:25:16Z", devise="EUR"):
    """Offre au schéma des fichiers de données."""
    return {"shop": shop, "price": prix, "currency": devise, "url": f"https://{shop}/p",
            "inStock": True, "lastSeen": vu, "product": produit, "image": None}


def gpu(ident, nom):
    return {"id": ident, "name": nom, "price": 500}


LISTE_LDLC = """<html><body><ul>
<li class="pdt-item"><div class="pic"><img src="https://media.ldlc.com/r150/a.jpg"></div>
<h3 class="title-3"><a href="/fiche/PB1.html">Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G</a></h3>
<div class="price">999€<sup>95</sup></div><div class="stock">En stock</div></li>
</ul></body></html>"""

AUCUN_LDLC = """<html><body><div class="result-search sbloc no-result">
0 résultat pour "GeForce RTX 4060" Aucun produit n'est actuellement disponible</div></body></html>"""

FICHE_LDLC = """<html><head><link rel="canonical" href="https://www.ldlc.com/fiche/PB00272562.html">
<script type="application/ld+json">{"@context": "https://schema.org/", "@type": "Product",
"name": "Noctua NH-U12A", "image": ["https://media.ldlc.com/r1600/ld/products/a.jpg"],
"offers": {"@type": "Offer", "priceCurrency": "EUR",
"availability": "https://schema.org/OutOfStock",
"itemCondition": "https://schema.org/NewCondition", "price": "144.95"}}</script>
</head><body><h1>Noctua NH-U12A</h1></body></html>"""

DEFI = "<html><head><title>Just a moment...</title></head><body>Vérification</body></html>"

CARTES_MORELE = """<html><body>
<div class="product-card"><img class="product-card__img" src="https://images.morele.net/i256/1.jpg">
<h2><a class="product-card__name-link" href="https://www.morele.net/karta-1/">
Karta graficzna Gigabyte GeForce RTX 5070 Windforce OC SFF 12GB GDDR7</a></h2>
<div class="product-card__price-current"> 4 504,36 zł </div>
<button class="product-card__add-to-cart">Do koszyka</button></div>
<div class="product-card"><h2><a class="product-card__name-link" href="https://www.morele.net/karta-2/">
Karta graficzna Gigabyte GeForce RTX 5070 Gaming OC 12GB GDDR7</a></h2>
<div class="product-card__price-current"> 3 999 zł </div>W Outlecie już od 3 999 zł
<button class="product-card__add-to-cart">Do koszyka</button></div>
</body></html>"""

BOITE_ALTERNATE = """<html><body>
<a class="productBox" href="https://www.alternate.fr/Patriot/1">
<div class="product-name"><span>Patriot</span> 32 Go DDR5-6000 (2x 16 Go) mémoire vive</div>
<ul><li>Capacité: 32 Go (2x 16 Go)</li><li>Timing: CL30 40-40-76</li></ul>
<span class="price">€ 519,00</span><div class="delivery-info">En stock</div></a>
</body></html>"""


# ---------------------------------------------------------------------------
# Sources : lecture des pages
# ---------------------------------------------------------------------------

class TestSources(unittest.TestCase):
    def test_ldlc_liste(self):
        src = LdlcSource()
        brancher(src, [(LISTE_LDLC, None)])
        offres = src.offres("GeForce RTX 5070")
        self.assertEqual(len(offres), 1)
        self.assertEqual(offres[0]["price"], 999.95)
        self.assertTrue(offres[0]["in_stock"])
        self.assertEqual(src.nb_requetes, 1)

    def test_ldlc_aucun_resultat_est_une_liste_vide(self):
        src = LdlcSource()
        brancher(src, [(AUCUN_LDLC, None)])
        self.assertEqual(src.offres("GeForce RTX 4060"), [])

    def test_ldlc_fiche_produit_apres_redirection(self):
        # « Noctua NH-U12A » : résultat unique → redirection vers la fiche.
        src = LdlcSource()
        brancher(src, [(FICHE_LDLC, "https://www.ldlc.com/fiche/PB00272562.html")])
        offres = src.offres("Noctua NH-U12A")
        self.assertEqual(len(offres), 1)
        self.assertEqual(offres[0]["product"], "Noctua NH-U12A")
        self.assertEqual(offres[0]["price"], 144.95)
        self.assertFalse(offres[0]["in_stock"])
        self.assertEqual(offres[0]["url"], "https://www.ldlc.com/fiche/PB00272562.html")

    def test_fiche_d_occasion_ignoree(self):
        src = LdlcSource()
        page = FICHE_LDLC.replace("NewCondition", "UsedCondition")
        brancher(src, [(page, "https://www.ldlc.com/fiche/PB1.html")])
        self.assertEqual(src.offres("Noctua NH-U12A"), [])

    def test_page_inconnue_leve_page_non_reconnue(self):
        for src in (LdlcSource(), boutiques.MaterielNetSource(), boutiques.AlternateSource(),
                    boutiques.MoreleSource(), boutiques.CanadaComputersSource()):
            brancher(src, [(DEFI, None)])
            with self.assertRaises(PageNonReconnue, msg=type(src).__name__):
                src.offres("GeForce RTX 5070")

    def test_barre_oblique_retiree_du_chemin(self):
        # « ASRock Z790 PG-ITX/TB4 » donnait un 404 chez Materiel.net.
        src = boutiques.MaterielNetSource()
        appels = brancher(src, [(AUCUN_LDLC, None)])
        src.offres("ASRock Z790 PG-ITX/TB4")
        self.assertEqual(appels[0], "https://www.materiel.net/recherche/ASRock%20Z790%20PG-ITX%20TB4/")

    def test_morele_nouvelle_mise_en_page_et_outlet(self):
        src = boutiques.MoreleSource()
        brancher(src, [(CARTES_MORELE, None)])
        offres = src.offres("GeForce RTX 5070")
        self.assertEqual(len(offres), 1)  # la carte « W Outlecie » est écartée
        self.assertEqual(offres[0]["price"], 4504.36)
        self.assertEqual(offres[0]["currency"], "PLN")
        self.assertTrue(offres[0]["in_stock"])

    def test_morele_aucun_resultat(self):
        src = boutiques.MoreleSource()
        brancher(src, [("<html><body>Szukasz zzqvx ( 0 ) Brak wyników :(</body></html>", None)])
        self.assertEqual(src.offres("zzqvx"), [])

    def test_alternate_latence_ajoutee_au_nom(self):
        src = boutiques.AlternateSource()
        brancher(src, [(BOITE_ALTERNATE, None)])
        offres = src.offres("DDR5 32 Go 6000 MHz CL30")
        self.assertEqual(offres[0]["product"], "Patriot 32 Go DDR5-6000 (2x 16 Go) mémoire vive CL30")
        self.assertEqual(offres[0]["price"], 519.0)

    def test_429_sans_attente_au_dela_de_l_echeance(self):
        src = boutiques.MoreleSource()
        brancher(src, [("", None, 429, {"Retry-After": "30"})])
        src.echeance = time.time() + 5
        debut = time.time()
        with self.assertRaises(requests.HTTPError):
            src.offres("RTX 5070")
        self.assertLess(time.time() - debut, 2)
        self.assertEqual(src.nb_requetes, 1)

    def test_429_puis_succes(self):
        src = boutiques.MoreleSource()
        brancher(src, [("", None, 429, {"Retry-After": "0"}), (CARTES_MORELE, None)])
        self.assertEqual(len(src.offres("RTX 5070")), 1)
        self.assertEqual(src.nb_requetes, 2)


# ---------------------------------------------------------------------------
# Requête et correspondance
# ---------------------------------------------------------------------------

RAM_32 = {"id": "ddr5_32_6000c30", "name": "32 Go DDR5-6000 CL30", "sizeGb": 32,
          "mhz": 6000, "cl": 30, "kind": "DDR5", "price": 120}
RAM_16 = dict(RAM_32, id="ddr5_16_6000c30", name="16 Go DDR5-6000 CL30", sizeGb=16)


def garde(cat, it, nom, items=None):
    rivaux = es.rivaux_specifiques(cat, items or [it]).get(it["id"], ())
    return es.garder_offre(cat, it, es.CATEGORIES[cat][4](it), nom, rivaux)


class TestCorrespondance(unittest.TestCase):
    def test_requete_memoire_selon_les_unites(self):
        self.assertEqual(es.requete_boutique("rams", RAM_32, "fr"), "DDR5 32 Go 6000 MHz CL30")
        self.assertEqual(es.requete_boutique("rams", RAM_32, "en"), "DDR5 6000 32GB CL30")
        self.assertEqual(es.requete_boutique("rams", RAM_32, "fr",
                                             boutiques.AlternateSource.requete_memoire),
                         "32 Go DDR5-6000")
        self.assertEqual(es.requete_boutique("gpus", gpu("g", "GeForce RTX 5070"), "fr"),
                         "GeForce RTX 5070")

    def test_capacite_totale_des_kits(self):
        self.assertEqual(es.capacite_ram_go("Kingston Fury Beast - 2 x 16 Go (32 Go) - DDR5 6000 MHz"), 32)
        self.assertEqual(es.capacite_ram_go("G.SKILL Trident Z5 Neo RGB 64GB (2x32GB) DDR5 6000MHz CL30"), 64)
        self.assertEqual(es.capacite_ram_go("G.Skill Flare X5 - 1 x 32 Go - DDR5 6000 MHz - CL30"), 32)
        self.assertEqual(es.capacite_ram_go("Patriot 32 Go DDR5-6000 (2x 16 Go) mémoire vive CL30"), 32)
        self.assertIsNone(es.capacite_ram_go("DDR5 6000 CL30"))

    def test_memoire_autre_capacite_ou_portable_ecartee(self):
        self.assertTrue(garde("rams", RAM_32, "Kingston Fury Beast - 2 x 16 Go (32 Go) - DDR5 6000 MHz - CL30"))
        self.assertFalse(garde("rams", RAM_16, "Kingston Fury Beast - 2 x 16 Go (32 Go) - DDR5 6000 MHz - CL30"))
        self.assertFalse(garde("rams", RAM_32, "G.Skill Ripjaws M5 - 2 x 32 Go (64 Go) - DDR5 6000 MHz - CL30"))
        self.assertFalse(garde("rams", RAM_32, "G.Skill Ripjaws SO-DIMM - 2 x 16 Go (32 Go) - DDR5 6000 MHz - CL30"))
        self.assertFalse(garde("rams", RAM_32, "Kingston FURY 32 Go DDR5-6000 (2x 16 Go) mémoire PC portable CL30"))
        self.assertTrue(garde("rams", RAM_32, "Patriot 32 Go DDR5-6000 (2x 16 Go) mémoire vive CL30"))

    def test_composant_plus_precis_du_catalogue(self):
        items = [gpu("rtx3050", "GeForce RTX 3050"), gpu("rtx3050-6g", "GeForce RTX 3050 6G"),
                 gpu("rx570", "Radeon RX 570"), gpu("rx5700xt", "Radeon RX 5700 XT")]
        rtx3050, rtx3050_6g, rx570 = items[0], items[1], items[2]
        self.assertFalse(garde("gpus", rtx3050, "MSI GeForce RTX 3050 VENTUS 2X 6G OC", items))
        self.assertTrue(garde("gpus", rtx3050_6g, "MSI GeForce RTX 3050 VENTUS 2X 6G OC", items))
        self.assertTrue(garde("gpus", rtx3050, "ASUS Dual GeForce RTX 3050 OC Edition 8GB GDDR6", items))
        self.assertFalse(garde("gpus", rx570, "MSI Radeon RX 5700 XT MECH OC", items))
        noctua = [{"id": "d15", "name": "Noctua NH-D15"}, {"id": "d15s", "name": "Noctua NH-D15S"},
                  {"id": "d15g2", "name": "Noctua NH-D15 G2"}]
        self.assertFalse(garde("coolers", noctua[0], "Noctua NH-D15S chromax.noir Refroidisseur CPU", noctua))
        self.assertFalse(garde("coolers", noctua[0], "Noctua NH-D15 G2 LBC Refroidisseur CPU", noctua))
        self.assertTrue(garde("coolers", noctua[0], "Noctua NH-D15 chromax.black Refroidisseur CPU", noctua))

    def test_unite_ne_rend_pas_plus_precis(self):
        items = [{"id": "sn850x", "name": "WD Black SN850X (Gen4)"},
                 {"id": "sn850x-1tb", "name": "WD Black SN850X 1 To", "sizeGb": 1000}]
        self.assertTrue(garde("ssds", items[0], "WD Black SN850X NVMe 1 To SSD", items))

    def test_numero_de_modele_prolonge_par_un_chiffre(self):
        p3 = {"id": "p3", "name": "Crucial P3 1 To", "sizeGb": 1000}
        self.assertFalse(garde("ssds", p3, "Crucial P310 1 To 2280"))
        self.assertFalse(garde("ssds", p3, "Patriot P300 1 To SSD"))
        self.assertTrue(garde("ssds", p3, "Crucial P3 1 To M.2 NVMe"))
        self.assertTrue(garde("gpus", gpu("rtx5070", "GeForce RTX 5070"), "MSI RTX5070 VENTUS 2X 12G"))

    def test_pc_monte_ou_carton_ouvert_ecarte(self):
        rx5700 = gpu("rx5700", "Radeon RX 5700")
        self.assertFalse(garde("gpus", rx5700, "ARMOURY AMD Ryzen 7 5700 Radeon RX 9060 XT 16GB RAM 1TB SSD W11H"))
        rtx5070 = gpu("rtx5070", "GeForce RTX 5070")
        self.assertFalse(garde("gpus", rtx5070, "GIGABYTE GeForce RTX 5070 GAMING OC 12G (Open Box)"))
        self.assertTrue(garde("gpus", rtx5070, "GIGABYTE GeForce RTX 5070 GAMING OC 12G"))

    def test_refroidissements_et_boitiers_par_les_mots(self):
        mc = es.modele_coherent
        self.assertFalse(mc("be quiet! Shadow Rock 3", "be quiet! Pure Wings 3 120 mm PWM - Noir"))
        self.assertFalse(mc("be quiet! Pure Rock 2", "be quiet! Pure Rock 3 Noir Refroidisseur CPU"))
        self.assertFalse(mc("be quiet! Pure Rock 2", "be quiet! Pure Rock Slim 2 Refroidisseur CPU"))
        self.assertTrue(mc("be quiet! Pure Rock 2", "be quiet! Pure Rock 2 Black"))
        self.assertFalse(mc("Thermalright Peerless Assassin 120", "Thermalright Peerless Assassin 120 SE ARGB"))
        self.assertTrue(mc("ASUS Prime AP201", "ASUS Prime AP201 boîtier mini tower"))
        self.assertTrue(mc("Fractal Terra (ITX)", "Fractal Design Terra boîtier mini tower"))
        self.assertFalse(mc("Lian Li O11 Dynamic", "Lian Li O11 Dynamic Mini V2 Flow boîtier mini tower"))
        self.assertTrue(mc("Fractal North", "Fractal Design North Charcoal Black TG Dark"))
        self.assertFalse(mc("Fractal North", "Fractal Design North XL boîtier midi tower"))

    def test_composant_sans_achat(self):
        self.assertTrue(es.sans_achat({"id": "refroidisseur-stock", "name": "Refroidisseur stock",
                                       "price": 0.0}))
        self.assertFalse(es.sans_achat({"id": "x", "name": "Noctua NH-D15", "price": 99}))


# ---------------------------------------------------------------------------
# Fusion, revalidation, rotation
# ---------------------------------------------------------------------------

DIRECTS = ("ldlc.com", "materiel.net", "alternate.fr")


class TestFusion(unittest.TestCase):
    def entree(self):
        return {"id": "rtx5070", "name": "GeForce RTX 5070", "priceMin": 1.0, "image": None,
                "prices": [publiee("Ancienne LDLC", 1100.0, "ldlc.com"),
                           publiee("Offre Amazon", 1050.0, "amazon.fr"),
                           publiee("Offre Alternate", 1000.0, "alternate.fr")]}

    def test_remplace_les_offres_de_la_boutique(self):
        e = self.entree()
        ajoutees, retirees = es.fusionner_offres(
            e, "ldlc.com", [offre("Nouvelle LDLC", 990.0, "ldlc.com")], "Q", DIRECTS)
        self.assertEqual((ajoutees, retirees), (1, 1))
        self.assertEqual(sorted(p["product"] for p in e["prices"]),
                         ["Nouvelle LDLC", "Offre Alternate", "Offre Amazon"])
        self.assertEqual(e["priceMin"], 990.0)
        self.assertEqual(e["lastUpdated"], "Q")

    def test_offre_plus_trouvee_disparait(self):
        e = self.entree()
        self.assertEqual(es.fusionner_offres(e, "ldlc.com", [], "Q", DIRECTS), (0, 1))
        self.assertNotIn("ldlc.com", [p["shop"] for p in e["prices"]])
        self.assertEqual(e["priceMin"], 1000.0)

    def test_derniere_offre_retiree_price_min_null(self):
        e = {"id": "x", "name": "X", "priceMin": 50.0, "image": "images/x.jpg",
             "prices": [publiee("Seule", 50.0, "ldlc.com")]}
        es.fusionner_offres(e, "ldlc.com", [], "Q", DIRECTS)
        self.assertEqual(e["prices"], [])
        self.assertIsNone(e["priceMin"])
        self.assertEqual(e["image"], "images/x.jpg")

    def test_rien_a_changer(self):
        e = self.entree()
        avant = json.dumps(e)
        self.assertEqual(es.fusionner_offres(e, "materiel.net", [], "Q", DIRECTS), (0, 0))
        self.assertEqual(json.dumps(e), avant)

    def test_nouvelle_offre_aberrante_ecartee(self):
        e = self.entree()
        ajoutees, _ = es.fusionner_offres(
            e, "ldlc.com", [offre("Bundle caché", 2600.0, "ldlc.com")], "Q", DIRECTS)
        self.assertEqual(ajoutees, 0)

    def test_revalidation_des_offres_publiees(self):
        existant = {RAM_16["id"]: {"id": RAM_16["id"], "name": RAM_16["name"], "priceMin": 1.0,
                                   "prices": [
                                       publiee("Kingston Fury Beast - 2 x 16 Go (32 Go) - DDR5 6000 MHz - CL30",
                                               841.95, "materiel.net"),
                                       publiee("Kingston FURY Beast 16GB DDR5 6000 CL30", 400.0, "amazon.fr")]}}
        retirees, vides = es.revalider_existant("rams", [RAM_16], existant, {}, DIRECTS)
        self.assertEqual((retirees, vides), (1, 0))
        self.assertEqual([p["shop"] for p in existant[RAM_16["id"]]["prices"]], ["amazon.fr"])
        self.assertEqual(existant[RAM_16["id"]]["priceMin"], 400.0)

    def test_ordre_de_rotation(self):
        items = [gpu("a", "A 1000"), gpu("b", "B 2000"), gpu("c", "C 3000"), gpu("d", "D 4000")]
        taches = [(0, i, "gpus", it) for i, it in enumerate(items)]
        visites = {"ldlc": {"gpus/a": "2026-11-18T00:00:00Z", "gpus/b": "2026-11-17T00:00:00Z"}}
        existants = {"gpus": {"c": {"prices": [publiee("C", 1.0, "ldlc.com", vu="2026-11-10T00:00:00Z")]}}}
        ordre = [t[3]["id"] for t in es.ordre_rotation(taches, "ldlc", "ldlc.com", visites, existants)]
        # d jamais vu, puis c (offre du 10/11, sans état), b (17/11), a (18/11).
        self.assertEqual(ordre, ["d", "c", "b", "a"])

    def test_ecriture_atomique(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = os.path.join(d, "x.json")
            es.ecrire_json(chemin, [{"id": "a"}])
            with open(chemin, encoding="utf-8") as f:
                self.assertEqual(json.load(f), [{"id": "a"}])
            self.assertFalse(os.path.exists(chemin + ".tmp"))


# ---------------------------------------------------------------------------
# Passage complet avec des boutiques factices
# ---------------------------------------------------------------------------

class SourceFactice:
    """Boutique sans réseau : [reponse(requete)] renvoie des offres ou lève."""

    unites = "fr"

    def __init__(self, domaine, reponse, delai=0.0):
        self.domaine, self.devise = domaine, "EUR"
        self.reponse, self.delai = reponse, delai
        self.nb_requetes = 0
        self.requetes = []

    def offres(self, requete):
        self.nb_requetes += 1
        self.requetes.append(requete)
        if self.delai:
            time.sleep(self.delai)
        r = self.reponse(requete)
        if isinstance(r, Exception):
            raise r
        return r


class TestPassage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.items = os.path.join(self.tmp.name, "catalog")
        os.makedirs(self.data)
        os.makedirs(self.items)
        self.reglages = mock.patch.multiple(es, PAUSE_ECHEC_S=0, PAUSE_REPRISE_S=0, SAUVEGARDE_S=0)
        self.reglages.start()

    def tearDown(self):
        self.reglages.stop()
        self.tmp.cleanup()

    def ecrire(self, dossier, nom, donnees):
        with open(os.path.join(dossier, nom), "w", encoding="utf-8") as f:
            json.dump(donnees, f, ensure_ascii=False)

    def lire(self, nom):
        with open(os.path.join(self.data, nom), encoding="utf-8") as f:
            return json.load(f)

    def lancer(self, sources, *options):
        sortie, erreurs = io.StringIO(), io.StringIO()
        with mock.patch.dict(es.BOUTIQUES, {n: (lambda s=s: s) for n, s in sources.items()},
                             clear=True), redirect_stdout(sortie), redirect_stderr(erreurs):
            es.principal(["--data-repo", self.data, "--items-dir", self.items,
                          "--shops", ",".join(sources), *options])
        return sortie.getvalue() + erreurs.getvalue()

    def test_remplacement_echec_et_recapitulatif(self):
        self.ecrire(self.items, "gpus.json", [gpu("rtx5070", "GeForce RTX 5070"),
                                              gpu("rtx5080", "GeForce RTX 5080")])
        self.ecrire(self.items, "coolers.json", [
            {"id": "refroidisseur-stock", "name": "Refroidisseur stock", "price": 0.0},
            {"id": "nh-d15", "name": "Noctua NH-D15", "price": 99}])
        self.ecrire(self.data, "gpus_app.json", [
            {"id": "rtx5070", "name": "GeForce RTX 5070", "priceMin": 1.0, "image": None,
             "prices": [publiee("Gigabyte GeForce RTX 5070 EAGLE OC 12G", 1200.0, "boutique-b.fr")]},
            {"id": "rtx5080", "name": "GeForce RTX 5080", "priceMin": 1.0, "image": None,
             "prices": [publiee("MSI GeForce RTX 5080 VENTUS", 1300.0, "boutique-a.fr"),
                        publiee("Offre Amazon RTX 5080", 1400.0, "amazon.fr")]}])

        def reponse_a(requete):
            if requete == "GeForce RTX 5070":
                return [offre("Gigabyte GeForce RTX 5070 WINDFORCE OC 12G", 999.95)]
            if requete == "Noctua NH-D15":
                return [offre("Noctua NH-D15 chromax.black Refroidisseur CPU", 99.9)]
            return []  # la RTX 5080 n'est plus vendue : visite réussie, aucune offre

        a = SourceFactice("boutique-a.fr", reponse_a)
        b = SourceFactice("boutique-b.fr", lambda r: erreur_http(403))
        log = self.lancer({"a": a, "b": b}, "--only", "gpus,coolers")

        gpus = {e["id"]: e for e in self.lire("gpus_app.json")}
        # RTX 5070 : nouvelle offre de A, l'offre de B (en échec) est gardée.
        self.assertEqual(sorted(p["shop"] for p in gpus["rtx5070"]["prices"]),
                         ["boutique-a.fr", "boutique-b.fr"])
        # RTX 5080 : l'ancienne offre de A disparaît, celle d'Amazon reste.
        self.assertEqual([p["shop"] for p in gpus["rtx5080"]["prices"]], ["amazon.fr"])
        self.assertEqual(gpus["rtx5080"]["priceMin"], 1400.0)
        coolers = {e["id"]: e for e in self.lire("coolers.json")}
        self.assertIn("nh-d15", coolers)
        # Le refroidisseur livré avec le processeur n'est jamais recherché.
        self.assertNotIn("Refroidisseur stock", a.requetes)
        etat = self.lire(es.FICHIER_ETAT)
        self.assertEqual(sorted(etat["visites"]["a"]), ["coolers/nh-d15", "gpus/rtx5070", "gpus/rtx5080"])
        self.assertNotIn("b", etat["visites"])  # aucune visite réussie
        self.assertIn("Récapitulatif par boutique", log)
        self.assertIn("HTTP 403 ×3", log)
        self.assertRegex(log, r"a: \d+ requêtes, 3 composants lus \(2 avec correspondance, 1 sans\)")

    def test_budget_sauvegarde_incrementale_et_rotation(self):
        items = [gpu(f"g{i}", f"GeForce RTX {5000 + i}") for i in range(12)]
        self.ecrire(self.items, "gpus.json", items)
        vu_sur_disque = []

        def source(delai):
            def reponse(requete):
                if len(src.requetes) == 3:  # 3e requête : le 1er composant est-il déjà écrit ?
                    try:
                        vu_sur_disque.extend(e["id"] for e in self.lire("gpus_app.json"))
                    except OSError:
                        pass
                return [offre(f"MSI {requete} VENTUS", 500.0)]
            src = SourceFactice("boutique-a.fr", reponse, delai)
            return src

        a = source(0.3)
        debut = time.time()
        self.lancer({"a": a}, "--only", "gpus", "--budget-min", "0.05")  # 3 s
        duree = time.time() - debut
        self.assertLess(duree, 4.0)
        premiers = set(self.lire(es.FICHIER_ETAT)["visites"]["a"])
        self.assertTrue(0 < len(premiers) < 12, premiers)
        self.assertTrue(vu_sur_disque, "aucune sauvegarde avant la fin du passage")
        donnees = self.lire("gpus_app.json")  # JSON valide, écrit avant l'échéance
        self.assertEqual({e["id"] for e in donnees}, {c.split("/")[1] for c in premiers})

        # 2e passage : les composants jamais visités passent d'abord.
        a2 = source(0.0)
        self.lancer({"a": a2}, "--only", "gpus")
        jamais = 12 - len(premiers)
        attendues = {f"GeForce RTX {5000 + i}" for i in range(12)
                     if f"gpus/g{i}" not in premiers}
        self.assertEqual(set(a2.requetes[:jamais]), attendues)
        self.assertEqual(len(self.lire(es.FICHIER_ETAT)["visites"]["a"]), 12)

    def test_coupure_apres_seconde_chance(self):
        self.ecrire(self.items, "gpus.json", [gpu(f"g{i}", f"GeForce RTX {5000 + i}") for i in range(9)])
        b = SourceFactice("boutique-b.fr", lambda r: requests.ConnectionError("connexion coupée"))
        log = self.lancer({"b": b}, "--only", "gpus")
        # 6 échecs → pause et seconde chance → 7e échec → coupée.
        self.assertEqual(b.nb_requetes, 7)
        self.assertIn("seconde chance", log)
        self.assertIn("ignorée pour la fin du passage", log)
        self.assertIn("coupée (blocage probable), 2 composant(s) non visité(s) sur 9", log)

    def test_page_non_reconnue_et_404_gardent_les_offres(self):
        self.ecrire(self.items, "gpus.json", [gpu("rtx5070", "GeForce RTX 5070"),
                                              gpu("rtx5080", "GeForce RTX 5080")])
        anciennes = [
            {"id": "rtx5070", "name": "GeForce RTX 5070", "priceMin": 1.0, "image": None,
             "prices": [publiee("Gigabyte GeForce RTX 5070 EAGLE", 1000.0, "boutique-a.fr")]},
            {"id": "rtx5080", "name": "GeForce RTX 5080", "priceMin": 1.0, "image": None,
             "prices": [publiee("MSI GeForce RTX 5080 VENTUS", 1300.0, "boutique-a.fr")]}]
        self.ecrire(self.data, "gpus_app.json", anciennes)

        def reponse(requete):
            return PageNonReconnue("défi") if "5070" in requete else erreur_http(404)

        a = SourceFactice("boutique-a.fr", reponse)
        log = self.lancer({"a": a}, "--only", "gpus")
        self.assertEqual(self.lire("gpus_app.json"), anciennes)  # rien retiré
        self.assertEqual(list(self.lire(es.FICHIER_ETAT)["visites"]["a"]), ["gpus/rtx5080"])
        self.assertIn("page non reconnue ×1", log)
        self.assertIn("HTTP 404 ×1", log)


if __name__ == "__main__":
    unittest.main()
