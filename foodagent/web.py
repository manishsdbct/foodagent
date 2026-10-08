"""Web chat UI (Phase 2). Standard library only, so it runs offline.

    python -m foodagent.web                  # http://127.0.0.1:8000
    python -m foodagent.web --now 18:45 --no-llm --port 8080

POST /api/chat  {"session_id": str|null, "message": str}  ->  {"session_id", "reply", "state", "view"}
                (view: the turn's tool result as card blocks, Claude orchestrator only; else null)
POST /api/chat/stream  same body; NDJSON lines {"type": "view", "view", "state"} as cards are ready,
                then {"type": "done"} with the /api/chat fields (the page uses this one)
POST /api/edit  {"session_id", "bundle_id", "item", "qty"}  ->  {"ok", "message", "state", "view"}
                (a quantity button: one engine edit, no model call; qty 0 removes the dish)
POST /api/reset {"session_id": str}

The catalog, orders and request log live in Postgres (DATABASE_URL, see db.py).
Every /api/chat request is logged as one JSON line to requests.jsonl (session, message, reply, state,
tools called, error, latency) and summarised on the console.
"""
from __future__ import annotations

import argparse
import json
import re
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
:root{--bg:#fff8f0;--panel:#fff;--panel-2:#fff3e8;--ink:#2b1d14;--muted:#8a7464;--line:#f1e1d1;--accent:#e8541c;--accent-2:#ffab2e;--accent-ink:#fff;--accent-soft:#ffe8d9;
 --veg:#1f8a4c;--nv:#c8372b;--egg:#d99a00;--ok:#1f8a4c;--ok-soft:#e1f5e8;--warn:#b7791f;--warn-soft:#fff4d6;--bad:#c8372b;--bad-soft:#fde6e3;
 --shadow:0 1px 2px rgba(120,60,20,.06),0 8px 24px rgba(120,60,20,.08);--shadow-lg:0 2px 4px rgba(120,60,20,.06),0 18px 40px rgba(120,60,20,.14);
 --grad:linear-gradient(135deg,#ffab2e 0%,#e8541c 100%);--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif}
@media (prefers-color-scheme:dark){:root{--bg:#140e0a;--panel:#1f1712;--panel-2:#2a1f17;--ink:#f8ede3;--muted:#b49d8b;--line:#3a2b20;--accent:#ff7a3d;--accent-2:#ffc15e;--accent-soft:#3d2216;
 --veg:#4cc77f;--nv:#ff6b5e;--egg:#f0bf45;--ok:#4cc77f;--ok-soft:#173322;--warn:#f0bf45;--warn-soft:#3a2e12;--bad:#ff6b5e;--bad-soft:#3d1a16;
 --shadow:0 1px 2px rgba(0,0,0,.35),0 8px 24px rgba(0,0,0,.3);--shadow-lg:0 2px 4px rgba(0,0,0,.35),0 18px 40px rgba(0,0,0,.45);--grad:linear-gradient(135deg,#ffc15e 0%,#ff6a2b 100%)}}
*{box-sizing:border-box}
html{scroll-padding-top:80px}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
 background:radial-gradient(600px 400px at 0% 0%,color-mix(in srgb,var(--accent-2) 22%,transparent),transparent 70%),
  radial-gradient(520px 420px at 100% 30%,color-mix(in srgb,var(--accent) 12%,transparent),transparent 70%),
  radial-gradient(600px 500px at 30% 100%,color-mix(in srgb,var(--ok) 10%,transparent),transparent 70%)}
button{font:inherit;cursor:pointer;color:inherit}
.ic{display:inline-flex;width:1em;height:1em;flex:none}.ic svg{width:100%;height:100%}

/* header */
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 78%,transparent);backdrop-filter:blur(14px) saturate(1.4);-webkit-backdrop-filter:blur(14px) saturate(1.4);border-bottom:1px solid color-mix(in srgb,var(--line) 70%,transparent)}
.bar{max-width:1080px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:16px}
.brand{display:flex;align-items:center;gap:10px;white-space:nowrap}
.brand b{display:block;font:700 17px/1.1 var(--serif)}.brand small{display:block;font-size:11.5px;color:var(--muted)}
.logo{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;color:#fff;background:var(--grad);box-shadow:0 6px 16px color-mix(in srgb,var(--accent) 40%,transparent);transform:rotate(-6deg)}
.logo .ic{width:22px;height:22px}
#steps{flex:1;display:flex;justify-content:center;align-items:center;gap:4px;list-style:none;margin:0;padding:0;min-width:0}
#steps li{display:flex;align-items:center;gap:7px;font-size:12.5px;font-weight:500;color:var(--muted);white-space:nowrap;padding:5px 10px 5px 5px;border-radius:99px;transition:background .2s,color .2s}
#steps li .n{width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700;background:var(--panel);border:1px solid var(--line)}
#steps li .n .ic{width:11px;height:11px}
#steps li+li::before{content:"";width:14px;height:2px;border-radius:2px;background:var(--line);margin:0 2px 0 -6px}
#steps .done{color:var(--ink)}#steps .done .n{background:var(--ok);border-color:var(--ok);color:#fff}
#steps .on{color:var(--accent);background:var(--accent-soft);font-weight:650}#steps .on .n{background:var(--grad);border:0;color:#fff}
#reset{display:flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 13px;font-size:13.5px;font-weight:550;white-space:nowrap;box-shadow:var(--shadow);transition:border-color .15s,color .15s}
#reset:hover{border-color:var(--accent);color:var(--accent)}

/* log */
main{max-width:1080px;margin:0 auto;padding:24px 16px 8px;min-height:calc(100vh - 200px)}
#log{display:flex;flex-direction:column;gap:24px}
.msg{display:flex;gap:12px;animation:rise .3s cubic-bezier(.2,.8,.2,1)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.me{justify-content:flex-end}
.me .bubble{background:var(--grad);color:#fff;border-radius:20px 20px 6px 20px;max-width:min(75%,560px);padding:10px 16px;white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:0 6px 18px color-mix(in srgb,var(--accent) 30%,transparent);font-weight:500}
.av{width:34px;height:34px;border-radius:12px;flex:none;display:grid;place-items:center;color:#fff;background:var(--grad);box-shadow:0 4px 12px color-mix(in srgb,var(--accent) 30%,transparent)}
.av .ic{width:19px;height:19px}
.body{flex:1;min-width:0;display:flex;flex-direction:column;gap:14px}
.bot .bubble{align-self:flex-start;background:var(--panel);border:1px solid var(--line);border-radius:6px 20px 20px 20px;padding:12px 16px;overflow-wrap:anywhere;max-width:680px;box-shadow:var(--shadow)}
.bubble p{margin:0}.bubble p+p,.bubble p+ul,.bubble ul+p{margin-top:8px}
.bubble ul{margin:4px 0 0;padding-left:20px}.bubble li{margin:2px 0}.bubble li::marker{color:var(--accent)}
.bubble strong{font-weight:650}
.err .bubble{border-color:var(--bad);color:var(--bad);background:var(--bad-soft)}
.typing{display:flex;align-items:center;gap:10px;padding:11px 16px;background:var(--panel);border:1px solid var(--line);border-radius:6px 20px 20px 20px;align-self:flex-start;box-shadow:var(--shadow);font-size:13.5px;color:var(--muted)}
.dots{display:flex;gap:4px}.dots i{width:7px;height:7px;border-radius:50%;background:var(--accent);animation:blink 1.2s infinite both}
.dots i:nth-child(2){animation-delay:.15s}.dots i:nth-child(3){animation-delay:.3s}
@keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}
.typing span{transition:opacity .25s}

/* constraint pills */
.look{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.eyebrow{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 4px 0 0}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:13px;font-weight:550;background:var(--panel);border:1px solid var(--line);border-radius:99px;padding:4px 12px 4px 9px;box-shadow:var(--shadow)}
.pill .ic{color:var(--accent);width:14px;height:14px}

/* cards */
.bundles{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);display:flex;flex-direction:column;max-width:100%;overflow:hidden}
.body>.card{max-width:540px}
.bundles .card{transition:transform .2s cubic-bezier(.2,.8,.2,1),box-shadow .2s,border-color .2s;animation:rise .4s cubic-bezier(.2,.8,.2,1) both}
.bundles .card:nth-child(2){animation-delay:.07s}.bundles .card:nth-child(3){animation-delay:.14s}
.bundles .card:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg);border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.cover{position:relative;height:92px;padding:14px;display:flex;align-items:flex-start;justify-content:space-between;color:#fff;overflow:hidden}
.cover::after{content:"";position:absolute;inset:0;background:radial-gradient(circle at 85% 120%,rgba(255,255,255,.28),transparent 55%);pointer-events:none}
.cover .emoji{position:absolute;right:10px;bottom:-14px;font-size:76px;line-height:1;filter:drop-shadow(0 6px 10px rgba(0,0,0,.25));transform:rotate(-8deg)}
.letter{position:relative;z-index:1;width:36px;height:36px;border-radius:12px;display:grid;place-items:center;font-weight:800;font-size:17px;background:rgba(255,255,255,.95);color:#2b1d14;box-shadow:0 4px 10px rgba(0,0,0,.15)}
.badges{position:relative;z-index:1;display:flex;flex-direction:column;gap:5px;align-items:flex-start;margin-left:8px;margin-right:auto}
.badge-s{font-size:11px;font-weight:700;letter-spacing:.02em;background:rgba(0,0,0,.28);backdrop-filter:blur(6px);border-radius:99px;padding:3px 9px;white-space:nowrap}
.inner{padding:16px;display:flex;flex-direction:column;gap:12px;flex:1}
.card h3{margin:0;font:700 19px/1.2 var(--serif);letter-spacing:-.01em}
.cuisine{margin:3px 0 0;font-size:13px;color:var(--muted)}
.tag{font-size:11.5px;font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:3px 10px;white-space:nowrap;align-self:flex-start}
.ttl-row{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.price{font-size:26px;font-weight:800;letter-spacing:-.02em;margin-right:auto;line-height:1}
.price small{display:block;font-size:11.5px;font-weight:500;color:var(--muted);letter-spacing:0;margin-top:4px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;font-weight:650;border-radius:99px;padding:4px 10px;white-space:nowrap}
.chip .ic{width:13px;height:13px}
.chip.eta{color:var(--ok);background:var(--ok-soft)}
.chip.safe{color:var(--ok);background:var(--ok-soft)}
.chip.warn{color:var(--warn);background:var(--warn-soft)}
.items{list-style:none;margin:0;padding:12px 0 0;display:flex;flex-direction:column;gap:7px;font-size:14px;border-top:1px dashed var(--line)}
.items li{display:flex;align-items:center;gap:9px}
.items .q{font-variant-numeric:tabular-nums;min-width:26px;font-size:12px;font-weight:700;text-align:center;color:var(--accent);background:var(--accent-soft);border-radius:7px;padding:1px 5px}
.items .n{flex:1;min-width:0}
.items .p{font-variant-numeric:tabular-nums;color:var(--muted)}
.items.edit .p{min-width:52px;text-align:right}
.stepper{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:99px;background:var(--panel-2)}
.mini{width:26px;height:26px;border:0;border-radius:99px;background:transparent;display:grid;place-items:center;color:var(--ink);padding:0}
.mini .ic{width:13px;height:13px}
.mini:hover{background:var(--accent);color:#fff}
.mini.rm{color:var(--muted)}.mini.rm:hover{color:#fff;background:var(--bad)}
.mini:disabled{visibility:hidden}
.qv{min-width:18px;text-align:center;font-weight:700;font-size:13.5px;font-variant-numeric:tabular-nums}
.hint{margin:0;font-size:12.5px;color:var(--muted)}
.diet{width:14px;height:14px;flex:none;border:1.5px solid currentColor;border-radius:3px;display:grid;place-items:center}
.diet::after{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.diet.veg{color:var(--veg)}.diet.egg{color:var(--egg)}
.diet.nv{color:var(--nv)}.diet.nv::after{border-radius:0;width:0;height:0;background:none;border-left:4px solid transparent;border-right:4px solid transparent;border-bottom:6.5px solid currentColor}
.diet.none{visibility:hidden}
.why{list-style:none;margin:0;padding:10px 12px;background:var(--panel-2);border-radius:12px;display:flex;flex-direction:column;gap:5px;font-size:13px}
.why li{display:flex;gap:7px;align-items:flex-start}.why .ic{color:var(--ok);margin-top:3px}
details.out{font-size:12.5px;color:var(--muted)}
details.out summary{cursor:pointer;display:flex;align-items:center;gap:6px;list-style:none;font-weight:600}
details.out summary::-webkit-details-marker{display:none}
details.out summary .ic{color:var(--accent)}
details.out summary::after{content:"›";margin-left:auto;font-size:16px;transition:transform .2s}
details.out[open] summary::after{transform:rotate(90deg)}
details.out ul{margin:6px 0 0;padding-left:26px}
.btn{border:0;border-radius:13px;padding:11px 16px;font-weight:700;font-size:14px;display:inline-flex;align-items:center;justify-content:center;gap:7px;transition:filter .15s,transform .1s,box-shadow .15s}
.btn .ic{width:16px;height:16px}
.btn:active{transform:scale(.97)}
.primary{background:var(--grad);color:#fff;box-shadow:0 6px 16px color-mix(in srgb,var(--accent) 32%,transparent)}.primary:hover{filter:brightness(1.06) saturate(1.1)}
.go{background:var(--ok);color:#fff;box-shadow:0 6px 16px color-mix(in srgb,var(--ok) 30%,transparent)}.go:hover{filter:brightness(1.08)}
.ghost{background:var(--panel);border:1px solid var(--line)}.ghost:hover{border-color:var(--accent);color:var(--accent)}
.btn:disabled{opacity:.4;cursor:default;filter:grayscale(.4);transform:none;box-shadow:none}
.pick{margin-top:auto}
.card.pending,.rc.pending{opacity:.6;pointer-events:none;transition:opacity .15s}
.flash .price{animation:flash 1s ease-out}
@keyframes flash{0%{color:var(--accent);transform:scale(1.06)}100%{color:inherit;transform:none}}
.cnote{display:flex;gap:8px;align-items:flex-start;margin:0;padding:9px 12px;border-radius:12px;background:var(--bad-soft);color:var(--bad);font-size:13px;font-weight:550;animation:rise .25s both}
.cnote .ic{margin-top:2px;width:15px;height:15px}

/* receipt */
.receipt .inner{gap:14px}
.receipt-head{display:flex;align-items:center;gap:12px}
.receipt-head .ico{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;font-size:22px;background:var(--accent-soft)}
.sums{display:flex;flex-direction:column;gap:5px;font-size:13.5px;color:var(--muted);border-top:1px dashed var(--line);padding-top:12px}
.row{display:flex;justify-content:space-between;gap:10px;font-variant-numeric:tabular-nums}
.row.total{font-size:19px;font-weight:800;color:var(--ink);padding-top:8px;margin-top:4px;border-top:2px solid var(--ink)}
.note{display:flex;gap:9px;align-items:flex-start;margin:0;padding:10px 12px;border-radius:12px;background:var(--warn-soft);color:var(--ink);font-size:13px}
.note .ic{color:var(--warn);margin-top:2px;width:16px;height:16px}
.paywith{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.paywith .ic{width:16px;height:16px;color:var(--accent)}
.ask{margin:0;font-size:14px}
.actions{display:flex;gap:8px;flex-wrap:wrap}.actions .btn{flex:1}
.zig{height:12px;background:linear-gradient(-45deg,transparent 8px,var(--panel) 0) 0 0/16px 12px repeat-x,linear-gradient(45deg,transparent 8px,var(--panel) 0) 0 0/16px 12px repeat-x;margin-bottom:-1px}
.rc{max-width:540px;filter:drop-shadow(0 8px 18px rgba(120,60,20,.12))}
.receipt{overflow:visible;border-bottom:0;border-radius:20px 20px 0 0;box-shadow:none}

/* placed */
.placed{position:relative;border-color:color-mix(in srgb,var(--ok) 45%,var(--line));background:linear-gradient(160deg,var(--ok-soft),var(--panel) 60%)}
.placed .inner{gap:16px}
.placed-top{display:flex;align-items:center;gap:14px}
.tick{width:52px;height:52px;flex:none;border-radius:50%;display:grid;place-items:center;background:var(--ok);color:#fff;animation:pop .5s cubic-bezier(.2,1.6,.4,1) both;box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 50%,transparent)}
.tick .ic{width:28px;height:28px}
@keyframes pop{from{transform:scale(.3);opacity:0}to{transform:none;opacity:1}}
.tick{animation:pop .5s cubic-bezier(.2,1.6,.4,1) both,ring 1.6s .5s ease-out 2}
@keyframes ring{to{box-shadow:0 0 0 18px transparent}}
.placed h3{font-size:22px}
.placed p{margin:2px 0 0}
.track{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(4,1fr);gap:4px;text-align:center;font-size:11.5px;color:var(--muted)}
.track li{display:flex;flex-direction:column;align-items:center;gap:6px;position:relative}
.track li::before{content:"";position:absolute;top:13px;right:50%;width:100%;height:3px;background:var(--line);z-index:0}
.track li:first-child::before{content:none}
.track .dot{position:relative;z-index:1;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;background:var(--panel);border:2px solid var(--line);font-size:14px}
.track .now{color:var(--ink);font-weight:650}.track .now .dot{border-color:var(--ok);background:var(--ok);color:#fff}
.track .now+li::before{background:linear-gradient(90deg,var(--ok),var(--line))}
.confetti{position:absolute;inset:0;pointer-events:none;overflow:hidden;border-radius:inherit}
.confetti i{position:absolute;top:-10px;width:8px;height:12px;border-radius:2px;opacity:0;animation:fall 1.8s ease-in forwards}
@keyframes fall{0%{opacity:1;transform:translateY(0) rotate(0)}100%{opacity:0;transform:translateY(260px) rotate(540deg)}}

/* declined / misses */
.declined{border-color:color-mix(in srgb,var(--bad) 40%,var(--line));background:linear-gradient(160deg,var(--bad-soft),var(--panel) 60%)}
.declined .head{display:flex;gap:12px;align-items:center}
.declined .head .ico{width:42px;height:42px;border-radius:50%;display:grid;place-items:center;background:var(--bad);color:#fff}
.declined .head .ico .ic{width:22px;height:22px}
.misses .rowm{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px dashed var(--line);font-size:13.5px}
.misses .rowm:first-of-type{border-top:0}
.misses .rowm .ic{color:var(--warn);margin-top:3px}
.misses .rowm b{display:block;font-size:14px}
.misses .rowm span{color:var(--muted)}
.misses .rowm .t{margin-left:auto;font-weight:700;white-space:nowrap}

/* hero */
#hero{padding:44px 0 16px;text-align:center}
.float{font-size:30px;display:flex;justify-content:center;gap:14px;margin-bottom:14px}
.float span{display:inline-block;animation:bob 3s ease-in-out infinite}
.float span:nth-child(2){animation-delay:.4s}.float span:nth-child(3){animation-delay:.8s}.float span:nth-child(4){animation-delay:1.2s}.float span:nth-child(5){animation-delay:1.6s}
@keyframes bob{0%,100%{transform:translateY(0) rotate(-4deg)}50%{transform:translateY(-8px) rotate(4deg)}}
#hero h2{font:700 clamp(32px,6vw,52px)/1.08 var(--serif);margin:0 0 12px;letter-spacing:-.02em}
#hero h2 em{font-style:italic;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;padding-right:4px}
#hero>p{color:var(--muted);margin:0 auto 18px;max-width:540px;font-size:16px}
.promises{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin:0 0 30px}
.ideas{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;text-align:left}
.idea{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px 18px 16px;box-shadow:var(--shadow);text-align:left;display:flex;flex-direction:column;gap:6px;transition:transform .2s cubic-bezier(.2,.8,.2,1),box-shadow .2s,border-color .2s}
.idea:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg);border-color:var(--accent)}
.idea .em{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;font-size:24px;margin-bottom:4px}
.idea b{font-size:15px}.idea span{font-size:13.5px;line-height:1.45;color:var(--muted)}
.idea .go-arrow{position:absolute;right:16px;top:18px;color:var(--muted);transition:transform .2s,color .2s}
.idea:hover .go-arrow{transform:translateX(3px);color:var(--accent)}

/* composer */
footer{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 26%);padding-top:22px}
.dock{max-width:1080px;margin:0 auto;padding:0 16px 18px}
#chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:10px}
#chips button{background:var(--panel);border:1px solid var(--line);border-radius:99px;padding:6px 14px;font-size:13px;font-weight:550;box-shadow:var(--shadow);transition:border-color .15s,color .15s,transform .1s;animation:rise .3s both}
#chips button:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}
#chips button.main{background:var(--grad);border-color:transparent;color:#fff}
form{display:flex;align-items:center;gap:8px;background:var(--panel);border:1.5px solid var(--line);border-radius:18px;padding:7px 7px 7px 18px;box-shadow:var(--shadow-lg);transition:border-color .15s,box-shadow .15s}
form:focus-within{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft),var(--shadow-lg)}
input{flex:1;min-width:0;font:inherit;font-size:15.5px;border:0;outline:0;background:transparent;color:var(--ink);padding:9px 0}
input::placeholder{color:var(--muted)}
#send{width:44px;height:44px;border:0;border-radius:13px;display:grid;place-items:center;background:var(--grad);color:#fff;box-shadow:0 6px 14px color-mix(in srgb,var(--accent) 35%,transparent);transition:transform .1s,filter .15s}
#send:hover{filter:brightness(1.07)}#send:active{transform:scale(.94)}
#send:disabled{opacity:.5;cursor:default}#send .ic{width:19px;height:19px}

@media (max-width:760px){#steps li:not(.on) .lbl{display:none}#steps li:not(.on){padding:5px}#steps{justify-content:flex-end}.brand .txt{display:none}#reset .lbl{display:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header><div class="bar">
 <div class="brand"><span class="logo" id="logo"></span><span class="txt"><b>Food assistant</b><small>Safe group orders, sorted</small></span></div>
 <ol id="steps"></ol>
 <button id="reset" type="button"></button>
</div></header>
<main>
 <section id="hero">
  <div class="float" aria-hidden="true"><span>🍛</span><span>🫓</span><span>🥘</span><span>🍜</span><span>🍨</span></div>
  <h2>What are we <em>eating</em> tonight?</h2>
  <p>Tell me who's eating, any diets or allergies, your budget and when it should arrive. I'll find three safe, priced options.</p>
  <div class="promises" id="promises"></div>
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
 shield:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3 4.5 6v5.5c0 4.5 3.2 8.2 7.5 9.5 4.3-1.3 7.5-5 7.5-9.5V6Z"/><path d="m9 12 2 2 4-4" stroke-linecap="round"/></svg>',
 send:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/></svg>',
 plus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
 minus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M5 12h14"/></svg>',
 trash:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>',
 arrow:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
 users:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.5 3.2-5.5 6.5-5.5s5.9 2 6.5 5.5"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.8c2 .7 3.2 2.5 3.5 5.2"/></svg>',
 leaf:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 19c0-8 5-14 15-14 0 10-6 15-14 15"/><path d="M5 19 13 11"/></svg>',
 wallet:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="3" y="6" width="18" height="13" rx="3"/><path d="M3 10h18M16 14.5h2" stroke-linecap="round"/></svg>',
 alert:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20Z"/><path d="M12 10v4M12 17v.5"/></svg>',
 x:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>'};
const $=s=>document.querySelector(s),log=$('#log'),q=$('#q'),chips=$('#chips'),hero=$('#hero'),steps=$('#steps');
function ic(n){const s=document.createElement('span');s.className='ic';s.innerHTML=ICON[n];return s}
function h(tag,cls,kids){const e=document.createElement(tag);if(cls)e.className=cls;for(const k of [].concat(kids??[]))if(k!=null&&k!==false&&k!=='')e.append(k);return e}
$('#logo').append(ic('bowl'));$('#send').append(ic('send'));$('#reset').append(ic('plus'),h('span','lbl','New order'));
for(const[i,t]of[['shield','Allergy-safe, checked every edit'],['wallet','Prices incl. GST & fees'],['clock','Arrives before your deadline']])$('#promises').append(h('span','pill',[ic(i),t]));

let sid=null;try{sid=sessionStorage.getItem('sid')}catch(e){}
const IDEAS=[["🎉","#ffe1cc","Group dinner","Order dinner for six people. Two are vegetarian, one has a nut allergy. Keep the total under ₹2,000 and deliver it by 8 PM."],
 ["🌶️","#ffd9d4","Craving something","suggest me spiccy paneer option"],["🥗","#d8f2e0","Party, all veg","dinner for 8, all veg, under 1200 by 7:30pm"]];
const SUGGEST=IDEAS.map(x=>({label:x[2],msg:x[3]}));
function chipsFor(state){const m={RECOMMENDING:[['Choose A','A',1],['Choose B','B',1],['Choose C','C',1]],EDITING:[['Looks good — confirm','confirm',1]],CONFIRMING:[['Yes, place it','yes',1],['Not yet','no']],ORDERED:[['Start a new order','New order',1]]};
 const v=m[state];return v?v.map(([label,msg,main])=>({label,msg,main})):(log.children.length?SUGGEST:[])}
function drawChips(state){chips.innerHTML='';chipsFor(state).forEach((c,i)=>{const b=h('button',c.main?'main':null,c.label);b.type='button';b.title=c.msg;b.style.animationDelay=i*40+'ms';b.onclick=()=>c.msg==='New order'?reset():send(c.msg);chips.append(b)})}
const STEPS=['Describe','Choose','Customize','Review','Placed'],IDX={GATHERING:0,NEGOTIATING:0,RECOMMENDING:1,EDITING:2,CONFIRMING:3,ORDERED:4};
function drawSteps(state){const i=IDX[state]??0;steps.innerHTML='';STEPS.forEach((s,k)=>steps.append(h('li',k<i?'done':k===i?'on':'',[h('span','n',k<i?ic('check'):String(k+1)),h('span','lbl',s)])))}
for(const[em,bg,t,full]of IDEAS){const e=h('span','em',em);e.style.background=bg;const b=h('button','idea',[e,h('b',null,t),h('span',null,full),h('span','go-arrow',ic('arrow'))]);b.type='button';b.onclick=()=>send(full);$('#ideas').append(b)}

/* ---- looks ---- */
const GRADS=[['#ffab2e','#e8541c'],['#ff8a65','#d63a2f'],['#43c48a','#14825a'],['#f2789f','#c8366b'],['#9b87f5','#5b45c9'],['#45b5dc','#1f74a8'],['#f6c344','#d9822b']];
function hash(s){let x=0;for(const c of s)x=(x*31+c.charCodeAt(0))|0;return Math.abs(x)}
const EMOJI=[[/biryani|hyderabad/i,'🍚'],[/chinese|wok|asian|thai|noodle/i,'🥡'],[/south|udupi|dosa/i,'🥞'],[/pizza|ital/i,'🍕'],[/mughlai|kebab|grill|tandoor/i,'🍢'],[/dessert|sweet|bakery/i,'🍰'],[/burger|american|fast/i,'🍔'],[/cafe|continental|salad|health/i,'🥗'],[/north|punjabi|dhaba|indian|curry/i,'🍛']];
function emojiFor(text){for(const[re,e]of EMOJI)if(re.test(text))return e;return '🍽️'}
function cover(name,cuisine,kids){const [a,b]=GRADS[hash(name)%GRADS.length];const c=h('div','cover',[...kids,h('span','emoji',emojiFor(cuisine+' '+name))]);c.style.background=`linear-gradient(135deg,${a},${b})`;return c}
const num=s=>+String(s||'').replace(/[^\d.]/g,'');

/* ---- turn the rule agent's text reply into structured blocks; anything unrecognised stays text ---- */
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
  if(cur?.t==='bundle'&&(m=ln.match(RE.why))){const w=m[1].split('; ');cur.safe=(w.find(x=>/free of/.test(x))||'').replace(/.*free of /,'').split(', ').filter(Boolean);
   const mm=(w.find(x=>/min before/.test(x))||'').match(/(\d+) min before/);if(mm)cur.margin=+mm[1];cur.why=w.filter(x=>!/free of|^arrives/.test(x));continue}
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

/* ---- Claude's prose next to structured cards: drop the parts the cards already show ---- */
function prose(text,view){const kinds=new Set(view.map(b=>b.t));
 const dishes=[...new Set(view.flatMap(b=>(b.items||[]).map(i=>i.n.toLowerCase())))];
 const listsDishes=ln=>dishes.filter(d=>ln.toLowerCase().includes(d)).length>=2;  // the card already lists them
 const paras=text.split(/\n\s*\n/).map(p=>p.split('\n').filter(ln=>{
  if(listsDishes(ln))return false;
  if(kinds.has('receipt')&&(/^\s*[-•*]\s.*(?:x\s?\d|\d\s?×|×\s?\d).*₹/i.test(ln)||/^\s*Subtotal\b/i.test(ln)))return false;
  return true}).join('\n')).filter(p=>p.trim()&&!(kinds.has('bundle')&&/^\s*(?:\*\*)?[A-E](?:\)|\.|:|\s[—–-])\s/.test(p))&&!(kinds.has('placed')&&/^\s*(?:\*\*)?Order placed/i.test(p)));
 return paras.length?[{t:'text',lines:paras.join('\n\n').split('\n')}]:[]}
function inline(s){const out=[];s.split(/(\*\*[^*]+\*\*)/).forEach(part=>{if(/^\*\*[^*]+\*\*$/.test(part))out.push(h('strong',null,part.slice(2,-2)));else if(part)out.push(part)});return out}
function textBlock(lines){const box=h('div','bubble');let ul=null;
 for(const ln of lines){const m=ln.match(/^\s*[-•*]\s+(.*)$/);
  if(m){if(!ul){ul=h('ul');box.append(ul)}ul.append(h('li',null,inline(m[1])));continue}
  ul=null;if(ln.trim())box.append(h('p',null,inline(ln)))}
 return box}

const DIET={veg:'veg',egg:'egg','non-veg':'nv'};
function mini(icon,msg,title,cls='',fn=null){const b=h('button','mini live '+cls,ic(icon));b.type='button';b.title=title;b.setAttribute('aria-label',title);b.onclick=()=>fn?fn(b):send(msg);return b}
function items(list,edit,ctx=null){return h('ul','items'+(edit?' edit':''),list.map(it=>{const q=+it.q,n=it.n;
 const to=k=>ctx?.id?b=>editQty(ctx,n,k,b):null;  // card with a bundle id: instant engine edit, else a chat message
 const qty=edit?h('span','stepper',[mini('minus',q>1?`make ${n} to ${q-1}`:`remove ${n}`,q>1?'One less':'Remove','',to(q-1)),h('span','qv',q),mini('plus',`make ${n} to ${q+1}`,'One more','',to(q+1))]):h('span','q',q+'×');
 const li=h('li',null,[h('i','diet '+(DIET[it.d]||'none')),edit?null:qty,h('span','n',n),edit?qty:null,h('span','p',it.p),edit&&mini('trash',`remove ${n}`,'Remove '+n,'rm',to(0))]);if(it.d)li.title=it.d;return li}))}
function act(label,msg,cls,icon){const b=h('button','btn live '+cls,[label,icon&&ic(icon)]);b.type='button';b.onclick=()=>send(msg);return b}
function bundleCard(b,badges=[]){
 const cv=cover(b.name,b.cuisine||'',[b.letter?h('span','letter',b.letter):null,h('div','badges',badges.map(x=>h('span','badge-s',x)))]);
 const ttl=h('div','ttl-row',[h('div',null,[h('h3',null,b.name),b.cuisine&&h('p','cuisine',b.cuisine)]),b.label&&h('span','tag',b.label)]);
 const chipsRow=h('div','meta',[h('span','price',[b.total,h('small',null,'incl. GST & fees')]),b.eta&&h('span','chip eta',[ic('clock'),'~'+b.eta])]);
 const extra=h('div','meta',[b.safe?.length&&h('span','chip safe',[ic('shield'),'Free of '+b.safe.join(', ')]),b.margin!=null&&h('span','chip '+(b.margin<15?'warn':'eta'),b.margin+' min early')]);
 const inner=h('div','inner',[ttl,chipsRow,extra.children.length?extra:null,items(b.items,!!(b.label||b.id),b.id?{id:b.id,letter:b.letter}:null)]);
 if(b.why?.length)inner.append(h('ul','why',b.why.map(w=>h('li',null,[ic('check'),h('span',null,w.charAt(0).toUpperCase()+w.slice(1))]))));
 if(b.out?.length){const d=h('details','out',[h('summary',null,[ic('shield'),`${b.out.length} dish${b.out.length>1?'es':''} left out for safety`]),h('ul',null,b.out.map(x=>h('li',null,x)))]);inner.append(d)}
 if(b.letter&&!b.label)inner.append(act('Choose '+b.letter,b.letter,'primary pick','arrow'));
 if(b.label)inner.append(h('p','hint live-hint','Use − / + to change quantities, or the bin to remove a dish.'),act(b.letter?`Confirm ${b.letter}`:'Looks good — confirm',b.letter?`confirm ${b.letter}`:'confirm','primary pick','arrow'));
 return h('article','card',[cv,inner])}
function receipt(r){
 const sums=[['Subtotal',r.sub],['GST',r.gst],['Delivery & packaging',r.fees]].filter(x=>x[1]).map(([k,v])=>h('div','row',[h('span',null,k),h('span',null,v)]));
 const inner=h('div','inner',[h('div','receipt-head',[h('span','ico',emojiFor(r.name)),h('div',null,[h('p','eyebrow','Final check'),h('h3',null,r.name)])]),
  items(r.items,true,r.id?{id:r.id}:null),sums.length&&h('div','sums',[...sums,r.total&&h('div','row total',[h('span',null,'Total'),h('span',null,r.total)])]),
  h('div','meta',[r.eta&&h('span','chip eta',[ic('clock'),'Arrives ~'+r.eta]),r.pay&&h('span','paywith',[ic('wallet'),'Pay with '+r.pay])]),
  r.note&&h('p','note',[ic('alert'),h('span',null,[h('strong',null,'Note to restaurant: '),r.note])]),
  r.ask&&h('p','ask',r.ask.replace(/\s*\(yes \/ no\)\s*$/,'')),
  h('div','actions',[act('Place order','yes','go','check'),act('Make changes','no','ghost')])]);
 return h('div','rc',[h('article','card receipt',inner),h('div','zig')])}
function placed(b){
 const conf=h('div','confetti');const cols=['#ffab2e','#e8541c','#43c48a','#f2789f','#45b5dc','#f6c344'];
 for(let i=0;i<28;i++){const p=h('i');p.style.left=Math.random()*100+'%';p.style.background=cols[i%cols.length];p.style.animationDelay=Math.random()*.6+'s';p.style.transform=`rotate(${Math.random()*180}deg)`;conf.append(p)}
 const track=h('ol','track',[['✓','Confirmed'],['👩‍🍳','Preparing'],['🛵','On the way'],['🏠',b.eta?'~'+b.eta:'Delivered']].map(([d,l],i)=>h('li',i===0?'now':'',[h('span','dot',d),l])));
 const inner=h('div','inner',[h('div','placed-top',[h('div','tick',ic('check')),h('div',null,[h('h3',null,'Order placed!'),h('p',null,[h('b',null,b.id),` · ${b.total}${b.name?' · '+b.name:''}`])])]),track,
  b.items?.length&&items(b.items,false),
  h('div','meta',[b.eta&&h('span','chip eta',[ic('clock'),'Arriving ~'+b.eta]),b.pay&&h('span','paywith',[ic('wallet'),'Paid with '+b.pay])]),
  b.note&&h('p','note',[ic('alert'),h('span',null,b.note)])]);
 return h('article','card placed',[conf,inner])}
function declined(b){
 return h('article','card declined',h('div','inner',[h('div','head',[h('span','ico',ic('x')),h('div',null,[h('h3',null,'Payment didn’t go through'),h('p','cuisine',b.held?`Your cart is held until ${b.held}.`:'Your cart is kept.')])]),
  b.others?.length&&h('div','actions',b.others.map(m=>act('Pay with '+m,'yes, pay with '+m,'primary','arrow')))]))}
function misses(b){
 return h('article','card misses',h('div','inner',[h('div',null,[h('p','eyebrow','Nothing fits everything'),h('h3',null,'Closest options')]),
  ...b.rows.map(r=>h('div','rowm',[ic('alert'),h('div',null,[h('b',null,r.name),h('span',null,r.why)]),r.total&&h('span','t',r.total)]))]))}
function render(blocks){const body=h('div','body');let grid=null;
 const bs=blocks.filter(b=>b.t==='bundle'&&b.letter);
 const cheapest=bs.length>1?bs.reduce((a,b)=>num(b.total)<num(a.total)?b:a):null;
 const fastest=bs.length>1&&bs.every(b=>b.eta)?bs.reduce((a,b)=>b.eta<a.eta?b:a):null;
 for(const b of blocks){
  if(b.t==='bundle'&&b.letter){if(!grid){grid=h('div','bundles');body.append(grid)}
   grid.append(bundleCard(b,[b===cheapest&&'💰 Best price',b===fastest&&'⚡ Fastest'].filter(Boolean)));continue}
  grid=null;
  if(b.t==='bundle')body.append(bundleCard(b));
  else if(b.t==='receipt')body.append(receipt(b));
  else if(b.t==='look')body.append(h('div','look',[h('span','eyebrow','Looking for'),...b.parts.map(p=>h('span','pill',p))]));
  else if(b.t==='placed')body.append(placed(b));
  else if(b.t==='declined')body.append(declined(b));
  else if(b.t==='misses')body.append(misses(b));
  else body.append(textBlock(b.lines))}
 return body}

function botRow(content,cls=''){return h('div','msg bot '+cls,[h('div','av',ic('bowl')),content])}
function push(row,block){hero.hidden=true;log.append(row);row.scrollIntoView({behavior:'smooth',block})}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});if(!r.ok){const t=await r.text();let m=t;try{m=JSON.parse(t).error||t}catch(e){}throw new Error(m)}return r.json()}
function cardNote(card,msg){card.querySelector('.cnote')?.remove();const inner=card.querySelector('.inner');if(!inner)return;
 const n=h('p','cnote',[ic('alert'),h('span',null,msg)]);const btn=inner.querySelector('.hint,.pick,.actions');btn?inner.insertBefore(n,btn):inner.append(n)}
async function editQty(ctx,name,qty,btn){if(busy)return;const card=btn.closest('.rc')||btn.closest('.card');busy=true;card.classList.add('pending');
 try{const r=await post('/api/edit',{session_id:sid,bundle_id:ctx.id,item:name,qty});
  if(r.ok&&r.view?.length){const nb=r.view[0];if(ctx.letter)nb.letter=ctx.letter;const fresh=bundleCard(nb);fresh.classList.add('flash');card.replaceWith(fresh);
   if(r.warnings?.length)cardNote(fresh,r.warnings.join(' '))}
  else cardNote(card,r.message||'That change could not be made.');
  drawSteps(r.state);drawChips(r.state)}
 catch(e){cardNote(card,e.message)}
 finally{busy=false;card.classList.remove('pending')}}
/* one chat turn over /api/chat/stream: cards arrive as soon as the engine has them, the reply after */
async function chat(text,onView){const r=await fetch('/api/chat/stream',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({session_id:sid,message:text})});
 if(!r.ok||!r.body){const t=await r.text();let m=t;try{m=JSON.parse(t).error||t}catch(e){}throw new Error(m)}
 const rd=r.body.getReader(),dec=new TextDecoder();let buf='',done=null;
 for(;;){const{value,done:end}=await rd.read();if(value)buf+=dec.decode(value,{stream:true});let i;
  while((i=buf.indexOf('\n'))>=0){const ln=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!ln)continue;const m=JSON.parse(ln);if(m.type==='view')onView(m);else if(m.type==='done')done=m}
  if(end)break}
 if(!done)throw new Error('the connection closed before the reply arrived');return done}
const STATUS=['Reading your request','Checking allergens','Comparing restaurants','Pricing bundles','Checking delivery times'];
let busy=false;
async function send(text){text=text.trim();if(!text||busy)return;busy=true;$('#send').disabled=true;
 document.querySelectorAll('.live').forEach(b=>{b.disabled=true;b.classList.remove('live')});document.querySelectorAll('.live-hint').forEach(e=>e.remove());
 push(h('div','msg me',h('div','bubble',text)),'end');q.value='';chips.innerHTML='';
 const label=h('span',null,STATUS[0]+'…');const wait=botRow(h('div','typing',[h('span','dots',[h('i'),h('i'),h('i')]),label]));push(wait,'end');
 let k=0;const tick=setInterval(()=>{k=(k+1)%STATUS.length;label.style.opacity=0;setTimeout(()=>{label.textContent=STATUS[k]+'…';label.style.opacity=1},250)},1800);
 let early=null;
 const onView=v=>{if(!v.view?.length)return;sid=v.session_id;const body=render(v.view);body.append(h('div','typing',[h('span','dots',[h('i'),h('i'),h('i')]),h('span',null,'Writing a reply…')]));
  const row=botRow(body);(early||wait).replaceWith(row);if(!early)row.scrollIntoView({behavior:'smooth',block:'start'});early=row;drawSteps(v.state)};
 try{const r=await chat(text,onView);sid=r.session_id;try{sessionStorage.setItem('sid',sid)}catch(e){}
  const blocks=r.view?.length?[...r.view.filter(b=>b.t==='look'),...prose(r.reply,r.view),...r.view.filter(b=>b.t!=='look')]:parse(r.reply);
  const row=botRow(render(blocks));(early||wait).replaceWith(row);if(!early)row.scrollIntoView({behavior:'smooth',block:'start'});drawSteps(r.state);drawChips(r.state)}
 catch(e){(early||wait).replaceWith(botRow(h('div','body',h('div','bubble','Something went wrong: '+e.message)),'err'));drawChips()}
 finally{clearInterval(tick);busy=false;$('#send').disabled=false;q.focus()}}
async function reset(){if(sid)await post('/api/reset',{session_id:sid}).catch(()=>{});sid=null;try{sessionStorage.removeItem('sid')}catch(e){}
 log.innerHTML='';hero.hidden=false;drawSteps('GATHERING');drawChips('GATHERING');window.scrollTo({top:0,behavior:'smooth'});q.focus()}
$('#f').onsubmit=e=>{e.preventDefault();send(q.value)};$('#reset').onclick=reset;drawSteps('GATHERING');drawChips('GATHERING');
</script></body></html>"""


# ---------------------------------------------------------------- structured cards for the page
def _inr(x) -> str:
    return f"₹{x:,.0f}" if float(x).is_integer() else f"₹{x:,.2f}"


def _items(items: list[dict], price) -> list[dict]:
    diet = {"veg": "veg", "vegan": "veg", "egg": "egg"}
    return [{"q": i["qty"], "n": i["name"], "d": diet.get(i["diet"], "non-veg") if "diet" in i else None, "p": _inr(price(i))}
            for i in items]


def _bundle(b: dict, letter: str | None = None, label: str | None = None) -> dict:
    return {"t": "bundle", "id": b["bundle_id"], "letter": letter, "label": label, "name": b["restaurant"], "cuisine": ", ".join(c.replace("_", " ").title() for c in b["cuisines"]),
            "total": _inr(b["price"]["total_display"]), "eta": b["eta"], "margin": b["deadline_margin_min"],
            "items": _items(b["items"], lambda i: i["line_total"]),
            "why": [w for w in b["reason_text"] if not w.startswith(("arrives", "every dish"))],
            "out": b["left_out_for_safety"], "safe": [a.replace("_", " ") for a in b["coverage"]["allergen_free"]]}


def _look(c: dict, allergens: list[str]) -> dict:
    parts = []
    if c.get("headcount"):
        parts.append(f"{c['headcount']} people")
    veg = sum(g["count"] for g in c.get("groups", []) if g.get("diet") in ("veg", "vegan", "egg"))
    if veg and veg != c.get("headcount"):
        parts.append(f"{veg} veg")
    elif veg:
        parts.append("all veg")
    if allergens:
        parts.append("no " + ", ".join(a.replace("_", " ") for a in allergens))
    if c.get("severe_allergy"):
        parts.append("severe allergy")
    if c.get("budget_inr"):
        parts.append(f"≤ {_inr(c['budget_inr']['max'])}")
    if m := re.search(r"\d{1,2}:\d\d", c.get("deliver_by") or ""):
        parts.append(f"by {m.group()}")
    return {"t": "look", "parts": parts}


def ui_view(result: dict | None) -> list[dict] | None:
    """The turn's last card-worthy tool result as blocks the page draws (the reply text stays alongside)."""
    tool = (result or {}).get("tool")
    if tool == "recommend_bundles":
        bundles = result["bundles"]
        allergens = bundles[0]["coverage"]["allergen_free"] if bundles else sorted(
            {a for g in result.get("constraints_used", {}).get("groups", []) for a in g.get("allergens", [])})
        blocks = [_look(result.get("constraints_used", {}), allergens)]
        blocks += [_bundle(b, letter) for letter, b in zip("ABCDE", bundles)]
        if not bundles and result.get("near_misses"):
            blocks.append({"t": "misses", "rows": [{"name": m["restaurant"], "why": m["breaks"],
                                                    "total": _inr(m["total"]) if m.get("total") else None}
                                                   for m in result["near_misses"]]})
        return blocks
    if tool == "modify_bundle":
        return [_bundle(result["bundle"], label="Updated" if result["applied"] else "Your pick")]
    if tool == "confirm_cart":
        p = result["price"]
        return [{"t": "receipt", "id": result["bundle_id"], "name": result["restaurant"], "items": _items(result["items"], lambda i: i["line_total"]),
                 "sub": _inr(p["subtotal"]), "gst": _inr(p["gst"]), "fees": _inr(p["delivery"] + p["packaging"]),
                 "total": _inr(p["total"]), "eta": result["eta"], "note": result.get("order_note"),
                 "pay": (result.get("payment_methods") or [None])[0]}]
    if tool == "place_order":
        return [{"t": "placed", "id": result["order_id"], "name": result["restaurant"], "total": _inr(result["total"]),
                 "eta": result["eta"], "pay": result.get("payment"), "note": result.get("note"),
                 "items": _items(result["items"], lambda i: i["price"] * i["qty"])}]
    if tool == "payment_failed":
        return [{"t": "declined", "held": result.get("cart_held_until"), "others": result.get("other_payment_methods", [])}]
    return None


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
            if self.path == "/api/edit":
                return self._edit(data)
            if self.path not in ("/api/chat", "/api/chat/stream"):
                return self._json(404, {"error": "not found"})
            message = str(data.get("message", "")).strip()[:2000]
            if not message:
                return self._json(400, {"error": "message is required"})
            sid, agent = store.get(data.get("session_id"))
            stream = self.path == "/api/chat/stream"
            if stream:  # NDJSON: a "view" line as soon as a card exists, then "done" with the reply
                self.send_response(200)
                self.send_header("content-type", "application/x-ndjson; charset=utf-8")
                self.send_header("cache-control", "no-store")
                self.end_headers()

            def line(obj: dict) -> None:
                try:
                    self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
                    self.wfile.flush()
                except OSError:  # the page went away; the turn still finishes and is logged
                    pass
            on_view = (lambda result: line({"type": "view", "session_id": sid, "state": agent.state,
                                            "view": ui_view(result)})) if stream else None
            with locks.setdefault(sid, threading.Lock()):  # one turn at a time per chat
                reply, row = run_turn(agent, sid, message, on_view)
            if turn_log:
                turn_log.write(row)
            view = ui_view(getattr(agent, "turn_view", None)) if row["tools"] else None  # guardrail-only turns draw no cards
            done = {"session_id": sid, "reply": reply, "state": agent.state, "view": view}
            return line({"type": "done", **done}) if stream else self._json(200, done)

        def _edit(self, data: dict):
            """− / + / bin on a card: one engine edit, no model call. Only the Claude orchestrator has it;
            the page falls back to a chat message for the rule agent."""
            try:
                bundle_id, item, qty = str(data["bundle_id"]), str(data["item"]), int(data["qty"])
            except (KeyError, TypeError, ValueError):
                return self._json(400, {"error": "bundle_id, item and qty are required"})
            sid, agent = store.get(data.get("session_id"))
            if sid != data.get("session_id"):
                store.drop(sid)
                return self._json(409, {"error": "This chat has expired. Start a new order."})
            if not hasattr(agent, "apply_edit"):
                return self._json(400, {"error": "quantity buttons need the Claude orchestrator"})
            with locks.setdefault(sid, threading.Lock()):
                out = agent.apply_edit(bundle_id, item, qty)
            if "error" in out:
                return self._json(200, {"ok": False, "message": out["error"], "state": agent.state})
            message = out.get("message")
            if out.get("breaks"):  # engine refused after applying: say why, not what was attempted
                message = "Can't make that change: " + "; ".join(out["breaks"]) + "."
            return self._json(200, {"ok": out["applied"], "message": message,
                                    "warnings": out.get("warnings", []), "state": agent.state,
                                    "view": ui_view({"tool": "modify_bundle", **out})})

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
    store = SessionStore(lambda: make_agent(local_now(args.now), not args.no_llm, orders, trace, cards=True))
    server = ThreadingHTTPServer((args.host, args.port), build_handler(store, TurnLog(Path("requests.jsonl"), db_url)))
    db.load_catalog(db_url)  # fail at startup, not on the first chat, if the database is missing
    print(f"Food assistant on http://{args.host}:{args.port}  (Ctrl+C to stop)  data: {db_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
