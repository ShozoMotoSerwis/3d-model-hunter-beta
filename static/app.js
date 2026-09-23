const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const queryEl = $('#query');
const searchBtn = $('#searchBtn');
const grid = $('#resultGrid');

let allResults = [];
let activeResultSources = new Set();
let thumbnailCache = new Map();
let denseMode = false;
let imageObserver = null;
let visibleCount = 60;


const SOURCE_NAMES = {
  printables:'Printables', makerworld:'MakerWorld', makeronline:'MakerOnline', nexprint:'Nexprint',
  thingiverse:'Thingiverse', cults:'Cults3D', myminifactory:'MyMiniFactory', thangs:'Thangs'
};

const SOURCE_LOGOS = {
  printables: {icon:'https://www.printables.com/favicon.ico', domain:'printables.com'},
  makerworld: {icon:'https://makerworld.com/favicon.ico', domain:'makerworld.com'},
  makeronline: {icon:'https://makeronline.com/favicon.ico', domain:'makeronline.com'},
  nexprint: {icon:'https://www.nexprint.com/favicon.ico', domain:'nexprint.com'},
  thingiverse: {icon:'https://www.thingiverse.com/favicon.ico', domain:'thingiverse.com'},
  cults: {icon:'https://cults3d.com/favicon.ico', domain:'cults3d.com'},
  myminifactory: {icon:'https://www.myminifactory.com/favicon.ico', domain:'myminifactory.com'},
  thangs: {icon:'https://thangs.com/favicon.ico', domain:'thangs.com'}
};

function sourceLogoHtml(r){
  const cfg = SOURCE_LOGOS[r.source];
  const fallback = esc(r.mark || '3D');
  const name = esc(r.source_name || r.source || 'Źródło');
  if(!cfg) return `<span class="sourceLogoFloat sourceLogoFallback" title="${name}"><b>${fallback}</b></span>`;
  const backup = `https://www.google.com/s2/favicons?sz=64&domain=${encodeURIComponent(cfg.domain)}`;
  return `<span class="sourceLogoFloat" title="${name}"><img src="${esc(cfg.icon)}" data-logo-backup="${esc(backup)}" alt="${name}" loading="lazy" referrerpolicy="no-referrer"><b>${fallback}</b></span>`;
}

function bindSourceLogoErrors(){
  $$('.sourceLogoFloat img').forEach(img => img.addEventListener('error', () => {
    const backup = img.dataset.logoBackup || '';
    if(backup && img.dataset.triedBackup !== '1'){
      img.dataset.triedBackup = '1';
      img.src = backup;
      return;
    }
    const holder = img.closest('.sourceLogoFloat');
    if(holder) holder.classList.add('sourceLogoFallback');
    img.remove();
  }));
}

function bindPanelSourceLogoErrors(){
  $$('.sourcecard-logo img').forEach(img => img.addEventListener('error', () => {
    const backup = img.dataset.logoBackup || '';
    if(backup && img.dataset.triedBackup !== '1'){
      img.dataset.triedBackup = '1';
      img.src = backup;
      return;
    }
    const holder = img.closest('.sourcecard-logo');
    if(holder) holder.classList.add('fallback');
    img.remove();
  }, {once:true}));
}

function updateSourcePanelCounts(counts={}){
  $$('#sources .sourcecard').forEach(card => {
    const id = card.dataset.source;
    const n = Number(counts[id] || 0);
    const countEl = card.querySelector('.sourcecard-count');
    if(countEl) countEl.textContent = n > 0 ? String(n) : '—';
    card.classList.toggle('has-results', n > 0);
  });
}

const uiScale = $('#uiScale');
const uiScaleValue = $('#uiScaleValue');
const uiScaleBox = $('.uiscale');
function applyUiScale(value, save=true){
  const pct = Math.max(90, Math.min(150, Number(value) || 115));
  document.body.style.zoom = String(pct / 100);
  if(uiScale) uiScale.value = String(pct);
  if(uiScaleValue) uiScaleValue.textContent = `${pct}%`;
  if(save) localStorage.setItem('hunter-ui-scale', String(pct));
}
const savedUiScale = Number(localStorage.getItem('hunter-ui-scale') || 115);
applyUiScale(savedUiScale, false);
bindPanelSourceLogoErrors();
updateSourcePanelCounts({});

// BETA 0.7: skala rolką myszy. Range jest tylko wskaźnikiem — nie da się go
// przeciągać, więc skalowanie nie zmienia geometrii kontrolki pod kursorem.
let uiScaleWheelLocked = false;
if(uiScaleBox){
  uiScaleBox.addEventListener('wheel', e => {
    e.preventDefault();
    if(uiScaleWheelLocked || e.deltaY === 0) return;
    uiScaleWheelLocked = true;
    const current = Number(uiScale?.value || localStorage.getItem('hunter-ui-scale') || 115);
    const direction = e.deltaY < 0 ? 1 : -1; // rolka w górę = większy interfejs
    applyUiScale(current + direction * 5);
    uiScaleBox.classList.add('wheel-active');
    window.setTimeout(() => {
      uiScaleWheelLocked = false;
      uiScaleBox.classList.remove('wheel-active');
    }, 85);
  }, {passive:false});
}
if(uiScaleValue) uiScaleValue.addEventListener('click', () => applyUiScale(115));

function selected(selector){ return $$(selector + ':checked').map(x => x.value); }
function show(el, yes=true){ if(el) el.classList.toggle('hidden', !yes); }

function setLoading(on){
  searchBtn.disabled = on;
  searchBtn.querySelector('span').textContent = on ? 'Szukam…' : 'Szukaj';
  if(on){
    show($('#emptyState'), false);
    show($('#resultSection'), true);
    grid.innerHTML = Array.from({length:16},()=>'<div class="skeleton"></div>').join('');
    $('#resultCount').textContent='Przeszukuję biblioteki…';
    $('#elapsed').textContent='';
    $('#directLinks').innerHTML='';
    show($('#showMoreBtn'), false);
    show($('#sourceHealth'), false);
  }
}

function renderSmart(data){
  show($('#smartPanel'), true);
  $('#originalQ').textContent = data.original || '';
  $('#translatedQ').textContent = data.translated || '';
  $('#translationMode').textContent = data.translation_mode === 'online' ? 'tłumaczenie online' : (data.translation_mode === 'online-fallback' ? 'tłumaczenie zapasowe' : (data.translation_mode === 'disabled' ? 'wyłączone' : 'tryb lokalny'));
  const anchors = data.anchors || [];
  $('#anchors').innerHTML = anchors.length ? `<span class="anchorLabel">KOTWICE</span>${anchors.map(a=>`<span class="anchor">${esc(a)}</span>`).join('')}` : '';
  $('#concepts').innerHTML = (data.concepts || []).map(c => `<span class="concept"><b>${esc(c.term)}</b> → ${c.synonyms.map(esc).join(' · ')}</span>`).join('');
  const variants = data.search_queries || data.variants || [];
  $('#variants').innerHTML = variants.slice(0,18).map((v,i) => `<span class="variant${i===0?' primary':''}">${i===0?'★ ':''}${esc(v)}</span>`).join('');
}

function buildSourceFilters(data){
  const counts = data.source_counts || {};
  activeResultSources = new Set(Object.keys(counts).filter(k => counts[k] > 0));
  const nameById = Object.fromEntries(allResults.map(r => [r.source, r.source_name]));
  const markById = Object.fromEntries(allResults.map(r => [r.source, r.mark]));
  $('#resultSourceFilters').innerHTML = Object.entries(counts)
    .filter(([,count]) => count > 0)
    .sort((a,b) => (nameById[a[0]] || a[0]).localeCompare(nameById[b[0]] || b[0]))
    .map(([sid,count]) => `<label class="checkrow"><input class="result-source" type="checkbox" value="${esc(sid)}" checked><span class="miniBadge">${esc(markById[sid] || '')}</span><span>${esc(nameById[sid] || sid)}</span><b>${count}</b></label>`).join('');

  $$('.result-source').forEach(cb => cb.addEventListener('change', () => {
    if(cb.checked) activeResultSources.add(cb.value); else activeResultSources.delete(cb.value);
    visibleCount = 60;
    renderGrid();
  }));
}

function currentResults(){
  let xs = allResults.filter(r => activeResultSources.has(r.source));
  if($('#hideNoImage').checked){
    xs = xs.filter(r => r.image || thumbnailCache.get(r.url));
  }
  const sort = $('#sortSelect').value;
  if(sort === 'name') xs.sort((a,b) => a.title.localeCompare(b.title, 'pl'));
  else if(sort === 'source') xs.sort((a,b) => a.source_name.localeCompare(b.source_name, 'pl') || b.score-a.score);
  else if(sort === 'downloads') xs.sort((a,b) => (Number(b.downloads)||-1) - (Number(a.downloads)||-1) || b.score-a.score);
  else if(sort === 'likes') xs.sort((a,b) => (Number(b.likes)||-1) - (Number(a.likes)||-1) || b.score-a.score);
  else if(sort === 'newest') xs.sort((a,b) => Date.parse(b.published||0) - Date.parse(a.published||0) || b.score-a.score);
  else xs.sort((a,b) => b.score-a.score || (Number(b.downloads)||0)-(Number(a.downloads)||0) || a.source_name.localeCompare(b.source_name));
  return xs;
}

function compactNumber(v){
  const n = Number(v);
  if(!Number.isFinite(n)) return '';
  return new Intl.NumberFormat('pl-PL',{notation:'compact',maximumFractionDigits:1}).format(n);
}

function metaHtml(r){
  const bits=[];
  if(r.author) bits.push(`<span title="autor">👤 ${esc(r.author)}</span>`);
  if(r.downloads !== null && r.downloads !== undefined && r.downloads !== '') bits.push(`<span title="pobrania">↓ ${compactNumber(r.downloads)}</span>`);
  if(r.likes !== null && r.likes !== undefined && r.likes !== '') bits.push(`<span title="polubienia">♥ ${compactNumber(r.likes)}</span>`);
  if(r.rating !== null && r.rating !== undefined && r.rating !== '' && Number.isFinite(Number(r.rating))) bits.push(`<span title="ocena">★ ${Number(r.rating).toFixed(1)}</span>`);
  if(r.license) bits.push(`<span title="licencja">⚖ ${esc(r.license)}</span>`);
  if((r.formats||[]).length) bits.push(`<span title="zweryfikowane formaty plików">▣ ${(r.formats||[]).map(x=>'.'+String(x).toUpperCase()).join(' ')}</span>`);
  return bits.length ? `<div class="metarow">${bits.join('')}</div>` : '';
}

function placeholder(mark){
  return `<div class="thumbPlaceholder"><div class="phCube"></div><b>${esc(mark || '3D')}</b><span>miniatura modelu</span></div>`;
}

function keywordCoverage(r){
  const m = r.match || {};
  const hits = Number(m.keyword_hits ?? 0);
  const total = Number(m.keyword_total ?? 0);
  return total > 0 ? {hits, total, text:`${hits}/${total}`} : null;
}

function qualityBadge(r){
  const q = r.match_quality || 'broad';
  const label = q === 'exact' ? 'bardzo trafny' : (q === 'good' ? 'trafny' : 'częściowo trafny');
  return `<span class="matchbadge ${esc(q)}" title="Trafność tekstowa rankingu. To nie jest procent pewności, że model jest właściwy.">${label}</span>`;
}

function cardHtml(r){
  const cached = thumbnailCache.get(r.url);
  const image = r.image || cached;
  const img = image
    ? `<img src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : placeholder(r.mark);
  const matched = (r.match?.matched_terms || []).slice(0,2).map(esc).join(' · ');
  const coverage = keywordCoverage(r);
  const coverageHtml = coverage
    ? `<span title="Ile grup słów kluczowych znaleziono w tytule/opisie. To nie jest procent pewności modelu.">słowa kluczowe <b>${coverage.text}</b></span>`
    : `<span>ranking tekstowy</span>`;
  return `<article class="resultcard" data-url="${esc(r.url)}" data-id="${esc(r.id || '')}" data-mark="${esc(r.mark || '3D')}">
    <a class="thumb" href="${esc(r.url)}" target="_blank" rel="noopener" aria-label="Otwórz ${esc(r.title)}">
      ${img}
      ${sourceLogoHtml(r)}
      ${qualityBadge(r)}
    </a>
    <div class="cardbody">
      <a class="titlelink" href="${esc(r.url)}" target="_blank" rel="noopener"><h3>${esc(r.title)}</h3></a>
      ${metaHtml(r)}
      ${matched ? `<div class="matchedterms">${matched}</div>` : ''}
      <p>${esc(r.description || 'Brak dodatkowego opisu w indeksie.')}</p>
      <div class="cardfoot">${coverageHtml}<a href="${esc(r.url)}" target="_blank" rel="noopener">Otwórz ↗</a></div>
    </div>
  </article>`;
}

function setupImageObserver(){
  if(imageObserver) imageObserver.disconnect();
  imageObserver = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if(entry.isIntersecting){
        imageObserver.unobserve(entry.target);
        hydrateThumbnail(entry.target);
      }
    });
  }, {rootMargin:'500px 0px'});
  $$('.resultcard').forEach(card => {
    if(!thumbnailCache.has(card.dataset.url)) imageObserver.observe(card);
  });
}

async function hydrateThumbnail(card){
  const url = card.dataset.url;
  if(!url || thumbnailCache.has(url)) return;
  thumbnailCache.set(url, null);
  try{
    const res = await fetch('/api/thumbnail?url=' + encodeURIComponent(url));
    const data = await res.json();
    if(data.image){
      thumbnailCache.set(url, data.image);
      const thumb = card.querySelector('.thumb');
      if(thumb && document.body.contains(card)){
        const old = thumb.querySelector('.thumbPlaceholder');
        if(old){
          const img = document.createElement('img');
          img.src = data.image;
          img.alt = '';
          img.loading = 'lazy';
          img.referrerPolicy = 'no-referrer';
          img.addEventListener('error', () => img.remove(), {once:true});
          thumb.insertBefore(img, old);
          old.remove();
        }
      }
    } else thumbnailCache.set(url, '');
  }catch(e){ thumbnailCache.set(url, ''); }
}

function bindImageErrors(){
  $$('.resultcard .thumb img').forEach(img => img.addEventListener('error', () => {
    const card = img.closest('.resultcard');
    if(!card) return;
    thumbnailCache.set(card.dataset.url, '');
    img.remove();
    const thumb=card.querySelector('.thumb');
    if(thumb && !thumb.querySelector('.thumbPlaceholder')) thumb.insertAdjacentHTML('afterbegin', placeholder(card.dataset.mark));
  }, {once:true}));
}

function renderGrid(){
  const xs = currentResults();
  $('#resultCount').textContent = `${xs.length} ${xs.length === 1 ? 'wynik' : (xs.length < 5 ? 'wyniki' : 'wyników')}`;
  grid.classList.toggle('dense', denseMode);
  if(!xs.length){
    grid.innerHTML = `<div class="noresults"><b>Brak wyników dla tych filtrów.</b><span>Zmień tryb trafności na „Szeroka” albo wyczyść filtr źródła.</span></div>`;
    show($('#showMoreBtn'), false);
    return;
  }
  const visible = xs.slice(0, visibleCount);
  grid.innerHTML = visible.map(cardHtml).join('');
  const more = xs.length - visible.length;
  show($('#showMoreBtn'), more > 0);
  if(more > 0) $('#showMoreBtn').textContent = `Pokaż więcej (${Math.min(60, more)} z ${more})`;
  bindImageErrors();
  bindSourceLogoErrors();
  setupImageObserver();
}

function renderSourceHealth(data){
  const box = $('#sourceHealth');
  if(!box) return;
  const status = data.source_status || {};
  const selectedSources = (status.selected || selected('#sources input') || []).filter(Boolean);
  const failed = new Set((status.failed || (data.errors || []).map(e=>e.source)).filter(Boolean));
  const responded = new Set((status.responded || selectedSources.filter(s=>!failed.has(s))).filter(Boolean));
  const total = selectedSources.length;
  const ok = responded.size;
  const failedNames = [...failed].map(s=>SOURCE_NAMES[s] || s);

  show(box, total > 0);
  box.classList.toggle('degraded', failed.size > 0 && ok > 0);
  box.classList.toggle('failed', total > 0 && ok === 0);
  $('#sourceHealthLabel').textContent = `Źródła ${ok}/${total}`;
  const detail = failedNames.length
    ? `Chwilowo niedostępne: ${failedNames.join(', ')}. Pozostałe źródła działają normalnie.`
    : `Wszystkie wybrane źródła odpowiedziały.`;
  $('#sourceHealthDetails').textContent = detail;
}

function renderResults(data){
  setLoading(false);
  renderSmart({...data.terms, translated:data.translated, translation_mode:data.translation_mode});
  allResults = data.results || [];
  visibleCount = 60;
  thumbnailCache = new Map(allResults.filter(r=>r.image).map(r=>[r.url,r.image]));
  buildSourceFilters(data);
  const imgInfo = data.image_count ? ` • ${data.image_count} z miniaturą` : '';
  const formatInfo = (data.formats||[]).length ? ` • format: ${(data.formats||[]).map(x=>'.'+String(x).toUpperCase()).join(' / ')} (${data.format_verified_count||0} zweryfikowanych)` : '';
  const depthInfo = data.limit ? ` • zasięg do ${data.limit}/źródło` : '';
  const mwWeb = Number(data.retrieval_counts?.makerworld_web || 0);
  const mwInfo = mwWeb > 0 ? ` • MakerWorld WWW: ${mwWeb}` : '';
  $('#elapsed').textContent = data.elapsed_ms ? `• ${(data.elapsed_ms/1000).toFixed(1)} s${imgInfo}${formatInfo}${depthInfo}${mwInfo}` : `${imgInfo}${formatInfo}${depthInfo}${mwInfo}`;
  renderGrid();
  updateSourcePanelCounts(data.source_counts || {});

  $('#directLinks').innerHTML = (data.direct_links || []).map(r => `<a href="${esc(r.url)}" target="_blank" rel="noopener"><b>${esc(r.mark || '')}</b>${esc(r.source_name)} ↗</a>`).join('');
  renderSourceHealth(data);
  const status = data.source_status || {};
  const allFailed = (status.selected || []).length > 0 && (status.responded || []).length === 0;
  show($('#errorBox'), allFailed);
  if(allFailed) $('#errorBox').textContent = 'Nie udało się połączyć z żadnym z wybranych źródeł. Spróbuj ponownie za chwilę.';
}

async function doSearch(){
  const q = queryEl.value.trim();
  if(!q){ queryEl.focus(); return; }
  setLoading(true);
  updateSourcePanelCounts({});
  show($('#errorBox'), false);
  $('#hideNoImage').checked = false;
  thumbnailCache = new Map();
  try{
    const res = await fetch('/api/search',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        q,
        sources:selected('#sources input'),
        formats:selected('#formats input'),
        limit:Number($('#depthSelect')?.value || 300),
        precision:$('#precisionSelect').value,
        smart:$('#smart').checked
      })
    });
    const data = await res.json();
    if(!res.ok) throw new Error(data.error || 'Błąd wyszukiwania');
    const url = new URL(location.href);
    url.searchParams.set('q', q);
    history.replaceState({}, '', url);
    renderResults(data);
  }catch(e){
    setLoading(false);
    show($('#errorBox'), true);
    $('#errorBox').textContent=e.message;
    grid.innerHTML='';
  }
}

searchBtn.addEventListener('click', doSearch);
queryEl.addEventListener('keydown', e => { if(e.key==='Enter') doSearch(); });
$('#allSources').addEventListener('click',()=>{
  const xs=$$('#sources input');
  const all=xs.every(x=>x.checked);
  xs.forEach(x=>x.checked=!all);
});
$('#sortSelect').addEventListener('change', ()=>{visibleCount=60;renderGrid();});
$('#hideNoImage').addEventListener('change', ()=>{visibleCount=60;renderGrid();});
$('#showMoreBtn').addEventListener('click',()=>{ visibleCount += 60; renderGrid(); });
$('#clearFilters').addEventListener('click',()=>{
  $$('.result-source').forEach(cb => cb.checked = true);
  activeResultSources = new Set($$('.result-source').map(cb => cb.value));
  $('#hideNoImage').checked = false;
  $('#sortSelect').value = 'relevance';
  visibleCount=60;
  renderGrid();
});
$('#densityBtn').addEventListener('click',()=>{ denseMode = !denseMode; renderGrid(); });
$('#toggleSmartDetails').addEventListener('click',()=>{
  const d = $('#smartDetails');
  const hidden = d.classList.toggle('hidden');
  $('#toggleSmartDetails').textContent = hidden ? 'rozwiń' : 'zwiń';
});

const demoData = {
  original:'zawias suszarki do ubrań Vileda', translated:'Vileda clothes dryer hinge', translation_mode:'demo', elapsed_ms:1180,
  terms:{original:'zawias suszarki do ubrań Vileda',translated:'Vileda clothes dryer hinge',anchors:['Vileda'],concepts:[
    {term:'suszarka do ubrań',synonyms:['drying rack','clothes airer','clothes drying rack']},
    {term:'zawias',synonyms:['hinge','pivot hinge','joint']}
  ],search_queries:['Vileda drying rack hinge','Vileda clothes airer hinge','Vileda drying rack joint']},
  source_counts:{printables:8,makerworld:7,makeronline:4,nexprint:3,thingiverse:2},
  quality_counts:{exact:9,good:12,broad:3}, image_count:19, source_status:{selected:['printables','makerworld'],responded:['printables','makerworld'],failed:[]},
  results:[
    {id:'a1',mark:'P',source:'printables',source_name:'Printables',title:'Vileda drying rack replacement hinge',description:'Replacement hinge for a Vileda folding drying rack.',score:42.8,match_quality:'exact',match:{matched_terms:['drying rack','hinge'],keyword_hits:3,keyword_total:3},url:'https://www.printables.com/search/models?q=Vileda%20dryer%20hinge'},
    {id:'b1',mark:'MW',source:'makerworld',source_name:'MakerWorld',title:'Vileda clothes airer pivot joint',description:'Repair part for a folding clothes airer.',score:38.2,match_quality:'exact',match:{matched_terms:['clothes airer','pivot joint'],keyword_hits:3,keyword_total:3},url:'https://makerworld.com/en/search/models?keyword=Vileda%20hinge'}
  ], direct_links:[], errors:[]
};
$('#demoBtn').addEventListener('click',()=>{
  queryEl.value=demoData.original;
  show($('#emptyState'),false);
  show($('#resultSection'),true);
  renderResults(demoData);
});

const initialParams = new URLSearchParams(location.search);
if(initialParams.get('demo') === '1'){
  $('#demoBtn').click();
}else if(initialParams.get('q')){
  queryEl.value = initialParams.get('q');
  setTimeout(doSearch, 60);
}
