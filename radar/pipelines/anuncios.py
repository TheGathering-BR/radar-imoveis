"""Pipeline de anúncios: coleta incremental -> upsert em `anuncios`.

Incremental: anúncio já visto (mesmo portal+id) tem preço e ultima_captura
atualizados (preservando primeira_captura); cada coleta também grava um
ponto em `anuncio_capturas` (histórico de preço).

Atribuição de bairro: point-in-polygon com as coordenadas do anúncio;
fallback por nome do bairro quando ele coincide com um distrito oficial.
"""
import hashlib
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

import shapely
from shapely.geometry import shape

from radar.config import CIDADE_ATIVA, PRECO_M2_MAX, PRECO_M2_MIN
from radar.portais import PortalIndisponivel
from radar.portais import imovelweb, vivareal, zapimoveis

PORTAIS = {"vivareal": vivareal, "zapimoveis": zapimoveis, "imovelweb": imovelweb}

# Tipo do portal -> classe de comparação (a mesma taxonomia do ITBI, mais as
# classes que só os anúncios distinguem: vila/condomínio e cobertura).
CLASSES_POR_TIPO = {
    "apartamento": "apartamento",
    "flat": "apartamento",
    "studio": "apartamento",
    "kitnet": "apartamento",
    "duplex": "apartamento",
    "loft": "apartamento",
    "casa": "casa",
    "sobrado": "casa",
    "single_storey_house": "casa",
    "chacara": "casa",
    "casa_condominio": "casa_vila",
    "village_house": "casa_vila",
    "condominium": "casa_vila",
    "cobertura": "cobertura",
    "penthouse": "cobertura",
}


def classificar_tipo(tipo) -> str | None:
    return CLASSES_POR_TIPO.get((tipo or "").lower())


def calcular_fingerprint(reg: dict) -> str | None:
    """Impressão digital do IMÓVEL (não do anúncio), para dedup entre portais.

    O mesmo apartamento anunciado no VivaReal e no ZAP tem ids diferentes,
    mas mesma classe, quartos, área e localização. Sem endereço nem
    coordenadas não há como deduplicar — retorna None (conta como único).
    """
    area = reg.get("area_m2")
    if not area:
        return None
    lugar = _normalizar_nome(reg.get("endereco") or "")
    if not lugar and reg.get("lat") is not None and reg.get("lon") is not None:
        lugar = f"{reg['lat']:.3f},{reg['lon']:.3f}"  # ~110 m
    if not lugar:
        return None
    base = "|".join(str(x) for x in (
        classificar_tipo(reg.get("tipo")), reg.get("quartos"),
        round(area), lugar, _normalizar_nome(reg.get("bairro_texto") or ""),
    ))
    return hashlib.sha1(base.encode()).hexdigest()


def _normalizar_nome(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


class _GeocodificadorBairros:
    def __init__(self, conn: sqlite3.Connection):
        import json
        self._idx = []
        self._por_nome = {}
        for bid, nome, geom_json in conn.execute(
            "SELECT id, nome, geometria FROM bairros WHERE cidade = ?",
            (CIDADE_ATIVA,),
        ):
            g = shape(json.loads(geom_json))
            shapely.prepare(g)
            self._idx.append((bid, g.bounds, g))
            self._por_nome[_normalizar_nome(nome)] = bid

    def bairro_de(self, lat, lon, bairro_texto=None):
        if lat is not None and lon is not None:
            for bid, (minx, miny, maxx, maxy), g in self._idx:
                if minx <= lon <= maxx and miny <= lat <= maxy \
                        and shapely.contains_xy(g, lon, lat):
                    return bid
        if bairro_texto:
            return self._por_nome.get(_normalizar_nome(bairro_texto))
        return None


def _preparar(reg: dict, geo: _GeocodificadorBairros, agora: str) -> dict:
    reg = dict(reg)
    reg["cidade"] = CIDADE_ATIVA
    reg["bairro_id"] = geo.bairro_de(reg.get("lat"), reg.get("lon"),
                                     reg.get("bairro_texto"))
    preco, area = reg.get("preco"), reg.get("area_m2")
    reg["preco_m2"] = None
    reg["elegivel_mediana"] = 0
    if preco and preco > 0 and area and area > 0:
        pm2 = preco / area
        if PRECO_M2_MIN <= pm2 <= PRECO_M2_MAX:
            reg["preco_m2"] = pm2
            reg["elegivel_mediana"] = 1
    reg["classe"] = classificar_tipo(reg.get("tipo"))
    reg["fingerprint"] = calcular_fingerprint(reg)
    reg["primeira_captura"] = agora
    reg["ultima_captura"] = agora
    return reg


_CAMPOS = ["cidade", "portal", "portal_id", "url", "tipo", "categoria",
           "preco", "condominio", "iptu_mensal", "area_m2", "quartos",
           "banheiros", "suites", "vagas", "endereco", "bairro_texto",
           "lat", "lon", "bairro_id", "preco_m2", "classe", "fingerprint",
           "elegivel_mediana", "primeira_captura", "ultima_captura"]

_CAMPOS_UPDATE = [c for c in _CAMPOS
                  if c not in ("cidade", "portal", "portal_id", "primeira_captura")]


def _upsert(conn: sqlite3.Connection, reg: dict) -> int:
    sets = ", ".join(f"{c}=excluded.{c}" for c in _CAMPOS_UPDATE)
    conn.execute(
        f"""INSERT INTO anuncios ({','.join(_CAMPOS)})
            VALUES ({','.join('?' * len(_CAMPOS))})
            ON CONFLICT (portal, portal_id) DO UPDATE SET {sets}""",
        tuple(reg[c] for c in _CAMPOS),
    )
    # lastrowid não é confiável no caminho de UPDATE do upsert
    return conn.execute(
        "SELECT id FROM anuncios WHERE portal=? AND portal_id=?",
        (reg["portal"], reg["portal_id"]),
    ).fetchone()[0]


def coletar(conn: sqlite3.Connection, portal: str = "vivareal",
            tipos=("apartamento",), paginas: int = 10,
            delay_min: float = 2.5, delay_max: float = 6.0) -> dict:
    mod = PORTAIS[portal]
    geo = _GeocodificadorBairros(conn)
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {"paginas": 0, "anuncios": 0, "novos": 0, "erros": 0}

    for tipo in tipos:
        print(f"[anuncios] {portal}/{tipo}: ate {paginas} paginas por zona")
        try:
            for rotulo, lote in mod.coletar_paginas(tipo, paginas,
                                                    delay_min, delay_max):
                novos = 0
                for bruto in lote:
                    reg = _preparar(bruto, geo, agora)
                    existia = conn.execute(
                        "SELECT 1 FROM anuncios WHERE portal=? AND portal_id=?",
                        (reg["portal"], reg["portal_id"]),
                    ).fetchone()
                    aid = _upsert(conn, reg)
                    conn.execute(
                        """INSERT OR IGNORE INTO anuncio_capturas
                           (anuncio_id, capturado_em, preco) VALUES (?, ?, ?)""",
                        (aid, agora, reg.get("preco")),
                    )
                    novos += 0 if existia else 1
                conn.commit()
                stats["paginas"] += 1
                stats["anuncios"] += len(lote)
                stats["novos"] += novos
                print(f"  {rotulo}: {len(lote)} anuncios ({novos} novos)")
        except PortalIndisponivel as e:
            # Quebra do portal não derruba o resto: loga e segue a vida.
            stats["erros"] += 1
            print(f"  ! coleta interrompida: {e}")
    return stats
