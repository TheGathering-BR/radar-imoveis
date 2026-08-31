"""Dashboard do Radar Imóveis — servidor local (Flask).

Endpoints:
  GET /            -> frontend (web/static)
  GET /api/mapa    -> GeoJSON dos bairros + métricas atuais por bairro

O payload é cacheado em memória e invalidado quando o arquivo do banco
muda (mtime), então rodadas de coleta aparecem no dashboard num F5.
"""
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from shapely.geometry import mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar import analisador
from radar.config import CIDADE_ATIVA, DB_PATH
from radar.db import get_conn
from radar.pipelines.agregados import _mes_add

app = Flask(__name__, static_folder="static", static_url_path="")

# Tolerância ~40 m: reduz os 4 MB de geometria para algo leve p/ o browser.
SIMPLIFICACAO_GRAUS = 0.0004
MIN_AMOSTRAS_ITBI_MES = 1000  # p/ escolher o mês de referência municipal

CLASSES_VALIDAS = ("todos", "apartamento", "casa", "casa_vila", "cobertura")
# Anúncios entram na mediana por RECÊNCIA, não por mês-calendário: um anúncio
# capturado semanas atrás segue sendo oferta ativa. Com bucket mensal, uma
# coleta parcial no dia 1º esvaziaria o mapa inteiro.
JANELA_ANUNCIOS_DIAS = 90
# O ITBI (uso IPTU) só separa vertical/horizontal: vila e cobertura caem na
# classe-mãe, e o payload avisa para o frontend exibir a ressalva.
CLASSE_ITBI_EQUIVALENTE = {"casa_vila": "casa", "cobertura": "apartamento"}

_cache = {"mtime": None, "payloads": {}}


def _mes_referencia_itbi(conn) -> str:
    """Último mês (data de transação) com amostra municipal razoável —
    os meses mais recentes ainda estão parciais (guias pagas com atraso)."""
    row = conn.execute(
        """SELECT mes FROM (
             SELECT mes, SUM(n_amostras) AS t FROM agregados_itbi
             WHERE classe = 'todos' GROUP BY mes
           ) WHERE t >= ? ORDER BY mes DESC LIMIT 1""",
        (MIN_AMOSTRAS_ITBI_MES,),
    ).fetchone()
    return row[0]


def _montar_payload(classe: str) -> dict:
    conn = get_conn()
    mes_itbi = _mes_referencia_itbi(conn)
    meses3 = [_mes_add(mes_itbi, -d) for d in range(3)]
    classe_itbi = CLASSE_ITBI_EQUIVALENTE.get(classe, classe)

    # Mediana ITBI: janela de 3 meses terminando no mês de referência.
    # 'todos' = residencial (classe atribuída); nunca inclui garagem/loja/etc.
    itbi = defaultdict(list)
    marcas = ",".join("?" * len(meses3))
    filtro_classe = "AND classe IS NOT NULL" if classe_itbi == "todos" \
        else "AND classe = ?"
    params = [CIDADE_ATIVA, *meses3] + ([] if classe_itbi == "todos" else [classe_itbi])
    for bid, pm2 in conn.execute(
        f"""SELECT bairro_id, preco_m2 FROM transacoes
            WHERE cidade = ? AND elegivel_mediana = 1
              AND bairro_id IS NOT NULL
              AND substr(data_transacao, 1, 7) IN ({marcas})
              {filtro_classe}""",
        params,
    ):
        itbi[bid].append(pm2)

    variacoes = {
        bid: {"m3": v3, "m6": v6, "m12": v12, "m24": v24}
        for bid, v3, v6, v12, v24 in conn.execute(
            """SELECT bairro_id, var_3m, var_6m, var_12m, var_24m
               FROM agregados_itbi WHERE mes = ? AND classe = ?""",
            (mes_itbi, classe_itbi),
        )
    }

    # Mediana pedida: anúncios vistos nos últimos JANELA_ANUNCIOS_DIAS,
    # deduplicados por imóvel (o mesmo apartamento em dois portais conta uma
    # vez, ficando a captura mais recente).
    corte = (datetime.now(timezone.utc) - timedelta(days=JANELA_ANUNCIOS_DIAS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    filtro_classe_anun = "" if classe == "todos" else " AND classe = ?"
    params_anun = [CIDADE_ATIVA, corte] + ([] if classe == "todos" else [classe])
    por_bairro = defaultdict(dict)  # bairro -> imóvel -> preço/m²
    for bid, fp, aid, pm2 in conn.execute(
        f"""SELECT bairro_id, fingerprint, id, preco_m2 FROM anuncios
            WHERE cidade = ? AND elegivel_mediana = 1
              AND bairro_id IS NOT NULL AND ultima_captura >= ?
              {filtro_classe_anun}
            ORDER BY ultima_captura ASC""",
        params_anun,
    ):
        por_bairro[bid][fp or f"id{aid}"] = pm2
    anuncios = {
        bid: {"mediana": statistics.median(imoveis.values()), "n": len(imoveis)}
        for bid, imoveis in por_bairro.items()
    }
    captura_anuncios = conn.execute(
        """SELECT MAX(ultima_captura) FROM anuncios
           WHERE cidade = ? AND bairro_id IS NOT NULL""",
        (CIDADE_ATIVA,),
    ).fetchone()[0]

    features = []
    for bid, nome, regiao, geom_json in conn.execute(
        "SELECT id, nome, regiao, geometria FROM bairros WHERE cidade = ? ORDER BY nome",
        (CIDADE_ATIVA,),
    ):
        g = shape(json.loads(geom_json)).simplify(
            SIMPLIFICACAO_GRAUS, preserve_topology=True
        )
        amostras_itbi = itbi.get(bid, [])
        med_itbi = statistics.median(amostras_itbi) if amostras_itbi else None
        anu = anuncios.get(bid, {})
        med_anu, n_anu = anu.get("mediana"), anu.get("n", 0)
        # Preço fechado (ITBI) em relação ao pedido, em %.
        # NEGATIVO = fecha abaixo do que se pede (caso comum).
        gap = None
        if med_itbi and med_anu:
            gap = (med_itbi / med_anu - 1.0) * 100.0
        features.append({
            "type": "Feature",
            "properties": {
                "id": bid,
                "nome": nome.title(),
                "regiao": regiao,
                "itbi_mediana": med_itbi,
                "itbi_n": len(amostras_itbi),
                "anuncios_mediana": med_anu,
                "anuncios_n": n_anu,
                "var": variacoes.get(bid, {}),
                "gap": gap,
            },
            "geometry": mapping(g),
        })
    conn.close()

    return {
        "cidade": CIDADE_ATIVA,
        "classe": classe,
        "classe_itbi": classe_itbi,  # difere quando o ITBI só tem a classe-mãe
        "mes_itbi": mes_itbi,
        "captura_anuncios": captura_anuncios,
        "janela_anuncios_dias": JANELA_ANUNCIOS_DIAS,
        "geojson": {"type": "FeatureCollection", "features": features},
    }


@app.get("/api/mapa")
def api_mapa():
    classe = request.args.get("classe", "todos")
    if classe not in CLASSES_VALIDAS:
        return jsonify({"erro": f"classe inválida: {classe}"}), 400
    mtime = DB_PATH.stat().st_mtime
    if _cache["mtime"] != mtime:
        _cache["payloads"] = {}
        _cache["mtime"] = mtime
    if classe not in _cache["payloads"]:
        _cache["payloads"][classe] = _montar_payload(classe)
    return jsonify(_cache["payloads"][classe])


@app.post("/api/analisar")
def api_analisar():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"erro": "Informe a URL do anúncio."}), 400
    conn = get_conn()
    try:
        resultado = analisador.analisar(conn, url)
        return jsonify(resultado)
    except analisador.ExtracaoFalhou as e:
        return jsonify({"erro": str(e)}), 422
    except Exception as e:
        return jsonify({"erro": f"Falha inesperada ao analisar: {e}"}), 500
    finally:
        conn.close()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8010, debug=False)
