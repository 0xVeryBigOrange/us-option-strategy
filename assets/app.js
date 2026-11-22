'use strict';
const GLOSS={
  ann:'年化收益：假设到期没被行权，这笔权利金按占用资金折算成一年的收益率 = 单次收益 × 365 ÷ 剩余天数。只是换算，不代表一年真能赚这么多。',
  per:'单次收益：这一笔交易真正赚到的比例 = 权利金 ÷（行权价 − 权利金）。',
  otm:'安全垫：现价跌到行权价需要的跌幅。到期时股价只要高于行权价，就不会被行权。',
  cost:'盈亏平衡价（接货价）= 行权价 − 权利金。被行权后这是你每股的真实成本，股价低于它才算亏。',
  cash:'现金担保：卖 1 张 Put 需要预留的现金 = 行权价 × 100。这样即使被行权也有钱接货，不加杠杆。',
  pitm:'行权概率：用期权价格（隐含波动率）反推的、到期股价低于行权价的概率。是市场定价，不是预测。',
  delta:'Delta：股价每涨跌 $1，这张期权价格变化多少。Put 的 Delta 绝对值也常被当作粗略的行权概率。',
  iv:'隐含波动率（IV）：期权价格里反映的未来一年预期波动幅度。IV 越高，权利金越贵。',
  ivhv:'IV 溢价 = 隐含波动率 ÷ 过去一年真实波动率。大于 1 代表期权比历史波动贵，卖方更占便宜。',
  dd:'距高点：现价相对过去一年最高价的跌幅。',
  fpe:'远期 PE：现价 ÷ 分析师预期的未来 12 个月每股盈利。越低越便宜，但要看预期是否靠谱。',
  mnav:'mNAV = 公司市值 ÷ 所持币的价值。小于 1 表示股价低于手里的币值，大于 1 表示有溢价。',
  spr:'买卖价差：（卖一 − 买一）÷ 中间价。价差越小越容易按中间价成交。',
  oi:'持仓量：这个合约目前未平仓的张数，越大流动性越好。',
  score:'综合分（满分 100）：价值股 = 质量 30 + 估值 30 + 波动率 20 + 跌幅 20；Crypto = 年化 30 + IV 溢价 20 + 跌幅 25 + 流动性 25。',
  adr:'ADR 溢价：美股 ADR 价格相对本土股价（按汇率和换股比例折算）贵多少。海力士 ADR 10 份 = 1 股首尔普通股，目前无法自由互换，所以长期有溢价；溢价收窄时 ADR 会额外下跌。',
  coin:'ETF 跟踪币价本身，没有市盈率等估值指标。',
};
const SORTS={value:[['score','综合推荐'],['ann','年化收益'],['otm','安全垫'],['pitm','行权概率低'],['dd','跌幅'],['val','估值'],['ivhv','IV 溢价']],
              crypto:[['score','综合推荐'],['ann','年化收益'],['otm','安全垫'],['pitm','行权概率低'],['dd','跌幅'],['ivhv','IV 溢价'],['liq','流动性']]};
const CN={BTC:'比特币',ETH:'以太坊'};
const HOLIDAYS=['2026-11-26','2026-12-25','2027-01-01','2027-01-18','2027-02-15','2027-03-26','2027-05-31','2027-06-18','2027-07-05','2027-09-06','2027-11-25','2027-12-24'];
const HALF=['2026-11-27','2026-12-24','2027-11-26'];
let DATA=null, sortKey='score', noEarn=false, tab=location.hash==='#crypto'?'crypto':'value';
const $=s=>document.querySelector(s);
const na='<span class="text-zinc-600">暂无</span>';
const f=(v,d=2)=>v==null?null:Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fp=v=>v==null?'—':(Math.round(v*100)%100===0?f(v,0):f(v,2)); // 价格：整数不带小数
const pr=v=>v==null?'—':(Math.abs(v*100-Math.round(v*100))>1e-6?f(v,3):f(v,2));          // 权利金：保留到分，半分显示 3 位
const pct=(v,d=1,sign=false)=>v==null?na:(sign&&v>0?'+':'')+f(v,d)+'%';
const usd=(v,d=2)=>v==null?na:'$'+f(v,d);
const md=s=>{if(!s)return null;const[y,m,d]=s.split('-');return `${+m}/${+d}`};
const escHTML=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
/* 安全：data.json 里的所有字符串在载入时统一转义（见 sanitize），渲染时不会被当成 HTML 执行 */
function sanitize(v){
  if(typeof v==='string')return escHTML(v);
  if(Array.isArray(v))return v.map(sanitize);
  if(v&&typeof v==='object'){const o={};for(const k of Object.keys(v))o[k]=sanitize(v[k]);return o}
  return v;
}
const esc=s=>String(s??'');   // 数据已在 sanitize 中转义
const T=(k,label)=>`<span class="tip-t" data-tip="${k}" tabindex="0" role="button" aria-label="${label.replace(/<[^>]*>/g,'')}：点击查看解释">${label}</span>`;

/* ---------- 市场状态（按美东时间） ---------- */
function etParts(d){const p=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',weekday:'short',hour12:false}).formatToParts(d);const o={};p.forEach(x=>o[x.type]=x.value);return o}
function marketState(now=new Date()){
  const o=etParts(now), day=`${o.year}-${o.month}-${o.day}`, m=(+o.hour%24)*60+(+o.minute);
  if(o.weekday==='Sat'||o.weekday==='Sun')return {open:false,txt:'美股休市（周末）'};
  if(HOLIDAYS.includes(day))return {open:false,txt:'美股休市（假日）'};
  const close=HALF.includes(day)?13*60:16*60;
  if(m>=570&&m<close)return {open:true,txt:'美股交易中'};
  if(m>=240&&m<570)return {open:false,txt:'美股盘前'};
  if(m>=close&&m<20*60)return {open:false,txt:'美股盘后'};
  return {open:false,txt:'美股休市'};
}
const fmtTZ=(iso,tz)=>new Intl.DateTimeFormat('zh-CN',{timeZone:tz,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(iso)).replace(/\//g,'-');
function ago(iso){const m=Math.round((Date.now()-new Date(iso))/60000);if(m<1)return '刚刚';if(m<60)return m+' 分钟前';const h=Math.floor(m/60);if(h<48)return h+' 小时前';return Math.floor(h/24)+' 天前'}
/* 最近一次收盘时间（UTC 毫秒）：从今天往前找交易日 */
function etOffsetMs(d){const o=etParts(d);return Date.UTC(+o.year,+o.month-1,+o.day,+o.hour%24,+o.minute)-Math.floor(d.getTime()/60000)*60000}
function lastCloseMs(now=new Date()){
  const off=etOffsetMs(now);
  for(let i=0;i<10;i++){
    const d=new Date(now.getTime()-i*86400000), o=etParts(d), day=`${o.year}-${o.month}-${o.day}`;
    if(o.weekday==='Sat'||o.weekday==='Sun'||HOLIDAYS.includes(day))continue;
    const ch=HALF.includes(day)?13:16, c=Date.UTC(+o.year,+o.month-1,+o.day,ch,0)-off;
    if(c<=now.getTime())return c;
  }
  return null;
}
function renderStatus(){
  if(!DATA)return;
  const st=marketState(), upd=new Date(DATA.updated_utc).getTime(), age=(Date.now()-upd)/60000;
  const lc=lastCloseMs();
  // 盘中超过 75 分钟没更新；或收盘 3 小时后仍没有收盘后的数据 → 提示可能延迟
  const stale = (st.open && age>75) || (!st.open && lc && Date.now()>lc+3*3600e3 && upd<lc);
  const dot = st.open?'bg-emerald-400 animate-pulse':'bg-zinc-500';
  $('#status').innerHTML=`<span class="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-zinc-200"><span class="w-1.5 h-1.5 rounded-full ${dot}"></span>${st.txt}</span>
    <span>数据更新 <b class="text-zinc-200 num">${fmtTZ(DATA.updated_utc,'Asia/Singapore')}</b> 新加坡 · <span class="num">${fmtTZ(DATA.updated_utc,'America/New_York')}</span> 美东 · ${ago(DATA.updated_utc)}</span>
    <span>期权报价：${DATA.session==='盘中'?'盘中实时（约延迟 15 分钟）':'美东 '+md(DATA.price_asof)+' 收盘价'}</span>
    ${stale?'<span class="rounded-full bg-amber-400/10 text-amber-300 border border-amber-400/20 px-2 py-0.5">⚠️ 数据没有按时更新，可能延迟</span>':''}
    <span class="text-zinc-600">盘中约每 30 分钟自动更新，页面每 10 分钟自动刷新</span>`;
}

/* ---------- 排序 ---------- */
function sortItems(a){
  const k=sortKey, v=x=>{
    if(k==='dd')return x.dd??0;
    if(k==='val')return x.fpe>0?x.fpe:1e9;
    if(k==='liq')return x.spr??1e9;
    if(k==='pitm')return x.pitm??1e9;
    return -(x[k]??-1e9);
  };
  return [...a].sort((x,y)=>v(x)-v(y));
}

function metric(label,val,sub){
  return `<div class="rounded-xl bg-white/[.03] border border-white/5 px-3 py-2.5 min-w-0">
    <div class="text-[11px] text-zinc-500">${label}</div>
    <div class="mt-0.5 text-[15px] font-semibold text-zinc-100 num truncate">${val}</div>${sub?`<div class="text-[11px] text-zinc-500 num">${sub}</div>`:''}</div>`;
}

/* 价格刻度：接货价 — 行权价 — 现价 */
function ladder(x){
  if(!x.px||!x.k||!x.cost)return '';
  const lo=x.cost*0.97, hi=x.px*1.01, pos=v=>((v-lo)/(hi-lo)*100).toFixed(1);
  return `<div class="mt-4">
    <div class="relative h-1.5 rounded-full bg-gradient-to-r from-rose-500/50 via-amber-400/40 to-emerald-400/50">
      <span class="absolute -top-1 w-3.5 h-3.5 rounded-full bg-rose-400 ring-2 ring-[#111] -translate-x-1/2" style="left:${pos(x.cost)}%"></span>
      <span class="absolute -top-1 w-3.5 h-3.5 rounded-full bg-brand ring-2 ring-[#111] -translate-x-1/2" style="left:${pos(x.k)}%"></span>
      <span class="absolute -top-1 w-3.5 h-3.5 rounded-full bg-white ring-2 ring-[#111] -translate-x-1/2" style="left:${pos(x.px)}%"></span>
    </div>
    <div class="mt-2 flex justify-between text-[11px] num">
      <span class="text-rose-300">接货价 $${fp(x.cost)}</span><span class="text-brand-soft">行权价 $${fp(x.k)}</span><span class="text-zinc-200">现价 $${fp(x.px)}</span>
    </div></div>`;
}

function card(x,i){
  const crypto=!!x.kind;
  const chips=[];
  if(x.ein) chips.push(`<span class="rounded-full bg-amber-400/10 text-amber-300 border border-amber-400/20 px-2 py-0.5 text-[11px] whitespace-nowrap">⚡ ${md(x.earn)} ${x.earn_est?'预计':''}财报</span>`);
  else if(x.earn) chips.push(`<span class="text-[11px] text-zinc-600 whitespace-nowrap">财报 ${md(x.earn)}${x.earn_est?'（预计）':''}</span>`);
  if(x.spr!=null&&x.spr>15) chips.push(`<span class="rounded-full bg-rose-400/10 text-rose-300 border border-rose-400/20 px-2 py-0.5 text-[11px] whitespace-nowrap">价差大</span>`);
  if(x.short_hist) chips.push(`<span class="rounded-full bg-sky-400/10 text-sky-300 border border-sky-400/20 px-2 py-0.5 text-[11px] whitespace-nowrap" title="美股 ${x.listed} 上市，波动率和高点按上市以来计算">美股上市不足一年</span>`);
  if(x.stale_q) chips.push(`<span class="rounded-full bg-amber-400/10 text-amber-300 border border-amber-400/20 px-2 py-0.5 text-[11px] whitespace-nowrap" title="本次刷新没拿到有效报价，沿用上次的数据">报价沿用 ${esc(x.stale_q)}</span>`);
  if(x.hold_age!=null&&x.hold_age>10) chips.push(`<span class="rounded-full bg-amber-400/10 text-amber-300 border border-amber-400/20 px-2 py-0.5 text-[11px] whitespace-nowrap" title="mNAV 用的持币数量来自公司公告，已超过 10 天未更新">持币数据 ${Math.round(x.hold_age)} 天前</span>`);
  const rankCls = i<3?'bg-brand text-black':'bg-white/10 text-zinc-300';
  const disc = x.px&&x.cost?(1-x.cost/x.px)*100:null;
  let m6;
  if(x.kind==='etf') m6=metric(T('coin','对应 '+x.coin), usd(x.coin_px,0), '估值指标不适用');
  else if(x.kind==='treasury') m6=metric(T('mnav','mNAV'), x.mnav==null?na:f(x.mnav,2)+'×', x.hold_amt?`持 ${f(x.hold_amt,0)} ${x.coin}（${md(x.hold_asof)}）`:'持币数据暂无');
  else m6=metric(T('fpe','远期PE'), x.fpe==null?na:(x.fpe<0?'亏损':f(x.fpe,1)+'×'), crypto?(x.tpe?'当前 '+f(x.tpe,1)+'×':'当前亏损'):(x.fpe_local!=null?`按${x.adr_local}股价 ${f(x.fpe_local,1)}×`:'FCF 收益率 '+(x.fcfy==null?'暂无':x.fcfy+'%')));
  const adrTag = x.adr_prem!=null?`<span class="ml-1 rounded-md bg-white/5 border border-white/10 px-1.5 py-px text-[10px] text-zinc-400 whitespace-nowrap tip-t" data-tip="adr">ADR 溢价 ${f(x.adr_prem,0)}%</span>`:'';
  const label = x.label?`<span class="ml-1 rounded-md bg-white/5 border border-white/10 px-1.5 py-px text-[10px] text-zinc-400 whitespace-nowrap">${esc(x.label)}</span>`:'';
  const premC=x.prem*100, nm=esc(x.t);
  // 极端情景：到期再跌 30%
  const crash=x.px*0.7, lossPut=Math.max(0,(x.cost-crash)*100), lossStock=x.px*0.3*100;
  return `<article class="card rounded-3xl p-5 flex flex-col">
    <div class="flex items-start justify-between gap-3">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-8 h-8 shrink-0 rounded-lg ${rankCls} grid place-items-center text-sm font-extrabold num">${i+1}</div>
        <div class="min-w-0">
          <div class="flex items-baseline gap-2 flex-wrap"><span class="text-xl font-extrabold text-white tracking-tight">${nm}</span>
            <span class="text-[13px] text-zinc-300 num">$${fp(x.px)}</span>
            <span class="text-[12px] num ${x.w1>=0?'text-emerald-400':'text-rose-400'}">${pct(x.w1,1,true)}<span class="text-zinc-600"> 5日</span></span>
            <span class="text-[12px] text-zinc-500 num">${T('dd',x.short_hist?'距上市高点':'距高点')} ${pct(x.dd)}</span></div>
          <div class="text-[12px] text-zinc-500 truncate">${esc(x.n)}${label}${adrTag}</div>
        </div>
      </div>
      <div class="text-right shrink-0">
        <div class="text-[10px] tracking-wider text-zinc-500">${T('score','综合分')}</div>
        <div class="text-lg font-bold text-zinc-200 num">${x.score}</div>
      </div>
    </div>

    <div class="mt-5 flex items-end justify-between gap-3">
      <div>
        <div class="text-[12px] text-zinc-500">${T('ann','权利金年化收益')}</div>
        <div class="ann text-[52px] leading-none font-extrabold tracking-tight num">${x.ann==null?'—':f(x.ann,1)}<span class="text-3xl">%</span></div>
        <div class="mt-1.5 text-[12px] text-zinc-400 num">${T('per','单次收益')} <b class="text-zinc-200">${pct(x.per,2)}</b> · ${x.dte} 天后到期</div>
      </div>
      <div class="text-right pb-1 flex flex-col items-end gap-1">${chips.join('')}</div>
    </div>

    <div class="mt-4 rounded-2xl bg-brand/[.07] border border-brand/20 px-4 py-3 text-[14px] text-zinc-200">
      卖出 <b class="text-white">${md(x.exp)}</b> 到期 <b class="text-white num">$${fp(x.k)}</b> Put
      <span class="text-zinc-400">·</span> 收 <b class="text-brand-soft num">$${pr(x.prem)}</b><span class="text-zinc-400">/股</span>
      <span class="text-zinc-500 num whitespace-nowrap">（$${f(premC,0)}/张）</span>
      <div class="text-[11px] text-zinc-500 mt-0.5 num">Bid/Ask ${x.bid??'—'}/${x.ask??'—'} · ${T('delta','Delta')} ${x.dl??'暂无'} · ${T('oi','持仓量')} ${x.oi==null?'暂无':f(x.oi,0)}${x.spr!=null?' · '+T('spr','价差')+' '+x.spr+'%':''}</div>
    </div>

    ${ladder(x)}

    <div class="mt-4 grid grid-cols-3 gap-2">
      ${metric(T('otm','安全垫'),pct(x.otm),'现价→行权价')}
      ${metric(T('cost','盈亏平衡价'),'$'+fp(x.cost),disc==null?'':'比现价便宜 '+f(disc,1)+'%')}
      ${metric(T('cash','现金担保'),x.cash==null?na:'$'+f(x.cash,0),'每张')}
      ${metric(T('pitm','行权概率'),x.pitm==null?na:'约 '+x.pitm+'%','市场定价')}
      ${metric(T('ivhv','IV 溢价'),x.ivhv==null?na:f(x.ivhv,2)+'×',`${T('iv','IV')} ${x.iv??'—'}% / 实际 ${x.hv??'—'}%`)}
      ${m6}
    </div>

    <div class="mt-4 rounded-2xl bg-white/[.03] border border-white/5 px-4 py-3 text-[13px] leading-6 text-zinc-300">
      <div class="text-[11px] text-zinc-500 mb-0.5">💡 一句话算账</div>
      卖 1 张，马上收 <b class="text-brand-soft num">$${f(premC,0)}</b>，账户留 <b class="num">$${f(x.cash,0)}</b> 现金。<br>
      到期 ${nm} ≥ $${fp(x.k)}：<b class="text-emerald-300">白赚 $${f(premC,0)}</b>（+${f(x.per,2)}%）。<br>
      到期 ${nm} &lt; $${fp(x.k)}：以 <b class="num">$${fp(x.cost)}</b>/股接 100 股，<b class="text-zinc-100">比现在买便宜 ${f(disc,1)}%</b>。
      <div class="mt-1.5 text-[12px] text-zinc-500">极端情况：到期再跌 30% 至 $${fp(crash)}，账面浮亏约 $${f(lossPut,0)}（同样情况现在直接买 100 股会亏 $${f(lossStock,0)}）。</div>
    </div>

    <div class="mt-4 space-y-2 text-[13px] leading-5">
      <div class="flex gap-2"><span class="shrink-0 mt-0.5 text-[11px] font-bold rounded-md px-1.5 py-0.5 bg-emerald-400/10 text-emerald-300">为什么</span><span class="text-zinc-300">${esc(x.why)}</span></div>
      <div class="flex gap-2"><span class="shrink-0 mt-0.5 text-[11px] font-bold rounded-md px-1.5 py-0.5 bg-rose-400/10 text-rose-300">风险</span><span class="text-zinc-400">${esc(x.risk)}</span></div>
    </div>
  </article>`;
}

function coinCard(c,k){
  if(!c)return '';
  return `<div class="card rounded-2xl p-4"><div class="flex items-center justify-between"><span class="text-[12px] text-zinc-500">${CN[k]} ${k}</span>
    <span class="text-[12px] num ${c.d7>=0?'text-emerald-400':'text-rose-400'}">7日 ${pct(c.d7,1,true)}</span></div>
    <div class="mt-1 text-2xl sm:text-3xl font-extrabold text-white num">$${f(c.px,0)}</div>
    <div class="text-[11px] text-zinc-500 num">30日 ${pct(c.d30,1,true)} · 距一年高点 ${pct(c.dd)}</div></div>`;
}
function header(){
  const d=tab==='crypto'?DATA.crypto:DATA, it=d.items||[];
  const avg=a=>a.length?a.reduce((s,v)=>s+v,0)/a.length:null;
  $('#s-top5').textContent=d.avg_ann_top5==null?'暂无':d.avg_ann_top5+'%';
  $('#s-avg').textContent=d.avg_ann==null?'暂无':d.avg_ann+'%';
  const o=avg(it.map(x=>x.otm).filter(v=>v!=null)); $('#s-otm').textContent=o==null?'暂无':o.toFixed(1)+'%';
  $('#s-pool').innerHTML=tab==='crypto'?`${it.length}<span class="text-zinc-500 text-xl"> 只</span>`:`${it.length}<span class="text-zinc-500 text-xl"> / ${d.pool_size}</span>`;
  $('#l-pool').textContent=tab==='crypto'?'Crypto 标的':'入选 / 候选池';
  $('#l-top5').textContent=tab==='crypto'?'年化最高 5 只平均':'前 5 名平均年化';
  $('#ctx-label').textContent=tab==='crypto'?'币价':'市场';
  $('#ctx').innerHTML=d.context||'暂无';
  $('#coins').classList.toggle('hidden',tab!=='crypto');
  if(tab==='crypto')$('#coins').innerHTML=coinCard(d.coins&&d.coins.BTC,'BTC')+coinCard(d.coins&&d.coins.ETH,'ETH');
  document.querySelectorAll('#tabs .tab').forEach(b=>b.setAttribute('aria-selected',b.dataset.tab===tab));
  $('#sorts').innerHTML=SORTS[tab].map(([k,l])=>`<button data-k="${k}" class="pill shrink-0 rounded-full border border-white/10 bg-white/5 px-3.5 py-1.5 text-[13px] text-zinc-300 transition hover:border-white/25 whitespace-nowrap">${l}</button>`).join('');
}
function render(){
  let a=(tab==='crypto'?DATA.crypto.items:DATA.items)||[]; if(noEarn)a=a.filter(x=>!x.ein);
  a=sortItems(a);
  $('#list').innerHTML=a.map(card).join('');
  $('#empty').classList.toggle('hidden',a.length>0);
  $('#empty').textContent=noEarn?'打开“避开财报”后没有符合的标的':'暂无数据';
  document.querySelectorAll('#sorts button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.k===sortKey));
}
function skeleton(){
  $('#list').innerHTML=Array.from({length:6}).map(()=>`<div class="card rounded-3xl p-5 space-y-4"><div class="skel h-6 w-1/2 rounded"></div><div class="skel h-12 w-2/3 rounded"></div><div class="skel h-16 rounded-2xl"></div><div class="grid grid-cols-3 gap-2"><div class="skel h-14 rounded-xl"></div><div class="skel h-14 rounded-xl"></div><div class="skel h-14 rounded-xl"></div></div></div>`).join('');
}

/* ---------- 名词解释弹窗（点击/悬停） ---------- */
const pop=$('#pop');
function showTip(el){
  const t=GLOSS[el.dataset.tip]; if(!t)return;
  pop.textContent=t; pop.style.display='block';
  const r=el.getBoundingClientRect(), w=Math.min(300,window.innerWidth-24);
  pop.style.maxWidth=w+'px';
  let left=Math.min(Math.max(12,r.left),window.innerWidth-w-12);
  let top=r.bottom+8; if(top+pop.offsetHeight>window.innerHeight-8)top=r.top-pop.offsetHeight-8;
  pop.style.left=left+'px'; pop.style.top=top+'px';
}
document.addEventListener('click',e=>{const el=e.target.closest('[data-tip]'); if(el){showTip(el);e.stopPropagation()} else pop.style.display='none'});
document.addEventListener('mouseover',e=>{const el=e.target.closest('[data-tip]'); if(el&&matchMedia('(hover:hover)').matches)showTip(el)});
document.addEventListener('mouseout',e=>{if(e.target.closest('[data-tip]')&&matchMedia('(hover:hover)').matches)pop.style.display='none'});
window.addEventListener('scroll',()=>pop.style.display='none',{passive:true});

function fetchData(){
  return fetch('data.json?t='+Date.now(),{cache:'no-store'}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(d=>{
    if(!d||!Array.isArray(d.items))throw new Error('bad data');
    d=sanitize(d); if(!d.crypto||!Array.isArray(d.crypto.items))d.crypto={items:[]};
    return d;
  });
}
function apply(d){
  DATA=d;
  document.querySelector('[data-tab=value] .cnt').textContent=d.items.length;
  document.querySelector('[data-tab=crypto] .cnt').textContent=d.crypto.items.length;
  renderStatus(); header(); render();
}
let timersOn=false;
function load(){
  skeleton(); $('#empty').classList.add('hidden');
  fetchData().then(d=>{
    apply(d);
    if(!timersOn){
      timersOn=true;
      setInterval(renderStatus,60000);
      // 页面开着时每 10 分钟静默拉一次新数据（失败就保留当前数据）
      setInterval(()=>{if(document.visibilityState==='visible')fetchData().then(n=>{if(n.updated_utc!==DATA.updated_utc)apply(n)}).catch(()=>{})},600000);
    }
  }).catch(()=>{
    $('#list').innerHTML='';
    $('#empty').classList.remove('hidden');
    $('#empty').innerHTML='数据加载失败，请检查网络 <button id="retry" class="ml-2 rounded-full bg-brand text-black px-3 py-1 text-[13px] font-semibold">重试</button>';
    $('#retry').addEventListener('click',load);
  });
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){pop.style.display='none';return}
  const el=e.target.closest&&e.target.closest('[data-tip]');
  if(el&&(e.key==='Enter'||e.key===' ')){e.preventDefault();showTip(el)}
});
$('#sorts').onclick=e=>{const b=e.target.closest('button');if(b){sortKey=b.dataset.k;render()}};
$('#noearn').onchange=e=>{noEarn=e.target.checked;render()};
$('#tabs').onclick=e=>{const b=e.target.closest('.tab');if(!b||!DATA)return;tab=b.dataset.tab;sortKey='score';history.replaceState(null,'',tab==='crypto'?'#crypto':location.pathname);header();render()};
window.addEventListener('hashchange',()=>{const t=location.hash==='#crypto'?'crypto':'value';if(t===tab||!DATA)return;tab=t;sortKey='score';header();render()});
load();
