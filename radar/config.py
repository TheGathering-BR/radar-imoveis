"""Configuração central do Radar Imóveis.

Tudo que é específico de cidade/fonte fica aqui, para facilitar a
expansão para outras cidades depois.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "radar.db"

CIDADE_ATIVA = "sao_paulo"

# O servidor da Prefeitura retorna 502 sem User-Agent de navegador.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

# --- ITBI (Secretaria Municipal da Fazenda de São Paulo) -------------------
# Fonte: https://prefeitura.sp.gov.br/web/fazenda/w/acesso_a_informacao/31501
# Um xlsx por ano, com uma aba por mês. O arquivo do ano vigente é
# reescrito mensalmente (a URL pode mudar a cada atualização — conferir na
# página acima se o download falhar).
ITBI_URLS = {
    2022: "https://www.prefeitura.sp.gov.br/cidade/secretarias/upload/fazenda/arquivos/XLSX/GUIAS_DE_ITBI_PAGAS_12-2022.xlsx",
    2023: "https://www.prefeitura.sp.gov.br/cidade/secretarias/upload/fazenda/arquivos/XLSX/GUIAS-DE-ITBI-PAGAS-2023.xlsx",
    2024: "https://prefeitura.sp.gov.br/cidade/secretarias/upload/fazenda/arquivos/itbi/GUIAS-DE-ITBI-PAGAS-2024.xlsx",
    2025: "https://prefeitura.sp.gov.br/cidade/secretarias/upload/fazenda/arquivos/itbi/GUIAS%20DE%20ITBI%20PAGAS%20%2828012026%29%20XLS.xlsx",
    2026: "https://prefeitura.sp.gov.br/documents/d/fazenda/guias-de-itbi-pagas-4-xlsx",
}
ANOS_PADRAO = sorted(ITBI_URLS)
ANO_VIGENTE = 2026  # sempre re-baixado (atualizado mensalmente pela Prefeitura)

# --- GeoSampa (WFS oficial da Prefeitura) ----------------------------------
WFS_URL = "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/ows"
WFS_LAYER_DISTRITOS = "geoportal:distrito_municipal"
WFS_LAYER_QUADRAS = "geoportal:quadra_fiscal"
WFS_PAGE_SIZE = 5000

# --- Filtros de mercado para a mediana de preço/m² -------------------------
# Só transações de compra e venda plena entram na mediana.
PRECO_M2_MIN = 100.0      # abaixo disso é quase certamente erro de digitação
PRECO_M2_MAX = 150_000.0  # acima disso, idem
MIN_AMOSTRAS_JANELA = 5   # mínimo de amostras na janela móvel p/ calcular variação
