# -*- coding: utf-8 -*-
"""Source de prix AMAZON via la **Product Advertising API 5.0** (PA-API).

Amazon n'est pas scrapable en direct (protections anti-bot) : les seuls prix
Amazon fiables passent par la PA-API, ouverte aux membres du programme
**Amazon Associates** (les comptes sont déjà créés — lot 31, partie 1).

Cette source suit le MÊME contrat que `sources/newegg.py` et
`sources/boutiques.py` — une méthode `offres(requete)` qui renvoie :

    {"shop": "amazon.fr", "price": float, "currency": "EUR",
     "url": str, "product": str, "image": str|None, "in_stock": bool}

Deux points la distinguent :

* l'`url` renvoyée est la `DetailPageURL` de l'API, qui contient **déjà le
  Partner Tag** de la marketplace : le lien est affilié à la source ;
* chaque marketplace a son propre hôte, sa région de signature, sa devise et
  son Partner Tag (table `MARKETPLACES`).

Authentification : signature **AWS Signature Version 4** (service
`ProductAdvertisingAPI`) calculée ici à la main — pas de SDK à installer, juste
`hmac`/`hashlib` de la bibliothèque standard et `requests`, déjà utilisé partout
dans le scraper.

Identifiants attendus (variables d'environnement, jamais en dur) :

    AMAZON_ACCESS_KEY   clé d'accès PA-API  (Associates Central > Outils > PA-API)
    AMAZON_SECRET_KEY   clé secrète PA-API

Les Partner Tags, eux, sont publics (ils apparaissent dans les URLs) et sont
donc versionnés dans `MARKETPLACES`.
"""

import datetime
import hashlib
import hmac
import json
import os
import time

import requests

# Marketplaces Amazon couvertes : hôte PA-API, région de signature, devise et
# Partner Tag du programme Associates correspondant.
# Réf. hôtes/régions : https://webservices.amazon.com/paapi5/documentation/common-request-parameters.html
MARKETPLACES = {
    "amazon.fr": {
        "host": "webservices.amazon.fr", "region": "eu-west-1",
        "marketplace": "www.amazon.fr", "currency": "EUR", "tag": "fraym-21",
    },
    "amazon.de": {
        "host": "webservices.amazon.de", "region": "eu-west-1",
        "marketplace": "www.amazon.de", "currency": "EUR", "tag": "fraym03-21",
    },
    "amazon.es": {
        "host": "webservices.amazon.es", "region": "eu-west-1",
        "marketplace": "www.amazon.es", "currency": "EUR", "tag": "fraym08-21",
    },
    "amazon.it": {
        "host": "webservices.amazon.it", "region": "eu-west-1",
        "marketplace": "www.amazon.it", "currency": "EUR", "tag": "fraym06-21",
    },
    "amazon.co.uk": {
        "host": "webservices.amazon.co.uk", "region": "eu-west-1",
        "marketplace": "www.amazon.co.uk", "currency": "GBP", "tag": "fraym0a-21",
    },
    "amazon.com": {
        "host": "webservices.amazon.com", "region": "us-east-1",
        "marketplace": "www.amazon.com", "currency": "USD", "tag": "fraym-20",
    },
    "amazon.com.au": {
        "host": "webservices.amazon.com.au", "region": "us-west-2",
        "marketplace": "www.amazon.com.au", "currency": "AUD", "tag": "fraym-22",
    },
}

# Champs demandés à l'API. Chaque ressource coûte du quota : on ne demande que
# le strict nécessaire (titre, prix, disponibilité, image, marque).
RESOURCES = [
    "ItemInfo.Title",
    "ItemInfo.ByLineInfo",
    "Offers.Listings.Price",
    "Offers.Listings.Availability.Message",
    "Offers.Listings.Availability.Type",
    "Images.Primary.Medium",
]

_SERVICE = "ProductAdvertisingAPI"
_CIBLE = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1"


class AmazonPaapiError(RuntimeError):
    """Erreur remontée par la PA-API (quota, identifiants, requête invalide)."""


class AmazonPaapiSource:
    """Client PA-API 5.0 pour UNE marketplace.

    [domaine] est une clé de `MARKETPLACES` (« amazon.fr », « amazon.com »…).
    [rate_limit] : le quota de départ d'un compte Associates est de **1 requête
    par seconde** (il monte avec le chiffre d'affaires généré) — on reste au-
    dessus par défaut.
    """

    def __init__(self, domaine="amazon.fr", access_key=None, secret_key=None,
                 rate_limit=1.2, timeout=25):
        if domaine not in MARKETPLACES:
            raise ValueError(f"marketplace inconnue : {domaine}")
        self.domaine = domaine
        self.conf = MARKETPLACES[domaine]
        self.devise = self.conf["currency"]
        self.tag = self.conf["tag"]
        self._access = access_key or os.environ.get("AMAZON_ACCESS_KEY", "")
        self._secret = secret_key or os.environ.get("AMAZON_SECRET_KEY", "")
        self._rate = rate_limit
        self._timeout = timeout
        self._dernier = 0.0
        self._sess = requests.Session()

    @property
    def configure(self):
        """Vrai si les deux clés PA-API sont présentes."""
        return bool(self._access and self._secret)

    # ------------------------------------------------------------------ API

    def offres(self, requete, search_index="Electronics", nb=6):
        """Offres Amazon pour la recherche textuelle [requete].

        Renvoie une liste au schéma commun du scraper (voir en-tête). Les
        articles sans prix (Amazon ne publie pas toujours d'offre via l'API)
        sont ignorés — ils ne constituent pas un « prix vérifié ».
        """
        data = self._appel("SearchItems", {
            "Keywords": requete,
            "SearchIndex": search_index,
            "ItemCount": max(1, min(10, nb)),
            "Resources": RESOURCES,
        })
        items = (data.get("SearchResult") or {}).get("Items") or []
        return [o for o in (self._vers_offre(it) for it in items) if o]

    def offres_par_asin(self, asins):
        """Offres pour une liste d'ASIN connus (≤ 10 par appel).

        Plus fiable et moins coûteux que la recherche quand l'ASIN a déjà été
        résolu une fois : c'est le chemin utilisé par le cache d'ASIN de
        `enrich_amazon.py`.
        """
        asins = [a for a in asins if a][:10]
        if not asins:
            return []
        data = self._appel("GetItems", {
            "ItemIds": asins,
            "Resources": RESOURCES,
        })
        items = (data.get("ItemsResult") or {}).get("Items") or []
        return [o for o in (self._vers_offre(it) for it in items) if o]

    # -------------------------------------------------------------- interne

    def _vers_offre(self, item):
        """Article PA-API -> offre au schéma du scraper (ou None si inutilisable)."""
        titre = (((item.get("ItemInfo") or {}).get("Title") or {}).get("DisplayValue")
                 or "").strip()
        listings = ((item.get("Offers") or {}).get("Listings")) or []
        if not titre or not listings:
            return None
        listing = listings[0]
        prix = ((listing.get("Price") or {}).get("Amount"))
        if not prix or float(prix) <= 0:
            return None
        devise = ((listing.get("Price") or {}).get("Currency")) or self.devise
        dispo = (listing.get("Availability") or {})
        # « Now » = expédié tout de suite ; le reste (précommande, délai long,
        # « actuellement indisponible ») est traité comme rupture côté app.
        type_dispo = (dispo.get("Type") or "").lower()
        message = (dispo.get("Message") or "").lower()
        in_stock = type_dispo in ("now", "") and "unavailable" not in message
        image = (((item.get("Images") or {}).get("Primary") or {})
                 .get("Medium") or {}).get("URL")
        return {
            "shop": self.domaine,
            "price": round(float(prix), 2),
            "currency": devise,
            # DetailPageURL porte DÉJÀ le Partner Tag de cette marketplace.
            "url": item.get("DetailPageURL") or "",
            "product": titre,
            "image": image or None,
            "in_stock": bool(in_stock),
            "asin": item.get("ASIN"),
        }

    def _appel(self, operation, charge):
        """Appel signé SigV4 d'une opération PA-API. Renvoie le JSON décodé."""
        if not self.configure:
            raise AmazonPaapiError(
                "clés PA-API absentes : renseigner AMAZON_ACCESS_KEY et "
                "AMAZON_SECRET_KEY (voir docs/AMAZON_AFFILIATION.md)")
        corps = dict(charge)
        corps["PartnerTag"] = self.tag
        corps["PartnerType"] = "Associates"
        corps["Marketplace"] = self.conf["marketplace"]
        payload = json.dumps(corps)
        chemin = f"/paapi5/{operation.lower()}"
        entetes = self._signer(operation, chemin, payload)

        ecart = time.time() - self._dernier
        if ecart < self._rate:
            time.sleep(self._rate - ecart)
        r = self._sess.post(f"https://{self.conf['host']}{chemin}",
                            data=payload.encode("utf-8"),
                            headers=entetes, timeout=self._timeout)
        self._dernier = time.time()
        if r.status_code == 429:
            raise AmazonPaapiError("quota PA-API dépassé (429) — ralentir le débit")
        if r.status_code >= 400:
            detail = r.text[:300].replace("\n", " ")
            raise AmazonPaapiError(f"HTTP {r.status_code} : {detail}")
        return r.json()

    def _signer(self, operation, chemin, payload):
        """En-têtes signés AWS Signature Version 4 pour la PA-API."""
        host = self.conf["host"]
        region = self.conf["region"]
        maintenant = datetime.datetime.now(datetime.timezone.utc)
        horodatage = maintenant.strftime("%Y%m%dT%H%M%SZ")
        jour = maintenant.strftime("%Y%m%d")
        cible = f"{_CIBLE}.{operation}"

        entetes = {
            "content-encoding": "amz-1.0",
            "content-type": "application/json; charset=utf-8",
            "host": host,
            "x-amz-date": horodatage,
            "x-amz-target": cible,
        }
        signes = ";".join(sorted(entetes))
        canoniques = "".join(f"{k}:{entetes[k]}\n" for k in sorted(entetes))
        requete_canonique = "\n".join([
            "POST", chemin, "", canoniques, signes,
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        ])
        portee = f"{jour}/{region}/{_SERVICE}/aws4_request"
        a_signer = "\n".join([
            "AWS4-HMAC-SHA256", horodatage, portee,
            hashlib.sha256(requete_canonique.encode("utf-8")).hexdigest(),
        ])

        cle = _hmac(f"AWS4{self._secret}".encode("utf-8"), jour)
        cle = _hmac(cle, region)
        cle = _hmac(cle, _SERVICE)
        cle = _hmac(cle, "aws4_request")
        signature = hmac.new(cle, a_signer.encode("utf-8"), hashlib.sha256).hexdigest()

        entetes["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._access}/{portee}, "
            f"SignedHeaders={signes}, Signature={signature}")
        return entetes


def _hmac(cle, message):
    return hmac.new(cle, message.encode("utf-8"), hashlib.sha256).digest()
