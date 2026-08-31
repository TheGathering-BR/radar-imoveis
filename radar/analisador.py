"""Módulo 4: analisador de anúncio.

Fluxo: URL colada -> baixa a página -> extrai preço/área/quartos/bairro ->
compara com a mediana de ANÚNCIOS do mesmo bairro e perfil similar ->
veredito "X% acima/abaixo da mediana" + similares no banco.

Extração em três camadas, da mais estável para a mais flexível:
  1. Payload RSC do VivaReal (mesmo formato da busca do módulo 2);
  2. ld+json genérico (schema.org — funciona em vários portais);
  3. API da Anthropic parseando o texto da página (resiliente a mudança de
     layout; exige credencial — ANTHROPIC_API_KEY ou `ant auth login`).

Nunca compara preço pedido contra ITBI: a mediana ITBI aparece só como
contexto informativo no resultado.
"""
import ipaddress
import json
import re
import socket
import sqlite3
import statistics
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import shapely
from shapely.geometry import shape

from radar.config import CIDADE_ATIVA, HTTP_HEADERS, PRECO_M2_MAX, PRECO_M2_MIN
from radar.pipelines.anuncios import (_GeocodificadorBairros, calcular_fingerprint,
                                      classificar_tipo)
from radar.portais import vivareal

ROTULOS_CLASSE = {
    "apartamento": "apartamentos",
    "casa": "casas",
    "casa_vila": "casas de vila/condomínio",
    "cobertura": "coberturas",
}

MIN_COMPARAVEIS = 5       # abaixo disso, expande para bairros vizinhos
JANELA_CAPTURA_DIAS = 90  # só compara com anúncios vistos recentemente
MAX_HTML = 3_000_000

# Vizinho geométrico só entra na expansão se o nível de preço dele for
# comparável: mediana ITBI residencial dentro de [0.75, 1.33] da do bairro.
# Resolve fronteiras "de papel" — p.ex. distritos que se tocam no eixo do
# Rio Pinheiros mas têm mercados completamente diferentes.
VIZINHO_RATIO_MIN = 0.75
VIZINHO_RATIO_MAX = 1.33
MIN_AMOSTRAS_NIVEL = 30   # amostra mínima p/ confiar no nível ITBI do bairro

_geocodificador = None
_vizinhos = None


class ExtracaoFalhou(Exception):
    pass


# ---------------------------------------------------------------- download

def baixar_pagina(url: str) -> str:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ExtracaoFalhou("URL inválida — cole o link completo do anúncio (https://...).")
    try:
        addr = socket.getaddrinfo(p.hostname, None)[0][4][0]
        if ipaddress.ip_address(addr).is_private or ipaddress.ip_address(addr).is_loopback:
            raise ExtracaoFalhou("URL aponta para endereço privado — não suportado.")
    except socket.gaierror:
        raise ExtracaoFalhou(f"Não consegui resolver o host {p.hostname}.")
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=60, allow_redirects=True)
    if resp.status_code != 200:
        raise ExtracaoFalhou(f"O portal respondeu HTTP {resp.status_code} para esse link.")
    return resp.text[:MAX_HTML]


# ---------------------------------------------------------------- extração

def _extrair_vivareal(html: str):
    """Página de detalhe do VivaReal: objeto '\"listing\":{...}' no payload RSC,
    no mesmo formato dos cards de busca do módulo 2."""
    blob = vivareal._decodificar_rsc(html)
    i = blob.find('"listing":{')
    if i < 0:
        return None
    obj, _ = json.JSONDecoder().raw_decode(blob[i + len('"listing":'):])
    reg = vivareal._normalizar(obj)
    if reg:
        reg["metodo_extracao"] = "vivareal_rsc"
    return reg


def _num_ldjson(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return _num_ldjson(v.get("value"))
    if isinstance(v, str):
        s = re.sub(r"[^\d,.]", "", v)
        try:
            return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)
        except ValueError:
            return None
    return None


def _extrair_ldjson(html: str):
    """Fallback genérico: schema.org (Product/Apartment/RealEstateListing)."""
    melhor = None
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for it in (d if isinstance(d, list) else [d]):
            if not isinstance(it, dict):
                continue
            offers = it.get("offers") or {}
            preco = _num_ldjson(offers.get("price") or it.get("price"))
            if not preco:
                continue
            addr = it.get("address") or {}
            geo = it.get("geo") or {}
            reg = {
                "portal": urllib.parse.urlparse(offers.get("url") or "").hostname or "ldjson",
                "portal_id": str(it.get("sku") or it.get("@id") or ""),
                "url": offers.get("url"),
                "tipo": None,
                "preco": preco,
                "area_m2": _num_ldjson(it.get("floorSize")),
                "quartos": it.get("numberOfBedrooms") or it.get("numberOfRooms"),
                "banheiros": it.get("numberOfBathroomsTotal"),
                "vagas": None,
                "endereco": addr.get("streetAddress") if isinstance(addr, dict) else None,
                "bairro_texto": addr.get("addressLocality") if isinstance(addr, dict) else None,
                "lat": _num_ldjson(geo.get("latitude")),
                "lon": _num_ldjson(geo.get("longitude")),
                "metodo_extracao": "ldjson",
            }
            # prefere o candidato mais completo
            score = sum(reg[k] is not None for k in ("area_m2", "quartos", "lat"))
            if melhor is None or score > melhor[0]:
                melhor = (score, reg)
    return melhor[1] if melhor else None


def _html_para_texto(html: str, limite: int = 60_000) -> str:
    html = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html,
                  flags=re.S | re.I)
    texto = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", texto)[:limite]


def _extrair_com_llm(url: str, html: str):
    """Último recurso: a API da Anthropic parseia o texto da página.

    Resiliente a mudanças de layout dos portais, mas exige credencial
    (ANTHROPIC_API_KEY ou perfil do `ant auth login`).
    """
    import anthropic
    from pydantic import BaseModel

    class AnuncioExtraido(BaseModel):
        preco: float | None
        area_m2: float | None
        quartos: int | None
        banheiros: int | None
        vagas: int | None
        tipo: str | None          # apartamento, casa, sobrado...
        endereco: str | None
        bairro: str | None
        cidade: str | None

    client = anthropic.Anthropic()
    try:
        resposta = client.messages.parse(
            model="claude-opus-4-8",
            max_tokens=2000,
            system=(
                "Você extrai dados estruturados de anúncios imobiliários "
                "brasileiros. Extraia apenas o que estiver explícito no texto; "
                "use null quando o campo não aparecer. Preço em reais "
                "(número, sem separadores), área útil em m²."
            ),
            messages=[{
                "role": "user",
                "content": f"Anúncio em {url}:\n\n{_html_para_texto(html)}",
            }],
            output_format=AnuncioExtraido,
        )
    except anthropic.AuthenticationError:
        raise ExtracaoFalhou(
            "Não consegui extrair os dados desta página com os parsers "
            "estruturados, e o fallback via API da Anthropic está sem "
            "credencial. Configure ANTHROPIC_API_KEY (ou `ant auth login`)."
        )
    d = resposta.parsed_output
    if not d or not d.preco:
        raise ExtracaoFalhou("A página não parece conter um anúncio com preço.")
    return {
        "portal": urllib.parse.urlparse(url).hostname or "desconhecido",
        "portal_id": "", "url": url,
        "tipo": d.tipo.lower() if d.tipo else None,
        "preco": d.preco, "area_m2": d.area_m2,
        "quartos": d.quartos, "banheiros": d.banheiros, "vagas": d.vagas,
        "endereco": d.endereco, "bairro_texto": d.bairro,
        "lat": None, "lon": None,
        "metodo_extracao": "llm",
    }


def extrair(url: str, html: str) -> dict:
    reg = None
    if "vivareal.com" in url:
        reg = _extrair_vivareal(html)
    if reg is None:
        reg = _extrair_ldjson(html)
    if reg is None or not reg.get("preco"):
        reg = _extrair_com_llm(url, html)
    return reg


# ---------------------------------------------------------------- análise

def _carregar_vizinhos(conn: sqlite3.Connection) -> dict:
    """id do bairro -> ids dos bairros que fazem fronteira (cacheado)."""
    global _vizinhos
    if _vizinhos is not None:
        return _vizinhos
    geoms = []
    for bid, gj in conn.execute(
        "SELECT id, geometria FROM bairros WHERE cidade = ?", (CIDADE_ATIVA,)
    ):
        g = shape(json.loads(gj))
        shapely.prepare(g)
        geoms.append((bid, g))
    _vizinhos = {bid: [] for bid, _ in geoms}
    for i, (ba, ga) in enumerate(geoms):
        for bb, gb in geoms[i + 1:]:
            if ga.intersects(gb):
                _vizinhos[ba].append(bb)
                _vizinhos[bb].append(ba)
    return _vizinhos


def _nivel_itbi(conn, bairro_id):
    """Nível de preço do bairro: mediana ITBI residencial mais recente
    com amostra decente (mercado fechado — base robusta p/ comparar bairros)."""
    row = conn.execute(
        """SELECT mediana_preco_m2 FROM agregados_itbi
           WHERE bairro_id = ? AND classe = 'todos' AND n_amostras >= ?
           ORDER BY mes DESC LIMIT 1""",
        (bairro_id, MIN_AMOSTRAS_NIVEL),
    ).fetchone()
    return row[0] if row else None


def _vizinhos_comparaveis(conn, bairro_id):
    """Vizinhos geométricos filtrados por nível de preço comparável."""
    nivel = _nivel_itbi(conn, bairro_id)
    aprovados, recusados = [], []
    for viz in _carregar_vizinhos(conn).get(bairro_id, []):
        nivel_viz = _nivel_itbi(conn, viz)
        if nivel and nivel_viz:
            ratio = nivel_viz / nivel
            (aprovados if VIZINHO_RATIO_MIN <= ratio <= VIZINHO_RATIO_MAX
             else recusados).append(viz)
        else:
            aprovados.append(viz)  # sem nível confiável: não dá p/ recusar
    return aprovados, recusados


def _comparaveis(conn, bairro_ids, alvo, excluir_url, classe=None):
    """Anúncios recentes de perfil similar: mesma classe de imóvel,
    ±20% de área, mesmo nº de quartos."""
    corte = (datetime.now(timezone.utc) - timedelta(days=JANELA_CAPTURA_DIAS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    sql = """SELECT a.url, a.preco, a.area_m2, a.quartos, a.vagas, a.tipo,
                    a.preco_m2, b.nome, a.fingerprint, a.ultima_captura
             FROM anuncios a JOIN bairros b ON b.id = a.bairro_id
             WHERE a.cidade = ? AND a.elegivel_mediana = 1
               AND a.ultima_captura >= ?
               AND a.bairro_id IN (%s)""" % ",".join("?" * len(bairro_ids))
    params = [CIDADE_ATIVA, corte, *bairro_ids]
    if classe:
        sql += " AND a.classe = ?"
        params.append(classe)
    if alvo.get("area_m2"):
        sql += " AND a.area_m2 BETWEEN ? AND ?"
        params += [alvo["area_m2"] * 0.8, alvo["area_m2"] * 1.2]
    if alvo.get("quartos") is not None:
        sql += " AND a.quartos = ?"
        params.append(alvo["quartos"])
    if excluir_url:
        sql += " AND a.url != ?"
        params.append(excluir_url)
    cols = ("url", "preco", "area_m2", "quartos", "vagas", "tipo", "preco_m2",
            "bairro", "fingerprint", "ultima_captura")
    linhas = [dict(zip(cols, r)) for r in conn.execute(sql, params)]
    # Dedup entre portais: mesmo imóvel (fingerprint) conta uma vez,
    # ficando a captura mais recente. O próprio imóvel analisado também
    # sai, mesmo quando está no banco via outro portal.
    fp_alvo = calcular_fingerprint(alvo)
    por_imovel = {}
    for i, c in enumerate(linhas):
        if fp_alvo and c["fingerprint"] == fp_alvo:
            continue
        chave = c["fingerprint"] or f"linha{i}"
        if chave not in por_imovel or c["ultima_captura"] > por_imovel[chave]["ultima_captura"]:
            por_imovel[chave] = c
    return list(por_imovel.values())


def analisar(conn: sqlite3.Connection, url: str) -> dict:
    global _geocodificador
    html = baixar_pagina(url)
    alvo = extrair(url, html)

    preco, area = alvo.get("preco"), alvo.get("area_m2")
    if not preco or not area or area <= 0:
        raise ExtracaoFalhou(
            "Consegui baixar a página, mas não extraí preço e área — sem eles "
            f"não há veredito. Extraído: {json.dumps(alvo, ensure_ascii=False)}"
        )
    preco_m2 = preco / area
    if not (PRECO_M2_MIN <= preco_m2 <= PRECO_M2_MAX):
        raise ExtracaoFalhou(
            f"Preço/m² extraído (R$ {preco_m2:,.0f}) fora da faixa plausível — "
            "confira se o anúncio é de venda (não aluguel)."
        )

    if _geocodificador is None:
        _geocodificador = _GeocodificadorBairros(conn)
    bairro_id = _geocodificador.bairro_de(alvo.get("lat"), alvo.get("lon"),
                                          alvo.get("bairro_texto"))
    if bairro_id is None:
        raise ExtracaoFalhou(
            "Não consegui atribuir o anúncio a um distrito de São Paulo "
            f"(bairro informado: {alvo.get('bairro_texto')!r})."
        )
    nome_bairro = conn.execute(
        "SELECT nome FROM bairros WHERE id = ?", (bairro_id,)
    ).fetchone()[0].title()

    # Comparáveis: mesma CLASSE de imóvel (casa não compara com apartamento),
    # mesmo bairro; expande para vizinhos e, em último caso, ignora a classe.
    classe = classificar_tipo(alvo.get("tipo"))
    rotulo_classe = ROTULOS_CLASSE.get(classe, "imóveis")
    excluir = alvo.get("url") or url
    vizinhos, _ = _vizinhos_comparaveis(conn, bairro_id)

    escopo = f"{rotulo_classe} do mesmo bairro"
    comps = _comparaveis(conn, [bairro_id], alvo, excluir, classe)
    if len(comps) < MIN_COMPARAVEIS:
        escopo = f"{rotulo_classe}, bairro + vizinhos de preço comparável"
        comps = _comparaveis(conn, [bairro_id, *vizinhos], alvo, excluir, classe)
    if len(comps) < MIN_COMPARAVEIS and classe:
        escopo = ("bairro + vizinhos de preço comparável, todas as classes — "
                  "amostra da classe insuficiente")
        comps = _comparaveis(conn, [bairro_id, *vizinhos], alvo, excluir)

    veredito = None
    if len(comps) >= MIN_COMPARAVEIS:
        mediana = statistics.median(c["preco_m2"] for c in comps)
        veredito = {
            "diferenca_pct": (preco_m2 / mediana - 1.0) * 100.0,
            "mediana_similares_m2": mediana,
            "n_similares": len(comps),
            "escopo": escopo,
        }

    # Contexto adicional (informativo; ITBI nunca entra no veredito).
    # ITBI da classe equivalente quando existe (vila->casa, cobertura->apto).
    classe_itbi = {"casa_vila": "casa", "cobertura": "apartamento"}.get(classe, classe) \
        or "todos"
    ctx = conn.execute(
        """SELECT mediana_preco_m2, n_amostras FROM agregados_anuncios
           WHERE bairro_id = ? AND classe = 'todos'
           ORDER BY mes DESC LIMIT 1""", (bairro_id,)
    ).fetchone()
    itbi = conn.execute(
        """SELECT mediana_preco_m2, n_amostras, mes, classe FROM agregados_itbi
           WHERE bairro_id = ? AND classe IN (?, 'todos') AND n_amostras >= 10
           ORDER BY (classe = ?) DESC, mes DESC LIMIT 1""",
        (bairro_id, classe_itbi, classe_itbi)
    ).fetchone()

    comps.sort(key=lambda c: abs(c["preco_m2"] - preco_m2))
    return {
        "anuncio": {**alvo, "preco_m2": preco_m2, "bairro": nome_bairro,
                    "classe": classe, "classe_rotulo": rotulo_classe},
        "veredito": veredito,
        "contexto": {
            "mediana_bairro_anuncios_m2": ctx[0] if ctx else None,
            "n_bairro_anuncios": ctx[1] if ctx else 0,
            "mediana_bairro_itbi_m2": itbi[0] if itbi else None,
            "mes_itbi": itbi[2] if itbi else None,
            "classe_itbi": itbi[3] if itbi else None,
        },
        "similares": comps[:10],
    }
