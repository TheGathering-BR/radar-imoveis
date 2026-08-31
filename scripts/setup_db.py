"""Cria o schema e carrega as camadas geográficas (bairros + quadras).

Rodar uma vez antes da primeira ingestão (e novamente se quiser atualizar
as geometrias). A carga das quadras fiscais baixa ~50 mil polígonos do WFS
do GeoSampa e leva alguns minutos.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.db import get_conn, init_schema
from radar.geo import load_bairros, load_quadras


def main():
    conn = get_conn()
    init_schema(conn)
    print("[geo] carregando distritos (bairros)...")
    n = load_bairros(conn)
    print(f"  -> {n} bairros")
    print("[geo] carregando quadras fiscais (pode levar alguns minutos)...")
    n = load_quadras(conn)
    print(f"  -> {n} quadras com centroide e bairro atribuido")
    conn.close()
    print("OK")


if __name__ == "__main__":
    main()
