"""Adaptador do VivaReal.

As páginas de busca são renderizadas no servidor (Next.js) e embutem o
payload RSC com os 30 anúncios da página em JSON — com preço, área,
quartos, vagas, bairro e até coordenadas. Não é preciso executar
JavaScript, então usamos requests puro (mais leve e estável que um
navegador). Se o portal passar a exigir JS, este adaptador é o único
lugar a trocar por Playwright.

Coleta educada: delays aleatórios entre páginas, caminhos permitidos
pelo robots.txt (/venda/... com ?pagina=N), volume moderado por rodada.
"""
import json
import random
import re
import time

import requests

from radar.config import HTTP_HEADERS
from radar.portais import PortalIndisponivel

PORTAL = "vivareal"

# Uma busca por zona multiplica a cobertura: a busca citywide devolve sempre
# as mesmas primeiras páginas, enquanto as zonas fatiam o estoque em 5.
ZONAS = ("zona-sul", "zona-oeste", "zona-norte", "zona-leste", "centro")
TIPOS_URL = {
    "apartamento": "apartamento_residencial",
    "casa": "casa_residencial",
}


def _seeds(tipo: str):
    slug = TIPOS_URL[tipo]
    return [(zona, f"https://www.vivareal.com.br/venda/sp/sao-paulo/{zona}/{slug}/")
            for zona in ZONAS]

UNIT_TYPES = {
    "APARTMENT": "apartamento",
    "HOME": "casa",
    "CONDOMINIUM": "casa_condominio",
    "PENTHOUSE": "cobertura",
    "FLAT": "flat",
    "STUDIO": "studio",
    "KITNET": "kitnet",
    "TWO_STORY_HOUSE": "sobrado",
    "COUNTRY_HOUSE": "chacara",
    "FARM": "fazenda",
    "RESIDENTIAL_ALLOTMENT_LAND": "terreno",
    "RESIDENTIAL_BUILDING": "predio",
}

_RE_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def _decodificar_rsc(html: str) -> str:
    """Concatena os chunks do payload RSC do Next.js num único texto."""
    partes = []
    for c in _RE_CHUNK.findall(html):
        try:
            partes.append(json.loads(f'"{c}"'))
        except json.JSONDecodeError:
            continue
    return "".join(partes)


def _extrair_listings(html: str) -> list:
    blob = _decodificar_rsc(html)
    i = blob.find('"listings":[')
    if i < 0:
        raise PortalIndisponivel(
            "vivareal: array 'listings' não encontrado no payload — "
            "layout pode ter mudado ou acesso foi bloqueado"
        )
    arr, _ = json.JSONDecoder().raw_decode(blob[i + len('"listings":'):])
    return arr


def _primeiro(lista):
    if isinstance(lista, list) and lista:
        return lista[0]
    return None


def _dicionario(v):
    """Campos-objeto podem vir como '$undefined' (string) no payload RSC."""
    return v if isinstance(v, dict) else {}


def _normalizar(item: dict) -> dict | None:
    if item.get("business") != "SALE":
        return None
    portal_id = str(item.get("id") or "").strip()
    if not portal_id:
        return None
    am = _dicionario(item.get("amenities"))
    addr = _dicionario(item.get("address"))
    coords = _dicionario(addr.get("coordinates"))
    prices = _dicionario(item.get("prices"))
    sale = _dicionario(prices.get("sale"))

    def _f(v):
        return float(v) if isinstance(v, (int, float)) else None

    rua = addr.get("street")
    num = addr.get("streetNumber")
    endereco = ", ".join(str(x) for x in (rua, num) if x and x != "$undefined") or None

    return {
        "portal": PORTAL,
        "portal_id": portal_id,
        "url": item.get("href"),
        "tipo": UNIT_TYPES.get(item.get("unitType"), str(item.get("unitType") or "").lower() or None),
        "categoria": item.get("listingType"),
        "preco": _f(sale.get("value")),
        "condominio": _f(sale.get("condominium")),
        "iptu_mensal": _f(sale.get("iptu")),
        "area_m2": _f(_primeiro(am.get("usableAreas"))),
        "quartos": _primeiro(am.get("bedrooms")),
        "banheiros": _primeiro(am.get("bathrooms")),
        "suites": _primeiro(am.get("suites")),
        "vagas": _primeiro(am.get("parkingSpaces")),
        "endereco": endereco,
        "bairro_texto": addr.get("neighborhood") if addr.get("neighborhood") != "$undefined" else None,
        "lat": _f(coords.get("latitude")),
        "lon": _f(coords.get("longitude")),
    }


def coletar_paginas(tipo: str, paginas: int, delay_min: float = 2.5,
                    delay_max: float = 6.0):
    """Gera (rótulo 'zona pN', [anúncios normalizados]) com delays aleatórios.

    `paginas` é por zona: 10 páginas => 5 zonas x 10 = 50 requisições/tipo.
    """
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
                raise PortalIndisponivel(
                    f"vivareal: HTTP {resp.status_code} em {url}"
                )
            brutos = _extrair_listings(resp.text)
            normalizados = []
            for b in brutos:
                try:
                    n = _normalizar(b)
                except Exception:
                    continue  # anúncio malformado não derruba a rodada
                if n:
                    normalizados.append(n)
            yield f"{zona} p{pagina}", normalizados
            if not brutos:
                break  # acabaram os resultados desta zona
