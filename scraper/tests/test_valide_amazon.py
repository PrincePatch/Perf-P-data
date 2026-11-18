# -*- coding: utf-8 -*-
"""Tests SANS réseau du contrôle d'identité `valide` (resolve_amazon_asins.py).

Lancement, depuis `App/` :
    python -m unittest discover -s scraper/tests -p "test_valide_amazon.py"

Chaque titre « faux » est un titre RÉELLEMENT retenu par l'ancien validateur
(champ `_titres` d'`amazon_asins.json` ou offre Amazon publiée), relevé par
l'audit manuel de novembre 2026. Chaque titre « bon » est un titre réel (ou
la forme courante d'un titre réel) du même modèle ou d'un modèle voisin, qui
doit rester accepté : un garde-fou qui rejette le bon produit coûte un lien.

Le catalogue est fourni explicitement à `valide` : les tests ne dépendent
d'aucun fichier du disque.
"""

import inspect
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import resolve_amazon_asins as ra  # noqa: E402


# --- catalogue minimal, par catégorie ------------------------------------------
# Les modèles voisins y figurent pour la règle « variante plus précise ».
CATALOGUE = {
    "cases": [{"name": n} for n in (
        "Fractal Meshify 2", "Fractal Meshify 2 Compact", "Fractal Ridge (ITX)",
        "Fractal Torrent", "Lian Li O11 Dynamic", "Lian Li O11 Dynamic EVO",
        "Lian Li O11 Dynamic Mini", "NZXT H1 V2", "NZXT H5 Flow",
        "Corsair 5000D Airflow", "Montech King 95", "Hyte Y70",
        "Cooler Master TD500 Mesh")],
    "coolers": [{"name": n} for n in (
        "Noctua NH-D15", "Noctua NH-D15S", "Noctua NH-D15 G2",
        "be quiet! Dark Rock 4", "be quiet! Dark Rock Pro 4",
        "Corsair Nautilus 360 RS", "Corsair iCUE Link Titan 360 RX",
        "NZXT Kraken 280", "NZXT Kraken 360", "NZXT Kraken Elite 360",
        "Thermalright Phantom Spirit 120 SE", "Noctua NH-U14S",
        "Arctic Freezer 36", "Arctic Freezer 36 CO", "Thermalright Frozen Notte 360 ARGB")],
    "fans": [{"name": n} for n in (
        "Arctic P12 (×3)", "Arctic P14 (×3)", "be quiet! Silent Wings 4 (×3)",
        "Corsair LL120 RGB (×3)", "Noctua NF-A12x25 (×2)", "Lian Li UNI SL120 (×3)",
        "NZXT F120 RGB Core (×3)")],
    "mobos": [{"name": n} for n in (
        "Gigabyte B550 Aorus Elite", "Gigabyte B660 DS3H DDR4", "MSI MAG B760M Mortar",
        "MSI MPG Z690 Edge WiFi", "ASUS Prime Z690-P WiFi", "Gigabyte Z890 Aorus Master",
        "ASUS ROG Maximus Z790 Apex", "ASUS TUF B550-Plus", "ASUS TUF Gaming X570-Plus",
        "ASUS ROG Strix Z890-I", "Gigabyte X870E Aorus Elite", "MSI PRO B660M-A WiFi DDR4",
        "ASRock B650E PG-ITX", "ASUS ROG Strix Z890-A Gaming WiFi")],
    "psus": [
        {"name": "Corsair RM1000x", "rating": "80+ Gold"},
        {"name": "ASUS ROG Strix 1000W", "rating": "80+ Gold"},
        {"name": "Corsair HX850", "rating": "80+ Platinum"},
        {"name": "be quiet! Pure Power 12 M 550W", "rating": "80+ Gold"},
        {"name": "be quiet! Pure Power 12 M 650W", "rating": "80+ Gold"},
        {"name": "be quiet! System Power 10 550W", "rating": "80+ Bronze"},
        {"name": "Cooler Master MWE Gold 750 V2", "rating": "80+ Gold"},
        {"name": "NZXT C750 Gold", "rating": "80+ Gold"},
    ],
    "gpus": [
        {"name": "GeForce GTX 1050", "vramGb": 2},
        {"name": "GeForce GTX 1650 Super", "vramGb": 4},
        {"name": "Radeon RX 570", "vramGb": 4},
        {"name": "Arc A770", "vramGb": 16},
        {"name": "RTX 6000 Ada", "vramGb": 48},
        {"name": "RTX 4000 Ada", "vramGb": 20},
        {"name": "GeForce RTX 5050", "vramGb": 8},
        {"name": "GeForce RTX 5080", "vramGb": 16},
        {"name": "GeForce RTX 3080", "vramGb": 10},
        {"name": "GeForce RTX 3080 12G", "vramGb": 12},
        {"name": "GeForce RTX 4060 Ti", "vramGb": 8},
        {"name": "GeForce RTX 4060 Ti 16G", "vramGb": 16},
        {"name": "GeForce RTX 5070", "vramGb": 12},
    ],
    "cpus": [{"name": n} for n in (
        "Ryzen 7 5700G", "Core i5-14500", "Core i5-10600K", "Xeon w9-3495X",
        "Ryzen 7 7800X3D", "Core i5-12600K", "Core i7-13700K", "Core Ultra 7 265KF",
        "Ryzen Threadripper 7980X")],
    "ssds": [
        {"name": "WD Blue 2 To (7200)", "kind": "HDD", "sizeGb": 2000},
        {"name": "Seagate FireCuda 2 To (7200)", "kind": "HDD", "sizeGb": 2000},
        {"name": "Seagate FireCuda 540 2 To", "kind": "NVMe Gen5", "sizeGb": 2000},
        {"name": "WD Black 4 To (7200)", "kind": "HDD", "sizeGb": 4000},
        {"name": "WD Black SN850X 4 To", "kind": "NVMe Gen4", "sizeGb": 4000},
        {"name": "Kingston A400 480 Go", "kind": "SATA SSD", "sizeGb": 480},
        {"name": "Seagate BarraCuda 2 To", "kind": "HDD", "sizeGb": 2000},
        {"name": "Samsung 990 Pro (Gen4)", "kind": "NVMe Gen4", "sizeGb": 2000},
    ],
    "rams": [],
}


def _ram(nom):
    """Fiche mémoire déduite du nom du catalogue (« 32 Go DDR5-6000 CL36 »)."""
    taille, _, reste = nom.partition(" Go ")
    kind, mhz_cl = reste.split("-")
    mhz, cl = mhz_cl.split(" CL")
    return {"name": nom, "sizeGb": int(taille), "kind": kind, "mhz": int(mhz), "cl": int(cl)}


def _fiche(cat, nom):
    if cat == "rams":
        return _ram(nom)
    for it in CATALOGUE[cat]:
        if it["name"] == nom:
            return it
    raise KeyError(f"{cat}/{nom} absent du catalogue de test")


def verdicts(cat, nom, titre):
    """(strict, souple) — `resolve_amazon_asins` utilise les deux,
    `verifier_amazon` le second."""
    it = _fiche(cat, nom)
    jetons = ra._jetons(cat, it)
    return (ra.valide(cat, nom, titre, jetons, catalogue=CATALOGUE[cat]),
            ra.valide(cat, nom, titre, jetons, souple=True, catalogue=CATALOGUE[cat]))


class _Base(unittest.TestCase):
    def rejete(self, cat, nom, titre):
        self.assertEqual(verdicts(cat, nom, titre), (False, False),
                         f"{cat}/{nom} ne doit PAS valider : {titre}")

    def accepte(self, cat, nom, titre, strict=True):
        s, o = verdicts(cat, nom, titre)
        self.assertTrue(o, f"{cat}/{nom} doit valider (souple) : {titre}")
        if strict:
            self.assertTrue(s, f"{cat}/{nom} doit valider (strict) : {titre}")


# --- les six fausses acceptations de départ ----------------------------------------
class FaussesAcceptationsDeDepart(_Base):
    def test_meshify_3_pour_meshify_2(self):
        self.rejete("cases", "Fractal Meshify 2",
                    "Fractal Design Meshify 3 Black Solid - High Airflow PC Gaming Case - "
                    "Mesh Front Panel - Three Momentum 14 Fans Included - Clean Interior")

    def test_habillage_na_hc4_pour_nh_d15s(self):
        self.rejete("coolers", "Noctua NH-D15S",
                    "Noctua NA-HC4 chromax.black, habillage pour ventirad, pour NH-D15, "
                    "NH-D15S & NH-D15 SE-AM4 (Noir)")
        self.rejete("coolers", "Noctua NH-D15S",
                    "Noctua NA-HC4 chromax.black, Cubierta Estética Accesoria para Disipador "
                    "| Apta para Disipadores de CPU de Serie NH-D15, NH-D15S")
        self.rejete("coolers", "Noctua NH-D15S",
                    "Noctua NA-HC4 chromax.black, Cover Estetica Aggiuntiva per Dissipatore "
                    "| Adatto ai Dissipatori per CPU Serie NH-D15, NH-D15S")

    def test_dark_rock_5_pour_dark_rock_4(self):
        self.rejete("coolers", "be quiet! Dark Rock 4",
                    "be quiet! Dark Rock 5 ventirad, puissance de refroidissement extrême, "
                    "6 caloducs haute performance, ventilateur Silent Wings 4 120 mm PWM")

    def test_lot_ridge_et_carte_mere(self):
        self.rejete("cases", "Fractal Ridge (ITX)",
                    "Fractal Design Ridge mITX Case, Gigabyte A520I AC Motherboard, "
                    "2X 140mm Fans : Electronics")

    def test_o11_dynamic_evo_pour_o11_dynamic(self):
        self.rejete("cases", "Lian Li O11 Dynamic",
                    "Lian Li O11 Dynamic Evo - Tower - Atx - Seitenteil Mit Fenster "
                    "(gehärtetes Glas) - Keine Spannungsversorgung - Weiß - Usb/audio (o11dexL-w)")

    def test_contrefacon_compatible_h1_v2(self):
        self.rejete("cases", "NZXT H1 V2", "Case NZXT Compatible H1 v2 White SFF Mini-ITX : Electronics")


class BonsTitresVoisinsDesSixCas(_Base):
    def test_meshify_2_et_meshify_2_compact(self):
        self.accepte("cases", "Fractal Meshify 2", "Fractal Design Meshify 2 Black TG Dark Tint")
        self.accepte("cases", "Fractal Meshify 2 Compact", "Fractal Design Meshify 2 Compact Noir")
        # Le Compact ne valide pas le Meshify 2 de base.
        self.rejete("cases", "Fractal Meshify 2", "Fractal Design Meshify 2 Compact Noir")

    def test_vrais_noctua(self):
        self.accepte("coolers", "Noctua NH-D15S",
                     "Noctua NH-D15S, Ventirad CPU Double Tour avec Ventilateur NF-A15 PWM (Marron)")
        self.accepte("coolers", "Noctua NH-D15",
                     "Noctua NH-D15, Ventirad CPU Double Tour Silencieux Haute Performance | "
                     "Ventirad Premium Silencieux Haut de 168mm, 6 Caloducs, pour Intel & AMD")
        self.rejete("coolers", "Noctua NH-D15", "Noctua NH-D15S, Ventirad CPU Double Tour (Marron)")
        self.rejete("coolers", "Noctua NH-D15", "Noctua NH-D15 G2, Ventirad CPU Double Tour (Brun)")

    def test_vrais_dark_rock(self):
        self.accepte("coolers", "be quiet! Dark Rock 4",
                     "be quiet! Dark Rock 4 BK021 ventirad, 200W TDP, ventilateur Silent Wings 135mm")
        self.accepte("coolers", "be quiet! Dark Rock Pro 4",
                     "be quiet! Dark Rock Pro 4 250W TDP CPU Cooler | Includes Two Silent Wings PWM Fans")
        self.rejete("coolers", "be quiet! Dark Rock 4",
                    "be quiet! Dark Rock Pro 4 250W TDP CPU Cooler | Includes Two Silent Wings PWM Fans")

    def test_ridge_seul(self):
        self.accepte("cases", "Fractal Ridge (ITX)",
                     "Fractal Design Ridge Black - Boîtier PC Mini-ITX format console, 2 ventilateurs 140 mm")

    def test_o11_dynamic_evo_et_mini_et_base(self):
        evo = ("Lian Li O11 Dynamic Evo - Tower - Atx - Seitenteil Mit Fenster "
               "(gehärtetes Glas) - Keine Spannungsversorgung - Schwarz - Usb/audio (o11dexL-x)")
        self.accepte("cases", "Lian Li O11 Dynamic EVO", evo)
        self.accepte("cases", "Lian Li O11 Dynamic Mini",
                     "Lian Li O11 Dynamic Mini Snow Edition, Midi-Tower, Tempered Glass - Weiß")
        self.accepte("cases", "Lian Li O11 Dynamic",
                     "Lian Li O11 Dynamic Midi-Tower, Tempered Glass - schwarz")
        self.rejete("cases", "Lian Li O11 Dynamic",
                    "Lian Li O11 Dynamic Mini Snow Edition, Midi-Tower, Tempered Glass - Weiß")

    def test_vrai_h1_v2(self):
        self.accepte("cases", "NZXT H1 V2", "NZXT H1 V2 - Boîtier PC Mini-ITX - Blanc mat")


# --- audit E1 : cartes graphiques, processeurs, mémoire, stockage -----------------
class AuditCartesGraphiques(_Base):
    def test_memoire_differente_du_catalogue(self):
        self.rejete("gpus", "GeForce GTX 1050",
                    "SAPLOS GTX 1050 Carte Graphique pour PC, 4Go GDDR5 128-bit, HDMI DisplayPort DVI-D")
        self.rejete("gpus", "Radeon RX 570", "MSI RX 570 Armor 8G OC Carte Graphique AMD 8 Go PCI Express x16")
        self.rejete("gpus", "Arc A770",
                    "WEELIAO GUNNIR Intel Arc A770 Photon 8GB OC GDDR6 2400MHz Triple Fan Black Graphics Card")

    def test_accessoire_pour_la_carte(self):
        self.rejete("gpus", "GeForce GTX 1650 Super",
                    "DOTODO Ventilateur de refroidissement pour carte graphique 75 mm HA8010H12F-Z "
                    "CGPU pour MSI GeForce GTX 1650 Super Ventus")

    def test_ada_exige(self):
        self.rejete("gpus", "RTX 6000 Ada",
                    "PNY Quadro RTX 6000 Professional Scheda grafica 24GB GDDR6 PCI Express 3.0 x16")
        self.rejete("gpus", "RTX 4000 Ada",
                    "PNY NVIDIA RTX A4000 Carte Graphique Professionnelle 16GB GDDR6 PCI Express 4.0 x16")
        self.accepte("gpus", "RTX 6000 Ada",
                     "PNY NVIDIA RTX 6000 Ada Generation 48GB GDDR6 Carte Graphique Professionnelle")

    def test_cartes_partenaires_correctes(self):
        self.accepte("gpus", "GeForce RTX 5080",
                     "ASUS TUF Gaming GeForce RTX 5080 16GB GDDR7 OC Edition Carte Graphique")
        self.accepte("gpus", "GeForce RTX 5050", "PNY Carte Graphique GeForce RTX™ 5050 8Go Dual Fan DLSS 4")
        self.accepte("gpus", "GeForce RTX 4060 Ti", "ASUS Dual GeForce RTX™ 4060 Ti EVO OC Edition 8GB GDDR6")

    def test_variante_memoire_du_catalogue(self):
        self.accepte("gpus", "GeForce RTX 3080", "MSI GeForce RTX 3080 Gaming Z Trio 10G LHR")
        self.rejete("gpus", "GeForce RTX 3080", "MSI GeForce RTX 3080 Gaming Z Trio 12G LHR")
        self.accepte("gpus", "GeForce RTX 3080 12G", "MSI GeForce RTX 3080 Gaming Z Trio 12G LHR")
        self.rejete("gpus", "GeForce RTX 4060 Ti", "ASUS Dual GeForce RTX 4060 Ti 16GB GDDR6 OC")


class AuditProcesseurs(_Base):
    def test_pc_complet_ou_accessoire(self):
        self.rejete("cpus", "Core i5-14500", "Acer Veriton X2720G i5-14500 16GB 512GBSSD B4B W11P")
        self.rejete("cpus", "Ryzen 7 5700G", "ARCTIC Senza 5700G - PC Montage sous Table, Refroidissement Passif")
        self.rejete("cpus", "Core i5-10600K",
                    "Genuine Intel Cooler for Core i5-10600K / i7-10700K / i9-10900K Heat Sink Tower Cooling Fan")
        self.rejete("cpus", "Xeon w9-3495X",
                    "A-Tech 64GB RAM for Intel Xeon w5-2465X, w7-3445, w9-3475X, w9-3495X | DDR5 4800MHz")

    def test_titres_corrects_malgre_leurs_ecarts(self):
        # « ™ » collé, « Ryzensets », « Core » omis : vrais processeurs vendus seuls.
        self.accepte("cpus", "Ryzen 7 7800X3D",
                     "AMD Ryzensets 7 7800X3D Processor with 3D V Cache technology (8 cores/16 threads)")
        self.accepte("cpus", "Core i5-12600K", "Intel i5-12600K 4.90GHZ LGA1700 Tray")
        self.accepte("cpus", "Core i7-13700K",
                     "Intel® Core™ i7-13700K Desktop-Prozessor 16 Kerne 30 MB Cache, bis zu 5,4 GHz")
        self.accepte("cpus", "Core Ultra 7 265KF",
                     "Intel Ultra 7 265KF Processeur de Bureau, 20 cœurs, jusqu'à 5,5 GHz, Cache 36 Mo")
        self.accepte("cpus", "Ryzen Threadripper 7980X",
                     "AMD Ryzen™ Threadripper™ 7980X 64-Core, 128-Thread Processor : Electronics")


class AuditMemoire(_Base):
    def test_kit_de_32_go_pour_16_go(self):
        self.rejete("rams", "16 Go DDR5-6000 CL36",
                    "G.skill Ripjaws M5 Rgb Ddr5 2x16gb 6000mhz Cl36-36 Black F5-6000j3636f16gx2-Rm5rk")

    def test_frequence_differente(self):
        self.rejete("rams", "32 Go DDR5-4800 CL40",
                    "Crucial RAM DDR5 32Go 5600MHz, PC Mémoire (ou 5200MHz / 4800MHz) CL46 - CT32G56C46U5")
        self.rejete("rams", "32 Go DDR5-8000 CL40",
                    "G.skill Trident Z5 Neo Rgb - Ddr5 - Kit - 32gb: 2 X 16gb - Dimm 288-Pin - "
                    "6000 Mhz / Pc5-48000 - Cl36 - NoN-Ecc")

    def test_barrette_de_portable(self):
        self.rejete("rams", "8 Go DDR4-2666 CL16",
                    "Timetec 8 Go Compatible pour Apple DDR4 2666 MHz pour iMac mi-2020 (20.1/20.2)")

    def test_bons_kits(self):
        self.accepte("rams", "32 Go DDR5-6000 CL36",
                     "Kingston FURY Beast 32Go (2x16Go) 6000MT/s DDR5 CL36 Kit de 2 Mémoire pour PC")
        self.accepte("rams", "16 Go DDR4-3200 CL16",
                     "Corsair Vengeance LPX 16 Go (2 x 8 Go) DDR4 3200 MHz CL16 Mémoire de bureau")
        # Latence différente : tolérée en mode souple seulement (choix existant).
        s, o = verdicts("rams", "32 Go DDR5-6000 CL36",
                        "Kingston FURY Beast 32Go (2x16Go) 6000MT/s DDR5 CL30 DIMM")
        self.assertEqual((s, o), (False, True))


class AuditStockage(_Base):
    def test_vitesse_de_rotation(self):
        self.rejete("ssds", "WD Blue 2 To (7200)",
                    "WD Blue 2 To Mobile Disque Dur Interne 2.5\", Classe 5400 RPM, SATA 6 Gb/s, Cache de 128 Mo")

    def test_disque_externe(self):
        self.rejete("ssds", "Seagate FireCuda 2 To (7200)",
                    "Seagate FireCuda Gaming Hard Drive, 2 To, Disque Dur Externe Portable HDD, USB-C, USB 3/2")

    def test_ssd_au_lieu_du_disque_dur(self):
        self.rejete("ssds", "WD Black 4 To (7200)",
                    "WD_BLACK SN850X NVMe SSD 4 To (Jusqu'à 7 300 Mo/s / 6 600 Mo/s en lecture/écriture)")

    def test_lot_de_deux(self):
        self.rejete("ssds", "Kingston A400 480 Go",
                    "Kingston Lot de 2 disques SSD internes A400 SATA 3 2,5\" SA400S37/480G - 480 Go")

    def test_gamme_pro(self):
        self.rejete("ssds", "Seagate BarraCuda 2 To",
                    "Seagate BarraCuda Pro 2 To, Disque dur interne HDD – 3,5\" SATA 6 Gbit/s 7 200 tr/min")

    def test_bons_disques(self):
        self.accepte("ssds", "WD Blue 2 To (7200)",
                     "WD Blue 2 To Disque dur interne 3.5\" 7200 tr/min SATA 6 Gb/s, Cache 256 Mo")
        self.accepte("ssds", "Seagate BarraCuda 2 To",
                     "Seagate BarraCuda 2 To, Disque dur interne HDD – 3,5\" SATA 6 Gbit/s 7 200 tr/min")
        self.accepte("ssds", "Kingston A400 480 Go", "Kingston A400 SSD Interne 2.5\" SATA 480 Go")
        self.accepte("ssds", "Samsung 990 Pro (Gen4)",
                     "Samsung 990 PRO NVMe M.2 SSD 2 To, PCIe 4.0, jusqu'à 7 450 Mo/s")


# --- audit E2 : cartes mères, alimentations, boîtiers, refroidissements, ventilateurs
class AuditCartesMeres(_Base):
    def test_format_m_atx(self):
        self.rejete("mobos", "Gigabyte B550 Aorus Elite",
                    "Gigabyte GIGABYTE B550M AORUS Elite (AM4/ AMD/ B550/ Micro-ATX/DDR4/Dual M.2/PCIe 4.0)")
        self.rejete("mobos", "Gigabyte B660 DS3H DDR4",
                    "GIGABYTE B660M DS3H DDR4 (Socket 1700/B660/DDR4/S-ATA 600/Micro ATX) Carte mère Noir")

    def test_variante_ddr4(self):
        self.rejete("mobos", "MSI MAG B760M Mortar",
                    "MSI MAG B760M Mortar WiFi DDR4 Carte Mère, Micro-ATX - pour Processeurs Intel Core 14e")
        self.rejete("mobos", "MSI MPG Z690 Edge WiFi",
                    "MSI MPG Z690 Edge WiFi DDR4 Carte Mère Gaming, ATX - pour Processeurs Intel Core 12è Gen")
        self.rejete("mobos", "ASUS Prime Z690-P WiFi",
                    "ASUS PRIME Z690-P WIFI D4 – Carte mère Intel Z690 LGA 1700 ATX (PCIe 5.0, DDR4)")
        self.accepte("mobos", "MSI PRO B660M-A WiFi DDR4",
                     "MSI PRO B660M-A WiFi DDR4 Motherboard, Micro-ATX - Supports Intel 12th Gen Core Processors")

    def test_autres_modeles(self):
        self.rejete("mobos", "Gigabyte Z890 Aorus Master",
                    "GIGABYTE Z890 AORUS Master AI Top Carte Mère - Prend en Charge Les processeurs Intel Core Ultra")
        self.rejete("mobos", "ASUS ROG Maximus Z790 Apex",
                    "ASUS ROG Maximus Z790 APEX Encore Carte mère Intel LGA 1700, ATX DDR5, Intel Wi-FI 7")

    def test_version_wifi(self):
        self.rejete("mobos", "ASUS TUF B550-Plus",
                    "ASUS TUF Gaming B550-PLUS WiFi II AMD AM4 (3rd Gen Ryzen™) ATX DDR4 Gaming Motherboard")
        self.rejete("mobos", "ASUS TUF Gaming X570-Plus",
                    "ASUS AM4 TUF Gaming X570-Plus (Wi-Fi) AM4 Zen 3 Ryzen 5000 & 3rd Gen Ryzen ATX Motherboard")
        self.rejete("mobos", "ASUS TUF Gaming X570-Plus",
                    "ASUS Computer TUF Gaming X570-Plus Carte mère AMD AM4 X570 ATX (PCIe 4.0, Dual M.2, "
                    "Wi-FI, 14 Dr. MOS Power Stages, HDMI, DP)")

    def test_cartes_qui_n_existent_qu_en_wifi(self):
        self.accepte("mobos", "ASUS ROG Strix Z890-I", "ASUS ROG STRIX Z890-I GAMING WIFI Mini-ITX Motherboard")
        self.accepte("mobos", "ASRock B650E PG-ITX",
                     "ASRock B650E PG-ITX WiFi AMD Ryzen 7000 Series CPU Compatible (Soket AM5) B650 Mini-ITX")
        self.accepte("mobos", "Gigabyte X870E Aorus Elite",
                     "GIGABYTE X870E AORUS Elite WIFI7 AM5 LGA 1718, ATX, DDR5, 4X M.2, PCIe 5.0, USB4, Wi-Fi 7")
        self.accepte("mobos", "ASUS ROG Strix Z890-A Gaming WiFi",
                     "ASUS ROG Strix Z890-A Gaming WiFi AI Overclocking, Carte mère ATX DDR5")

    def test_bonnes_cartes(self):
        self.accepte("mobos", "Gigabyte B550 Aorus Elite",
                     "Gigabyte B550 AORUS Elite V2 (AM4/AMD/B550/ATX/DDR4/Dual M.2/PCIe 4.0) Carte mère")
        self.accepte("mobos", "ASUS TUF B550-Plus",
                     "ASUS TUF Gaming B550-PLUS AMD AM4 ATX Gaming Carte mère (PCIe 4.0, 2.5 Gb LAN)")


class AuditAlimentations(_Base):
    def test_gamme_ou_label_differents(self):
        self.rejete("psus", "Corsair RM1000x",
                    "Corsair RM1000x SHIFT Alimentation ATX Entièrement Modulaire - Interface Latérale Modulaire")
        self.rejete("psus", "ASUS ROG Strix 1000W",
                    "ASUS ROG Strix 1000W Platinum, Alimentation entièrement modulaire, 80 Plus Platinum Certified")
        self.rejete("psus", "Corsair HX850",
                    "Corsair 850 Watts 80PLUS Gold Certified Modular ATX12V/EPS12V Power Supply HX850 : Electronics")
        self.rejete("psus", "be quiet! Pure Power 12 M 550W",
                    "be quiet! Pure Power 12 550W Alimentation, Certification 80 Plus® Gold, Alimentation ATX 3.1")

    def test_bonnes_alimentations(self):
        self.accepte("psus", "be quiet! Pure Power 12 M 650W",
                     "be quiet! Pure Power 12 M 650 W Bloc d’Alimentation, Certification 80 Plus® Gold")
        self.accepte("psus", "Corsair RM1000x",
                     "CORSAIR RM1000x Alimentation ATX entièrement modulaire et silencieuse – 80 PLUS Gold")
        self.accepte("psus", "Cooler Master MWE Gold 750 V2",
                     "Cooler Master MWE Gold 750 V2 entièrement modulaire, 750 W, efficacité or 80+")
        # « System Power » est une gamme, pas un « system » monté (rejet ancien).
        self.accepte("psus", "be quiet! System Power 10 550W",
                     "be quiet! System Power 10 550W Alimentation, 80 PLUS Bronze, ATX")


class AuditBoitiers(_Base):
    def test_variantes(self):
        self.rejete("cases", "Fractal Torrent",
                    "Fractal Design Torrent Compact RGB Black - Light Tint Tempered Glass Side Panels")
        self.rejete("cases", "Corsair 5000D Airflow",
                    "CORSAIR 5000D Core Airflow Boîtier PC ATX Mid Tower - Aucun Ventilateur Inclus")
        self.rejete("cases", "Montech King 95",
                    "MONTECH King 95 Pro Boîtier de Jeu PC ATX à Double Chambre avec Haut débit d'air")
        self.rejete("cases", "Hyte Y70", "Hyte Y70 Touch Infinite Boîtier ATX avec écran Tactile LCD 2,5 K intégré")

    def test_format_et_revision_toleres(self):
        self.accepte("cases", "NZXT H5 Flow",
                     "NZXT H5 Flow Compact ATX Mid-Tower PC Gaming Case – High Airflow Perforated Front Panel")
        self.accepte("cases", "Fractal Torrent",
                     "Fractal Design Torrent Black - Tempered Glass Dark Tint - Open Grille for Maximum air Intake")
        self.accepte("cases", "Cooler Master TD500 Mesh",
                     "Cooler Master MasterBox TD500 Mesh V2 - Boîtier PC Moyen E-ATX avec Maillage Mesh Polygonal")
        self.accepte("cases", "Montech King 95", "MONTECH King 95 Boîtier PC ATX Double Chambre, Noir")


class AuditRefroidissements(_Base):
    def test_variantes_lcd(self):
        self.rejete("coolers", "Corsair Nautilus 360 RS",
                    "CORSAIR NAUTILUS 360 RS LCD AIO pour CPU, Inclus 3x RS120 – Blanc | Ecran LCD IPS 5.3cm")
        self.rejete("coolers", "Corsair iCUE Link Titan 360 RX",
                    "CORSAIR iCUE Link Titan 360 RX LCD Refroidisseur Liquide pour CPU – Chambre FlowDrive")

    def test_bons_refroidissements(self):
        self.accepte("coolers", "Corsair Nautilus 360 RS",
                     "CORSAIR Nautilus 360 RS ARGB AIO pour CPU, Inclus 3x RS120 ARGB – Noir")
        # Le Kraken 2023 a toujours un écran : « LCD » loin du modèle ne compte pas.
        self.accepte("coolers", "NZXT Kraken 280",
                     "NZXT Kraken 280 - Refroidisseur CPU AIO Liquide 280mm, LCD - Noir | AIO 280mm avec LCD carré")
        self.rejete("coolers", "NZXT Kraken 360",
                    "NZXT Kraken Elite RGB 360 - RL-KR36E-W1 - Refroidisseur pour processeur AIO de 360 mm")
        self.accepte("coolers", "Thermalright Phantom Spirit 120 SE",
                     "Thermalright Phantom Spirit 120SE ARGB Refroidisseur de CPU, 7 caloducs")
        self.accepte("coolers", "Noctua NH-U14S",
                     "Noctua NH-U14S, Ultra Silencieux et Haut de Gamme Radiateur pour CPU avec Ventilateur NF-A15")
        self.accepte("coolers", "Thermalright Frozen Notte 360 ARGB",
                     "Thermalright Frozen Notte 360 Noir ARGB V2 Refroidissement à eau CPU Radiateur 360")

    def test_suffixe_co(self):
        self.accepte("coolers", "Arctic Freezer 36 CO", "ARCTIC Freezer 36 CO - Refroidisseur d'air pour processeur monotour")
        self.rejete("coolers", "Arctic Freezer 36 CO", "ARCTIC Freezer 36 - Single-Tower CPU Cooler with Push-Pull")
        self.rejete("coolers", "Arctic Freezer 36", "ARCTIC Freezer 36 CO - Refroidisseur d'air pour processeur monotour")


class AuditVentilateurs(_Base):
    def test_ventilateur_a_l_unite(self):
        self.rejete("fans", "Arctic P12 (×3)",
                    "ARCTIC P12 PWM PST A-RGB - PC Fan, 120mm Fan, PC Case Fan, Single Fan : Patio, Lawn & Garden")
        self.rejete("fans", "Arctic P12 (×3)", "ARCTIC P12 PWM PST A-RGB (noir) - 120 mm Ventilateur PWM PC, Fan")
        self.rejete("fans", "Arctic P14 (×3)", "ARCTIC P14 Pro A-RGB (noir) - 140 mm Ventilateur Puissant et Haut de Gamme")
        self.rejete("fans", "be quiet! Silent Wings 4 (×3)",
                    "be quiet! Silent Wings 4 140mm Black ventilateur, pales optimisées")
        self.rejete("fans", "Noctua NF-A12x25 (×2)",
                    "Noctua NF-A12x25 PWM, Ventilateur Silencieux Haut de Gamme, 4 Broches (120mm, Marron)")
        self.rejete("fans", "NZXT F120 RGB Core (×3)",
                    "NZXT F120 RGB Core - Ventilateur de boîtier PC 120mm sur Hub RGB - Blanc")
        self.rejete("fans", "Lian Li UNI SL120 (×3)", "Lian Li UNI FAN SL120 V2 RGB PWM Lüfter - 120mm, schwarz")

    def test_extension_compatible(self):
        self.rejete("fans", "Lian Li UNI SL120 (×3)",
                    "Lian Li UNI Fan SL140 V2 RGB Black Single Pack Without Controller "
                    "(Compatible with Lian Li SL120 V2 3 Pack only) - UF-SL140V2-1B")

    def test_lots_corrects(self):
        self.accepte("fans", "Corsair LL120 RGB (×3)",
                     "Corsair LL120 RGB, Ventilateur de Boitier Dual Light Loop RGB LED PWM 120mm "
                     "avec Lighting Node et Hub (Triple Pack)")
        self.accepte("fans", "Arctic P12 (×3)", "ARCTIC P12 PWM PST (3 Pack) - 120 mm Ventilateur de boîtier")
        self.accepte("fans", "Noctua NF-A12x25 (×2)", "Noctua NF-A12x25 PWM, lot de 2 ventilateurs 120mm (Marron)")
        self.accepte("fans", "Lian Li UNI SL120 (×3)", "Lian Li UNI FAN SL120 V2 RGB 3er Pack mit Controller, schwarz")


# --- premier run GitHub du validateur (18/11/2026) : 5 faux encore acceptés -------
class PremierRunGithub(_Base):
    def test_memoire_collee_au_code_du_modele(self):
        # « O8G » (O = OC), « 08G » : 8 Go, quand le catalogue dit 4 Go.
        self.rejete("gpus", "Radeon RX 570", "Asustor ROG-STRIX-RX570-O8G-GAMING")
        self.rejete("gpus", "Radeon RX 570", "Sapphire Pulse Radeon RX 570 08G GDDR5")
        self.accepte("gpus", "Radeon RX 570",
                     "ASUS ROG-STRIX-RX570-O4G-GAMING Carte Graphique AMD Radeon RX 570 HDMI")

    def test_version_sff(self):
        self.rejete("gpus", "RTX 4000 Ada", "Nvidia Leadtek RTX 4000 SFF Ada Retail")
        self.accepte("gpus", "RTX 4000 Ada",
                     "PNY NVIDIA RTX 4000 Ada Generation 20GB GDDR6 Carte Graphique Professionnelle")
        # « SFF-Ready » décrit une carte normale qui entre dans un petit boîtier.
        self.accepte("gpus", "GeForce RTX 5070",
                     "ASUS Prime GeForce RTX™ 5070 GDDR7 12GB OC Edition – Graphics Card "
                     "(PCIe 5.0, HDMI, Display 2.1, 2.5 Slot, Axial Fans, SFF-Ready)")

    def test_label_sans_80_plus(self):
        self.rejete("psus", "ASUS ROG Strix 1000W",
                    "ASUS ROG Strix - 1000W Platinum, Alimentation modulaire, Blanc | 1000W, ATX 3.1")
        self.accepte("psus", "ASUS ROG Strix 1000W",
                     "ASUS ROG Strix 1000W Gold Aura Edition, Alimentation modulaire, ATX 3.0")
        # Un label Cybenetics n'est pas un label 80 PLUS.
        self.accepte("psus", "NZXT C750 Gold",
                     "NZXT C750 Gold - 750W ATX 3.1 Netzteil, Cybenetics Platinum, vollmodular")

    def test_vitesse_en_tours_par_minute(self):
        self.rejete("ssds", "WD Blue 2 To (7200)",
                    "WESTERN DIGITAL Blue WD20EZAZ - Disque dur - 2 To - interne - 3.5\" - "
                    "SATA 6Gb/s - 5400 tours/min")
        self.accepte("ssds", "WD Blue 2 To (7200)",
                     "WESTERN DIGITAL Blue WD20EZBX - Disque dur - 2 To - interne - 3.5\" - "
                     "SATA 6Gb/s - 7200 tours/min")

    def test_disque_de_2_5_pouces_pour_un_7200(self):
        self.rejete("ssds", "Seagate FireCuda 2 To (7200)",
                    "Seagate FireCuda, 2 To, Disque hybride hautes performances SSHD, 2,5\" "
                    "SATA 6 Gbit/s, 8 Go de mémoire cache")
        self.rejete("ssds", "Seagate FireCuda 2 To (7200)",
                    "Seagate FireCuda 2 To SSHD 2.5 pouces SATA 6 Gbit/s")
        self.accepte("ssds", "Seagate FireCuda 2 To (7200)",
                     "Seagate FireCuda, 2 To, Disque dur hybride SSHD interne 3,5\" SATA 6 Gbit/s, "
                     "7200 tr/min, 64 Mo de mémoire cache")


# --- mécanique ------------------------------------------------------------------
class Mecanique(unittest.TestCase):
    def test_signature_compatible(self):
        # `verifier_amazon` appelle valide(cat, nom, titre, jetons, souple=True).
        params = list(inspect.signature(ra.valide).parameters.values())
        self.assertEqual([p.name for p in params[:5]], ["cat", "nom_app", "titre", "jetons", "souple"])
        self.assertIs(params[4].default, False)
        self.assertTrue(all(p.default is not inspect.Parameter.empty for p in params[4:]))

    def test_catalogue_charge_depuis_le_dossier_designe(self):
        ancien = ra._DOSSIER_CATALOGUE
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "cases.json"), "w", encoding="utf-8") as f:
                json.dump([{"name": "Lian Li O11 Dynamic"}, {"name": "Lian Li O11 Dynamic EVO"}], f)
            try:
                ra.definir_catalogue(d)
                titre = "Lian Li O11 Dynamic Evo - Midi Tower, Tempered Glass - Schwarz"
                jetons = ra._jetons("cases", {"name": "Lian Li O11 Dynamic"})
                # Sans catalogue explicite, le fichier du dossier fait foi.
                self.assertFalse(ra.valide("cases", "Lian Li O11 Dynamic", titre, jetons, souple=True))
            finally:
                ra.definir_catalogue(ancien)

    def test_catalogue_illisible_ne_bloque_rien(self):
        jetons = ra._jetons("cases", {"name": "NZXT H5 Flow"})
        self.assertTrue(ra.valide("cases", "NZXT H5 Flow", "NZXT H5 Flow - Boîtier PC Compact ATX Mid-Tower",
                                  jetons, souple=True, catalogue=[]))

    def test_numeros_de_toute_longueur(self):
        self.assertTrue(ra.numeros_coherents("Fractal Meshify 2", "Fractal Design Meshify 2 Black"))
        self.assertFalse(ra.numeros_coherents("Fractal Meshify 2", "Fractal Design Meshify 3 Black"))
        self.assertTrue(ra.numeros_coherents("be quiet! Pure Base 500 FX", "be quiet! Pure Base 500 FX Boîtier"))
        self.assertFalse(ra.numeros_coherents("be quiet! Pure Base 500 FX", "be quiet! Pure Base 500DX"))
        self.assertTrue(ra.numeros_coherents("Thermalright Assassin King 120 SE",
                                             "Thermalright Assassin King 120SE ARGB"))
        self.assertFalse(ra.numeros_coherents("Thermalright Assassin X120 R", "Thermalright AssassinX120 SE ARGB"))
        self.assertTrue(ra.numeros_coherents("Lian Li Lancool III", "Lian Li Lancool Iii Rgb - Mid Tower"))
        self.assertFalse(ra.numeros_coherents("Lian Li Lancool II", "Lian Li Lancool III Rgb - Mid Tower"))


    def test_memoire_et_ssd_disent_leur_machine_cible(self):
        # « Desktop », « Ordinateur de bureau », « Laptop » décrivent la machine
        # cible d'une barrette ou d'un SSD, pas un PC monté (18/11/2026).
        ram48 = {"name": "48 Go DDR5-6000 CL30", "kind": "DDR5", "mhz": 6000, "sizeGb": 48, "cl": 30}
        ram32 = {"name": "32 Go DDR5-4800 CL40", "kind": "DDR5", "mhz": 4800, "sizeGb": 32, "cl": 40}
        t500 = {"name": "Crucial T500 (Gen4)", "kind": "NVMe Gen4"}
        cas = [
            ("rams", ram48, "CORSAIR VENGEANCE DDR5 RAM 48Go (2x24Go) 6000MHz CL30 Intel XMP "
             "iCUE Compatible Mémoire pour Ordinateur de Bureau - Noir (CMK48GX5M2B6000C30)"),
            ("rams", ram32, "Corsair VENGEANCE DDR5 32GB (2x16GB) 4800MHz C40 "
             "Desktop-Arbeitsspeicher (CMK32GX5M2A4800C40)"),
            ("ssds", t500, "Crucial T500 SSD 1To PCIe Gen4 NVMe M.2 SSD Interne, "
             "Laptop und Desktop (CT1000T500SSD8)"),
        ]
        for cat, it, titre in cas:
            self.assertTrue(ra.valide(cat, it["name"], titre, ra._jetons(cat, it), souple=True,
                                      catalogue=[]), titre)
        # Un vrai PC monté reste rejeté.
        ram = {"name": "32 Go DDR5-6000 CL30", "kind": "DDR5", "mhz": 6000, "sizeGb": 32, "cl": 30}
        self.assertFalse(ra.valide("rams", ram["name"], "PC de bureau gaming Ryzen 7 avec 32 Go DDR5 6000 CL30",
                                   ra._jetons("rams", ram), souple=True, catalogue=[]))

    def test_kit_accessoires_et_vitesses_ne_font_pas_un_lot(self):
        # « Rich Accessory Kit, 2 Speed Settings » : un seul ventilateur (18/11/2026).
        self.assertFalse(ra.lot_de_ventilateurs("Noctua NF-A12x25 (×2)",
            "Noctua NF-A12x25 ULN, High-Performance Whisper Quiet Fan 120mm | Ultra Low Noise 25mm "
            "Thick Fan, for Heatsinks and Radiators, Rich Accessory Kit, 2 Speed Settings 1200/900rpm"))
        self.assertTrue(ra.lot_de_ventilateurs("Noctua NF-A12x25 (×2)",
                                               "Noctua NF-A12x25 PWM, lot de 2 ventilateurs 120 mm"))

if __name__ == "__main__":
    unittest.main()
