"""Adaptador do Imovelweb (grupo QuintoAndar/Navent).

Estoque de grupo diferente do OLX (VivaReal/ZAP) — agrega anúncios
genuinamente novos. As páginas de busca embutem o estado da aplicação em
`window.__PRELOADED_STATE__`, com os 30 anúncios da página em
`listStore.listPostings`.

Limitação conhecida: os cards não trazem coordenadas — a atribuição de
bairro depende do nome do bairro coincidir com um distrito oficial
(fallback por nome do geocodificador). Anúncios sem match ficam no banco
com bairro_id NULL, fora das medianas.
"""
import json
import random
import re
import subprocess
import time

from radar.config import HTTP_HEADERS
from radar.portais import PortalIndisponivel

PORTAL = "imovelweb"
BASE = "https://www.imovelweb.com.br"

TIPOS_URL = {
    "apartamento": "apartamentos-venda-sao-paulo-sp",
    "casa": "casas-venda-sao-paulo-sp",
}

TIPOS_NORMALIZADOS = {
    "apartamentos": "apartamento",
    "casas": "casa",
    "casas de condomínio": "casa_condominio",
    "coberturas": "cobertura",
    "flats": "flat",
    "kitnets": "kitnet",
    "sobrados": "sobrado",
}

_RE_STATE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)


def _extrair_postings(html: str) -> list:
    m = _RE_STATE.search(html)
    if not m:
        raise PortalIndisponivel(
            "imovelweb: __PRELOADED_STATE__ não encontrado — layout mudou "
            "ou acesso foi bloqueado"
        )
    estado, _ = json.JSONDecoder().raw_decode(m.group(1))
    return (estado.get("listStore") or {}).get("listPostings") or []


def _feature(main_features: dict, rotulo_prefixo: str):
    for f in (main_features or {}).values():
        rotulo = (f.get("label") or "").lower()
        if rotulo.startswith(rotulo_prefixo):
            try:
                return float(f.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def _normalizar(p: dict):
    ops = p.get("priceOperationTypes") or []
    preco = None
    for op in ops:
        if (op.get("operationType") or {}).get("name") == "Venda":
            precos = op.get("prices") or []
            if precos:
                preco = precos[0].get("amount")
    if preco is None:
        return None
    portal_id = str(p.get("postingId") or "").strip()
    if not portal_id:
        return None

    loc = (p.get("postingLocation") or {})
    endereco = ((loc.get("address") or {}).get("name") or "").strip(", ") or None
    bairro = None
    no = loc.get("location") or {}
    while isinstance(no, dict) and no:
        if no.get("label") == "ZONA":
            bairro = no.get("name")
            break
        no = no.get("parent")
    geo = loc.get("postingGeolocation") or {}
    if isinstance(geo, dict):
        geo = geo.get("geolocation") or geo
    lat = geo.get("latitude") if isinstance(geo, dict) else None
    lon = geo.get("longitude") if isinstance(geo, dict) else None

    mf = p.get("mainFeatures") or {}
    tipo_raw = ((p.get("realEstateType") or {}).get("name") or "").lower()
    quartos = _feature(mf, "quarto")
    banheiros = _feature(mf, "banheiro")
    vagas = _feature(mf, "vaga")
    expenses = p.get("expenses") or {}

    return {
        "portal": PORTAL,
        "portal_id": portal_id,
        "url": BASE + p.get("url", ""),
        "tipo": TIPOS_NORMALIZADOS.get(tipo_raw, tipo_raw or None),
        "categoria": "DEVELOPMENT" if p.get("developmentFeatures") else "USED",
        "preco": float(preco),
        "condominio": expenses.get("amount"),
        "iptu_mensal": None,
        "area_m2": _feature(mf, "área útil") or _feature(mf, "área total"),
        "quartos": int(quartos) if quartos else None,
        "banheiros": int(banheiros) if banheiros else None,
        "suites": None,
        "vagas": int(vagas) if vagas else None,
        "endereco": endereco,
        "bairro_texto": bairro,
        "lat": lat,
        "lon": lon,
    }


def _baixar_curl(url: str) -> str:
    """O WAF do Imovelweb bloqueia a stack TLS do Python (403), mas aceita
    a do curl — baixa via subprocess."""
    r = subprocess.run(
        ["curl", "-s", "-m", "60", "-w", "\n%{http_code}",
         "-A", HTTP_HEADERS["User-Agent"], url],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
        timeout=90,
    )
    corpo, _, status = r.stdout.rpartition("\n")
    if status.strip() != "200":
        raise PortalIndisponivel(f"imovelweb: HTTP {status.strip() or '?'} em {url}")
    return corpo


def coletar_paginas(tipo: str, paginas: int, delay_min: float = 8.0,
                    delay_max: float = 15.0):
    """Gera ('pN', [anúncios]) — paginação: sufixo -pagina-N.html.

    Delays maiores que os dos outros portais: o WAF daqui bloqueia
    facilmente rajadas. Poucas páginas por rodada é o modo sustentável.
    """
    slug = TIPOS_URL[tipo]
    delay_min = max(delay_min, 8.0)
    delay_max = max(delay_max, delay_min + 5.0)
    for pagina in range(1, paginas + 1):
        if pagina > 1:
            time.sleep(random.uniform(delay_min, delay_max))
        url = f"{BASE}/{slug}.html" if pagina == 1 \
            else f"{BASE}/{slug}-pagina-{pagina}.html"
        brutos = _extrair_postings(_baixar_curl(url))
        normalizados = []
        for b in brutos:
            try:
                n = _normalizar(b)
            except Exception:
                continue
            if n:
                normalizados.append(n)
        yield f"p{pagina}", normalizados
        if not brutos:
            return
