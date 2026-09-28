"""Web chat UI (Phase 2). Standard library only, so it runs offline.

    python -m foodagent.web                  # http://127.0.0.1:8000
    python -m foodagent.web --now 18:45 --no-llm --port 8080

POST /api/chat  {"session_id": str|null, "message": str}  ->  {"session_id", "reply", "state"}
POST /api/reset {"session_id": str}

The catalog, orders and request log live in Postgres (DATABASE_URL, see db.py).
Every /api/chat request is logged as one JSON line to requests.jsonl (session, message, reply, state,
tools called, error, latency) and summarised on the console.
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db
from .orders import OrderService
from .session import SessionStore, TurnLog, local_now, make_agent, run_turn

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Food assistant</title>
<style>
:root{--bg:#eef6f0;--panel:#fff;--panel-2:#e5f0e8;--ink:#2a211b;--muted:#7a6d62;--line:#d6e6da;--accent:#d9622b;--accent-2:#f3a24b;--accent-ink:#fff;--accent-soft:#fbe7da;
 --veg:#2e7d32;--nv:#b3372b;--egg:#d99a00;--ok:#2e7d32;--ok-soft:#e3f1e4;--shadow:0 1px 2px rgba(60,35,15,.06),0 6px 20px rgba(60,35,15,.07);
 --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif}
@media (prefers-color-scheme:dark){:root{--bg:#0f1712;--panel:#18221c;--panel-2:#203027;--ink:#f3ebe2;--muted:#a8998b;--line:#2c3d32;--accent:#ef7a3e;--accent-2:#f5a85a;--accent-soft:#3a2518;
 --veg:#5cb860;--nv:#e0645a;--egg:#e8b53a;--ok:#63c168;--ok-soft:#1f3322;--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.25)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
button{font:inherit;cursor:pointer;color:inherit}
.ic{display:inline-flex;width:1em;height:1em;flex:none}.ic svg{width:100%;height:100%}

/* header */
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.bar{max-width:1040px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:16px}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;font-size:16px;white-space:nowrap}
.logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,var(--accent-2),var(--accent));box-shadow:0 4px 12px color-mix(in srgb,var(--accent) 35%,transparent)}
.logo .ic{width:20px;height:20px}
#steps{flex:1;display:flex;justify-content:center;gap:6px;list-style:none;margin:0;padding:0;min-width:0}
#steps li{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);white-space:nowrap}
#steps li::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--line);flex:none}
#steps li+li::after{content:none}
#steps li:not(:last-child){padding-right:6px;border-right:0}
#steps .done{color:var(--ink)}#steps .done::before{background:var(--accent)}
#steps .on{color:var(--accent);font-weight:600}#steps .on::before{background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
#reset{display:flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:7px 12px;font-size:13.5px;white-space:nowrap}
#reset:hover{border-color:var(--accent);color:var(--accent)}

/* log */
main{max-width:1040px;margin:0 auto;padding:24px 16px 8px;min-height:calc(100vh - 190px)}
#log{display:flex;flex-direction:column;gap:22px}
.msg{display:flex;gap:12px;animation:rise .25s ease-out}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.me{justify-content:flex-end}
.me .bubble{background:var(--accent);color:var(--accent-ink);border-radius:18px 18px 4px 18px;max-width:min(75%,560px);padding:10px 15px;white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:var(--shadow)}
.av{width:32px;height:32px;border-radius:50%;flex:none;display:grid;place-items:center;color:var(--accent);background:var(--accent-soft)}
.av .ic{width:18px;height:18px}
.body{flex:1;min-width:0;display:flex;flex-direction:column;gap:12px}
.bot .bubble{align-self:flex-start;background:var(--panel);border:1px solid var(--line);border-radius:4px 18px 18px 18px;padding:10px 15px;white-space:pre-wrap;overflow-wrap:anywhere;max-width:680px}
.err .bubble{border-color:var(--nv);color:var(--nv)}
.typing{display:flex;gap:5px;padding:14px 16px;background:var(--panel);border:1px solid var(--line);border-radius:4px 18px 18px 18px;align-self:flex-start}
.typing i{width:7px;height:7px;border-radius:50%;background:var(--muted);animation:blink 1.2s infinite both}
.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}
@keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}

/* constraint pills */
.look{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.eyebrow{font-size:11.5px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 4px 0 0}
.pill{font-size:13px;background:var(--panel-2);border:1px solid var(--line);border-radius:99px;padding:3px 11px}

/* cards */
.bundles{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:12px;max-width:100%}
.body>.card{max-width:520px}
.bundles .card{transition:transform .15s,border-color .15s}
.bundles .card:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
.card-top{display:flex;align-items:flex-start;gap:12px}
.letter{width:34px;height:34px;flex:none;border-radius:10px;display:grid;place-items:center;font-weight:700;background:var(--accent-soft);color:var(--accent)}
.ttl{flex:1;min-width:0}
.card h3{margin:0;font:600 18px/1.25 var(--serif)}
.cuisine{margin:2px 0 0;font-size:13px;color:var(--muted)}
.tag{font-size:11.5px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:2px 9px;white-space:nowrap}
.meta{display:flex;align-items:baseline;justify-content:space-between;gap:10px;flex-wrap:wrap;padding-bottom:12px;border-bottom:1px dashed var(--line)}
.price{font-size:22px;font-weight:700;letter-spacing:-.01em}
.fine{font-size:12.5px;color:var(--muted)}
.eta{display:inline-flex;align-items:center;gap:5px;font-size:13px;font-weight:600;color:var(--ok);background:var(--ok-soft);border-radius:99px;padding:3px 10px;margin:0;align-self:flex-start}
.items{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px;font-size:14px}
.items li{display:flex;align-items:center;gap:8px}
.items .q{color:var(--muted);font-variant-numeric:tabular-nums;min-width:22px}
.items .n{flex:1;min-width:0}
.items .p{font-variant-numeric:tabular-nums;color:var(--muted)}
.items.edit .p{min-width:48px;text-align:right}
.stepper{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:99px;background:var(--panel)}
.mini{width:26px;height:26px;border:0;border-radius:99px;background:transparent;display:grid;place-items:center;color:var(--ink);padding:0}
.mini .ic{width:13px;height:13px}
.mini:hover{background:var(--accent-soft);color:var(--accent)}
.mini.rm{color:var(--muted)}.mini.rm:hover{color:var(--nv);background:color-mix(in srgb,var(--nv) 12%,transparent)}
.mini:disabled{visibility:hidden}
.qv{min-width:18px;text-align:center;font-weight:600;font-size:13.5px;font-variant-numeric:tabular-nums}
.hint{margin:0;font-size:12.5px;color:var(--muted)}
.diet{width:13px;height:13px;flex:none;border:1.5px solid currentColor;border-radius:2px;display:grid;place-items:center}
.diet::after{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}
.diet.veg{color:var(--veg)}.diet.egg{color:var(--egg)}
.diet.nv{color:var(--nv)}.diet.nv::after{border-radius:0;width:0;height:0;background:none;border-left:3.5px solid transparent;border-right:3.5px solid transparent;border-bottom:6px solid currentColor}
.why{list-style:none;margin:0;padding:10px 12px;background:var(--panel-2);border-radius:10px;display:flex;flex-direction:column;gap:5px;font-size:13px}
.why li{display:flex;gap:7px;align-items:flex-start}.why .ic{color:var(--ok);margin-top:3px}
.out{display:flex;gap:7px;align-items:flex-start;margin:0;font-size:12.5px;color:var(--muted)}.out .ic{margin-top:2px;color:var(--accent)}
.btn{border:0;border-radius:11px;padding:10px 14px;font-weight:600;font-size:14px;transition:filter .15s,transform .1s}
.btn:active{transform:scale(.98)}
.primary{background:var(--accent);color:var(--accent-ink)}.primary:hover{filter:brightness(1.07)}
.ghost{background:transparent;border:1px solid var(--line)}.ghost:hover{border-color:var(--accent);color:var(--accent)}
.btn:disabled{opacity:.45;cursor:default;filter:none;transform:none}
.card .pick{margin-top:auto}
.sums{display:flex;flex-direction:column;gap:4px;font-size:13.5px;color:var(--muted);border-top:1px dashed var(--line);padding-top:10px}
.row{display:flex;justify-content:space-between;gap:10px;font-variant-numeric:tabular-nums}
.row.total{font-size:17px;font-weight:700;color:var(--ink)}
.ask{margin:0;font-size:14px}
.actions{display:flex;gap:8px}.actions .btn{flex:1}
.placed{flex-direction:row;align-items:center;gap:14px;border-color:color-mix(in srgb,var(--ok) 40%,var(--line));background:linear-gradient(135deg,var(--ok-soft),var(--panel) 70%)}
.badge{width:44px;height:44px;flex:none;border-radius:50%;display:grid;place-items:center;background:var(--ok);color:#fff}
.badge .ic{width:24px;height:24px}
.placed p{margin:2px 0 0}

/* hero */
#hero{padding:48px 0 12px;text-align:center}
#hero h2{font:600 clamp(28px,5vw,40px)/1.15 var(--serif);margin:0 0 10px;letter-spacing:-.01em}
#hero h2 em{font-style:italic;color:var(--accent)}
#hero>p{color:var(--muted);margin:0 auto 28px;max-width:520px}
.ideas{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;text-align:left}
.idea{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow);text-align:left;display:flex;flex-direction:column;gap:6px;transition:transform .15s,border-color .15s}
.idea:hover{transform:translateY(-2px);border-color:var(--accent)}
.idea b{font-size:13px;color:var(--accent)}.idea span{font-size:14px;line-height:1.45}

/* composer */
footer{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 22%);padding-top:18px}
.dock{max-width:1040px;margin:0 auto;padding:0 16px 18px}
#chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
#chips button{background:var(--panel);border:1px solid var(--line);border-radius:99px;padding:5px 13px;font-size:13px}
#chips button:hover{border-color:var(--accent);color:var(--accent)}
form{display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:6px 6px 6px 16px;box-shadow:var(--shadow);transition:border-color .15s,box-shadow .15s}
form:focus-within{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft),var(--shadow)}
input{flex:1;min-width:0;font:inherit;border:0;outline:0;background:transparent;color:var(--ink);padding:8px 0}
input::placeholder{color:var(--muted)}
#send{width:42px;height:42px;border:0;border-radius:12px;display:grid;place-items:center;background:var(--accent);color:#fff}
#send:disabled{opacity:.5;cursor:default}#send .ic{width:18px;height:18px}

@media (max-width:720px){#steps li:not(.on){font-size:0;gap:0}#steps{justify-content:flex-end}.brand span.name{display:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header><div class="bar">
 <div class="brand"><span class="logo" id="logo"></span><span class="name">Food assistant</span></div>
 <ol id="steps"></ol>
 <button id="reset" type="button"></button>
</div></header>
<main>
 <section id="hero">
  <h2>What are we <em>eating</em> tonight?</h2>
  <p>Tell me who's eating, any diets or allergies, your budget and when it should arrive. I'll find three safe, priced options.</p>
  <div class="ideas" id="ideas"></div>
 </section>
 <div id="log"></div>
</main>
<footer><div class="dock">
 <div id="chips"></div>
 <form id="f"><input id="q" autocomplete="off" placeholder="Order dinner for six people…" autofocus><button id="send" aria-label="Send"></button></form>
</div></footer>
<script>
const ICON={
 bowl:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11h18a9 9 0 0 1-18 0Z"/><path d="M7 21h10"/><path d="M9 7c0-1.5 1-2 1-3.5M13 7c0-1.5 1-2 1-3.5"/></svg>',
 clock:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
 check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
 shield:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3 4.5 6v5.5c0 4.5 3.2 8.2 7.5 9.5 4.3-1.3 7.5-5 7.5-9.5V6Z"/></svg>',
 send:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/></svg>',
 plus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
 minus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M5 12h14"/></svg>',
 trash:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>'};
const $=s=>document.querySelector(s),log=$('#log'),q=$('#q'),chips=$('#chips'),hero=$('#hero'),steps=$('#steps');
function ic(n){const s=document.createElement('span');s.className='ic';s.innerHTML=ICON[n];return s}
function h(tag,cls,kids){const e=document.createElement(tag);if(cls)e.className=cls;for(const k of [].concat(kids??[]))if(k!=null&&k!==false)e.append(k);return e}
$('#logo').append(ic('bowl'));$('#send').append(ic('send'));$('#reset').append(ic('plus'),'New order');

let sid=null;try{sid=sessionStorage.getItem('sid')}catch(e){}
const IDEAS=[["Group dinner","Order dinner for six people. Two are vegetarian, one has a nut allergy. Keep the total under ₹2,000 and deliver it by 8 PM."],
 ["Craving something","suggest me spiccy paneer option"],["Party, all veg","dinner for 8, all veg, under 1200 by 7:30pm"]];
const SUGGEST=IDEAS.map(x=>x[1]);
function chipsFor(state){const m={RECOMMENDING:["A","B","C"],EDITING:["confirm"],CONFIRMING:["yes","no"],ORDERED:["New order"]};return m[state]||(log.children.length?SUGGEST:[])}
function drawChips(state){chips.innerHTML='';for(const t of chipsFor(state)){const b=h('button',null,t.length>48?t.slice(0,46)+'…':t);b.type='button';b.title=t;b.onclick=()=>t==='New order'?reset():send(t);chips.append(b)}}
const STEPS=['Describe','Choose','Customize','Review','Placed'],IDX={GATHERING:0,RECOMMENDING:1,EDITING:2,CONFIRMING:3,ORDERED:4};
function drawSteps(state){const i=IDX[state]??0;steps.innerHTML='';STEPS.forEach((s,k)=>steps.append(h('li',k<i?'done':k===i?'on':'',s)))}
for(const[t,full]of IDEAS){const b=h('button','idea',[h('b',null,t),h('span',null,full)]);b.type='button';b.onclick=()=>send(full);$('#ideas').append(b)}

/* ---- turn the agent's text reply into structured blocks; anything unrecognised stays text ---- */
const RE={head:/^(?:([ABC])\. |(Selected|Updated|Your order): )?(.+?) \(([^()]+)\) — (₹[\d,]+) incl\. GST & fees(?: · ETA ~(\d{1,2}:\d\d))?$/,
 item:/^\s*(\d+)× (.+?) \((veg|egg|non-veg)\) — (₹[\d,]+)$/,why:/^\s*Why: (.*)$/,out:/^\s*Left out for safety: (.*)$/,
 look:/^Looking for: (.*)$/,fin:/^Final check — (.+)$/,
 sub:/^\s*Subtotal (₹[\d,]+) \+ GST (₹[\d,]+) \+ delivery & packaging (₹[\d,]+) = (₹[\d,]+)$/,
 arr:/^\s*Arrives ~(\d{1,2}:\d\d)\. (.*)$/,placed:/^Order placed — (#\S+), (₹[\d,]+), arriving around (\d{1,2}:\d\d)\.\s*(.*)$/};
function parse(text){const out=[];let cur=null,brk=true,m;
 for(const ln of text.split('\n')){
  if(m=ln.match(RE.head)){cur={t:'bundle',letter:m[1],label:m[2],name:m[3],cuisine:m[4],total:m[5],eta:m[6],items:[],why:[],out:[]};out.push(cur);continue}
  if(m=ln.match(RE.fin)){cur={t:'receipt',name:m[1],items:[]};out.push(cur);continue}
  if(cur&&(m=ln.match(RE.item))){cur.items.push({q:m[1],n:m[2],d:m[3],p:m[4]});continue}
  if(cur?.t==='bundle'&&(m=ln.match(RE.why))){cur.why=m[1].split('; ');continue}
  if(cur?.t==='bundle'&&(m=ln.match(RE.out))){cur.out=m[1].split(', ');continue}
  if(cur?.t==='receipt'&&(m=ln.match(RE.sub))){Object.assign(cur,{sub:m[1],gst:m[2],fees:m[3],total:m[4]});continue}
  if(cur?.t==='receipt'&&(m=ln.match(RE.arr))){cur.eta=m[1];cur.ask=m[2];continue}
  cur=null;
  if(m=ln.match(RE.look)){out.push({t:'look',parts:m[1].split(' · ')});brk=true;continue}
  if(m=ln.match(RE.placed)){out.push({t:'placed',id:m[1],total:m[2],eta:m[3],note:m[4]});brk=true;continue}
  if(!ln.trim()){brk=true;continue}
  const last=out[out.length-1];
  if(!brk&&last?.t==='text')last.lines.push(ln);else out.push({t:'text',lines:[ln]});brk=false}
 return out}
const DIET={veg:'veg',egg:'egg','non-veg':'nv'};
function mini(icon,msg,title,cls=''){const b=h('button','mini live '+cls,ic(icon));b.type='button';b.title=title;b.setAttribute('aria-label',title);b.onclick=()=>send(msg);return b}
function items(list,edit){return h('ul','items'+(edit?' edit':''),list.map(it=>{const q=+it.q,n=it.n;
 const qty=edit?h('span','stepper',[mini('minus',q>1?`make ${n} to ${q-1}`:`remove ${n}`,q>1?'One less':'Remove'),h('span','qv',q),mini('plus',`make ${n} to ${q+1}`,'One more')]):h('span','q',q+'×');
 const li=h('li',null,[h('i','diet '+DIET[it.d]),edit?null:qty,h('span','n',n),edit?qty:null,h('span','p',it.p),edit&&mini('trash',`remove ${n}`,'Remove '+n,'rm')]);li.title=it.d;return li}))}
function act(label,msg,cls){const b=h('button','btn live '+cls,label);b.type='button';b.onclick=()=>send(msg);return b}
function bundleCard(b){
 const top=h('div','card-top',[b.letter&&h('span','letter',b.letter),h('div','ttl',[h('h3',null,b.name),h('p','cuisine',b.cuisine)]),b.label&&h('span','tag',b.label)]);
 const card=h('article','card',[top,h('div','meta',[h('div',null,[h('span','price',b.total),h('div','fine','incl. GST & fees')]),b.eta&&h('span','eta',[ic('clock'),'~'+b.eta])]),items(b.items,!!b.label)]);
 if(b.why.length)card.append(h('ul','why',b.why.map(w=>h('li',null,[ic('check'),h('span',null,w)]))));
 if(b.out.length)card.append(h('p','out',[ic('shield'),h('span',null,'Left out for safety: '+b.out.join(', '))]));
 if(b.letter)card.append(act('Choose '+b.letter,b.letter,'primary pick'));
 if(b.label)card.append(h('p','hint live-hint','Use − / + to change quantities, or the bin to remove a dish.'),act('Looks good — confirm','confirm','primary pick'));
 return card}
function receipt(r){
 const sums=[['Subtotal',r.sub],['GST',r.gst],['Delivery & packaging',r.fees]].filter(x=>x[1]).map(([k,v])=>h('div','row',[h('span',null,k),h('span',null,v)]));
 return h('article','card receipt',[h('div',null,[h('p','eyebrow','Final check'),h('h3',null,r.name)]),items(r.items,true),sums.length&&h('div','sums',sums),
  r.total&&h('div','row total',[h('span',null,'Total'),h('span',null,r.total)]),r.eta&&h('p','eta',[ic('clock'),'Arrives ~'+r.eta]),
  r.ask&&h('p','ask',r.ask.replace(/\s*\(yes \/ no\)\s*$/,'')),h('div','actions',[act('Place order','yes','primary'),act('Make changes','no','ghost')])])}
function render(text){const body=h('div','body');let grid=null;
 for(const b of parse(text)){
  if(b.t==='bundle'&&b.letter){if(!grid){grid=h('div','bundles');body.append(grid)}grid.append(bundleCard(b));continue}
  grid=null;
  if(b.t==='bundle')body.append(bundleCard(b));
  else if(b.t==='receipt')body.append(receipt(b));
  else if(b.t==='look')body.append(h('div','look',[h('span','eyebrow','Looking for'),...b.parts.map(p=>h('span','pill',p))]));
  else if(b.t==='placed')body.append(h('article','card placed',[h('div','badge',ic('check')),h('div',null,[h('h3',null,'Order placed'),h('p',null,[h('b',null,b.id),` · ${b.total} · arriving around ${b.eta}`]),b.note&&h('p','fine',b.note)])]));
  else body.append(h('div','bubble',b.lines.join('\n')))}
 return body}

function botRow(content,cls=''){return h('div','msg bot '+cls,[h('div','av',ic('bowl')),content])}
function push(row,block){hero.hidden=true;log.append(row);row.scrollIntoView({behavior:'smooth',block})}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error(await r.text());return r.json()}
let busy=false;
async function send(text){text=text.trim();if(!text||busy)return;busy=true;$('#send').disabled=true;
 document.querySelectorAll('.live').forEach(b=>{b.disabled=true;b.classList.remove('live')});document.querySelectorAll('.live-hint').forEach(e=>e.remove());
 push(h('div','msg me',h('div','bubble',text)),'end');q.value='';chips.innerHTML='';
 const wait=botRow(h('div','typing',[h('i'),h('i'),h('i')]));push(wait,'end');
 try{const r=await post('/api/chat',{session_id:sid,message:text});sid=r.session_id;try{sessionStorage.setItem('sid',sid)}catch(e){}
  const row=botRow(render(r.reply));wait.replaceWith(row);row.scrollIntoView({behavior:'smooth',block:'start'});drawSteps(r.state);drawChips(r.state)}
 catch(e){wait.replaceWith(botRow(h('div','body',h('div','bubble','Something went wrong: '+e.message)),'err'));drawChips()}
 finally{busy=false;$('#send').disabled=false;q.focus()}}
async function reset(){if(sid)await post('/api/reset',{session_id:sid}).catch(()=>{});sid=null;try{sessionStorage.removeItem('sid')}catch(e){}
 log.innerHTML='';hero.hidden=false;drawSteps('GATHERING');drawChips('GATHERING');window.scrollTo({top:0,behavior:'smooth'});q.focus()}
$('#f').onsubmit=e=>{e.preventDefault();send(q.value)};$('#reset').onclick=reset;drawSteps('GATHERING');drawChips('GATHERING');
</script></body></html>"""


def build_handler(store: SessionStore, turn_log: TurnLog | None = None):
    locks: dict[str, threading.Lock] = {}

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                return self._json(404, {"error": "not found"})
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON"})
            if self.path == "/api/reset":
                store.drop(str(data.get("session_id")))
                return self._json(200, {"ok": True})
            if self.path != "/api/chat":
                return self._json(404, {"error": "not found"})
            message = str(data.get("message", "")).strip()[:2000]
            if not message:
                return self._json(400, {"error": "message is required"})
            sid, agent = store.get(data.get("session_id"))
            with locks.setdefault(sid, threading.Lock()):  # one turn at a time per chat
                reply, row = run_turn(agent, sid, message)
            if turn_log:
                turn_log.write(row)
            return self._json(200, {"session_id": sid, "reply": reply, "state": agent.state})

        def log_message(self, fmt, *args):  # quieter console
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Food assistant web chat")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--now", help="simulated local time HH:MM (fixed for every session)")
    ap.add_argument("--no-llm", action="store_true", help="offline rule-based agent")
    args = ap.parse_args()
    db_url = db.database_url()
    orders = OrderService(Path("orders.jsonl"), db_url)
    trace = Path("trace.jsonl")
    store = SessionStore(lambda: make_agent(local_now(args.now), not args.no_llm, orders, trace))
    server = ThreadingHTTPServer((args.host, args.port), build_handler(store, TurnLog(Path("requests.jsonl"), db_url)))
    db.load_catalog(db_url)  # fail at startup, not on the first chat, if the database is missing
    print(f"Food assistant on http://{args.host}:{args.port}  (Ctrl+C to stop)  data: {db_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
