"""Adaptador do ZAP Imóveis.

O ZAP roda na mesma plataforma do VivaReal (Grupo OLX): mesmo payload RSC
com o array "listings", mesmo modelo de dados. Este adaptador reaproveita
o parser do VivaReal e troca só as URLs de busca e o nome do portal.

Atenção: por serem do mesmo grupo, boa parte do estoque é cross-postado —
a dedup por fingerprint (pipelines/anuncios.py) é o que impede o mesmo
imóvel de contar duas vezes nas medianas.
"""
import random
import time

import requests

from radar.config import HTTP_HEADERS
from radar.portais import PortalIndisponivel
from radar.portais import vivareal

PORTAL = "zapimoveis"

ZONAS = ("zona-sul", "zona-oeste", "zona-norte", "zona-leste", "centro")
TIPOS_URL = {
    "apartamento": "apartamentos",
    "casa": "casas",
}


def _seeds(tipo: str):
    slug = TIPOS_URL[tipo]
    return [(zona, f"https://www.zapimoveis.com.br/venda/{slug}/sp+sao-paulo+{zona}/")
            for zona in ZONAS]


def coletar_paginas(tipo: str, paginas: int, delay_min: float = 2.5,
                    delay_max: float = 6.0):
    sess = requests.Session()
    sess.headers.update(HTTP_HEADERS)
    primeiro = True
    for zona, seed in _seeds(tipo):
        for pagina in range(1, paginas + 1):
            if not primeiro:
                time.sleep(random.uniform(delay_min, delay_max))
            primeiro = False
            url = seed if pagina == 1 else f"{seed}?pagina={pagina}"
            resp = sess.get(url, timeout=60)
            if resp.status_code != 200:
                raise PortalIndisponivel(f"zap: HTTP {resp.status_code} em {url}")
            try:
                brutos = vivareal._extrair_listings(resp.text)
            except PortalIndisponivel as e:
                raise PortalIndisponivel(f"zap: {e}")
            normalizados = []
            for b in brutos:
                try:
                    n = vivareal._normalizar(b)
                except Exception:
                    continue
                if n:
                    n["portal"] = PORTAL
                    normalizados.append(n)
            yield f"{zona} p{pagina}", normalizados
            if not brutos:
                break
