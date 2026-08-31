"""Gera a versão estática do dashboard em docs/ (para GitHub Pages).

O GitHub Pages serve apenas arquivos estáticos — não roda Flask. Este script
congela o que o backend serviria em `/api/mapa` como um JSON por classe de
imóvel, e copia o frontend com a flag `RADAR_ESTATICO` ligada (o app.js então
lê `dados/mapa-<classe>.json` em vez de chamar a API, e desabilita a aba de
análise de anúncio, que depende do backend).

Só publica AGREGADOS (medianas por bairro/classe) — nunca a base de anúncios
individuais nem o banco.

Uso:
    python scripts/build_estatico.py
Depois: commit da pasta docs/ e GitHub Pages apontando para main /docs.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "web"))

from app import CLASSES_VALIDAS, _montar_payload  # noqa: E402

ORIGEM = RAIZ / "web" / "static"
DESTINO = RAIZ / "docs"

MARCADOR = '<script src="app.js"></script>'


def main() -> None:
    if DESTINO.exists():
        shutil.rmtree(DESTINO)
    DESTINO.mkdir(parents=True)

    # 1. Frontend, com a flag de modo estático injetada no index.html
    for arquivo in ORIGEM.iterdir():
        if arquivo.is_file():
            shutil.copy2(arquivo, DESTINO / arquivo.name)

    index = DESTINO / "index.html"
    html = index.read_text(encoding="utf-8")
    if MARCADOR not in html:
        raise SystemExit(
            f"marcador {MARCADOR!r} nao encontrado em index.html — "
            "o build precisa ser ajustado"
        )
    # Versão dos assets: sem isso, quem já visitou o site continua vendo o
    # app.js/style.css antigos em cache depois de um novo deploy.
    versao = hashlib.sha1(
        (DESTINO / "app.js").read_bytes() + (DESTINO / "style.css").read_bytes()
    ).hexdigest()[:8]
    html = html.replace(
        MARCADOR,
        f'<script>window.RADAR_ESTATICO = true;</script>\n'
        f'<script src="app.js?v={versao}"></script>',
    ).replace('href="style.css"', f'href="style.css?v={versao}"')
    index.write_text(html, encoding="utf-8")

    # 2. Um JSON por classe, com o mesmo formato que a API devolve
    dados = DESTINO / "dados"
    dados.mkdir()
    total = 0
    for classe in CLASSES_VALIDAS:
        payload = _montar_payload(classe)
        destino = dados / f"mapa-{classe}.json"
        destino.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
        tam = destino.stat().st_size
        total += tam
        print(f"  {classe}: {tam / 1024:.0f} KB")

    # 3. Impede o Jekyll de processar a pasta no GitHub Pages
    (DESTINO / ".nojekyll").write_text("", encoding="utf-8")

    print(f"[build] docs/ pronto — {total / 1024 / 1024:.1f} MB de dados")


if __name__ == "__main__":
    main()
