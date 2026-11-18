# -*- coding: utf-8 -*-
"""Tests SANS réseau de `verifier_amazon.py`.

Lancement, depuis `App/` :
    python -m unittest discover -s scraper/tests -p "test_verifier_amazon.py"

Les fixtures HTML sont des extraits réduits de vraies fiches mobiles (novembre
2026) : bloc panier vide suivi d'un widget à prix (le « RTX 5080 à 67,42 € »
d'amazon.es), ancien rendu au prix dans `a-offscreen`, rendu actuel
d'amazon.fr où `a-offscreen` est vide et le prix découpé en morceaux.
"""

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import verifier_amazon as va  # noqa: E402


# --- fixtures HTML -------------------------------------------------------------

def _widget(nom, contenu):
    """Un widget Amazon tel qu'il apparaît dans la page (id + slot-id)."""
    return (f'<div id="{nom}_feature_div" data-csa-c-asin="" '
            f'data-csa-c-content-id="{nom}" data-csa-c-slot-id="{nom}_feature_div" '
            f'data-csa-c-type="widget" class="a-section a-spacing-none celwidget">'
            f'{contenu}</div>')


# Bloc panier VIDE (article non vendu par Amazon sur ce site), suivi d'un
# widget de garantie dont le prix tombe dans les 20 000 caractères qu'avalait
# l'ancienne lecture.
WIDGET_GARANTIE = _widget(
    "mapleAdvertisement",
    '<div class="a-row"><span>Protection 3 ans</span>'
    '<span class="a-price" data-a-size="m" data-a-color="base">'
    '<span class="a-offscreen">67,42€</span><span aria-hidden="true">'
    '<span class="a-price-whole">67<span class="a-price-decimal">,</span></span>'
    '<span class="a-price-fraction">42</span><span class="a-price-symbol">€</span>'
    '</span></span></div>')
PAGE_BLOC_VIDE = (
    "<html><head><title>Gigabyte GeForce RTX 5080 Gaming OC 16G : Amazon.es</title>"
    "</head><body>"
    + _widget("dealBadge", " ")
    + _widget("corePrice_mobile",
              '<div class="a-row a-spacing-small"> <div class="a-column a-span12">'
              ' </div> </div>')
    + _widget("paymentOptions_Priceblock",
              "<!-- <div> dans un commentaire ne doit rien ouvrir -->"
              '<div class="a-section aok-hidden"><input type="checkbox"/></div>')
    + WIDGET_GARANTIE
    + '<div id="availability" class="a-section"> </div>'
    + "</body></html>")

# Ancien rendu : prix dans `a-offscreen`, précédé d'un prix conseillé BARRÉ.
PAGE_ANCIEN_RENDU = (
    "<html><body>"
    + _widget("corePrice_mobile",
              '<div class="a-section">'
              '<span class="a-price a-text-price" data-a-size="s" data-a-strike="true" '
              'data-a-color="secondary"><span class="a-offscreen">1.499,00 €</span>'
              '<span aria-hidden="true">1.499,00 €</span></span>'
              '<span class="a-price aok-align-center" data-a-size="xl" data-a-color="base">'
              '<span class="a-offscreen">1.299,99 €</span><span aria-hidden="true">'
              '<span class="a-price-whole">1.299<span class="a-price-decimal">,</span>'
              '</span><span class="a-price-fraction">99</span>'
              '<span class="a-price-symbol">€</span></span></span>'
              '<span class="a-price a-text-price" data-a-size="mini">'
              '<span class="a-offscreen">81,25 €</span></span> / unité</div>')
    + WIDGET_GARANTIE
    + '<div id="availability"><span>En stock</span></div>'
    + "</body></html>")

# Rendu actuel d'amazon.fr (fiche be quiet! Silent Wings 4 relevée le
# 18/11/2026) : `a-offscreen` VIDE, prix en morceaux + libellé d'accessibilité.
CORE_PRICE_A_PAYER = (
    '<style type="text/css"> .savingPriceOverride { color: #CC0C39 } </style>'
    '<div class="a-section apex-core-price-identifier">'
    '<div class="a-section a-spacing-none aok-align-center aok-relative">'
    '<span id="apex-pricetopay-accessibility-label" '
    'data-pricetopay-label="{priceToPay}" class="aok-offscreen"> 30,37\xa0€ </span>'
    '<span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay '
    'apex-pricetopay-value" data-a-size="xxl" data-a-color="base">'
    '<span class="a-offscreen"> </span><span aria-hidden="true">'
    '<span class="a-price-whole">30<span class="a-price-decimal">,</span></span>'
    '<span class="a-price-fraction">37</span><span class="a-price-symbol">€</span>'
    '</span></span></div></div>')
PAGE_PRICE_TO_PAY = (
    "<html><body>"
    + _widget("corePrice_mobile", '<div class="a-row"> </div>')
    + _widget("corePriceDisplay_mobile", CORE_PRICE_A_PAYER)
    + WIDGET_GARANTIE
    + '<div id="availability"><span>Il ne reste plus que 1 exemplaire(s) en '
      'stock.</span></div>'
    + "</body></html>")


def _page_avec_bloc(nom, contenu):
    return "<html><body>" + _widget(nom, contenu) + WIDGET_GARANTIE + "</body></html>"


# --- lecture du prix -----------------------------------------------------------

class TestPrix(unittest.TestCase):

    def test_bloc_vide_suivi_d_un_widget_a_prix_donne_pas_de_prix(self):
        # La fixture reproduit bien le défaut : le prix parasite tombe dans la
        # fenêtre de 20 000 caractères de l'ancienne lecture.
        i = PAGE_BLOC_VIDE.find('id="corePrice_mobile_feature_div"')
        self.assertIn("67,42", PAGE_BLOC_VIDE[i:i + 20000])
        self.assertEqual(va.prix(PAGE_BLOC_VIDE, "amazon.es"), (None, None, ""))

    def test_contenu_bloc_s_arrete_a_la_fermeture_du_bloc(self):
        contenu = va.contenu_bloc(PAGE_BLOC_VIDE, "corePrice_mobile_feature_div")
        self.assertIsNotNone(contenu)
        self.assertNotIn("67,42", contenu)
        self.assertNotIn("paymentOptions", contenu)

    def test_bloc_absent_donne_none(self):
        self.assertIsNone(va.contenu_bloc(PAGE_BLOC_VIDE, "corePrice_feature_div"))

    def test_ancien_rendu_prend_le_prix_a_payer_pas_le_prix_barre(self):
        self.assertEqual(va.prix(PAGE_ANCIEN_RENDU, "amazon.de"),
                         (1299.99, "EUR", "1.299,99 €"))

    def test_rendu_actuel_offscreen_vide_lu_en_morceaux(self):
        montant, dev, _ = va.prix(PAGE_PRICE_TO_PAY, "amazon.fr")
        self.assertEqual((montant, dev), (30.37, "EUR"))

    def test_morceaux_sans_libelle_d_accessibilite(self):
        sans_libelle = CORE_PRICE_A_PAYER.replace(
            'id="apex-pricetopay-accessibility-label"', 'id="autre"')
        page = _page_avec_bloc("corePriceDisplay_mobile", sans_libelle)
        self.assertEqual(va.prix(page, "amazon.fr")[:2], (30.37, "EUR"))

    def test_libelle_d_accessibilite_seul(self):
        page = _page_avec_bloc(
            "corePriceDisplay_mobile",
            '<span id="apex-pricetopay-accessibility-label" class="aok-offscreen">'
            ' 249,90 € </span>')
        self.assertEqual(va.prix(page, "amazon.fr")[:2], (249.9, "EUR"))

    def test_prix_a_payer_prefere_au_premier_prix_ordinaire(self):
        page = _page_avec_bloc(
            "corePriceDisplay_desktop",
            '<span class="a-price" data-a-size="s"><span class="a-offscreen">'
            '12,00 €</span></span>'
            '<span class="a-price a-text-price apexPriceToPay" data-a-size="b">'
            '<span class="a-offscreen">186,61 €</span></span>')
        self.assertEqual(va.prix(page, "amazon.fr")[:2], (186.61, "EUR"))

    def test_format_americain(self):
        page = _page_avec_bloc(
            "corePriceDisplay_mobile",
            '<span class="a-price priceToPay" data-a-size="xl">'
            '<span class="a-offscreen">$1,299.99</span></span>')
        self.assertEqual(va.prix(page, "amazon.com"), (1299.99, "USD", "$1,299.99"))

    def test_seul_un_prix_unitaire_ou_barre_donne_pas_de_prix(self):
        page = _page_avec_bloc(
            "corePrice_mobile",
            '<span class="a-price a-text-price" data-a-size="mini">'
            '<span class="a-offscreen">8,12 €</span></span>'
            '<span class="a-price" data-a-strike="true">'
            '<span class="a-offscreen">99,00 €</span></span>')
        self.assertEqual(va.prix(page, "amazon.fr"), (None, None, ""))

    def test_panier_strict_ignore_un_prix_qui_n_est_pas_a_payer(self):
        garantie = ('<div id="buybox"><div class="a-row">Ajouter une protection '
                    '<span class="a-price"><span class="a-offscreen">49,99 €'
                    '</span></span></div></div>')
        self.assertEqual(va.prix("<html><body>" + garantie + "</body></html>",
                                 "amazon.fr"), (None, None, ""))
        a_payer = garantie.replace('class="a-price"', 'class="a-price priceToPay"')
        self.assertEqual(va.prix("<html><body>" + a_payer + "</body></html>",
                                 "amazon.fr")[:2], (49.99, "EUR"))

    def test_page_tronquee_s_arrete_au_widget_suivant(self):
        # Le bloc n'est jamais refermé (page coupée au premier mégaoctet) : la
        # lecture s'arrête au widget suivant au lieu de lire son prix.
        page = ('<html><body><div id="corePrice_mobile_feature_div" class="x">'
                '<div class="a-row"> ' + WIDGET_GARANTIE)
        self.assertEqual(va.prix(page, "amazon.es"), (None, None, ""))

    def test_slot_id_ne_se_confond_pas_avec_l_id(self):
        # Un AUTRE élément porte le même slot-id avant le vrai bloc.
        page = ('<html><body><div data-csa-c-slot-id="corePrice_mobile_feature_div">'
                '<span class="a-price"><span class="a-offscreen">5,00 €</span></span>'
                '</div>'
                + _widget("corePrice_mobile",
                          '<span class="a-price"><span class="a-offscreen">'
                          '77,00 €</span></span>')
                + "</body></html>")
        self.assertEqual(va.prix(page, "amazon.fr")[:2], (77.0, "EUR"))

    def test_div_dans_un_script_ne_desequilibre_pas_le_bloc(self):
        page = _page_avec_bloc(
            "corePrice_mobile",
            '<script>var a = "<div><div>";</script><div class="a-row"> </div>')
        self.assertEqual(va.prix(page, "amazon.es"), (None, None, ""))


# --- fausses données ------------------------------------------------------------

def _offre(shop, asin, prix=10.0, vu="2026-11-01T00:00:00Z", url=None):
    return {"shop": shop, "price": prix, "currency": va.DEVISES[shop],
            "url": url or va.lien(shop, asin), "inStock": True,
            "lastSeen": vu, "product": "x", "image": None}


def _gputracker(shop, prix=99.0):
    return {"shop": shop, "price": prix, "currency": "EUR",
            "url": "https://www.gputracker.eu/si-click/123", "inStock": True,
            "lastSeen": "2026-11-01T00:00:00Z", "product": "y", "image": None}


class TestFileAttente(unittest.TestCase):

    def setUp(self):
        self.cache = {
            "gpus": {"g1": {"amazon.fr": "B0000000G1", "amazon.de": "B0000000G1",
                            "amazon.es": None, "amazon.it": False}},
            "fans": {"f1": {"amazon.fr": "B0000000F1"},
                     "f2": {"amazon.fr": "B0000000F2"},
                     "f3": {"amazon.fr": "B0000000F3"},
                     "hors_catalogue": {"amazon.fr": "B0000000F9"}},
        }
        self.items = {"gpus": {"g1": {"name": "G1"}},
                      "fans": {"f1": {"name": "F1"}, "f2": {"name": "F2"},
                               "f3": {"name": "F3"}}}
        self.verif = {"gpus": {"g1": {"amazon.fr": "2026-11-18T00:00:00Z",
                                      "amazon.de": "2026-11-10T00:00:00Z"}},
                      "fans": {"f2": {"amazon.fr": "2026-11-15T00:00:00Z"}}}
        # f1 n'a pas de date mémorisée mais une offre publiée le 01/11 ; f3
        # n'a ni l'un ni l'autre : jamais vérifié.
        self.offres = {"gpus": {}, "fans": {"f1": {"id": "f1", "prices": [
            _offre("amazon.fr", "B0000000F1", vu="2026-11-01T00:00:00Z")]}}}
        self.marches = ["amazon.fr", "amazon.de", "amazon.es", "amazon.it"]

    def _ordre(self, **kw):
        return [(t.cat, t.cid, t.marche) for t in va.file_attente(
            self.cache, self.verif, self.offres, self.items, self.marches, **kw)]

    def test_plus_anciennement_verifie_d_abord_toutes_categories(self):
        self.assertEqual(self._ordre(), [
            ("fans", "f3", "amazon.fr"),   # jamais vérifié : en tête
            ("fans", "f1", "amazon.fr"),   # 01/11 (date de l'offre publiée)
            ("gpus", "g1", "amazon.de"),   # 10/11
            ("fans", "f2", "amazon.fr"),   # 15/11
            ("gpus", "g1", "amazon.fr"),   # 18/11
        ])

    def test_couples_sans_asin_et_composants_hors_catalogue_ignores(self):
        ordre = self._ordre()
        self.assertNotIn(("gpus", "g1", "amazon.es"), ordre)
        self.assertNotIn(("gpus", "g1", "amazon.it"), ordre)
        self.assertNotIn(("fans", "hors_catalogue", "amazon.fr"), ordre)

    def test_frais_h_ecarte_les_couples_recents(self):
        maintenant = va._horodatage("2026-11-18T06:00:00Z")
        ordre = self._ordre(frais_h=12, maintenant=maintenant)
        self.assertNotIn(("gpus", "g1", "amazon.fr"), ordre)  # vu il y a 6 h
        self.assertIn(("fans", "f3", "amazon.fr"), ordre)     # jamais vérifié
        self.assertEqual(len(ordre), 4)

    def test_limite_par_categorie_sur_les_premiers_de_la_file(self):
        self.assertEqual(self._ordre(limite=1), [
            ("fans", "f3", "amazon.fr"), ("gpus", "g1", "amazon.de"),
            ("gpus", "g1", "amazon.fr")])


class TestAppliquer(unittest.TestCase):

    def setUp(self):
        self.cache = {"fans": {"f1": {"amazon.fr": "B0000000F1",
                                      "amazon.de": "B0000000F1"}}}
        self.verif = {"fans": {"f1": {"amazon.de": "2026-11-01T00:00:00Z"}}}
        self.offres = {"fans": {"f1": {"id": "f1", "name": "F1", "prices": [
            _offre("amazon.fr", "B0000000F1", prix=50.0),
            _offre("amazon.de", "B0000000F1", prix=40.0),
            _gputracker("amazon.fr")]}}}
        self.quand = "2026-11-18T12:00:00Z"

    def _appliquer(self, r, marche):
        t = va.Tache("fans", "f1", marche, "B0000000F1")
        return va.appliquer(r, t, "F1", self.cache, self.verif, self.offres,
                            self.quand)

    def _shops(self):
        return sorted((p["shop"], p["price"])
                      for p in self.offres["fans"]["f1"]["prices"])

    def test_lien_mort_retire_l_asin_l_offre_et_la_date(self):
        _, sales = self._appliquer({"etat": "mort"}, "amazon.de")
        self.assertIsNone(self.cache["fans"]["f1"]["amazon.de"])
        self.assertNotIn(("amazon.de", 40.0), self._shops())
        self.assertNotIn("amazon.de", self.verif["fans"]["f1"])
        self.assertEqual(sales, {"asins", "verif", "fans"})

    def test_sans_prix_retire_l_offre_amazon_mais_pas_celle_de_gputracker(self):
        _, sales = self._appliquer({"etat": "sans_prix", "dispo": True,
                                    "phrase": ""}, "amazon.fr")
        self.assertEqual(self._shops(), [("amazon.de", 40.0), ("amazon.fr", 99.0)])
        self.assertEqual(self.verif["fans"]["f1"]["amazon.fr"], self.quand)
        self.assertEqual(sales, {"verif", "fans"})

    def test_prix_remplace_l_offre_de_la_meme_marketplace(self):
        self._appliquer({"etat": "prix", "dispo": False, "titre": "T",
                         "montant": 45.5, "devise": "EUR", "brut": "45,50 €"},
                        "amazon.fr")
        self.assertEqual(self._shops(), [("amazon.de", 40.0), ("amazon.fr", 45.5)])
        nouvelle = [p for p in self.offres["fans"]["f1"]["prices"]
                    if p["shop"] == "amazon.fr"][0]
        self.assertFalse(nouvelle["inStock"])
        self.assertEqual(nouvelle["url"], va.lien("amazon.fr", "B0000000F1"))
        self.assertEqual(nouvelle["lastSeen"], self.quand)

    def test_prix_cree_l_entree_d_un_composant_sans_offre(self):
        self.offres["fans"] = {}
        self._appliquer({"etat": "prix", "dispo": True, "titre": "T",
                         "montant": 9.99, "devise": "EUR", "brut": "9,99 €"},
                        "amazon.fr")
        e = self.offres["fans"]["f1"]
        self.assertEqual((e["id"], e["name"], e["priceMin"]), ("f1", "F1", None))
        self.assertEqual(len(e["prices"]), 1)

    def test_devise_convertie_garde_l_offre_et_date_la_verification(self):
        avant = self._shops()
        self._appliquer({"etat": "devise", "dispo": True, "brut": "$9"}, "amazon.fr")
        self.assertEqual(self._shops(), avant)
        self.assertEqual(self.verif["fans"]["f1"]["amazon.fr"], self.quand)

    def test_page_d_attente_ne_change_rien(self):
        avant = json.dumps([self.cache, self.verif, self.offres], sort_keys=True)
        _, sales = self._appliquer({"etat": "bloque"}, "amazon.fr")
        self.assertEqual(sales, set())
        self.assertEqual(json.dumps([self.cache, self.verif, self.offres],
                                    sort_keys=True), avant)

    def test_derniere_fiche_morte_remet_le_composant_a_rechercher(self):
        # Laissé à {marché: None}, le composant passerait pour « cherché, rien
        # trouvé » et ne serait plus jamais re-résolu.
        self.cache["_titres"] = {"fans": {"f1": "Ancien titre"}}
        self._appliquer({"etat": "mort"}, "amazon.de")
        self._appliquer({"etat": "mort"}, "amazon.fr")
        self.assertNotIn("f1", self.cache["fans"])
        self.assertNotIn("f1", self.cache["_titres"]["fans"])

    def test_verdict_tardif_apres_retrait_de_l_entree_est_ignore(self):
        del self.cache["fans"]["f1"]
        avant = json.dumps([self.cache, self.verif, self.offres], sort_keys=True)
        ligne, sales = self._appliquer({"etat": "mort"}, "amazon.fr")
        self.assertEqual(sales, set())
        self.assertIn("ignoré", ligne)
        self.assertEqual(json.dumps([self.cache, self.verif, self.offres],
                                    sort_keys=True), avant)


class TestPurge(unittest.TestCase):

    def test_purge_des_offres_sans_asin_ou_vers_un_autre_asin(self):
        cache = {"fans": {"f1": {"amazon.fr": "B0000000F1", "amazon.de": None,
                                 "amazon.es": False, "amazon.it": "B0000000F1",
                                 "amazon.co.uk": "B0000000F1"}}}
        offres = {"fans": {"f1": {"id": "f1", "prices": [
            _offre("amazon.fr", "B0000000F1"),                      # gardée
            _offre("amazon.de", "B0000000F1"),                      # ASIN None
            _offre("amazon.es", "B0000000F1"),                      # ASIN False
            _offre("amazon.it", "B0000000ZZ"),                      # autre ASIN
            _offre("amazon.co.uk", "B0000000F1",                    # autre domaine
                   url="https://www.amazon.com/dp/B0000000F1?tag=fraym-20"),
            _offre("amazon.com", "B0000000F1"),                     # absente du cache
            _gputracker("amazon.fr"),                               # autre source
        ]}}}
        n, touchees = va.purger(cache, offres)
        self.assertEqual(n, 5)
        self.assertEqual(touchees, {"fans"})
        restantes = [(p["shop"], "gputracker" in p["url"])
                     for p in offres["fans"]["f1"]["prices"]]
        self.assertEqual(restantes, [("amazon.fr", False), ("amazon.fr", True)])

    def test_categorie_absente_du_cache_jamais_purgee(self):
        offres = {"fans": {"f1": {"id": "f1", "prices": [
            _offre("amazon.fr", "B0000000F1")]}}}
        self.assertEqual(va.purger({}, offres), (0, set()))
        self.assertEqual(len(offres["fans"]["f1"]["prices"]), 1)

    def test_dates_oubliees_quand_l_asin_disparait(self):
        cache = {"fans": {"f1": {"amazon.fr": "B0000000F1", "amazon.de": None}}}
        verif = {"fans": {"f1": {"amazon.fr": "a", "amazon.de": "b"},
                          "f2": {"amazon.fr": "c"}},
                 "gpus": {"g1": {"amazon.fr": "d"}}}
        self.assertTrue(va.oublier_sans_asin(cache, verif))
        # « gpus » absent du cache : on ne décide rien pour lui.
        self.assertEqual(verif, {"fans": {"f1": {"amazon.fr": "a"}},
                                 "gpus": {"g1": {"amazon.fr": "d"}}})
        self.assertFalse(va.oublier_sans_asin(cache, verif))


# --- examen d'une fiche ---------------------------------------------------------

class _Source:
    """Fausse `AmazonMobileSource` : réponses fixées par (domaine, ASIN)."""

    reponses = {}
    delai = 0.0
    actives = {}
    verrou = threading.Lock()
    chevauchements = []

    def __init__(self, domaine="amazon.fr", rate_limit=1.0, timeout=15):
        self.domaine = domaine

    def fiche_complete(self, asin, essais=2):
        cls = type(self)
        with cls.verrou:
            if cls.actives.get(self.domaine):
                cls.chevauchements.append(self.domaine)
            cls.actives[self.domaine] = True
        try:
            if cls.delai:
                time.sleep(cls.delai)
            return cls.reponses.get((self.domaine, asin), ("bloque", "", ""))
        finally:
            with cls.verrou:
                cls.actives[self.domaine] = False


TITRE = "be quiet! Silent Wings 4 140mm ventilateur PC"


class TestExaminer(unittest.TestCase):

    def setUp(self):
        _Source.reponses = {
            ("amazon.fr", "A1"): ("ok", TITRE, PAGE_PRICE_TO_PAY),
            ("amazon.es", "A1"): ("ok", TITRE, PAGE_BLOC_VIDE),
            ("amazon.com", "A1"): ("ok", TITRE, PAGE_PRICE_TO_PAY),  # euros sur .com
            ("amazon.de", "A1"): ("absent", "", ""),
        }
        self.it = {"id": "f1", "name": "be quiet! Silent Wings 4 (×3)"}
        # `valide` (resolve_amazon_asins) est durcie ailleurs : on la remplace
        # ici pour ne tester que la logique de ce script.
        self.valide = mock.patch.object(va, "valide", return_value=True)
        self.valide.start()
        self.addCleanup(self.valide.stop)

    def _etat(self, marche, asin="A1"):
        return va.examiner(_Source(marche), "fans", self.it, marche, asin)

    def test_verdicts(self):
        r = self._etat("amazon.fr")
        self.assertEqual((r["etat"], r["montant"], r["devise"], r["dispo"]),
                         ("prix", 30.37, "EUR", True))
        self.assertEqual(self._etat("amazon.es")["etat"], "sans_prix")
        self.assertEqual(self._etat("amazon.com")["etat"], "devise")
        self.assertEqual(self._etat("amazon.de")["etat"], "mort")
        self.assertEqual(self._etat("amazon.it")["etat"], "bloque")

    def test_titre_non_conforme_vaut_lien_mort(self):
        va.valide.return_value = False
        self.assertEqual(self._etat("amazon.fr")["etat"], "mort")


# --- exécution parallèle --------------------------------------------------------

def _taches(n_par_marche, marches):
    return [va.Tache("fans", f"f{i}", m, "A")
            for i in range(n_par_marche) for m in marches]


class TestExecuter(unittest.TestCase):

    def test_un_fil_suit_l_ordre_de_la_file(self):
        taches = _taches(3, ["amazon.fr", "amazon.de"])
        vues = []
        self.assertEqual(va.executer(taches, vues.append, 1), (6, 0, "fini"))
        self.assertEqual(vues, taches)

    def test_jamais_deux_fils_sur_la_meme_marketplace(self):
        marches = ["amazon.fr", "amazon.de", "amazon.es"]
        taches = _taches(4, marches)
        actives, chevauchements, faites = set(), [], []
        verrou = threading.Lock()

        def traiter(t):
            with verrou:
                if t.marche in actives:
                    chevauchements.append(t.marche)
                actives.add(t.marche)
            time.sleep(0.01)
            with verrou:
                actives.discard(t.marche)
                faites.append(t)

        self.assertEqual(va.executer(taches, traiter, 5), (12, 0, "fini"))
        self.assertEqual(chevauchements, [])
        self.assertEqual(sorted(faites), sorted(taches))

    def test_budget_epuise_aucune_fiche_ouverte(self):
        taches = _taches(2, ["amazon.fr"])
        vues = []
        self.assertEqual(va.executer(taches, vues.append, 2,
                                     fin=time.monotonic() - 1), (0, 2, "budget"))
        self.assertEqual(vues, [])

    def test_arret_demande_et_sauvegarde_d_urgence(self):
        arret = threading.Event()
        arret.set()
        sauvegardes = []
        bilan = va.executer(_taches(2, ["amazon.fr"]), lambda t: None, 1,
                            arret=arret, sur_arret=lambda: sauvegardes.append(1))
        self.assertEqual(bilan, (0, 2, "arret"))
        self.assertEqual(sauvegardes, [1])

    def test_une_erreur_n_arrete_pas_le_fil(self):
        taches = _taches(3, ["amazon.fr"])

        def traiter(t):
            if t.cid == "f0":
                raise RuntimeError("page illisible")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(va.executer(taches, traiter, 1), (3, 0, "fini"))


# --- de bout en bout, avec une fausse source ------------------------------------

class TestPrincipal(unittest.TestCase):

    def setUp(self):
        self.dossier = tempfile.mkdtemp(prefix="verif_amazon_")
        os.makedirs(os.path.join(self.dossier, "catalog"))
        self._ecrire("catalog/fans.json", [{"id": "f1", "name": "Ventilo Un"},
                                           {"id": "f2", "name": "Ventilo Deux"}])
        self._ecrire("amazon_asins.json", {"_titres": {}, "fans": {
            "f1": {"amazon.fr": "B0000000F1", "amazon.de": "B0000000F1",
                   "amazon.es": None},
            "f2": {"amazon.fr": "B0000000F2", "amazon.de": False}}})
        self._ecrire("fans.json", [
            {"id": "f1", "name": "Ventilo Un", "priceMin": None, "image": None,
             "lastUpdated": "x", "prices": [
                 _offre("amazon.fr", "B0000000F1", prix=50.0),
                 _offre("amazon.de", "B0000000F1", prix=40.0),
                 _offre("amazon.es", "B0000000F1", prix=67.42)]},
            {"id": "f2", "name": "Ventilo Deux", "priceMin": None, "image": None,
             "lastUpdated": "x", "prices": [
                 _offre("amazon.fr", "B0000000F2", prix=30.0),
                 _offre("amazon.de", "B0000000F2", prix=31.0),
                 _gputracker("amazon.fr", prix=29.0)]}])
        _Source.reponses = {
            ("amazon.fr", "B0000000F1"): ("ok", TITRE, PAGE_PRICE_TO_PAY),
            ("amazon.de", "B0000000F1"): ("absent", "", ""),
            ("amazon.fr", "B0000000F2"): ("ok", TITRE, PAGE_BLOC_VIDE),
        }
        _Source.delai = 0.0
        _Source.chevauchements = []
        _Source.actives = {}
        for cible in (mock.patch.object(va, "AmazonMobileSource", _Source),
                      mock.patch.object(va, "valide", return_value=True)):
            cible.start()
            self.addCleanup(cible.stop)
        sigterm = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, sigterm)

    def tearDown(self):
        for racine, dossiers, fichiers in os.walk(self.dossier, topdown=False):
            for f in fichiers:
                os.remove(os.path.join(racine, f))
            for d in dossiers:
                os.rmdir(os.path.join(racine, d))
        os.rmdir(self.dossier)

    def _ecrire(self, nom, donnees):
        with open(os.path.join(self.dossier, nom), "w", encoding="utf-8") as f:
            json.dump(donnees, f)

    def _lire(self, nom):
        with open(os.path.join(self.dossier, nom), encoding="utf-8") as f:
            return json.load(f)

    def _lancer(self, *options):
        argv = ["verifier_amazon.py", "--data-repo", self.dossier,
                "--markets", "amazon.fr,amazon.de,amazon.es", "--pause", "0",
                *options]
        sortie = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(sortie):
            self.assertEqual(va.principal(), 0)
        return sortie.getvalue()

    def test_passe_complete(self):
        journal = self._lancer("--paralleles", "3", "--sauver-tous", "1")
        self.assertIn("arrêt : fini", journal)
        asins = self._lire("amazon_asins.json")
        self.assertIsNone(asins["fans"]["f1"]["amazon.de"])         # lien mort
        offres = {e["id"]: sorted((p["shop"], p["price"], "gputracker" in p["url"])
                                  for p in e["prices"])
                  for e in self._lire("fans.json")}
        self.assertEqual(offres["f1"], [("amazon.fr", 30.37, False)])
        # f2 : offre Amazon retirée (bloc vide), offre gputracker gardée, offre
        # amazon.de purgée (ASIN False).
        self.assertEqual(offres["f2"], [("amazon.fr", 29.0, True)])
        verif = self._lire(va.FICHIER_VERIF)
        self.assertEqual(sorted(verif["fans"]), ["f1", "f2"])
        self.assertEqual(list(verif["fans"]["f1"]), ["amazon.fr"])
        self.assertEqual(_Source.chevauchements, [])
        self.assertFalse([f for f in os.listdir(self.dossier) if f.endswith(".tmp")])

    def test_budget_tres_court_sauvegarde_partielle(self):
        # 20 composants × 2 marketplaces à 0,2 s la fiche, budget de 0,6 s.
        items = [{"id": f"c{i}", "name": f"C{i}"} for i in range(20)]
        self._ecrire("catalog/fans.json", items)
        self._ecrire("amazon_asins.json", {"fans": {
            it["id"]: {"amazon.fr": f"B{i:09d}", "amazon.de": f"B{i:09d}"}
            for i, it in enumerate(items)}})
        _Source.reponses = {(m, f"B{i:09d}"): ("ok", TITRE, PAGE_PRICE_TO_PAY)
                            for i in range(20) for m in ("amazon.fr", "amazon.de")}
        _Source.delai = 0.2
        journal = self._lancer("--paralleles", "2", "--budget-min", "0.01",
                               "--sauver-tous", "1000")
        self.assertIn("arrêt : budget", journal)
        verif = self._lire(va.FICHIER_VERIF)
        n = sum(len(v) for v in verif["fans"].values())
        self.assertTrue(0 < n < 40, n)
        offres = self._lire("fans.json")
        self.assertEqual(sum(len(e["prices"]) for e in offres if e["id"].startswith("c")), n)

        # Exécution suivante : les couples déjà vérifiés passent APRÈS les
        # autres, même avec --frais-h 0.
        cache = self._lire("amazon_asins.json")
        par_id = {e["id"]: e for e in offres}
        taches = va.file_attente(cache, verif, {"fans": par_id},
                                 {"fans": {it["id"]: it for it in items}},
                                 ["amazon.fr", "amazon.de"])
        deja = {(cid, m) for cid, v in verif["fans"].items() for m in v}
        rangs = [(t.cid, t.marche) in deja for t in taches]
        self.assertEqual(rangs, sorted(rangs))  # tous les False avant les True


class TestEcriture(unittest.TestCase):

    def test_ecriture_atomique(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = os.path.join(d, "x.json")
            va.ecrire_json(chemin, {"b": 1, "a": "é"}, sort_keys=True)
            with open(chemin, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": "é", "b": 1})
            self.assertEqual(os.listdir(d), ["x.json"])


if __name__ == "__main__":
    unittest.main()
