# Radar Imóveis

Dashboard de análise do mercado imobiliário de São Paulo (capital), cruzando
**preço fechado** (transações reais de ITBI da Prefeitura) com **preço pedido**
(anúncios ativos de três portais) — por distrito e por classe de imóvel.

**[→ Ver o mapa online](https://SEU-USUARIO.github.io/radar-imoveis/)**
(versão estática: o mapa funciona; o analisador de anúncio precisa rodar local)

## O que ele responde

- Qual a mediana de R$/m² de cada um dos 96 distritos — fechado e pedido
- Quanto cada bairro valorizou em 3, 6, 12 ou 24 meses
- Qual o "desconto típico" entre o que se pede e o que efetivamente fecha
- Se um anúncio específico está caro ou barato para o bairro e o perfil dele

Tudo separado por classe: apartamento, casa, casa de vila/condomínio, cobertura
— porque misturá-las distorce a mediana (em Pinheiros, casas fecham a
~R$ 20 mil/m² contra ~R$ 8 mil/m² dos apartamentos).

## Módulos

1. **Base geográfica + ITBI** — distritos oficiais do GeoSampa e transações
   reais com recolhimento de ITBI da Secretaria da Fazenda.
2. **Coletor de anúncios ativos** (VivaReal, ZAP, Imovelweb) — mediana do preço
   PEDIDO por bairro
3. **Dashboard com mapa coroplético** (Leaflet + Flask) — `python web/app.py`
4. **Analisador de anúncio** — aba "Analisar anúncio" no dashboard

## Setup

> A pasta `data/` **não é versionada** (o banco tem ~580 MB). Os dois comandos
> abaixo a reconstroem do zero a partir das fontes públicas — leva ~15 min,
> quase tudo download do ITBI.

```bash
pip install -r requirements.txt
python scripts/setup_db.py        # schema + distritos + quadras fiscais (WFS GeoSampa)
python scripts/atualizar_itbi.py  # baixa e ingere ITBI 2022-2026 + agregados
```

Testado em Python 3.10+.

Para atualizar mensalmente, basta rodar `atualizar_itbi.py` de novo: o ano
vigente é re-baixado e só as abas que mudaram são reprocessadas.

```bash
python scripts/coletar_anuncios.py --tipos apartamento casa --paginas 15
```

A coleta de anúncios é incremental (anúncio revisto atualiza preço e
`ultima_captura`; histórico de preço em `anuncio_capturas`) e educada:
delays aleatórios de 2,5–6 s entre páginas, caminhos permitidos pelo
robots.txt, volume moderado por rodada. A busca é fatiada por zona
(sul/oeste/norte/leste/centro) — `--paginas` é por zona, então 12 páginas
= 60 requisições por tipo. Rodadas frequentes acumulam amostra por bairro.

**Portais** (`--portal`): `vivareal` e `zapimoveis` (Grupo OLX — mesma
plataforma, parser compartilhado) e `imovelweb` (grupo QuintoAndar — estoque
independente; WAF agressivo: download via curl, delays de 8–15 s, poucas
páginas por rodada). Como os portais cross-postam estoque, cada anúncio
recebe uma **fingerprint do imóvel** (classe + quartos + área + endereço/
coordenadas) e as medianas, comparáveis e similares contam cada imóvel uma
única vez, mesmo anunciado em vários portais. Avaliados e descartados por
ora: Lopes e QuintoAndar (SPA, exigem navegador), OLX (anti-bot), Chaves na
Mão (viável, mas cards sem coordenadas — geocodificação fraca).

```bash
python web/app.py   # dashboard em http://127.0.0.1:8010
```

O dashboard mostra o coroplético dos 96 distritos com toggle entre preço/m²
pedido (anúncios), fechado (ITBI) e valorização % (janelas de 3/6/12/24
meses), tooltip com as duas medianas + amostras + desconto típico, e ranking
lateral de altas/quedas (só bairros com ≥30 vendas na janela de 3 meses).
O payload da API é cacheado e invalidado quando `radar.db` muda.

A aba **Analisar anúncio** recebe o link de um anúncio de venda e devolve o
veredito "X% acima/abaixo da mediana" comparando com anúncios de perfil
similar (±20% de área, mesmo nº de quartos; expande para bairros vizinhos se
a amostra do bairro for curta), além dos similares com links. A expansão só
aceita vizinhos de **preço comparável** (mediana ITBI residencial dentro de
0,75–1,33× a do bairro) — vizinhança geométrica não basta: distritos que se
tocam no eixo do Rio Pinheiros (ex.: Santo Amaro × Vila Andrade) são mercados
diferentes e não se comparam. A extração dos
dados da página tem três camadas: payload RSC do VivaReal → ld+json genérico
(schema.org, funciona em vários portais) → API da Anthropic (resiliente a
mudança de layout; requer `ANTHROPIC_API_KEY` ou `ant auth login`). O ITBI
aparece apenas como referência informativa — preço pedido nunca é comparado
com preço fechado.

## Fontes de dados

| Dado | Fonte | Observação |
|---|---|---|
| Transações ITBI | [Fazenda/PMSP](https://prefeitura.sp.gov.br/web/fazenda/w/acesso_a_informacao/31501) | 1 xlsx/ano, 1 aba/mês; exige User-Agent de navegador |
| Distritos (96) | WFS GeoSampa `geoportal:distrito_municipal` | geometrias em WGS84 |
| Quadras fiscais | WFS GeoSampa `geoportal:quadra_fiscal` | base da geocodificação |
| Anúncios ativos | [VivaReal](https://www.vivareal.com.br), [ZAP](https://www.zapimoveis.com.br), [Imovelweb](https://www.imovelweb.com.br) | páginas server-rendered com JSON embutido |

## Decisões de método

- **Geocodificação por quadra fiscal, offline.** O nº de cadastro SQL do ITBI
  embute setor (3 dígitos) e quadra (3 dígitos). O centroide da quadra fiscal
  (precisão ~50–100 m) dá lat/lon e o distrito por point-in-polygon — sem
  geocodificador externo, sem rate limit. Cobertura: >99,9% das quadras.
- **A coluna "Bairro" do ITBI é texto livre do IPTU** ("JD MORUMBI" etc.) e não
  bate com os distritos oficiais — fica guardada em `bairro_iptu`, mas o campo
  analítico é `bairro_id` (distrito).
- **Filtro de mercado para a mediana**: só natureza "1. Compra e venda",
  proporção transmitida = 100%, área construída > 0 e preço/m² dentro de
  limites de sanidade. Leilões, integralização de capital, permutas etc. ficam
  no banco (`elegivel_mediana = 0`), fora da mediana.
- **Mediana, não média**, por bairro/mês, no eixo do mês da **data de
  transação** (não do pagamento da guia).
- **Variações 3/6/12/24 meses** comparam janelas móveis de 3 meses (mínimo de
  5 amostras em cada ponta) para reduzir ruído em bairros com poucas vendas.
- **Ingestão idempotente**: hash por aba em `ingestoes`; aba inalterada é
  pulada, aba alterada é substituída. Após mudança no parser, rodar com
  `--recarregar`.
- **Anúncios sem Playwright**: o VivaReal embute os 30 anúncios de cada página
  de busca em JSON server-rendered (payload RSC do Next.js), com coordenadas —
  requests puro basta e é mais estável que dirigir um navegador. O adaptador
  em `radar/portais/vivareal.py` é o único ponto a trocar se o portal mudar;
  uma quebra lá não afeta o resto do sistema (`PortalIndisponivel` é capturada).
- **Anúncio geocodificado por coordenadas** (point-in-polygon no distrito), com
  fallback por nome quando o "bairro" do anúncio coincide com um distrito.
- **Comparação por classe de imóvel**: dropdown no dashboard com Todos os
  residenciais / Apartamentos / Casas / Casas de vila-condomínio / Coberturas.
  Anúncios são classificados pelo tipo do portal; transações ITBI pelo uso
  IPTU, que só distingue vertical (apartamento) × horizontal (casa) — para
  vila e cobertura o mapa ITBI usa a classe-mãe com aviso na legenda. Usos
  não residenciais do ITBI (garagem, loja, escritório — ~89 mil transações)
  ficam fora de TODAS as medianas, inclusive de "Todos". O analisador compara
  automaticamente com a mesma classe do anúncio analisado (com fallback
  declarado quando a amostra da classe é insuficiente).
- **Nível do preço/m² ITBI é estruturalmente menor que o de anúncios**: a área
  construída vem do cadastro IPTU (inclui rateio de áreas comuns) e o valor é
  o declarado. A série é consistente no tempo e entre bairros, mas o "desconto
  típico" (módulo 2) embute essa diferença estrutural, além da negociação —
  interpretar como métrica relativa entre bairros, não como desconto absoluto.

## Publicar o mapa (GitHub Pages)

O GitHub Pages serve só arquivos estáticos — não roda Flask. O build congela
o que a API serviria em um JSON por classe e liga a flag `RADAR_ESTATICO` no
frontend (que passa a ler os arquivos e desabilita a aba de análise):

```bash
python scripts/build_estatico.py   # gera docs/ (~0,8 MB)
git add docs && git commit -m "atualiza mapa publicado" && git push
```

Em *Settings → Pages*, apontar para **branch `main`, pasta `/docs`**.
Só agregados por bairro são publicados — a base de anúncios individuais e o
banco nunca saem da máquina.

Para conferir o build antes de publicar:
`python -m http.server 8011 -d docs`

## Estrutura

```
radar/                  pacote Python (config, db, geo, pipelines, portais)
scripts/                CLIs: setup_db, atualizar_itbi, coletar_anuncios, build_estatico
web/                    dashboard Flask + frontend (módulos 3 e 4)
docs/                   build estático publicado no GitHub Pages
data/                   banco e xlsx baixados (não versionado)
```

## Aviso

Projeto pessoal de análise de dados. Os dados de ITBI e GeoSampa são públicos;
os anúncios são coletados em volume moderado, com delays e respeitando o
robots.txt, e o repositório publica apenas **estatísticas agregadas por bairro**
— nunca a base de anúncios. As medianas são estimativas a partir de amostras,
não avaliação formal de imóvel.
