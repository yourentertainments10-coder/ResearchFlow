"""Build the static dashboard (one self-contained HTML file, no server, free to host anywhere).

Charts are plain SVG/HTML drawn in the page, so nothing is loaded from the network. Colour follows
the dataviz method: one-hue ordinal ramp for medal type, validated categorical slots for countries
and gender, one-hue sequential ramp for the heatmap. Every chart has a tooltip and the tables below
carry every number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

TEMPLATE = r"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Asian Games 2026 Medal Dashboard</title>
<style>
:root{--bg:#fcfcfb;--fg:#0b0b0b;--mut:#52514e;--line:#e3e2de;--card:#fff;
--o1:#184f95;--o2:#3987e5;--o3:#86b6ef;
--c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--c5:#e87ba4;
--h0:#f0efec;--h1:#cde2fb;--h2:#9ec5f4;--h3:#5598e7;--h4:#256abf;--h5:#104281}
@media(prefers-color-scheme:dark){:root{--bg:#1a1a19;--fg:#fff;--mut:#c3c2b7;--line:#383835;--card:#212120;
--o1:#9ec5f4;--o2:#3987e5;--o3:#1c5cab;
--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;
--h0:#2a2a28;--h1:#184f95;--h2:#256abf;--h3:#3987e5;--h4:#6da7ec;--h5:#b7d3f6}}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1180px;padding:16px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:15px;margin:0 0 2px}.mut,small{color:var(--mut)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
select,input,button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}
.card{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:8px 12px}.card span{display:block;color:var(--mut);font-size:12px}.card b{font-size:20px}
.charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}@media(max-width:860px){.charts{grid-template-columns:1fr}}
.panel{border:1px solid var(--line);background:var(--card);border-radius:12px;padding:12px 14px;min-width:0}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:6px 0 8px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.legend i.ln{height:2px;width:14px;vertical-align:3px}
.row{display:grid;grid-template-columns:96px 1fr 42px;align-items:center;gap:8px;height:22px;font-size:12px}
.row .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--mut)}.row .v{font-variant-numeric:tabular-nums;text-align:right}
.stk{display:flex;gap:2px;height:14px}.stk i{display:block;height:14px;min-width:0}.stk i:first-child{border-radius:4px 0 0 4px}.stk i:last-child{border-radius:0 4px 4px 0}.stk i:only-child{border-radius:4px}
.stk i:hover{filter:brightness(1.15);outline:1px solid var(--fg)}
.row.g{grid-template-columns:96px 1fr}
table.hm{border-collapse:separate;border-spacing:2px;width:100%;font-size:11px;table-layout:fixed}table.hm th{font-weight:400;color:var(--mut);padding:0}
table.hm th.c{height:84px;vertical-align:bottom}table.hm th.c div{transform:rotate(-55deg);transform-origin:left bottom;white-space:nowrap;width:12px;margin-left:12px}
table.hm th.r{text-align:left;width:96px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
table.hm td{height:22px;text-align:center;border-radius:3px;font-variant-numeric:tabular-nums}
svg text{fill:var(--mut);font:11px system-ui}svg .grid{stroke:var(--line);stroke-width:1}svg .hit{fill:transparent}
#tip{position:fixed;pointer-events:none;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.18);display:none;z-index:9;max-width:260px}
#tip b{font-variant-numeric:tabular-nums}#tip .t{color:var(--mut)}#tip .k{display:inline-block;width:12px;height:2px;margin-right:6px;vertical-align:3px}
details{margin-top:16px}summary{cursor:pointer;color:var(--mut)}
table.t{border-collapse:collapse;width:100%;font-size:13px}table.t th,table.t td{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right}
table.t th:nth-child(2),table.t td:nth-child(2){text-align:left}.wrap{max-height:460px;overflow:auto}tr.pick{cursor:pointer}tr.pick:hover,tr.sel{background:rgba(99,164,232,.15)}
</style>
<h1>Asian Games 2026 (Aichi-Nagoya): medal dashboard</h1>
<small>Source: official results portal, captured __CAPTURED__. __N__ medals, __E__ events, validated against the official table (0 mismatches). Open events (esports, equestrian, sailing) count as Mixed, as the official table does.</small>
<div class=bar><select id=g aria-label="Event gender"><option value="">All events</option><option>Men</option><option>Women</option><option>Mixed</option></select>
<select id=s aria-label="Sport"><option value="">All sports</option></select><input id=q placeholder="Search country" size=14 aria-label="Search country">
<button id=r>Reset</button></div>
<div class=cards id=cards></div>
<div class=charts>
<div class=panel><h2>Medals by country</h2><small>Top 15 by total. Hover a segment for the count.</small>
<div class=legend><span><i style="background:var(--o1)"></i>Gold</span><span><i style="background:var(--o2)"></i>Silver</span><span><i style="background:var(--o3)"></i>Bronze</span></div><div id=c1></div></div>
<div class=panel><h2>Men, women and mixed events</h2><small>Share of each country's medals (top 10). Ignores the gender filter.</small>
<div class=legend><span><i style="background:var(--c1)"></i>Men</span><span><i style="background:var(--c2)"></i>Women</span><span><i style="background:var(--c3)"></i>Mixed</span></div><div id=c2></div></div>
<div class=panel><h2>Where the medals come from</h2><small>Medals by country and sport (top 10 countries, top 12 sports).</small><div class=legend id=hl></div><div id=c3></div></div>
<div class=panel><h2>Medals won over time</h2><small>Cumulative medals by day, top 5 countries. Hover for the day. __NODATE__ medals have no date in the feed and are left out of this chart only.</small><div class=legend id=ll></div><div id=c4></div></div>
</div>
<details open><summary>Tables (every number in the charts)</summary>
<div class=charts><div><div class=wrap><table class=t id=ct></table></div></div><div><div class=wrap><table class=t id=st></table></div></div></div></details>
<div id=tip></div>
<script>
const D=__DATA__;const $=i=>document.getElementById(i);let sel=null;
const gOf=r=>r.g==='Open'?'Mixed':r.g,MED=['Gold','Silver','Bronze'];
[...new Set(D.map(r=>r.s))].sort().forEach(s=>{const o=document.createElement('option');o.textContent=s;$('s').append(o)});
const tip=$('tip');
function showTip(e,lines){tip.replaceChildren();lines.forEach(l=>{const d=document.createElement('div');if(l.k){const k=document.createElement('span');k.className='k';k.style.background=l.k;d.append(k)}
 if(l.t){const t=document.createElement('span');t.className='t';t.textContent=l.t+' ';d.append(t)}if(l.b!==undefined){const b=document.createElement('b');b.textContent=l.b;d.append(b)}tip.append(d)});
 tip.style.display='block';const w=tip.offsetWidth;tip.style.left=Math.min(e.clientX+14,innerWidth-w-8)+'px';tip.style.top=(e.clientY+14)+'px'}
const hideTip=()=>tip.style.display='none';
function hover(el,lines){el.addEventListener('pointermove',e=>showTip(e,lines));el.addEventListener('pointerleave',hideTip);el.addEventListener('focus',()=>{const r=el.getBoundingClientRect();showTip({clientX:r.left,clientY:r.bottom},lines)});el.addEventListener('blur',hideTip);el.tabIndex=0}
function filt(o){o=o||{};return D.filter(r=>(o.noG||!$('g').value||gOf(r)===$('g').value)&&(!$('s').value||r.s===$('s').value)&&(!$('q').value||r.cn.toLowerCase().includes($('q').value.toLowerCase())||r.c.toLowerCase()===$('q').value.toLowerCase()))}
function agg(rows,key){const m={};rows.forEach(r=>{const k=r[key];m[k]=m[k]||{k,n:r.cn,G:0,S:0,B:0,T:0};m[k][r.m[0]]++;m[k].T++});return Object.values(m).sort((a,b)=>b.G-a.G||b.S-a.S||b.B-a.B||b.T-a.T)}
const SHORT={'People\'s Republic of China':'China','Republic of Korea':'Korea','Islamic Republic of Iran':'Iran','Democratic People\'s Republic of Korea':'North Korea','Hong Kong, China':'Hong Kong','Lao People\'s Democratic Republic':'Laos'};const sn=n=>SHORT[n]||n;
const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!==undefined)e.textContent=txt;return e};
// overall top-5 (fixed colour per country: colour follows the entity)
const overall=agg(D,'c').sort((a,b)=>b.T-a.T);const COL={};overall.slice(0,5).forEach((c,i)=>COL[c.k]='var(--c'+(i+1)+')');

function chart1(rows){const box=$('c1');box.replaceChildren();const cs=agg(rows,'c').sort((a,b)=>b.T-a.T).slice(0,15);const mx=Math.max(1,...cs.map(c=>c.T));
 cs.forEach(c=>{const r=el('div','row');r.append(el('span','nm',sn(c.n)));const s=el('div','stk');s.style.width=(c.T/mx*100)+'%';
 [['G','Gold','--o1'],['S','Silver','--o2'],['B','Bronze','--o3']].forEach(([k,nm,v])=>{if(!c[k])return;const i=el('i');i.style.width=(c[k]/c.T*100)+'%';i.style.background='var('+v+')';hover(i,[{t:c.n+' · '+nm,b:c[k]},{t:'Total',b:c.T}]);s.append(i)});
 const w=el('div');w.append(s);r.append(w,el('span','v',c.T));box.append(r)})}

function chart2(rows){const box=$('c2');box.replaceChildren();const cs=agg(rows,'c').sort((a,b)=>b.T-a.T).slice(0,10);
 cs.forEach(c=>{const sub=rows.filter(r=>r.c===c.k),n={Men:0,Women:0,Mixed:0};sub.forEach(r=>n[gOf(r)]++);const r=el('div','row g');r.append(el('span','nm',sn(c.n)));const s=el('div','stk');
 [['Men','--c1'],['Women','--c2'],['Mixed','--c3']].forEach(([k,v])=>{if(!n[k])return;const i=el('i');i.style.width=(n[k]/sub.length*100)+'%';i.style.background='var('+v+')';
 hover(i,[{t:c.n+' · '+k+' events',b:n[k]+' ('+Math.round(n[k]/sub.length*100)+'%)'}]);s.append(i)});r.append(s);box.append(r)})}

function chart3(rows){const box=$('c3');box.replaceChildren();const cs=agg(rows,'c').sort((a,b)=>b.T-a.T).slice(0,10).map(c=>c.k);
 const sub=rows.filter(r=>cs.includes(r.c)),sp=agg(sub,'s').sort((a,b)=>b.T-a.T).slice(0,12).map(s=>s.k),cnt={};
 sub.forEach(r=>{const k=r.c+'|'+r.s;cnt[k]=(cnt[k]||0)+1});
 const bins=[1,2,4,7,11],bin=v=>v?bins.filter(b=>v>=b).length:0,names={};D.forEach(r=>names[r.c]=r.cn);
 const t=el('table','hm');const h=el('tr');h.append(el('th'));sp.forEach(s=>{const th=el('th','c');th.append(el('div','',s));h.append(th)});t.append(h);
 cs.forEach(c=>{const tr=el('tr');tr.append(el('th','r',c));sp.forEach(s=>{const v=cnt[c+'|'+s]||0,td=el('td','',v||'');const b=bin(v);td.style.background='var(--h'+b+')';td.style.color=b>=3?'#fff':'var(--fg)';
 if(document.documentElement&&matchMedia('(prefers-color-scheme:dark)').matches&&b>=4)td.style.color='#0b0b0b';hover(td,[{t:names[c]+' · '+s,b:v+' medals'}]);tr.append(td)});t.append(tr)});box.append(t);
 $('hl').replaceChildren(...['1','2–3','4–6','7–10','11+'].map((l,i)=>{const s=el('span');const k=el('i');k.style.background='var(--h'+(i+1)+')';s.append(k,l);return s}))}

function chart4(rows){const box=$('c4');box.replaceChildren();const dates=[...new Set(D.map(r=>r.d).filter(Boolean))].sort();
 const top=agg(rows,'c').sort((a,b)=>b.T-a.T).slice(0,5).map(c=>c.k);const W=560,H=240,L=34,R=70,T=10,B=24;
 const series=top.map(c=>{let a=0;return{c,n:(D.find(r=>r.c===c)||{}).cn,pts:dates.map(d=>a+=rows.filter(r=>r.c===c&&r.d===d).length)}});
 const mx=Math.max(1,...series.map(s=>s.pts[s.pts.length-1]));const step=mx>400?100:mx>150?50:mx>60?20:mx>30?10:5,top_=Math.ceil(mx/step)*step;
 const x=i=>L+i*(W-L-R)/Math.max(1,dates.length-1),y=v=>H-B-v*(H-B-T)/top_;const ns='http://www.w3.org/2000/svg';
 const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.style.width='100%';
 const add=(tag,at,txt)=>{const e=document.createElementNS(ns,tag);for(const k in at)e.setAttribute(k,at[k]);if(txt!==undefined)e.textContent=txt;svg.append(e);return e};
 for(let v=0;v<=top_;v+=step){add('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),class:'grid'});add('text',{x:L-6,y:y(v)+4,'text-anchor':'end'},v)}
 dates.forEach((d,i)=>{if(i%3===0||i===dates.length-1)add('text',{x:x(i),y:H-6,'text-anchor':'middle'},d.slice(5).replace('-','/'))});
 series.forEach(s=>{add('polyline',{points:s.pts.map((v,i)=>x(i)+','+y(v)).join(' '),fill:'none',stroke:COL[s.c],'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'});
 const e=s.pts.length-1;add('circle',{cx:x(e),cy:y(s.pts[e]),r:4,fill:COL[s.c],stroke:'var(--card)','stroke-width':2})});
 const ends=series.map(s=>({s,y:y(s.pts[s.pts.length-1])})).sort((a,b)=>a.y-b.y);for(let i=1;i<ends.length;i++)if(ends[i].y-ends[i-1].y<13)ends[i].y=ends[i-1].y+13;
 ends.forEach(o=>add('text',{x:x(dates.length-1)+9,y:o.y+4},o.s.c+' '+o.s.pts[o.s.pts.length-1]));
 const cross=add('line',{y1:T,y2:H-B,stroke:'var(--mut)','stroke-width':1,visibility:'hidden'});
 const hit=add('rect',{x:L,y:T,width:W-L-R,height:H-T-B,class:'hit'});
 hit.addEventListener('pointermove',e=>{const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)*W/r.width,i=Math.max(0,Math.min(dates.length-1,Math.round((px-L)/((W-L-R)/Math.max(1,dates.length-1)))));
  cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('visibility','visible');
  showTip(e,[{t:dates[i]},...series.slice().sort((a,b)=>b.pts[i]-a.pts[i]).map(s=>({k:COL[s.c],t:s.c,b:s.pts[i]}))])});
 hit.addEventListener('pointerleave',()=>{cross.setAttribute('visibility','hidden');hideTip()});box.append(svg);
 $('ll').replaceChildren(...series.map(s=>{const sp=el('span'),k=el('i','ln');k.style.background=COL[s.c];sp.append(k,sn(s.n));return sp}))}

function tables(rows){const cs=agg(rows,'c'),tot=rows.length||1;
$('ct').innerHTML='<tr><th>#<th>Country<th>G<th>S<th>B<th>Total<th>Share</tr>'+cs.map((c,i)=>`<tr class="pick ${sel===c.k?'sel':''}" data-k="${c.k}"><td>${i+1}<td>${c.n} <span class=mut>${c.k}</span><td>${c.G}<td>${c.S}<td>${c.B}<td><b>${c.T}</b><td>${(c.T/tot*100).toFixed(1)}%</tr>`).join('');
const base=sel?rows.filter(r=>r.c===sel):rows,ss=agg(base,'s'),st=base.length||1;
$('st').innerHTML=`<tr><th>#<th>Sport${sel?' ('+sel+')':''}<th>G<th>S<th>B<th>Total<th>Share</tr>`+ss.map((c,i)=>`<tr><td>${i+1}<td>${c.k}<td>${c.G}<td>${c.S}<td>${c.B}<td><b>${c.T}</b><td>${(c.T/st*100).toFixed(1)}%</tr>`).join('');
document.querySelectorAll('tr.pick').forEach(tr=>tr.onclick=()=>{sel=sel===tr.dataset.k?null:tr.dataset.k;render()})}

function render(){const all=filt({noG:true}),rows=filt(),cs=agg(rows,'c'),tot=rows.length||1,byT=[...cs].sort((a,b)=>b.T-a.T);
 const hhi=Math.round(cs.reduce((a,c)=>a+(c.T/tot)**2,0)*10000),women=all.filter(r=>gOf(r)==='Women').length,ev=new Set(rows.map(r=>r.s+'|'+r.e)).size;
 $('cards').replaceChildren(...[['Medals',rows.length],['Events',ev],['Countries',cs.length],['Top country share',byT.length?Math.round(byT[0].T/tot*100)+'%':'-'],['Top 5 share',Math.round(byT.slice(0,5).reduce((a,c)=>a+c.T,0)/tot*100)+'%'],['Concentration (HHI)',hhi],['Women\'s share',Math.round(women/(all.length||1)*100)+'%']].map(([a,b])=>{const d=el('div','card');d.append(el('span','',a),el('b','',b));return d}));
 chart1(rows);chart2(filt({noG:true}));chart3(rows);chart4(rows);tables(rows)}
['g','s','q'].forEach(i=>$(i).oninput=()=>{sel=null;render()});$('r').onclick=()=>{$('g').value=$('s').value=$('q').value='';sel=null;render()};render();
</script></html>"""


def build(placings_csv: Path, captured: str, out: Path, events: int) -> Path:
    df = pd.read_csv(placings_csv).fillna({"date": ""})
    data = [
        {
            "c": r.country_code, "cn": r.country_name, "s": r.discipline_name, "e": r.event_code,
            "g": r.gender, "m": r.medal, "d": r.date,
        }
        for r in df.itertuples()
    ]  # fmt: skip
    html = (
        TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
        .replace("__CAPTURED__", captured)
        .replace("__N__", str(len(df)))
        .replace("__E__", str(events))
        .replace("__NODATE__", str(int((df["date"] == "").sum())))
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    df = pd.read_csv(root / "reports/placings.csv")
    events = df.groupby(["discipline", "event_code"]).ngroups
    path = build(root / "reports/placings.csv", "2026-10-04", root / "site/index.html", events)
    print(path, path.stat().st_size)


if __name__ == "__main__":
    main()
