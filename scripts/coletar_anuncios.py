"""Coleta incremental de anúncios ativos e recálculo da mediana pedida.

Uso:
    python scripts/coletar_anuncios.py                          # 10 págs de apartamentos
    python scripts/coletar_anuncios.py --tipos apartamento casa --paginas 20
    python scripts/coletar_anuncios.py --portal zapimoveis --tipos apartamento casa
    python scripts/coletar_anuncios.py --portal imovelweb --tipos apartamento casa
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.db import get_conn, init_schema
from radar.pipelines import agregados, anuncios


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", default="vivareal", choices=sorted(anuncios.PORTAIS))
    ap.add_argument("--tipos", nargs="+", default=["apartamento"],
                    choices=["apartamento", "casa"])
    ap.add_argument("--paginas", type=int, default=10,
                    help="páginas por tipo (30 anúncios/página)")
    ap.add_argument("--delay-min", type=float, default=2.5)
    ap.add_argument("--delay-max", type=float, default=6.0)
    args = ap.parse_args()

    conn = get_conn()
    init_schema(conn)
    stats = anuncios.coletar(conn, portal=args.portal, tipos=args.tipos,
                             paginas=args.paginas,
                             delay_min=args.delay_min, delay_max=args.delay_max)
    print(f"[anuncios] {stats['anuncios']} anuncios em {stats['paginas']} paginas "
          f"({stats['novos']} novos, {stats['erros']} erros)")
    print("[agregados] recalculando mediana pedida por bairro...")
    n = agregados.recalcular_anuncios(conn)
    print(f"  -> {n} linhas bairro x mes")
    conn.close()
    print("OK")


if __name__ == "__main__":
    main()
