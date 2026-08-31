"""Baixa/atualiza os dados de ITBI e recalcula os agregados.

Uso:
    python scripts/atualizar_itbi.py                # todos os anos configurados
    python scripts/atualizar_itbi.py --anos 2025 2026
    python scripts/atualizar_itbi.py --force-download
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.config import ANOS_PADRAO
from radar.db import get_conn, init_schema
from radar.pipelines import agregados, itbi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anos", nargs="+", type=int, default=ANOS_PADRAO)
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--recarregar", action="store_true",
                    help="ignora o cache de ingestão e reprocessa as abas do zero "
                         "(usar após mudanças no parser)")
    args = ap.parse_args()

    conn = get_conn()
    init_schema(conn)
    if args.recarregar:
        print("[itbi] --recarregar: limpando ingestões anteriores dos anos escolhidos")
        for ano in args.anos:
            conn.execute("DELETE FROM transacoes WHERE fonte_arquivo = ?",
                         (f"itbi_{ano}.xlsx",))
            conn.execute("DELETE FROM ingestoes WHERE fonte='itbi' AND arquivo = ?",
                         (f"itbi_{ano}.xlsx",))
        conn.commit()
    itbi.ingerir(conn, args.anos, force_download=args.force_download)
    print("[agregados] recalculando medianas e variacoes...")
    n = agregados.recalcular(conn)
    print(f"  -> {n} linhas bairro x mes")
    conn.close()
    print("OK")


if __name__ == "__main__":
    main()
