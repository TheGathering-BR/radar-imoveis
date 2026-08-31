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
from radar.config import CIDADE_ATIVA, DB_PATH, JANELAS_MESES
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
    classe_itbi = CLASSE_ITBI_EQUIVALENTE.get(classe, classe)

    # Carrega de uma vez o histórico da MAIOR janela e depois vai acumulando:
    # como as janelas são aninhadas (3 ⊂ 6 ⊂ 12...), uma passada basta para
    # calcular todas. O payload leva todas as janelas para que trocar de
    # período no dashboard não exija outra requisição.
    janela_max = max(JANELAS_MESES)
    meses = [_mes_add(mes_itbi, -d) for d in range(janela_max)]
    marcas = ",".join("?" * len(meses))
    filtro_classe = "AND classe IS NOT NULL" if classe_itbi == "todos" \
        else "AND classe = ?"
    params = [CIDADE_ATIVA, *meses] + ([] if classe_itbi == "todos" else [classe_itbi])

    # bairro -> mês -> lista de (preço/m², ágio sobre o venal ou None)
    por_mes = defaultdict(lambda: defaultdict(list))
    for bid, mes, pm2, valor, venal in conn.execute(
        f"""SELECT bairro_id, substr(data_transacao, 1, 7), preco_m2,
                   valor_transacao, valor_venal_ref
            FROM transacoes
            WHERE cidade = ? AND elegivel_mediana = 1
              AND bairro_id IS NOT NULL
              AND substr(data_transacao, 1, 7) IN ({marcas})
              {filtro_classe}""",
        params,
    ):
        ag = (valor / venal - 1.0) * 100.0 if (venal and venal > 0 and valor) else None
        por_mes[bid][mes].append((pm2, ag))

    # bairro -> {"m3": {...}, "m6": {...}, ...}
    niveis = {}
    for bid, meses_bairro in por_mes.items():
        acc_pm2, acc_agio, idx, por_janela = [], [], 0, {}
        for w in JANELAS_MESES:
            while idx < w:
                for pm2, ag in meses_bairro.get(meses[idx], ()):
                    acc_pm2.append(pm2)
                    if ag is not None:
                        acc_agio.append(ag)
                idx += 1
            por_janela[f"m{w}"] = {
                "mediana": statistics.median(acc_pm2) if acc_pm2 else None,
                "n": len(acc_pm2),
                "agio": statistics.median(acc_agio) if acc_agio else None,
                "agio_n": len(acc_agio),
            }
        niveis[bid] = por_janela

    colunas_var = ", ".join(f"var_{j}m" for j in JANELAS_MESES)
    variacoes = {
        linha[0]: {f"m{j}": v for j, v in zip(JANELAS_MESES, linha[1:])}
        for linha in conn.execute(
            f"""SELECT bairro_id, {colunas_var}
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
        anu = anuncios.get(bid, {})
        med_anu, n_anu = anu.get("mediana"), anu.get("n", 0)
        features.append({
            "type": "Feature",
            "properties": {
                "id": bid,
                "nome": nome.title(),
                "regiao": regiao,
                # por janela: mediana ITBI, amostra e ágio sobre o venal.
                # O gap pedido×fechado é derivado no cliente, para acompanhar
                # a janela escolhida sem inchar o payload.
                "itbi": niveis.get(bid, {}),
                "anuncios_mediana": med_anu,
                "anuncios_n": n_anu,
                "var": variacoes.get(bid, {}),
            },
            "geometry": mapping(g),
        })
    conn.close()

    return {
        "cidade": CIDADE_ATIVA,
        "classe": classe,
        "classe_itbi": classe_itbi,  # difere quando o ITBI só tem a classe-mãe
        "mes_itbi": mes_itbi,
        "janelas": list(JANELAS_MESES),
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
