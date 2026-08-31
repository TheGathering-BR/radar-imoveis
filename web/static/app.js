/* Radar Imóveis — mapa coroplético dos distritos de SP. */
"use strict";

// Rampas da paleta de referência (7 classes).
const RAMPA_AZUL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#1c5cab", "#0d366b"];
const RAMPA_AQUA = ["#d2f1e3", "#a6e3c8", "#74d1aa", "#3fbd8a", "#1baf7a", "#12855c", "#0a5e40"];
// Divergente vermelho (queda) ↔ cinza neutro ↔ azul (alta).
const RAMPA_DIVERGENTE = ["#b13332", "#e34948", "#f2a3a2", "#f0efec", "#9ec5f4", "#3987e5", "#184f95"];
// Gap pedido×ITBI, do mais negativo ao mais positivo: azul escuro = fecha bem
// abaixo do pedido, cinza = fecha perto do pedido, vermelho = fecha acima.
const RAMPA_GAP = ["#184f95", "#3987e5", "#6da7ec", "#9ec5f4", "#f0efec", "#e34948", "#b13332"];
// Sequencial violeta (hue livre — azul e verde-água já são pedido e ITBI):
// ágio do valor declarado sobre o venal de referência.
const RAMPA_VIOLETA = ["#e6e2f8", "#cbc3ef", "#b0a4e6", "#9085e9", "#6f61c9", "#524897", "#372f68"];
const COR_SEM_DADOS = getComputedStyle(document.documentElement)
  .getPropertyValue("--hairline").trim() || "#e1e0d9";

const MIN_AMOSTRAS_RANKING = 30; // amostra ITBI (3 meses) p/ entrar no ranking
const MIN_ANUNCIOS_RANKING = 5;  // anúncios mínimos p/ ranquear preço pedido
const MIN_ANUNCIOS_GAP = 3;      // anúncios mínimos p/ colorir o gap pedido×ITBI

// Acesso ao bloco da janela escolhida: {mediana, n, agio, agio_n}
const nivel = (p, janela) => (p.itbi && p.itbi[janela]) || {};

// Gap pedido×fechado derivado no cliente, para seguir a janela selecionada.
function calcularGap(p, janela) {
  const med = nivel(p, janela).mediana;
  if (!med || !p.anuncios_mediana) return null;
  return (med / p.anuncios_mediana - 1.0) * 100.0;
}

const METRICAS = {
  anuncios: { rotulo: "Pedido — mediana R$/m²", rampa: RAMPA_AZUL,
              semJanela: true,
              valor: p => p.anuncios_mediana },
  itbi:     { rotulo: "Fechado (ITBI) — mediana R$/m²", rampa: RAMPA_AQUA,
              valor: (p, janela) => nivel(p, janela).mediana },
  var:      { rotulo: "Valorização (ITBI)", rampa: RAMPA_DIVERGENTE,
              valor: (p, janela) => p.var ? p.var[janela] : null },
  // Quanto o preço que FECHA (ITBI) fica abaixo do que se PEDE (anúncios).
  // Exige amostra mínima de anúncios: com 1 ou 2, o gap é ruído.
  // Quanto o valor declarado supera o Valor Venal de Referência (piso de
  // tributação). Perto de zero = declarado no piso.
  agio:     { rotulo: "Declarado vs Venal", rampa: RAMPA_VIOLETA,
              valor: (p, janela) => {
                const nv = nivel(p, janela);
                return (nv.agio_n >= MIN_AMOSTRAS_RANKING
                        && nv.agio !== null && nv.agio !== undefined)
                       ? nv.agio : null;
              } },
  gap:      { rotulo: "Pedido vs Fechado (ITBI)", rampa: RAMPA_GAP,
              valor: (p, janela) => (p.anuncios_n >= MIN_ANUNCIOS_GAP)
                                    ? calcularGap(p, janela) : null },
};

const estado = { metrica: "anuncios", janela: "m12", classe: "todos", dados: null };

const ROTULOS_CLASSE = {
  apartamento: "apartamentos", casa: "casas",
  casa_vila: "casas de vila/condomínio", cobertura: "coberturas",
};
let mapa, camada;

const fmtReal = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });
const fmtPct = v => (v > 0 ? "+" : "") + v.toFixed(1).replace(".", ",") + "%";
const fmtMes = m => m ? m.split("-").reverse().join("/") : "—";
const fmtData = iso => iso
  ? iso.slice(0, 10).split("-").reverse().join("/")
  : "—";

function quantis(valores, n) {
  const v = [...valores].sort((a, b) => a - b);
  const breaks = [];
  for (let i = 1; i < n; i++) breaks.push(v[Math.floor((i / n) * (v.length - 1))]);
  return breaks;
}

// Classes da métrica atual: quantis p/ sequencial, simétrico em 0 p/ divergente.
function calcularClasses() {
  const m = METRICAS[estado.metrica];
  const valores = estado.dados.geojson.features
    .map(f => m.valor(f.properties, estado.janela))
    .filter(v => v !== null && v !== undefined);
  if (!valores.length) return { breaks: [], rampa: m.rampa };
  if (estado.metrica === "var") {
    const amp = Math.max(...valores.map(Math.abs));
    const passo = amp / 3;
    return { breaks: [-2 * passo, -passo, -passo / 4, passo / 4, passo, 2 * passo],
             rampa: m.rampa };
  }
  if (estado.metrica === "gap") {
    // Divergente com pivô no zero (a troca de sinal inverte o significado),
    // mas com braços assimétricos: quase todo bairro é positivo, então uma
    // escala simétrica desperdiçaria metade da rampa.
    const neg = valores.filter(v => v < 0).sort((a, b) => a - b);
    const pos = valores.filter(v => v > 0).sort((a, b) => a - b);
    const breaks = [];
    for (let i = 1; i <= 4; i++) {  // 4 faixas dentro dos negativos
      breaks.push(neg.length ? neg[Math.floor((i / 5) * (neg.length - 1))] : -1);
    }
    breaks.push(0);
    breaks.push(pos.length ? pos[Math.floor(pos.length / 2)] : 1);
    return { breaks, rampa: m.rampa };
  }
  return { breaks: quantis(valores, m.rampa.length), rampa: m.rampa };
}

function corDe(valor, classes) {
  if (valor === null || valor === undefined) return COR_SEM_DADOS;
  let i = 0;
  while (i < classes.breaks.length && valor > classes.breaks[i]) i++;
  return classes.rampa[i];
}

function estiloFeature(classes) {
  const m = METRICAS[estado.metrica];
  return f => ({
    fillColor: corDe(m.valor(f.properties, estado.janela), classes),
    fillOpacity: 0.82,
    color: "rgba(11,11,11,0.35)",
    weight: 1,
  });
}

/* ---------- tooltip ---------- */
const tooltip = document.getElementById("tooltip");

function htmlTooltip(p) {
  const jan = estado.janela.replace("m", "") + " meses";
  const linhas = [];
  linhas.push(["<span class='marcador' style='background:" + RAMPA_AZUL[4] + "'></span>Pedido",
    p.anuncios_mediana ? "R$ " + fmtReal.format(p.anuncios_mediana) + "/m²" : "sem dados",
    p.anuncios_n ? p.anuncios_n + " anúncios" : ""]);
  const nv = nivel(p, estado.janela);
  linhas.push(["<span class='marcador' style='background:" + RAMPA_AQUA[4] + "'></span>Fechado (ITBI)",
    nv.mediana ? "R$ " + fmtReal.format(nv.mediana) + "/m²" : "sem dados",
    nv.n ? `${nv.n} vendas (${jan.replace(" meses", "m")})` : ""]);
  const v = p.var ? p.var[estado.janela] : null;
  linhas.push(["Valorização " + jan,
    v !== null && v !== undefined ? fmtPct(v) : "amostra insuficiente", ""]);
  linhas.push(["Declarado vs venal",
    nv.agio !== null && nv.agio !== undefined ? fmtPct(nv.agio) : "—", ""]);
  const gap = calcularGap(p, estado.janela);
  linhas.push(["Pedido vs fechado",
    gap !== null && gap !== undefined
      ? (gap <= 0 ? `${fmtPct(gap)} — fecha abaixo do pedido`
                  : `${fmtPct(gap)} — fecha acima do pedido`)
      : "—", ""]);
  return `<h3>${p.nome}</h3><div class="regiao">Zona ${p.regiao || "—"}</div>
    <table>${linhas.map(([k, v2, n]) =>
      `<tr><td>${k}</td><td>${v2}${n ? ` <span class="amostra">· ${n}</span>` : ""}</td></tr>`
    ).join("")}</table>`;
}

const ehTelaEstreita = () => matchMedia("(max-width: 800px)").matches;

function moverTooltip(ev) {
  // Em tela estreita o tooltip é uma faixa ancorada no rodapé (CSS): seguir o
  // toque com um card quase da largura da tela deixava metade dele fora.
  if (ehTelaEstreita()) {
    tooltip.style.left = "";
    tooltip.style.top = "";
    return;
  }
  const margem = 14;
  const r = tooltip.getBoundingClientRect();
  let x = ev.originalEvent.clientX + margem;
  let y = ev.originalEvent.clientY + margem;
  if (x + r.width > innerWidth - 8) x = ev.originalEvent.clientX - r.width - margem;
  if (y + r.height > innerHeight - 8) y = ev.originalEvent.clientY - r.height - margem;
  // Trava final: virar de lado pode jogar o card para fora quando ele é quase
  // tão largo quanto a janela.
  x = Math.max(8, Math.min(x, innerWidth - r.width - 8));
  y = Math.max(8, Math.min(y, innerHeight - r.height - 8));
  tooltip.style.left = x + "px";
  tooltip.style.top = y + "px";
}

/* ---------- legenda ---------- */
function renderLegenda(classes) {
  const el = document.getElementById("legenda");
  if (!el) return;  // o controle só existe depois que o mapa é criado
  const m = METRICAS[estado.metrica];
  const ePct = ["var", "gap", "agio"].includes(estado.metrica);
  const fmt = ePct ? fmtPct : v => "R$ " + fmtReal.format(v);
  const jan = estado.janela.replace("m", "") + "m";
  const titulo = estado.metrica === "var"
    ? m.rotulo + " — " + estado.janela.replace("m", "") + " meses"
    : estado.metrica === "gap"
      ? `ITBI (${jan}) vs pedido — − = fecha abaixo`
      : estado.metrica === "agio"
        ? `Declarado sobre o venal — ${jan}`
        : estado.metrica === "itbi"
          ? `${m.rotulo} — ${jan}`
          : m.rotulo;
  const vistos = new Set();
  const faixas = classes.rampa.flatMap((cor, i) => {
    let rotulo;
    if (i === 0) rotulo = "até " + fmt(classes.breaks[0]);
    else if (i === classes.rampa.length - 1) rotulo = "acima de " + fmt(classes.breaks[i - 1]);
    else rotulo = fmt(classes.breaks[i - 1]) + " – " + fmt(classes.breaks[i]);
    // amostras pequenas colapsam quantis em faixas repetidas — mostra uma só
    if (vistos.has(rotulo) || (i > 0 && i < classes.rampa.length - 1
        && classes.breaks[i - 1] === classes.breaks[i])) return [];
    vistos.add(rotulo);
    return [`<div class="faixa"><span class="cor" style="background:${cor}"></span>
            <span class="rotulo">${rotulo}</span></div>`];
  });
  faixas.push(`<div class="faixa"><span class="cor" style="background:${COR_SEM_DADOS}"></span>
               <span class="rotulo">sem dados</span></div>`);
  if (estado.metrica === "agio") {
    faixas.push(`<div class="nota-legenda">O venal de referência é o piso que a
      Prefeitura usa para tributar. Bairros perto de 0% concentram declarações
      no piso; os mais altos declaram bem acima dele.</div>`);
  }
  if (estado.metrica === "gap") {
    faixas.push(`<div class="nota-legenda">Quase nada disso é negociação: a
      área do ITBI (cadastro IPTU) é cerca de <b>1,7× a área anunciada</b>,
      medido nos dados, e isso sozinho explica a maior parte do gap. Compare
      bairros entre si, não o valor absoluto.</div>`);
  }
  const d = estado.dados;
  if (d.classe !== "todos") {
    const usaItbi = estado.metrica !== "anuncios";
    if (usaItbi && d.classe_itbi !== d.classe) {
      faixas.push(`<div class="nota-legenda">Classe:
        ${ROTULOS_CLASSE[d.classe]} — o ITBI não distingue essa classe;
        exibindo ${ROTULOS_CLASSE[d.classe_itbi]} (aproximação).</div>`);
    } else {
      faixas.push(`<div class="nota-legenda">Classe:
        somente ${ROTULOS_CLASSE[d.classe]}.</div>`);
    }
  }
  el.innerHTML = `<div class="titulo">${titulo}</div>` + faixas.join("");
}

/* ---------- ranking ---------- */
// O ranking acompanha a métrica escolhida no mapa. Cada uma tem seu próprio
// critério de ordenação, amostra mínima e rótulos.
const RANKINGS = {
  anuncios: {
    topo: "Mais caros", base: "Mais baratos", ordem: "desc",
    valor: p => p.anuncios_mediana,
    apto: p => p.anuncios_n >= MIN_ANUNCIOS_RANKING,
    fmt: v => "R$ " + fmtReal.format(v),
    secundario: p => p.anuncios_n + " anún.",
    nota: () => `Mediana do preço/m² pedido; só bairros com ao menos
                 ${MIN_ANUNCIOS_RANKING} anúncios.`,
  },
  itbi: {
    topo: "Mais caros", base: "Mais baratos", ordem: "desc", janela: true,
    valor: (p, j) => nivel(p, j).mediana,
    apto: (p, j) => nivel(p, j).n >= MIN_AMOSTRAS_RANKING,
    fmt: v => "R$ " + fmtReal.format(v),
    secundario: (p, j) => nivel(p, j).n + " vendas",
    nota: () => `Mediana do preço/m² fechado (ITBI) no período selecionado;
                 só bairros com ao menos ${MIN_AMOSTRAS_RANKING} vendas.`,
  },
  var: {
    topo: "Maiores altas", base: "Maiores quedas", ordem: "desc",
    janela: true,
    valor: (p, janela) => (p.var ? p.var[janela] : null),
    apto: p => nivel(p, "m3").n >= MIN_AMOSTRAS_RANKING,
    fmt: fmtPct,
    classe: v => (v >= 0 ? "pos" : "neg"),
    secundario: p => "R$ " + fmtReal.format(nivel(p, "m3").mediana),
    nota: () => `Valorização do preço/m² fechado (ITBI), janelas móveis de
                 3 meses; só bairros com amostra suficiente.`,
  },
  agio: {
    topo: "Maior ágio sobre o venal", base: "Declaram perto do piso venal",
    ordem: "desc", janela: true,
    valor: (p, j) => nivel(p, j).agio,
    apto: (p, j) => nivel(p, j).agio_n >= MIN_AMOSTRAS_RANKING,
    fmt: fmtPct,
    secundario: (p, j) => nivel(p, j).agio_n + " vendas",
    nota: () => `Mediana de quanto o valor declarado supera o Valor Venal de
                 Referência, nas vendas do período selecionado.`,
  },
  gap: {
    // ascendente: o mais negativo (fecha bem abaixo do pedido) vem primeiro
    topo: "Fecham mais abaixo do pedido", base: "Fecham mais perto do pedido",
    ordem: "asc", janela: true,
    valor: (p, j) => calcularGap(p, j),
    apto: p => p.anuncios_n >= MIN_ANUNCIOS_GAP,
    fmt: fmtPct,
    // sem cor por sinal: aqui negativo não é "ruim", é só direção — e verde/
    // vermelho contradiria o azul/vermelho que o mapa usa para os mesmos valores
    classe: null,
    secundario: p => "R$ " + fmtReal.format(p.anuncios_mediana) + " ped.",
    nota: () => `Preço fechado em relação ao pedido; negativo = fecha abaixo.
                 Embute diferença de metodologia entre as séries.`,
  },
};

function renderRanking() {
  const cfg = RANKINGS[estado.metrica];
  const lista = estado.dados.geojson.features
    .map(f => f.properties)
    .filter(p => {
      const v = cfg.valor(p, estado.janela);
      return v !== null && v !== undefined && cfg.apto(p, estado.janela);
    })
    .sort((a, b) => {
      const va = cfg.valor(a, estado.janela), vb = cfg.valor(b, estado.janela);
      return cfg.ordem === "asc" ? va - vb : vb - va;
    });

  const sufixoJanela = cfg.janela
    ? ` <span class="janela-rotulo">${estado.janela.replace("m", "")}m</span>`
    : "";
  document.getElementById("titulo-topo").innerHTML = cfg.topo + sufixoJanela;
  document.getElementById("titulo-base").innerHTML = cfg.base + sufixoJanela;
  document.getElementById("nota-ranking").textContent = cfg.nota();

  const item = p => {
    const v = cfg.valor(p, estado.janela);
    const classe = cfg.classe ? cfg.classe(v) : "";
    return `<li data-id="${p.id}">
      <span class="nome">${p.nome}</span>
      <span class="mediana">${cfg.secundario(p, estado.janela)}</span>
      <span class="delta ${classe}">${cfg.fmt(v)}</span></li>`;
  };
  document.getElementById("ranking-altas").innerHTML =
    lista.slice(0, 10).map(item).join("");
  document.getElementById("ranking-quedas").innerHTML =
    lista.slice(-10).reverse().map(item).join("");

  document.querySelectorAll(".ranking li").forEach(li =>
    li.addEventListener("click", () => focarBairro(+li.dataset.id)));
}

function focarBairro(id) {
  camada.eachLayer(l => {
    if (l.feature.properties.id === id) {
      mapa.fitBounds(l.getBounds(), { maxZoom: 13, padding: [20, 20] });
      l.setStyle({ weight: 3, color: "#0b0b0b" });
      setTimeout(() => camada.resetStyle(l), 2500);
    }
  });
}

/* ---------- render principal ---------- */
function render() {
  const classes = calcularClasses();
  const estilo = estiloFeature(classes);
  // options.style é o que o resetStyle() usa no mouseout — sem atualizar,
  // polígonos "hoverados" voltariam para o estilo da métrica anterior.
  camada.options.style = estilo;
  camada.setStyle(estilo);
  // O período vale para tudo, menos o preço pedido: os anúncios só têm as
  // capturas recentes (janela de 90 dias), não há histórico para agregar.
  const usaJanela = !METRICAS[estado.metrica].semJanela;
  document.querySelectorAll(".botao-janela").forEach(b => {
    b.disabled = !usaJanela;
    b.title = usaJanela ? ""
      : "Os anúncios só têm as capturas recentes — sem histórico para agregar";
  });
  renderLegenda(classes);
  renderRanking();
}

/* ---------- bootstrap ---------- */
// Modo estático (GitHub Pages): sem backend, lê JSONs pré-gerados.
// A flag é injetada pelo scripts/build_estatico.py.
const ESTATICO = !!window.RADAR_ESTATICO;

async function carregarDados() {
  const url = ESTATICO
    ? `dados/mapa-${estado.classe}.json`
    : `/api/mapa?classe=${estado.classe}`;
  const resp = await fetch(url);
  estado.dados = await resp.json();
}

async function trocarClasse(classe) {
  estado.classe = classe;
  await carregarDados();
  camada.clearLayers();
  camada.addData(estado.dados.geojson);
  render();
}

async function iniciar() {
  await carregarDados();

  mapa = L.map("mapa", { zoomControl: true }).setView([-23.68, -46.63], 11);
  // OpenStreetMap: sem API key (o CARTO passou a exigir uma). No tema escuro,
  // um filtro CSS sobre o tile-pane escurece a base sem afetar os polígonos.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
  }).addTo(mapa);

  // A legenda vive como controle do Leaflet: assim fica sempre ancorada ao
  // canto do mapa, em qualquer viewport (como elemento solto ela se
  // posicionava contra a página e vazava no layout de coluna do mobile).
  const controleLegenda = L.control({ position: "bottomleft" });
  controleLegenda.onAdd = () => {
    const el = L.DomUtil.create("div", "legenda");
    el.id = "legenda";
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);
    return el;
  };
  controleLegenda.addTo(mapa);

  camada = L.geoJSON(estado.dados.geojson, {
    style: estiloFeature(calcularClasses()),
    onEachFeature: (f, l) => {
      l.on("mouseover", ev => {
        l.setStyle({ weight: 2.5, color: "#0b0b0b" });
        l.bringToFront();
        tooltip.innerHTML = htmlTooltip(f.properties);
        tooltip.hidden = false;
        moverTooltip(ev);
      });
      l.on("mousemove", moverTooltip);
      l.on("mouseout", () => {
        camada.resetStyle(l);
        tooltip.hidden = true;
      });
    },
  }).addTo(mapa);

  // O fetch pode resolver antes do CSS/layout: sem isso o fitBounds vê o
  // container com 0x0 e cai no zoom 0 (mapa "cinza").
  // Depois que o usuário navega no mapa, parar de reenquadrar sozinho.
  // Só eventos de interação real entram aqui — fitBounds programático não
  // dispara nenhum deles.
  let usuarioMexeu = false;
  ["dragstart", "wheel", "mousedown", "touchstart"].forEach(ev =>
    mapa.on(ev, () => { usuarioMexeu = true; }));

  const ajustarMapa = () => {
    mapa.invalidateSize();
    if (!usuarioMexeu) mapa.fitBounds(camada.getBounds(), { padding: [8, 8] });
  };
  ajustarMapa();

  // O container muda de tamanho por vários motivos além do resize da janela:
  // rotação de tela, cabeçalho que passa a ocupar duas linhas, troca de aba.
  // O ResizeObserver pega todos; `resize` da janela só pegava um.
  if (window.ResizeObserver) {
    let timer;
    new ResizeObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(ajustarMapa, 150);
    }).observe(document.getElementById("mapa"));
  } else {
    window.addEventListener("resize", ajustarMapa);
  }

  document.querySelectorAll(".botao-metrica").forEach(b =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".botao-metrica").forEach(x => x.classList.remove("ativo"));
      b.classList.add("ativo");
      estado.metrica = b.dataset.metrica;
      render();
    }));
  document.querySelectorAll(".botao-janela").forEach(b =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".botao-janela").forEach(x => x.classList.remove("ativo"));
      b.classList.add("ativo");
      estado.janela = b.dataset.janela;
      render();
    }));
  document.getElementById("seletor-classe").addEventListener("change",
    ev => trocarClasse(ev.target.value));

  const d = estado.dados;
  document.getElementById("rodape").textContent =
    `Fechado (ITBI): transações até ${fmtMes(d.mes_itbi)} · ` +
    `Pedido: anúncios ativos até ${fmtData(d.captura_anuncios)} ` +
    `(janela de ${d.janela_anuncios_dias} dias) · ` +
    `Fontes: Prefeitura de SP (ITBI, GeoSampa), VivaReal, ZAP e Imovelweb · ` +
    `Medianas por distrito — compare bairros, não valores absolutos.`;

  render();
}

/* ---------- aba Analisar anúncio ---------- */
const fmtM2 = v => "R$ " + fmtReal.format(v) + "/m²";

function trocarAba(nome) {
  document.querySelectorAll(".aba").forEach(b =>
    b.classList.toggle("ativo", b.dataset.aba === nome));
  document.getElementById("vista-mapa").hidden = nome !== "mapa";
  document.getElementById("vista-analisar").hidden = nome !== "analisar";
  document.getElementById("controles-mapa").style.visibility =
    nome === "mapa" ? "visible" : "hidden";
  // o tooltip é position:fixed — sem esconder, ele vaza para a outra aba
  tooltip.hidden = true;
  if (nome === "mapa" && mapa) mapa.invalidateSize();
}
document.querySelectorAll(".aba").forEach(b =>
  b.addEventListener("click", () => trocarAba(b.dataset.aba)));

function htmlResultado(r) {
  const a = r.anuncio;
  const partes = [];

  if (r.veredito) {
    const v = r.veredito;
    const acima = v.diferenca_pct >= 0;
    partes.push(`<div class="cartao">
      <div class="veredito-numero ${acima ? "pos" : "neg"}">
        ${fmtPct(v.diferenca_pct)} ${acima ? "acima" : "abaixo"} da mediana</div>
      <div class="veredito-frase">Comparado com ${v.n_similares} anúncios de perfil
        similar (${v.escopo}: ${a.bairro}${v.escopo.includes("vizinho") ? " e arredores" : ""}),
        cuja mediana é ${fmtM2(v.mediana_similares_m2)}.</div>`);
  } else {
    partes.push(`<div class="cartao">
      <div class="veredito-frase">Amostra insuficiente de anúncios similares no banco
        para um veredito confiável — rode mais coletas (<code>coletar_anuncios.py</code>)
        para engordar a base. Contexto do bairro abaixo.</div>`);
  }

  partes.push(`<div class="detalhes">
    <div>Preço <b>R$ ${fmtReal.format(a.preco)}</b></div>
    <div>Área <b>${fmtReal.format(a.area_m2)} m²</b></div>
    <div>Preço/m² <b>${fmtM2(a.preco_m2)}</b></div>
    ${a.quartos != null ? `<div>Quartos <b>${a.quartos}</b></div>` : ""}
    ${a.vagas != null ? `<div>Vagas <b>${a.vagas}</b></div>` : ""}
    ${a.tipo ? `<div>Tipo <b>${a.tipo}</b></div>` : ""}
    <div>Bairro <b>${a.bairro}</b></div>
    <div>Extração <b>${a.metodo_extracao}</b></div>
  </div></div>`);

  const c = r.contexto;
  const linhasCtx = [];
  if (c.mediana_bairro_anuncios_m2) {
    linhasCtx.push(`Mediana pedida no bairro (todos os perfis):
      <b>${fmtM2(c.mediana_bairro_anuncios_m2)}</b> · ${c.n_bairro_anuncios} anúncios`);
  }
  if (c.mediana_bairro_itbi_m2) {
    linhasCtx.push(`Referência informativa — preço <em>fechado</em> (ITBI,
      ${c.mes_itbi}): <b>${fmtM2(c.mediana_bairro_itbi_m2)}</b>. Metodologias
      diferentes: não comparável diretamente com o preço pedido.`);
  }
  if (linhasCtx.length) {
    partes.push(`<div class="cartao"><div class="veredito-frase">
      ${linhasCtx.join("<br>")}</div></div>`);
  }

  if (r.similares.length) {
    const linhas = r.similares.map(s => `<tr>
      <td><a href="${s.url}" target="_blank" rel="noopener">${s.bairro.replace(/\w\S*/g,
        t => t[0] + t.slice(1).toLowerCase())} · ${s.tipo || "imóvel"}</a></td>
      <td>R$ ${fmtReal.format(s.preco)}</td>
      <td>${fmtReal.format(s.area_m2)} m²</td>
      <td>${s.quartos ?? "—"}</td>
      <td>${s.vagas ?? "—"}</td>
      <td>${fmtM2(s.preco_m2)}</td></tr>`).join("");
    partes.push(`<div class="cartao"><h2>Similares no banco</h2>
      <table class="similares">
        <thead><tr><th>Anúncio</th><th>Preço</th><th>Área</th><th>Q</th><th>V</th>
        <th>Preço/m²</th></tr></thead><tbody>${linhas}</tbody></table></div>`);
  }
  return partes.join("");
}

if (ESTATICO) {
  // Sem backend não há como baixar e analisar a página do anúncio.
  document.getElementById("url-anuncio").disabled = true;
  document.getElementById("botao-analisar").disabled = true;
  const saida = document.getElementById("resultado-analise");
  saida.hidden = false;
  saida.innerHTML = `<div class="cartao">
    <div class="veredito-frase"><strong>Esta aba precisa do servidor local.</strong>
    Analisar um anúncio exige baixar a página dele e consultar o banco completo —
    coisas que o GitHub Pages (só arquivos estáticos) não faz. O mapa ao lado
    funciona normalmente aqui.<br><br>
    Para usar o analisador, rode o projeto na sua máquina:<br>
    <code>git clone</code> do repositório → <code>pip install -r requirements.txt</code>
    → <code>python scripts/setup_db.py</code> → <code>python scripts/atualizar_itbi.py</code>
    → <code>python web/app.py</code><br>
    O passo a passo completo está no README do repositório.</div></div>`;
}

document.getElementById("form-analisar").addEventListener("submit", async ev => {
  ev.preventDefault();
  if (ESTATICO) return;
  const botao = document.getElementById("botao-analisar");
  const saida = document.getElementById("resultado-analise");
  botao.disabled = true;
  botao.textContent = "Analisando...";
  saida.hidden = false;
  saida.innerHTML = `<div class="cartao veredito-frase">Baixando e analisando o anúncio…</div>`;
  try {
    const resp = await fetch("/api/analisar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: document.getElementById("url-anuncio").value }),
    });
    const dados = await resp.json();
    saida.innerHTML = resp.ok
      ? htmlResultado(dados)
      : `<div class="erro-analise">${dados.erro || "Erro desconhecido."}</div>`;
  } catch (e) {
    saida.innerHTML = `<div class="erro-analise">Falha de rede: ${e}</div>`;
  } finally {
    botao.disabled = false;
    botao.textContent = "Analisar";
  }
});

iniciar();
