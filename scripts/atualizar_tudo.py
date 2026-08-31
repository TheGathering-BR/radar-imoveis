"""Atualiza tudo em um comando: ITBI -> anúncios -> agregados -> docs -> push.

    python scripts/atualizar_tudo.py               # ciclo completo e publica
    python scripts/atualizar_tudo.py --sem-push    # atualiza e gera docs/, sem publicar
    python scripts/atualizar_tudo.py --sem-itbi    # só anúncios (ITBI muda 1x/mês)
    python scripts/atualizar_tudo.py --paginas 20  # coleta mais funda

Cada etapa é isolada: se um portal cair (layout novo, bloqueio), as demais
seguem e o que já foi coletado é preservado — o banco é incremental. O resumo
final diz o que entrou e o que falhou.

Leva ~30-40 min no padrão, quase tudo esperando os delays educados entre
requisições aos portais.
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # p/ importar build_estatico

from radar.config import ANOS_PADRAO  # noqa: E402
from radar.db import get_conn, init_schema  # noqa: E402
from radar.pipelines import agregados, anuncios, itbi  # noqa: E402

# páginas por portal: no VivaReal/ZAP a busca é fatiada por zona (5 zonas),
# então o valor é por zona; no Imovelweb é o total, e o WAF de lá exige
# volume menor.
PORTAIS_PADRAO = ["vivareal", "zapimoveis", "imovelweb"]
TIPOS = ["apartamento", "casa"]


def _passo(titulo: str) -> None:
    print(f"\n{'=' * 62}\n== {titulo}\n{'=' * 62}", flush=True)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=RAIZ, capture_output=True,
                          text=True, encoding="utf-8", errors="ignore")


def publicar() -> str:
    """Commita e envia docs/ para o GitHub. Devolve uma linha de status."""
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return "pulado — a pasta não é um repositório git"

    pendente = _git("status", "--porcelain", "docs").stdout.strip()
    if not pendente:
        return "nada a publicar — os agregados não mudaram desde o último push"

    _git("add", "docs")
    carimbo = datetime.now().strftime("%d/%m/%Y %H:%M")
    commit = _git("commit", "-m", f"atualiza dados ({carimbo})")
    if commit.returncode != 0:
        return f"falhou no commit — {commit.stderr.strip().splitlines()[-1:]}"

    push = _git("push", "origin", "HEAD")
    if push.returncode != 0:
        erro = (push.stderr or push.stdout).strip().splitlines()
        return ("commit feito, mas o push falhou — "
                f"{erro[-1] if erro else 'erro desconhecido'}. "
                "Rode `git push` manualmente para autenticar.")
    return "publicado — o GitHub Pages republica em cerca de 1 minuto"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Atualiza ITBI + anúncios, regenera docs/ e publica.")
    ap.add_argument("--paginas", type=int, default=12,
                    help="páginas por zona no VivaReal/ZAP (padrão: 12)")
    ap.add_argument("--paginas-imovelweb", type=int, default=8,
                    help="páginas no Imovelweb, que tem WAF sensível (padrão: 8)")
    ap.add_argument("--portais", nargs="+", default=PORTAIS_PADRAO,
                    choices=sorted(anuncios.PORTAIS))
    ap.add_argument("--anos", nargs="+", type=int, default=ANOS_PADRAO)
    ap.add_argument("--sem-itbi", action="store_true",
                    help="pula o ITBI (publicado mensalmente pela Prefeitura)")
    ap.add_argument("--sem-push", action="store_true",
                    help="gera docs/ mas não commita nem envia ao GitHub")
    args = ap.parse_args()

    inicio = time.monotonic()
    resumo: list[str] = []
    conn = get_conn()
    init_schema(conn)

    if args.sem_itbi:
        resumo.append("ITBI: pulado (--sem-itbi)")
    else:
        _passo("1/4  ITBI — transações da Prefeitura")
        try:
            itbi.ingerir(conn, args.anos)
            resumo.append(f"ITBI: anos {args.anos[0]}-{args.anos[-1]} atualizados")
        except Exception as e:
            resumo.append(f"ITBI: FALHOU ({type(e).__name__}: {e}) — "
                          "dados anteriores preservados")
            print(f"  ! {e}", flush=True)

    _passo("2/4  Anúncios — portais")
    for i, portal in enumerate(args.portais, 1):
        paginas = (args.paginas_imovelweb if portal == "imovelweb"
                   else args.paginas)
        print(f"\n--- portal {i}/{len(args.portais)}: {portal} "
              f"({paginas} páginas) ---", flush=True)
        try:
            st = anuncios.coletar(conn, portal=portal, tipos=TIPOS,
                                  paginas=paginas)
            resumo.append(f"{portal}: {st['anuncios']} anúncios "
                          f"({st['novos']} novos, {st['erros']} erros)")
        except Exception as e:
            resumo.append(f"{portal}: FALHOU ({type(e).__name__}: {e})")
            print(f"  ! {e}", flush=True)

    _passo("3/4  Agregados e build estático")
    n_itbi = agregados.recalcular(conn)
    n_anun = agregados.recalcular_anuncios(conn)
    resumo.append(f"agregados: {n_itbi} linhas ITBI, {n_anun} linhas anúncios")

    total, unicos = conn.execute(
        """SELECT COUNT(*), COUNT(DISTINCT COALESCE(fingerprint, 'id' || id))
           FROM anuncios"""
    ).fetchone()
    resumo.append(f"base: {total} anúncios -> {unicos} imóveis únicos após dedup")
    conn.close()

    from build_estatico import main as build  # noqa: E402
    build()

    _passo("4/4  Publicação")
    if args.sem_push:
        resumo.append("push: pulado (--sem-push)")
        print("  docs/ atualizado. Para conferir antes de publicar:\n"
              "    python -m http.server 8011 -d docs", flush=True)
    else:
        status = publicar()
        resumo.append(f"push: {status}")
        print(f"  {status}", flush=True)

    minutos = (time.monotonic() - inicio) / 60
    _passo(f"Resumo — {minutos:.0f} min")
    for linha in resumo:
        print(f"  • {linha}")


if __name__ == "__main__":
    main()
