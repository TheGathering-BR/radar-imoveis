"""Camadas geográficas do GeoSampa (WFS) e geocodificação por quadra fiscal.

Estratégia: o nº de cadastro SQL do ITBI embute setor (3 dígitos) e quadra
(3 dígitos). O centroide da quadra fiscal dá lat/lon com precisão de ~50-100 m,
suficiente para atribuir o distrito por point-in-polygon — tudo offline,
sem depender de geocodificador externo.
"""
import json
import sqlite3
from collections import defaultdict

import requests
import shapely
from shapely.geometry import shape

from radar.config import (
    CIDADE_ATIVA,
    HTTP_HEADERS,
    WFS_LAYER_DISTRITOS,
    WFS_LAYER_QUADRAS,
    WFS_PAGE_SIZE,
    WFS_URL,
)


def fetch_wfs_features(layer: str, sort_by: str):
    """Itera as features de uma camada WFS em páginas, já em WGS84."""
    start = 0
    while True:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeName": layer,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "count": WFS_PAGE_SIZE,
            "startIndex": start,
            "sortBy": sort_by,
        }
        resp = requests.get(WFS_URL, params=params, headers=HTTP_HEADERS, timeout=300)
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            return
        yield from features
        if len(features) < WFS_PAGE_SIZE:
            return
        start += WFS_PAGE_SIZE


def load_bairros(conn: sqlite3.Connection) -> int:
    """Carrega os 96 distritos oficiais na tabela bairros."""
    rows = []
    for feat in fetch_wfs_features(WFS_LAYER_DISTRITOS, "cd_identificador_distrito"):
        p = feat["properties"]
        rows.append((
            CIDADE_ATIVA,
            p["cd_distrito_municipal"],
            p["nm_distrito_municipal"],
            p.get("sg_distrito_municipal"),
            p.get("nm_regiao_05"),
            json.dumps(feat["geometry"]),
        ))
    conn.executemany(
        """INSERT INTO bairros (cidade, codigo, nome, sigla, regiao, geometria)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (cidade, codigo) DO UPDATE SET
             nome=excluded.nome, sigla=excluded.sigla,
             regiao=excluded.regiao, geometria=excluded.geometria""",
        rows,
    )
    conn.commit()
    return len(rows)


def _bairro_index(conn: sqlite3.Connection):
    """Geometrias preparadas dos bairros para point-in-polygon rápido."""
    idx = []
    for bid, geom_json in conn.execute(
        "SELECT id, geometria FROM bairros WHERE cidade = ?", (CIDADE_ATIVA,)
    ):
        g = shape(json.loads(geom_json))
        shapely.prepare(g)
        idx.append((bid, g.bounds, g))
    return idx


def load_quadras(conn: sqlite3.Connection, progress_every: int = 10000) -> int:
    """Baixa as quadras fiscais, calcula centroides e atribui o bairro.

    Uma quadra pode ter subquadras (várias features com o mesmo par
    setor/quadra); usamos a média dos centroides.
    """
    acc = defaultdict(lambda: [0.0, 0.0, 0])  # (setor, quadra) -> [sum_lon, sum_lat, n]
    n_feat = 0
    for feat in fetch_wfs_features(WFS_LAYER_QUADRAS, "cd_identificador"):
        p = feat["properties"]
        setor = str(p.get("cd_setor_fiscal") or "").strip().zfill(3)
        quadra = str(p.get("cd_quadra_fiscal") or "").strip().zfill(3)
        if not setor.strip("0") and not quadra.strip("0"):
            continue
        try:
            c = shape(feat["geometry"]).centroid
        except Exception:
            continue
        slot = acc[(setor, quadra)]
        slot[0] += c.x
        slot[1] += c.y
        slot[2] += 1
        n_feat += 1
        if n_feat % progress_every == 0:
            print(f"  ... {n_feat} quadras baixadas")

    bairros = _bairro_index(conn)
    rows = []
    for (setor, quadra), (sx, sy, n) in acc.items():
        lon, lat = sx / n, sy / n
        bairro_id = None
        for bid, (minx, miny, maxx, maxy), g in bairros:
            if minx <= lon <= maxx and miny <= lat <= maxy and shapely.contains_xy(g, lon, lat):
                bairro_id = bid
                break
        rows.append((CIDADE_ATIVA, setor, quadra, lat, lon, bairro_id))

    conn.executemany(
        """INSERT INTO quadras (cidade, setor, quadra, lat, lon, bairro_id)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (cidade, setor, quadra) DO UPDATE SET
             lat=excluded.lat, lon=excluded.lon, bairro_id=excluded.bairro_id""",
        rows,
    )
    conn.commit()
    return len(rows)


def quadra_lookup(conn: sqlite3.Connection) -> dict:
    """Dicionário (setor, quadra) -> (lat, lon, bairro_id) para o pipeline."""
    return {
        (s, q): (lat, lon, bid)
        for s, q, lat, lon, bid in conn.execute(
            "SELECT setor, quadra, lat, lon, bairro_id FROM quadras WHERE cidade = ?",
            (CIDADE_ATIVA,),
        )
    }
