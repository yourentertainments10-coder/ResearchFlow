"""Build the static dashboard (one self-contained HTML file, no server, free to host anywhere)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

TEMPLATE = r"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Asian Games 2026 Medal Dashboard</title>
<style>:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e3e3e3;--ac:#2b6cb0;--g:#d4a017;--s:#9aa3ad;--b:#b87333}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#eee;--mut:#9aa;--line:#2c3036;--ac:#63a4e8}}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1150px;padding:16px}
h1{font-size:22px;margin:0}small,.mut{color:var(--mut)}.bar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
select,input{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}.card{border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.card span{display:block;color:var(--mut);font-size:12px}.card b{font-size:20px}
.grid{display:grid;grid-template-columns:1.3fr 1fr;gap:20px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right}
th:nth-child(2),td:nth-child(2){text-align:left}th{cursor:default;position:sticky;top:0;background:var(--bg)}tr.row{cursor:pointer}tr.row:hover,tr.sel{background:rgba(99,164,232,.15)}
.stk{display:flex;height:12px;border-radius:3px;overflow:hidden;min-width:60px}.stk i{display:block}.wrap{max-height:520px;overflow:auto}h2{font-size:16px;margin:22px 0 6px}</style>
<h1>Asian Games 2026 (Aichi-Nagoya): medal dashboard</h1>
<small>Source: official results portal, captured __CAPTURED__. __N__ medals, __E__ events, validated against the official table (0 mismatches). Open events (esports, equestrian, sailing) count as Mixed, as the official table does.</small>
<div class=bar><select id=g><option value="">All events</option><option>Men</option><option>Women</option><option>Mixed</option></select>
<select id=s><option value="">All sports</option></select><input id=q placeholder="Search country" size=14>
<button id=r style="padding:6px 10px">Reset</button></div>
<div class=cards id=cards></div>
<div class=grid><div><h2>Countries <small>(click a row for its sports)</small></h2><div class=wrap><table id=ct></table></div></div>
<div><h2 id=dt>Sports</h2><div class=wrap><table id=st></table></div></div></div>
<script>
const D=__DATA__;const $=i=>document.getElementById(i);let sel=null;
const gOf=r=>r.g==='Open'?'Mixed':r.g;
[...new Set(D.map(r=>r.s))].sort().forEach(s=>{const o=document.createElement('option');o.textContent=s;$('s').append(o)});
function filt(ignoreC){return D.filter(r=>(!$('g').value||gOf(r)===$('g').value)&&(!$('s').value||r.s===$('s').value)&&(ignoreC||!$('q').value||r.cn.toLowerCase().includes($('q').value.toLowerCase())||r.c.toLowerCase()===$('q').value.toLowerCase()))}
function agg(rows,key){const m={};rows.forEach(r=>{const k=r[key];m[k]=m[k]||{k,n:r.cn,G:0,S:0,B:0,T:0};m[k][r.m[0]]++;m[k].T++});return Object.values(m).sort((a,b)=>b.G-a.G||b.S-a.S||b.B-a.B||b.T-a.T)}
function bar(x){const t=x.T||1;return `<div class=stk><i style="width:${x.G/t*100}%;background:var(--g)"></i><i style="width:${x.S/t*100}%;background:var(--s)"></i><i style="width:${x.B/t*100}%;background:var(--b)"></i></div>`}
function render(){const all=filt(true),rows=filt(false),cs=agg(rows,'c'),tot=rows.length||1;
const hhi=Math.round(cs.reduce((a,c)=>a+(c.T/tot)**2,0)*10000),women=all.filter(r=>gOf(r)==='Women').length;
const byT=[...cs].sort((a,b)=>b.T-a.T);const ev=new Set(rows.map(r=>r.s+'|'+r.e)).size;
$('cards').innerHTML=[['Medals',rows.length],['Events',ev],['Countries',cs.length],['Top country share',cs.length?Math.round(byT[0].T/tot*100)+'%':'-'],['Top 5 share',Math.round(byT.slice(0,5).reduce((a,c)=>a+c.T,0)/tot*100)+'%'],['Concentration (HHI)',hhi],['Women\'s share',Math.round(women/(all.length||1)*100)+'%']].map(([a,b])=>`<div class=card><span>${a}</span><b>${b}</b></div>`).join('');
$('ct').innerHTML='<tr><th>#<th>Country<th>G<th>S<th>B<th>Total<th>Share<th></tr>'+cs.map((c,i)=>`<tr class="row ${sel===c.k?'sel':''}" data-k="${c.k}"><td>${i+1}<td>${c.n} <span class=mut>${c.k}</span><td>${c.G}<td>${c.S}<td>${c.B}<td><b>${c.T}</b><td>${(c.T/tot*100).toFixed(1)}%<td>${bar(c)}</tr>`).join('');
const base=sel?rows.filter(r=>r.c===sel):rows;const ss=agg(base,'s');
$('dt').textContent=sel?`Sports: ${(cs.find(c=>c.k===sel)||{}).n||sel}`:'Sports';
const st=base.length||1;
$('st').innerHTML='<tr><th>#<th>Sport<th>G<th>S<th>B<th>Total<th>Share<th></tr>'+ss.map((c,i)=>`<tr><td>${i+1}<td>${c.k}<td>${c.G}<td>${c.S}<td>${c.B}<td><b>${c.T}</b><td>${(c.T/st*100).toFixed(1)}%<td>${bar(c)}</tr>`).join('');
document.querySelectorAll('tr.row').forEach(tr=>tr.onclick=()=>{sel=sel===tr.dataset.k?null:tr.dataset.k;render()})}
['g','s','q'].forEach(i=>$(i).oninput=()=>{sel=null;render()});$('r').onclick=()=>{$('g').value=$('s').value=$('q').value='';sel=null;render()};render();
</script></html>"""


def build(placings_csv: Path, captured: str, out: Path, events: int) -> Path:
    df = pd.read_csv(placings_csv)
    data = [
        {
            "c": r.country_code,
            "cn": r.country_name,
            "s": r.discipline_name,
            "e": r.event_code,
            "g": r.gender,
            "m": r.medal,
        }
        for r in df.itertuples()
    ]
    html = (
        TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
        .replace("__CAPTURED__", captured)
        .replace("__N__", str(len(df)))
        .replace("__E__", str(events))
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    df = pd.read_csv(root / "reports/placings.csv")
    path = build(
        root / "reports/placings.csv",
        "2026-10-04",
        root / "site/index.html",
        df.groupby(["discipline", "event_code"]).ngroups,
    )
    print(path, path.stat().st_size)


if __name__ == "__main__":
    main()
