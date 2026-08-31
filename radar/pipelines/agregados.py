"""Agregados mensais por bairro e classe: mediana do preço/m² e variações.

- Eixo temporal: mês da DATA DE TRANSAÇÃO (evento econômico), não o mês de
  pagamento da guia. Guias pagas com atraso reforçam meses antigos.
- Dimensão `classe`: 'todos' (todo o residencial) + uma linha por classe de
  imóvel. Usos não residenciais do ITBI (garagem, loja, escritório...) têm
  classe NULL e ficam fora de TODAS as medianas — inclusive de 'todos'.
- mediana_preco_m2 / n_amostras: do mês seco.
- var_3m/6m/12m/24m: comparam a mediana de uma janela móvel de 3 meses
  (m-2..m) contra a mesma janela X meses antes. Janela móvel porque bairros
  com poucas transações/mês teriam variações mês-a-mês muito ruidosas.
  Cada ponta da comparação exige MIN_AMOSTRAS_JANELA amostras.
"""
import sqlite3
import statistics
from collections import defaultdict

from radar.config import CIDADE_ATIVA, JANELAS_MESES, MIN_AMOSTRAS_JANELA

CLASSES_ITBI = ("apartamento", "casa")
CLASSES_ANUNCIOS = ("apartamento", "casa", "casa_vila", "cobertura")


def _mes_add(mes: str, delta: int) -> str:
    ano, m = int(mes[:4]), int(mes[5:7])
    total = ano * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _mediana_janela(por_mes: dict, mes_fim: str, tamanho: int = 3):
    valores = []
    for d in range(tamanho):
        valores.extend(por_mes.get(_mes_add(mes_fim, -d), ()))
    if len(valores) < MIN_AMOSTRAS_JANELA:
        return None
    return statistics.median(valores)


def recalcular_anuncios(conn: sqlite3.Connection) -> int:
    """Reconstrói agregados_anuncios (mediana do preço PEDIDO por bairro/mês/classe).

    Um anúncio conta uma vez por mês em que foi capturado, com o preço da
    última captura do mês (anúncio parado por meses entra em cada mês —
    ele segue sendo oferta ativa àquele preço).
    """
    # Chave = fingerprint do IMÓVEL (quando existe): o mesmo apartamento
    # anunciado em dois portais conta uma única vez na mediana.
    ultima_do_mes = {}  # (imovel, mes) -> (capturado_em, preco_m2, bairro, classe)
    for aid, fp, cap, preco, area, bairro_id, classe in conn.execute(
        """SELECT c.anuncio_id, a.fingerprint, c.capturado_em, c.preco,
                  a.area_m2, a.bairro_id, a.classe
           FROM anuncio_capturas c
           JOIN anuncios a ON a.id = c.anuncio_id
           WHERE a.cidade = ? AND a.elegivel_mediana = 1
             AND a.bairro_id IS NOT NULL""",
        (CIDADE_ATIVA,),
    ):
        if not (preco and area):
            continue
        chave = (fp or f"id{aid}", cap[:7])
        atual = ultima_do_mes.get(chave)
        if atual is None or cap > atual[0]:
            ultima_do_mes[chave] = (cap, preco / area, bairro_id, classe)

    # (bairro, classe) -> mes -> [pm2]; cada anúncio entra em 'todos' + na sua classe
    dados = defaultdict(lambda: defaultdict(list))
    for (_imovel, mes), (_, pm2, bairro_id, classe) in ultima_do_mes.items():
        dados[(bairro_id, "todos")][mes].append(pm2)
        if classe in CLASSES_ANUNCIOS:
            dados[(bairro_id, classe)][mes].append(pm2)

    linhas = [
        (bairro_id, mes, classe, statistics.median(v), len(v))
        for (bairro_id, classe), por_mes in dados.items()
        for mes, v in por_mes.items()
    ]
    conn.execute("DELETE FROM agregados_anuncios")
    conn.executemany(
        """INSERT INTO agregados_anuncios
             (bairro_id, mes, classe, mediana_preco_m2, n_amostras)
           VALUES (?, ?, ?, ?, ?)""",
        linhas,
    )
    conn.commit()
    return len(linhas)


def recalcular(conn: sqlite3.Connection) -> int:
    """Reconstrói agregados_itbi a partir das transações residenciais elegíveis."""
    # (bairro, classe) -> mes -> [preco_m2]
    dados = defaultdict(lambda: defaultdict(list))
    for bairro_id, data_tx, preco, classe in conn.execute(
        """SELECT t.bairro_id, t.data_transacao, t.preco_m2, t.classe
           FROM transacoes t
           WHERE t.cidade = ? AND t.elegivel_mediana = 1
             AND t.classe IS NOT NULL
             AND t.bairro_id IS NOT NULL AND t.data_transacao IS NOT NULL
             AND t.data_transacao >= '2000-01-01'""",
        (CIDADE_ATIVA,),
    ):
        mes = data_tx[:7]
        dados[(bairro_id, "todos")][mes].append(preco)
        if classe in CLASSES_ITBI:
            dados[(bairro_id, classe)][mes].append(preco)

    linhas = []
    for (bairro_id, classe), por_mes in dados.items():
        for mes, valores in por_mes.items():
            variacoes = {}
            atual = _mediana_janela(por_mes, mes)
            for janela in JANELAS_MESES:
                var = None
                if atual is not None:
                    anterior = _mediana_janela(por_mes, _mes_add(mes, -janela))
                    if anterior:
                        var = (atual / anterior - 1.0) * 100.0
                variacoes[janela] = var
            linhas.append((
                bairro_id, mes, classe,
                statistics.median(valores), len(valores),
                *(variacoes[j] for j in JANELAS_MESES),
            ))

    colunas_var = ", ".join(f"var_{j}m" for j in JANELAS_MESES)
    marcas = ", ".join("?" * (5 + len(JANELAS_MESES)))
    conn.execute("DELETE FROM agregados_itbi")
    conn.executemany(
        f"""INSERT INTO agregados_itbi
              (bairro_id, mes, classe, mediana_preco_m2, n_amostras, {colunas_var})
            VALUES ({marcas})""",
        linhas,
    )
    conn.commit()
    return len(linhas)
