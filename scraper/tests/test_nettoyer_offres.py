# -*- coding: utf-8 -*-
"""Tests de `nettoyer_offres.py` : purge des offres périmées (`--age-max-h`),
aberrations hautes comparées en euros, `priceMin` recalculé (null sans offre).

Sans réseau : offres fabriquées, fichiers dans un dossier temporaire. Les cas
reprennent des offres réelles des données du 18/11/2026 (Noctua NH-D15S,
Ryzen 5 2600, RX 6500 XT…).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nettoyer_offres as no  # noqa: E402

MAINTENANT = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)


def vu(heures):
    """`lastSeen` d'une offre revue il y a [heures] heures."""
    return (MAINTENANT - timedelta(hours=heures)).strftime("%Y-%m-%dT%H:%M:%SZ")


def offre(prix, shop="ldlc.com", devise="EUR", age_h=1.0, produit="Produit", stock=True):
    """Offre au format des fichiers id-clés du dépôt de données."""
    return {"shop": shop, "price": prix, "currency": devise,
            "url": f"https://www.{shop}/p", "inStock": stock,
            "lastSeen": vu(age_h), "product": produit, "image": None}


def prix_de(offres):
    return sorted(o["price"] for o in offres)


class OffrePerimee(unittest.TestCase):
    def test_offre_recente_gardee(self):
        self.assertFalse(no.offre_perimee(offre(100, age_h=10), MAINTENANT, 72))

    def test_offre_trop_vieille_retiree(self):
        self.assertTrue(no.offre_perimee(offre(100, age_h=80), MAINTENANT, 72))

    def test_seuil_exact_garde(self):
        # « dépasse N heures » : pile 72 h n'est pas encore périmé.
        self.assertFalse(no.offre_perimee(offre(100, age_h=72), MAINTENANT, 72))

    def test_sans_lastseen_ou_illisible_perimee(self):
        sans = offre(100)
        del sans["lastSeen"]
        self.assertTrue(no.offre_perimee(sans, MAINTENANT, 72))
        for valeur in (None, "", "hier", 12):
            o = offre(100)
            o["lastSeen"] = valeur
            self.assertTrue(no.offre_perimee(o, MAINTENANT, 72), valeur)

    def test_formats_de_date_acceptes(self):
        # Décalage explicite, et date sans fuseau (lue en UTC).
        o = offre(100)
        o["lastSeen"] = "2026-11-17T20:00:00+00:00"
        self.assertFalse(no.offre_perimee(o, MAINTENANT, 72))
        o["lastSeen"] = "2026-11-10T20:00:00"
        self.assertTrue(no.offre_perimee(o, MAINTENANT, 72))


class PurgeParAge(unittest.TestCase):
    def test_perimees_retirees_avant_les_medianes(self):
        # Noctua NH-D15S au 18/11 : trois caches NA-HC4 (~27 €) vus il y a
        # 56 jours, et les deux vraies offres Alternate du jour.
        prices = [offre(26.7, "amazon.it", age_h=1330), offre(27.27, "amazon.fr", age_h=1330),
                  offre(30.79, "amazon.es", age_h=1330),
                  offre(97.9, "alternate.fr"), offre(111.9, "alternate.fr")]
        gardees, n = no.nettoyer_prices(prices, "coolers", MAINTENANT, 72)
        self.assertEqual(prix_de(gardees), [97.9, 111.9])
        self.assertEqual(n["perimees"], 3)
        self.assertEqual(n["hautes"], 0)

        # Sans la purge, les caches serviraient de référence et les vraies
        # offres passeraient pour des aberrations hautes : l'ordre compte.
        gardees, n = no.nettoyer_prices(prices, "coolers", MAINTENANT, 0)
        self.assertEqual(prix_de(gardees), [26.7, 27.27, 30.79])
        self.assertEqual(n["hautes"], 2)

    def test_age_nul_desactive_la_purge(self):
        prices = [offre(100, age_h=500), offre(105, age_h=900)]
        gardees, n = no.nettoyer_prices(prices, "gpus", MAINTENANT, 0)
        self.assertEqual(len(gardees), 2)
        self.assertEqual(n["perimees"], 0)


class AberrationsHautes(unittest.TestCase):
    def test_offre_haute_retiree(self):
        gardees, basses, hautes = no.aberrations(
            [offre(1000), offre(1050), offre(1100), offre(4276.52, "amazon.fr")])
        self.assertEqual(prix_de(gardees), [1000, 1050, 1100])
        self.assertEqual((basses, hautes), (0, 1))

    def test_deux_autres_offres_suffisent(self):
        # Ryzen 5 2600 : un PC complet (1 310 €) face à deux processeurs.
        gardees, _b, hautes = no.aberrations(
            [offre(86.8, "amazon.fr"), offre(89.47, "amazon.it"),
             offre(1310, "alternate.fr", produit="ASUS TUF Gaming TM500, PC gaming")])
        self.assertEqual(prix_de(gardees), [86.8, 89.47])
        self.assertEqual(hautes, 1)

    def test_une_seule_autre_offre_ne_tranche_pas(self):
        gardees, basses, hautes = no.aberrations([offre(100), offre(900)])
        self.assertEqual(len(gardees), 2)
        self.assertEqual((basses, hautes), (0, 0))

    def test_ecart_sous_le_seuil_garde(self):
        # RX 6500 XT : 462 € n'est qu'à 1,4 × la médiane des deux autres.
        gardees, _b, hautes = no.aberrations(
            [offre(288.67, "amazon.it"), offre(363.57, "amazon.fr"), offre(462.18, "amazon.de")])
        self.assertEqual(len(gardees), 3)
        self.assertEqual(hautes, 0)

    def test_comparaison_en_euros(self):
        # Même prix réel dans cinq devises : rien n'est aberrant, même si
        # 4 300 PLN est numériquement 4 fois plus grand que 1 000 EUR.
        meme_prix = [offre(1000), offre(1080, "newegg.com", "USD"), offre(4300, "morele.net", "PLN"),
                     offre(1470, "canadacomputers.com", "CAD"), offre(860, "amazon.co.uk", "GBP")]
        gardees, basses, hautes = no.aberrations(meme_prix)
        self.assertEqual(len(gardees), 5)
        self.assertEqual((basses, hautes), (0, 0))
        # 2 000 £ ≈ 2 326 € face à ~620 € : aberration.
        gardees, _b, hautes = no.aberrations(
            [offre(600), offre(620), offre(640), offre(2000, "amazon.co.uk", "GBP")])
        self.assertEqual(hautes, 1)
        self.assertNotIn("GBP", {o["currency"] for o in gardees})

    def test_devise_inconnue_ni_jugee_ni_reference(self):
        # Devise absente des taux (couronne suédoise, amazon.se) : 1 650 SEK
        # comptés 1:1 paraîtraient 2,6 fois trop chers.
        gardees, _b, hautes = no.aberrations(
            [offre(600), offre(620), offre(640), offre(1650, "amazon.se", "SEK")])
        self.assertEqual(len(gardees), 4)
        self.assertEqual(hautes, 0)
        # Les offres SEK ne comptent pas non plus pour atteindre les 3 offres
        # d'une médiane : 10 et 12 SEK, comptés en euros, sortiraient en
        # aberrations basses face à 500 €.
        gardees, basses, hautes = no.aberrations(
            [offre(500), offre(520), offre(10, "amazon.se", "SEK"), offre(12, "amazon.se", "SEK")])
        self.assertEqual(len(gardees), 4)
        self.assertEqual((basses, hautes), (0, 0))

    def test_dollar_australien_converti(self):
        # 1 650 AUD ≈ 1 000 € : converti, il reste dans la norme d'offres à
        # ~620 € (moins de 2,5 fois) ; compté 1:1 il aurait été retiré.
        gardees, _b, hautes = no.aberrations(
            [offre(600), offre(620), offre(640), offre(1650, "amazon.com.au", "AUD")])
        self.assertEqual((len(gardees), hautes), (4, 0))

    def test_aberrations_basses_exclues_de_la_reference_haute(self):
        # Trois accessoires à ~20 € : sans leur exclusion, la médiane des
        # « autres » tomberait à 22 € et les vraies offres sortiraient.
        gardees, basses, hautes = no.aberrations(
            [offre(20), offre(21), offre(22), offre(100), offre(105), offre(110)])
        self.assertEqual(prix_de(gardees), [100, 105, 110])
        self.assertEqual((basses, hautes), (3, 0))


class ReglesExistantes(unittest.TestCase):
    def test_aberration_basse_inchangee(self):
        gardees, n = no.nettoyer_prices(
            [offre(30), offre(1000), offre(1050), offre(1100)], "gpus", MAINTENANT, 72)
        self.assertEqual(prix_de(gardees), [1000, 1050, 1100])
        self.assertEqual(n["basses"], 1)

    def test_accessoires_et_exemptions(self):
        bloc = offre(220, produit="Alphacool Core Geforce RTX 5080")
        gardees, n = no.nettoyer_prices([bloc, offre(1200), offre(1250)], "gpus", MAINTENANT, 72)
        self.assertEqual(n["accessoires"], 1)
        self.assertNotIn(bloc, gardees)
        # Refroidissements et ventilateurs : « câble », « Alphacool » sont légitimes…
        aio = offre(120, produit="Alphacool Eisbaer 360 watercooling")
        gardees, n = no.nettoyer_prices([aio], "coolers", MAINTENANT, 72)
        self.assertEqual((len(gardees), n["accessoires"]), (1, 0))
        ventilo = offre(25, produit="ARCTIC P12 PWM PST, câble de 40 cm")
        gardees, n = no.nettoyer_prices([ventilo], "fans", MAINTENANT, 72)
        self.assertEqual((len(gardees), n["accessoires"]), (1, 0))
        # … mais l'occasion reste écartée partout.
        gardees, n = no.nettoyer_prices([offre(20, produit="ARCTIC P12 - Occasion")],
                                        "fans", MAINTENANT, 72)
        self.assertEqual((len(gardees), n["accessoires"]), (0, 1))

    def test_produit_null_tolere(self):
        o = offre(100)
        o["product"] = None
        gardees, _n = no.nettoyer_prices([o], "gpus", MAINTENANT, 72)
        self.assertEqual(len(gardees), 1)


class Principal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dossier = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def ecrire(self, nom, data):
        chemin = os.path.join(self.dossier, nom)
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        return chemin

    def lire(self, nom):
        with open(os.path.join(self.dossier, nom), encoding="utf-8") as f:
            return json.load(f)

    def lancer(self, *options):
        with redirect_stdout(io.StringIO()):  # bilan imprimé par catégorie : hors sortie des tests
            no.principal(["--data-repo", self.dossier, *options], maintenant=MAINTENANT)

    def test_fichiers_purges_et_price_min(self):
        self.ecrire("gpus_app.json", [
            {"id": "mixte", "name": "Mixte", "image": "images/gpus-mixte.png", "priceMin": 400.0,
             "lastUpdated": vu(1),
             "prices": [offre(400, "amazon.it", age_h=130), offre(520, stock=False),
                        offre(560), offre(590)]},
            {"id": "toutes-vieilles", "name": "Vieille", "image": "images/gpus-v.png",
             "priceMin": 4989.99, "lastUpdated": vu(200),
             "prices": [offre(4989.99, "amazon.com", "USD", age_h=200)]},
            {"id": "orphelin", "name": "Orphelin", "image": None, "priceMin": 703.68,
             "lastUpdated": vu(380), "prices": []},
        ])
        self.ecrire("fans.json", [{"id": "arctic_p12_3", "name": "Arctic P12", "image": None,
                                   "priceMin": None, "lastUpdated": vu(1350),
                                   "prices": [offre(27.29, "amazon.fr", age_h=2370)]}])
        gpus_modele = self.ecrire("gpus.json", [{"name": "RTX 5080", "priceMin": 999.0,
                                                  "prices": [offre(999, age_h=900)]}])
        with open(gpus_modele, encoding="utf-8") as f:
            avant_gpus = f.read()

        self.lancer()  # --age-max-h par défaut : 72 h

        par_id = {e["id"]: e for e in self.lire("gpus_app.json")}
        self.assertEqual(len(par_id), 3, "aucune entrée supprimée")
        # Offre périmée sortie ; priceMin = moins cher EN STOCK (520 est en rupture).
        self.assertEqual(prix_de(par_id["mixte"]["prices"]), [520, 560, 590])
        self.assertEqual(par_id["mixte"]["priceMin"], 560.0)
        # Plus aucune offre : priceMin null, entrée et image gardées.
        self.assertEqual(par_id["toutes-vieilles"]["prices"], [])
        self.assertIsNone(par_id["toutes-vieilles"]["priceMin"])
        self.assertEqual(par_id["toutes-vieilles"]["image"], "images/gpus-v.png")
        # priceMin sans aucune offre derrière : null aussi.
        self.assertIsNone(par_id["orphelin"]["priceMin"])
        # Ventilateurs traités ; gpus.json (retirer_prix_perimes) intact.
        self.assertEqual(self.lire("fans.json")[0]["prices"], [])
        with open(gpus_modele, encoding="utf-8") as f:
            self.assertEqual(f.read(), avant_gpus)

    def test_seuil_par_defaut_72_h_et_option(self):
        self.ecrire("rams.json", [{"id": "r", "name": "R", "image": None, "priceMin": 100.0,
                                   "lastUpdated": vu(1),
                                   "prices": [offre(100, age_h=70), offre(101, age_h=75)]}])
        self.lancer("--age-max-h", "0")
        self.assertEqual(len(self.lire("rams.json")[0]["prices"]), 2)
        self.lancer()
        self.assertEqual(prix_de(self.lire("rams.json")[0]["prices"]), [100])

    def test_fichier_inchange_non_reecrit(self):
        propre = [{"id": "c", "name": "C", "image": None, "priceMin": 50.0, "lastUpdated": vu(1),
                   "prices": [offre(50), offre(52), offre(55)]}]
        chemin = self.ecrire("coolers.json", propre)
        os.utime(chemin, (1_000_000_000, 1_000_000_000))
        self.lancer()
        self.assertEqual(os.path.getmtime(chemin), 1_000_000_000)

    def test_only_limite_les_categories(self):
        vieille = [{"id": "x", "name": "X", "image": None, "priceMin": 10.0, "lastUpdated": vu(1),
                    "prices": [offre(10, age_h=500)]}]
        self.ecrire("ssds.json", vieille)
        self.ecrire("psus.json", vieille)
        self.lancer("--only", "ssds")
        self.assertEqual(self.lire("ssds.json")[0]["prices"], [])
        self.assertEqual(len(self.lire("psus.json")[0]["prices"]), 1)


if __name__ == "__main__":
    unittest.main()
