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
const COR_SEM_DADOS = getComputedStyle(document.documentElement)
  .getPropertyValue("--hairline").trim() || "#e1e0d9";

const MIN_AMOSTRAS_RANKING = 30; // amostra ITBI (3 meses) p/ entrar no ranking
const MIN_ANUNCIOS_GAP = 3;      // anúncios mínimos p/ colorir o gap pedido×ITBI

const METRICAS = {
  anuncios: { rotulo: "Pedido — mediana R$/m²", rampa: RAMPA_AZUL,
              valor: p => p.anuncios_mediana },
  itbi:     { rotulo: "Fechado (ITBI) — mediana R$/m²", rampa: RAMPA_AQUA,
              valor: p => p.itbi_mediana },
  var:      { rotulo: "Valorização (ITBI)", rampa: RAMPA_DIVERGENTE,
              valor: (p, janela) => p.var ? p.var[janela] : null },
  // Quanto o preço que FECHA (ITBI) fica abaixo do que se PEDE (anúncios).
  // Exige amostra mínima de anúncios: com 1 ou 2, o gap é ruído.
  gap:      { rotulo: "Pedido vs ITBI", rampa: RAMPA_GAP,
              valor: p => (p.anuncios_n >= MIN_ANUNCIOS_GAP
                           && p.gap !== null && p.gap !== undefined)
                          ? p.gap : null },
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
  linhas.push(["<span class='marcador' style='background:" + RAMPA_AQUA[4] + "'></span>Fechado (ITBI)",
    p.itbi_mediana ? "R$ " + fmtReal.format(p.itbi_mediana) + "/m²" : "sem dados",
    p.itbi_n ? p.itbi_n + " vendas (3m)" : ""]);
  const v = p.var ? p.var[estado.janela] : null;
  linhas.push(["Valorização " + jan,
    v !== null && v !== undefined ? fmtPct(v) : "amostra insuficiente", ""]);
  const gap = p.gap;
  linhas.push(["Pedido vs ITBI",
    gap !== null && gap !== undefined
      ? (gap <= 0 ? `${fmtPct(gap)} — fecha abaixo do pedido`
                  : `${fmtPct(gap)} — fecha acima do pedido`)
      : "—", ""]);
  return `<h3>${p.nome}</h3><div class="regiao">Zona ${p.regiao || "—"}</div>
    <table>${linhas.map(([k, v2, n]) =>
      `<tr><td>${k}</td><td>${v2}${n ? ` <span class="amostra">· ${n}</span>` : ""}</td></tr>`
    ).join("")}</table>`;
}

function moverTooltip(ev) {
  const margem = 14;
  let x = ev.originalEvent.clientX + margem;
  let y = ev.originalEvent.clientY + margem;
  const r = tooltip.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = ev.originalEvent.clientX - r.width - margem;
  if (y + r.height > innerHeight - 8) y = ev.originalEvent.clientY - r.height - margem;
  tooltip.style.left = x + "px";
  tooltip.style.top = y + "px";
}

/* ---------- legenda ---------- */
function renderLegenda(classes) {
  const el = document.getElementById("legenda");
  if (!el) return;  // o controle só existe depois que o mapa é criado
  const m = METRICAS[estado.metrica];
  const ePct = estado.metrica === "var" || estado.metrica === "gap";
  const fmt = ePct ? fmtPct : v => "R$ " + fmtReal.format(v);
  const titulo = estado.metrica === "var"
    ? m.rotulo + " — " + estado.janela.replace("m", "") + " meses"
    : estado.metrica === "gap"
      ? "ITBI vs pedido (− = fecha abaixo)"
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
  if (estado.metrica === "gap") {
    faixas.push(`<div class="nota-legenda">A diferença não é
      só negociação: a área do ITBI vem do cadastro IPTU e inclui áreas comuns,
      o que já derruba o R$/m² fechado. Compare bairros entre si, não o valor
      absoluto.</div>`);
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
function renderRanking() {
  const feats = estado.dados.geojson.features
    .map(f => f.properties)
    .filter(p => p.var && p.var[estado.janela] !== null
                 && p.var[estado.janela] !== undefined
                 && p.itbi_n >= MIN_AMOSTRAS_RANKING)
    .sort((a, b) => b.var[estado.janela] - a.var[estado.janela]);

  document.querySelectorAll(".janela-rotulo").forEach(el =>
    el.textContent = estado.janela.replace("m", "") + "m");

  const item = p => {
    const v = p.var[estado.janela];
    return `<li data-id="${p.id}">
      <span class="nome">${p.nome}</span>
      <span class="mediana">R$ ${fmtReal.format(p.itbi_mediana)}</span>
      <span class="delta ${v >= 0 ? "pos" : "neg"}">${fmtPct(v)}</span></li>`;
  };
  document.getElementById("ranking-altas").innerHTML =
    feats.slice(0, 10).map(item).join("");
  document.getElementById("ranking-quedas").innerHTML =
    feats.slice(-10).reverse().map(item).join("");

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
  const ajustarMapa = () => {
    mapa.invalidateSize();
    mapa.fitBounds(camada.getBounds(), { padding: [8, 8] });
  };
  ajustarMapa();
  requestAnimationFrame(ajustarMapa);
  window.addEventListener("load", ajustarMapa, { once: true });
  // Rotação de tela no celular muda a proporção do container: sem reenquadrar,
  // o mapa fica cortado.
  let timerResize;
  window.addEventListener("resize", () => {
    clearTimeout(timerResize);
    timerResize = setTimeout(ajustarMapa, 250);
  });

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
    `Pedido: anúncios de ${fmtMes(d.mes_anuncios)} · ` +
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
