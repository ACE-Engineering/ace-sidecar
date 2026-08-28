"""ace.sidecar.dashboard_render — the local dashboard, rendered server-side.

Rendered in Python, with no client-side script: a template bug raises instead of blanking the
page, and ``curl`` verifies the content. Auto-refresh is a ``<meta http-equiv=refresh>`` for
the same reason.

Design follows the external ACE control plane: left rail, breadcrumb, ``§ NN /`` section
markers, mono labels, mint accent, LIVE badges. Local-only additions: the **sidecar controls**
rail (mode, levers, share) and the **session storage** panel.

Every lever switch renders **disabled** — Phase 0 ships no levers.
"""

from __future__ import annotations

import datetime
import os
from html import escape
from typing import Any, Dict, List, Optional
from urllib.parse import quote

try:
    from ace.branding import FAVICON_LINK
except ImportError:
    FAVICON_LINK = '<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚡</text></svg>">'


REFRESH_SECONDS = 20

_CSS = """
/* Two families, as the control plane uses them:
   SANS  — headings, prose, nav, descriptions (read)
   MONO  — section markers, stat labels, numbers, paths, table data (scanned)
   All-mono reads as a terminal dump, not a dashboard. */
:root{--paper:#0A0B0C;--rail:#08090A;--surface:#101314;--surface-2:#0C0E10;--ink:#F2F4F3;
--ink-2:#A6ADAA;--ink-3:#6C7572;--ink-4:#4A5250;--line:#212627;--line-2:#2E3436;
--mint:#3ECF8E;--accent-2:#5FE3A1;--blue:#3987e5;--gold:#D8A33C;--crit:#E05D45;
--good-bg:#0F231A;--warn-bg:#241D0E;
--mono:ui-monospace,'SF Mono','Cascadia Code','JetBrains Mono',Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);display:flex;min-height:100vh;
font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:inherit;text-decoration:none}
code{font-family:var(--mono);font-size:.92em}

/* ---- left rail ---- */
.rail{width:250px;flex:0 0 250px;background:var(--rail);border-right:1px solid var(--line);
padding:20px 0 32px;position:sticky;top:0;height:100vh;overflow-y:auto}
/* Mark form matches the shipped one (dashboards/cfo.html .brandmark, ace.branding
   FAVICON_SVG, ace-fleet public/favicon.svg):
     - corners SQUARE (border-radius:0);
     - border 1.5px #5b6169 — brighter and thicker than var(--line-2);
     - wordmark MONO 700 at .01em, essentially untracked. Wide tracking (.16em+) turns the
       logo into a status label.
   Size is NOT cfo.html's 30px, which is a masthead size set against a wordmark carrying a
   descriptor on the same line. 22px with a 10px core keeps the shipped ratio (10/22 vs
   14/30) at a weight the two-line rail lockup can hold. One mark per page — the rail
   already says ACE, so no second copy in the breadcrumb. */
.mark{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:0;
border:1.5px solid #5b6169;background:transparent;flex:none}
.mark::after{content:"";width:10px;height:10px;background:var(--mint)}
.brand{padding:0 18px 18px}
.brand .r{display:flex;align-items:center;gap:.6rem}
/* The mark and wordmark lead; the repo link is an afterthought at the far end of the same
   row, so the lockup still reads as one unit rather than two competing marks. Muted to
   --ink-4 -- the weight of the "local sidecar" descriptor under it -- and resolving to full
   ink only on hover: it is a way out of the page, not a thing to look at. */
.brand .r .gh{margin-left:auto;display:grid;place-items:center;width:22px;height:22px;
color:var(--ink-4);flex:none}
.brand .r .gh:hover{color:var(--ink)}
.brand .r .gh svg{width:15px;height:15px;display:block;fill:currentColor}
.brand .n{font-family:var(--mono);color:var(--ink);font-size:.92rem;letter-spacing:.01em;
font-weight:700}
.brand .s{color:var(--ink-4);font-size:11.5px;margin-top:9px}
.rail h4{font-family:var(--mono);color:var(--ink-4);font-size:9.5px;text-transform:uppercase;
letter-spacing:.15em;margin:20px 0 7px;padding:0 18px;font-weight:400}
.rail .item{display:flex;align-items:center;gap:10px;padding:7px 18px;color:var(--ink-2);
font-size:13.5px}
.rail .item:hover{background:#141718;color:var(--ink)}
/* `.on` is only the *initial* highlight, before any click. After that :target decides, via
   the generated block appended to this stylesheet (see _nav_css). */
.rail .item.on{background:#141718;color:var(--ink);box-shadow:inset 2px 0 0 var(--mint)}
.rail .ic{width:14px;text-align:center;opacity:.7;font-size:12px}
.scope{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:0 0 26px;
padding-bottom:18px;border-bottom:1px solid var(--line)}
.scope .lbl{font-family:var(--mono);font-size:9.5px;letter-spacing:.15em;
text-transform:uppercase;color:var(--ink-4);margin-right:4px}
.scope a{font-family:var(--mono);border:1px solid var(--line-2);border-radius:4px;
padding:5px 12px;font-size:11.5px;color:var(--ink-3);background:var(--surface-2);
text-decoration:none;cursor:pointer;display:inline-block;transition:all 0.15s ease}
.scope a:hover{color:var(--ink);border-color:var(--ink-4);background:var(--surface)}
.scope a.on{color:var(--mint);border-color:#1d3b2e;background:#0F231A}
.scope .span{font-family:var(--mono);font-size:11px;color:var(--ink-4);margin-left:6px;
font-variant-numeric:tabular-nums}
.ctl{margin:0 14px 8px;background:var(--surface);border:1px solid var(--line);
border-radius:7px;padding:11px 12px}
.ctl .h{font-family:var(--mono);font-size:9.5px;letter-spacing:.13em;text-transform:uppercase;
color:var(--ink-4);margin-bottom:9px}
.sw{display:flex;align-items:center;justify-content:space-between;padding:4px 0;
font-size:12.5px;color:var(--ink-2);cursor:default}
/* Unshipped controls must read as unavailable, not as broken — otherwise they look like the
   live rows above them and a click that does nothing reads as a bug. */
.sw.dis{color:var(--ink-4);cursor:not-allowed}
.pill{font-family:var(--mono);border:1px solid var(--line-2);border-radius:9px;
padding:1px 8px;font-size:9.5px;color:var(--ink-4);letter-spacing:.06em;
font-variant-numeric:tabular-nums}
.pill.on{color:var(--mint);border-color:#1d3b2e;background:#0F231A}
.pill.soon{color:var(--gold);border-color:#3a2f14}
/* ---- lever headroom, in the rail ----
   Rows are ranked by measured headroom with the dollars attached, so they answer "which of
   these is worth building". The bar is proportional to the largest lever, making the
   ordering legible before the numbers are read. */
.lv{padding:7px 0;border-top:1px solid var(--line)}
.lv.f{border-top:0;padding-top:1px}
.lr{display:flex;align-items:baseline;gap:7px;font-size:12.5px}
.lv .rk{font-family:var(--mono);font-size:9.5px;color:var(--ink-4);min-width:8px}
.lv .ln{color:var(--ink-3);flex:1;min-width:0;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.lv .lu{font-family:var(--mono);font-size:12px;color:var(--mint);
font-variant-numeric:tabular-nums}
/* A lever measured at zero on this corpus must not be painted as a saving. */
.lv .lu.z{color:var(--ink-4)}
.lb{height:3px;background:var(--surface-2);border-radius:2px;margin:6px 0 4px;
overflow:hidden}
.lb i{display:block;height:100%;background:var(--mint);border-radius:2px;min-width:1px}
.lb.z i{background:var(--line-2)}
.lm{font-family:var(--mono);font-size:9.5px;color:var(--ink-4);letter-spacing:.04em;
display:flex;justify-content:space-between;gap:6px;font-variant-numeric:tabular-nums}
.lm .HIGH{color:var(--crit)}.lm .MEDIUM{color:var(--gold)}.lm .LOW{color:var(--ink-3)}
.lm .NONE{color:var(--mint)}
.btn{display:block;text-align:center;border:1px solid var(--line-2);border-radius:5px;
padding:7px;font-size:12px;color:var(--ink-2);margin-top:8px;background:var(--surface-2);
cursor:pointer}
.btn:hover{border-color:#2d3435;color:var(--ink);background:var(--surface)}
.note-s{color:var(--ink-4);font-size:11.5px;margin-top:7px;line-height:1.45}
/* Caveat text under a figure. Sized to be read: qualifications on an upper bound are part
   of the number. */
.note{color:var(--ink-3);font-size:12px;margin-top:9px;line-height:1.5;max-width:78ch}
.note code{color:var(--ink-2)}.note b{color:var(--ink-2);font-weight:600}
.up{margin:16px 14px 0;background:linear-gradient(180deg,#0F231A,#0A0C0D);
border:1px solid #1d3b2e;border-radius:7px;padding:13px}
.up .t{color:var(--mint);font-size:13px;margin-bottom:6px;font-weight:600}
.up p{margin:0 0 10px;color:var(--ink-3);font-size:11.5px;line-height:1.5}
.up .cta{display:block;text-align:center;background:var(--mint);color:#04120B;
border-radius:5px;padding:7px;font-size:12px;font-weight:600}

/* ---- main ---- */
.main{flex:1;min-width:0}
.top{display:flex;align-items:center;justify-content:space-between;padding:13px 28px;
border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px;
color:var(--ink-3);letter-spacing:.11em;text-transform:uppercase}
.top b{color:var(--ink-2);font-weight:400}
.top .w{color:var(--ink);letter-spacing:.01em;font-weight:700}
.top .p{text-transform:none;letter-spacing:0;color:var(--ink-4)}
.wrap{padding:24px 28px 72px}
h1{font-size:clamp(1.15rem,2.1vw,1.5rem);margin:0 0 5px;letter-spacing:-.025em;
font-weight:700;text-wrap:balance}
.lede{color:var(--ink-3);font-size:13px;margin-bottom:22px}
.sec{font-family:var(--mono);color:var(--ink-3);font-size:.63rem;letter-spacing:.16em;
text-transform:uppercase;margin:30px 0 7px;font-weight:500;scroll-margin-top:18px}
.hd{font-size:clamp(.95rem,1.7vw,1.12rem);margin:0 0 13px;display:flex;align-items:center;
justify-content:space-between;font-weight:700;letter-spacing:-.025em}
.hd .g{color:var(--ink-3);font-weight:400}
.live{font-family:var(--mono);border:1px solid var(--line-2);border-radius:10px;padding:2px 10px;
font-size:9.5px;color:var(--ink-2);letter-spacing:.11em;font-weight:400}
.live i{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--mint);margin-right:6px}
.live.calc i{background:var(--blue)}
.grid{display:grid;gap:0;grid-template-columns:repeat(auto-fit,minmax(232px,1fr));
border:1px solid var(--line);border-radius:3px;background:var(--surface-2);overflow:hidden}
.st{border-right:1px solid var(--line);background:transparent;padding:15px 20px 17px}
.st:last-child{border-right:0}
.st .k{font-family:var(--mono);color:var(--ink-3);font-size:10px;letter-spacing:.13em;
text-transform:uppercase;font-weight:500}
/* The hero figure is SANS BOLD with tight tracking — a display number, not a code token.
   Must not drift to mono (cfo.html sets .stat-big to mono; the current control plane does
   not, and mono reads wider and lighter here), to regular weight, or oversize: the widest
   value ("$30,829.59", 10 glyphs) has to clear a 232px tile, capping this near 1.75rem. */
.st .v{font-family:var(--sans);font-weight:700;font-size:clamp(1.4rem,2.3vw,1.75rem);
line-height:1.05;margin:11px 0 8px;letter-spacing:-.03em;
font-variant-numeric:tabular-nums;color:var(--ink)}
.st .v.mint{color:var(--mint)}.st .v.gold{color:var(--gold)}.st .v.crit{color:var(--crit)}
.st .n{font-family:var(--mono);color:var(--ink-3);font-size:11px;letter-spacing:.01em}
.st .n .d{color:var(--mint)}
.st .n .d.warn{color:var(--gold)}.st .n .d.crit{color:var(--crit)}
/* A tile whose figure carries its own arithmetic. The dotted rule under the label is the
   only affordance a native title= tooltip can advertise, and costs no JS. */
.st.calc .k{border-bottom:1px dotted var(--line-2);padding-bottom:3px;cursor:help}
.src{font-family:var(--mono);font-size:10.5px;color:var(--ink-4)}
.src a{color:var(--ink-3);border-bottom:1px solid var(--line-2)}
.src a:hover{color:var(--mint);border-bottom-color:#1d3b2e}
.calcbox{font-family:var(--mono);font-size:11px;color:var(--ink-3);background:#0F1213;
border:1px solid var(--line);border-radius:3px;padding:10px 12px;margin-top:11px;
line-height:1.7;overflow-x:auto}
.calcbox b{color:var(--ink-2);font-weight:600}
.calcbox .cm{color:var(--ink-4)}
.pan{border:1px solid var(--line);background:var(--surface-2);border-radius:3px;margin-top:11px}
.pan .ph{display:flex;align-items:center;justify-content:space-between;padding:10px 15px;
border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11.5px;color:var(--ink-2)}
.pan .pb{padding:13px 15px}
.pan .pb .exp{color:var(--ink-3);font-size:12.5px;margin-top:10px;line-height:1.5}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{font-family:var(--mono);text-align:left;color:var(--ink-4);font-weight:400;padding:7px 8px;
font-size:9.5px;text-transform:uppercase;letter-spacing:.11em;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid #101314;color:var(--ink-2)}
td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;
letter-spacing:-.01em}
th.num{text-align:right}
td.m{font-family:var(--mono);font-size:11.5px}
td b{color:var(--ink);font-weight:600}
tr.hi td{background:#0C1512}
.bar{display:flex;align-items:center;gap:10px;margin-bottom:3px}
.bar .l{width:158px;font-family:var(--mono);font-size:11.5px;color:var(--ink-2)}
.bar .t{flex:1;height:9px;background:#141718;border-radius:2px;overflow:hidden}
.bar .t>i{display:block;height:100%}
.bar .v{width:140px;text-align:right;font-family:var(--mono);font-size:11.5px;
color:var(--ink-3);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.rec{border:1px solid var(--line);border-left:2px solid var(--mint);background:var(--surface-2);
border-radius:3px;padding:13px 16px;margin-bottom:9px}
.rec.HIGH{border-left-color:var(--crit)}.rec.MEDIUM{border-left-color:var(--gold)}
.rec h3{margin:0 0 6px;font-size:14.5px;color:var(--ink);font-weight:600}
.rec p{margin:0 0 10px;color:var(--ink-3);font-size:13px;line-height:1.55}
.tags{display:flex;gap:6px;flex-wrap:wrap;font-size:10.5px;font-family:var(--mono)}
.tg{border:1px solid var(--line-2);border-radius:3px;padding:2px 8px;color:var(--ink-4)}
.tg.s{color:var(--mint);border-color:#1d3b2e}
.tg.HIGH{color:var(--crit)}.tg.MEDIUM{color:var(--gold)}.tg.NONE{color:var(--mint)}
.snip{color:var(--ink-4);font-size:11.5px;max-width:330px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
/* Activity chart. Inline SVG scaled by the container: a strict CSP blocks external chart
   libraries, and the page is server-rendered. Colours come from the shared variables. */
.chart{display:block;width:100%;height:auto}
.chart text{font-family:var(--mono);font-size:9.5px;fill:var(--ink-4)}
.chart .ax{stroke:var(--line-2);stroke-width:1}
.lg{display:flex;gap:16px;flex-wrap:wrap;font-family:var(--mono);font-size:10.5px;
color:var(--ink-4);margin-top:10px}
.lg i{display:inline-block;width:8px;height:8px;margin-right:6px;vertical-align:-1px}
/* ---- Q&A (§ 09). Native <details>, not a JS tab strip: the page ships no script, and a
   disclosure widget is keyboard-reachable, in-page-searchable when open, and printable. */
.qa{border:1px solid var(--line);background:var(--surface-2);border-radius:3px;margin-top:9px}
.qa>summary{list-style:none;cursor:pointer;padding:13px 16px;font-size:13.5px;font-weight:600;
color:var(--ink);display:flex;gap:11px;align-items:baseline}
.qa>summary::-webkit-details-marker{display:none}
.qa>summary::before{content:"+";font-family:var(--mono);color:var(--mint);font-weight:400}
.qa[open]>summary::before{content:"\2013"}
.qa>summary:hover{background:#141718}
.qa[open]>summary{border-bottom:1px solid var(--line)}
.qa .a{padding:14px 16px 16px 39px;color:var(--ink-2);font-size:13px;line-height:1.62}
.qa .a p{margin:0 0 11px}
.qa .a p:last-child{margin:0}
.qa .a b{color:var(--ink);font-weight:600}
.qa .a code{background:#0F1213;border:1px solid var(--line);border-radius:3px;padding:1px 5px}
.qa .a table{margin:4px 0 11px}
/* The caveat that travels with a simulated number. Set apart from the answer prose so it is
   not read as part of the finding, and kept legible — an unreadable assumption is not
   disclosed. */
.qa .a p.lim{border-left:2px solid var(--line-2);padding:2px 0 2px 11px;color:var(--ink-3);
font-size:12px}
.qa .a p.lim b{color:var(--ink-2)}
/* ---- § 10 about / contact. Cards rather than a paragraph: what ACE is, how to reach the
   team, and how to share it are independent errands. Same border/surface vocabulary as .pan
   so it reads as part of the page, not an ad. */
.tag{border:1px solid rgba(29,59,46,0.85);border-radius:8px;padding:22px 24px;margin-top:14px;
background:radial-gradient(circle at 10% 20%, rgba(15,35,26,0.7) 0%, rgba(10,12,13,0.95) 90%);
box-shadow:0 8px 32px -8px rgba(0,0,0,0.5), inset 0 1px 0 rgba(0,230,153,0.18);position:relative;overflow:hidden}
.tag::before{content:"";position:absolute;top:0;left:0;right:0;height:1px;
background:linear-gradient(90deg,transparent,rgba(0,230,153,0.45),transparent)}
.tag .t{font-size:clamp(1.05rem,2vw,1.3rem);font-weight:700;letter-spacing:-.02em;
background:linear-gradient(135deg,#ffffff 0%,var(--mint) 100%);-webkit-background-clip:text;
-webkit-text-fill-color:transparent;text-wrap:balance}
.tag p{margin:10px 0 0;color:var(--ink-2);font-size:13.5px;line-height:1.65;max-width:76ch}
.cards{display:grid;gap:14px;margin-top:14px;
grid-template-columns:repeat(auto-fit,minmax(270px,1fr))}
.card{border:1px solid rgba(255,255,255,0.08);background:rgba(18,22,23,0.75);backdrop-filter:blur(12px);
border-radius:8px;padding:20px 22px 22px;display:flex;flex-direction:column;position:relative;
transition:all .25s cubic-bezier(0.16,1,0.3,1);box-shadow:0 4px 16px rgba(0,0,0,0.3)}
.card:hover{border-color:rgba(0,230,153,0.4);transform:translateY(-3px);
box-shadow:0 12px 28px -6px rgba(0,230,153,0.15),0 4px 16px rgba(0,0,0,0.4)}
.card .k{font-family:var(--mono);color:var(--ink-4);font-size:9.5px;letter-spacing:.13em;
text-transform:uppercase;margin-bottom:10px}
.card h3{margin:4px 0 8px;font-size:14.5px;color:var(--ink);font-weight:600;letter-spacing:-.01em}
.card p{margin:0 0 16px;color:var(--ink-3);font-size:13px;line-height:1.6;flex:1}
/* Spacing between glyph and label is `gap`, not a margin on the arrow: the arrow slides on
   hover, and a transformed element with a margin drags the gap along with it. break-all is
   deliberately not used — it would split "acefleet.dev" mid-word — but the mail buttons still
   have to survive a long address in a 270px card, so wrapping is allowed anywhere only once a
   word genuinely cannot fit. */
.lk{align-self:flex-start;display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);
font-size:12px;line-height:1.2;color:var(--mint);background:rgba(0,230,153,0.06);
border:1px solid rgba(0,230,153,0.25);border-radius:7px;padding:9px 14px;text-decoration:none;
overflow-wrap:anywhere;
transition:background .2s ease,border-color .2s ease,color .2s ease,box-shadow .2s ease,transform .2s ease}
.lk:hover{background:rgba(0,230,153,0.16);border-color:var(--mint);color:#ffffff;
box-shadow:0 0 12px rgba(0,230,153,0.25);transform:translateY(-1px)}
.lk:focus-visible{outline:2px solid var(--mint);outline-offset:2px}
.lk:active{transform:translateY(0)}
.lk .arw{transition:transform .2s ease}
.lk:hover .arw{transform:translateX(3px)}
/* The one outbound link on the page, so it carries more weight than the mail buttons. */
.site-lk{background:linear-gradient(135deg,rgba(0,230,153,0.22) 0%,rgba(0,230,153,0.08) 100%);
border-color:rgba(0,230,153,0.55);color:#EAFFF6;font-weight:600;font-size:12.5px;padding:11px 18px;
box-shadow:0 2px 10px -2px rgba(0,230,153,0.22)}
.site-lk:hover{background:linear-gradient(135deg,rgba(0,230,153,0.34) 0%,rgba(0,230,153,0.16) 100%);
box-shadow:0 6px 18px -4px rgba(0,230,153,0.35)}
@media (prefers-reduced-motion:reduce){
.lk,.lk .arw{transition:none}
.lk:hover{transform:none}
.lk:hover .arw{transform:none}}
.foot{color:var(--ink-4);font-size:11.5px;margin-top:36px;padding-top:14px;
border-top:1px solid var(--line);line-height:1.65}
/* ---- modal drawer ---- */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.75);backdrop-filter:blur(6px);z-index:9999;justify-content:center;align-items:center}
.modal-content{background:#121516;border:1px solid rgba(255,255,255,0.12);border-radius:10px;width:92%;max-width:820px;max-height:88vh;overflow-y:auto;padding:24px;color:var(--ink);box-shadow:0 24px 48px rgba(0,0,0,0.6)}
.modal-header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px}
.modal-header h2{margin:0;font-size:17px;color:var(--ink);font-weight:600;display:flex;align-items:center;gap:8px}
.modal-close{background:transparent;border:none;color:var(--ink-3);font-size:22px;cursor:pointer;padding:2px 8px;line-height:1}
.modal-close:hover{color:var(--mint)}
.metrics-box{background:#0a0c0d;border:1px solid #1e2325;border-radius:6px;padding:14px;margin-bottom:16px;font-family:var(--mono);font-size:12.5px;color:var(--mint);white-space:pre-wrap;word-break:break-all;max-height:260px;overflow-y:auto}
.tab-btn{background:#161a1d;border:1px solid var(--line-2);color:var(--ink-2);padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;transition:all 0.15s ease}
.tab-btn:hover{color:var(--ink);border-color:var(--line-3)}
.tab-btn.on{background:var(--mint);color:#04120B;border-color:var(--mint)}
.promql-tag{background:#0a0c0d;border:1px solid #1e2325;border-radius:4px;padding:4px 8px;font-family:var(--mono);font-size:11.5px;color:#a3e635;display:inline-block;}
"""



def _f(n: Any, d: int = 0) -> str:
    try:
        return f"{float(n):,.{d}f}"
    except Exception:
        return "—"


def _usd(n: Any) -> str:
    try:
        return f"${float(n):,.2f}"
    except Exception:
        return "—"


def _pct(n: Any, d: int = 1) -> str:
    try:
        return f"{float(n) * 100:.{d}f}%"
    except Exception:
        return "—"


def _kb(n: int) -> str:
    return f"{n/1024:,.0f} KB" if n < 1_048_576 else f"{n/1_048_576:,.1f} MB"


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    s = max(0, datetime.datetime.now().timestamp() - ts)
    for lim, div, u in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h")):
        if s < lim:
            return f"{s/div:.0f}{u} ago"
    return f"{s/86400:.0f}d ago"


def _st(
    k: str,
    v: str,
    note: str = "",
    cls: str = "",
    delta: str = "",
    dcls: str = "",
    title: str = "",
) -> str:
    """One stat tile: mono label, SANS-bold figure, mono delta + meta beneath.

    ``delta`` is the coloured leading fragment ("+23.4%", "98.0%"); ``note`` is the muted
    qualifier after it. Splitting them is what gives the reference strip its two-tone
    footer rather than one flat grey line.

    ``title`` carries the arithmetic behind a figure as a native tooltip. A dollar number
    stated to the cent with no way to see how it was reached asks to be trusted rather than
    checked; the formula belongs on the number, not in a footnote three sections away.
    """
    foot = ""
    if delta or note:
        d = f"<span class='d {dcls}'>{escape(delta)}</span>" if delta else ""
        sep = " · " if (delta and note) else ""
        foot = f"<div class='n'>{d}{escape(sep + note) if note else ''}</div>"
    tip = f" title='{escape(title)}'" if title else ""
    hint = " calc" if title else ""
    return (
        f"<div class='st{hint}'{tip}><div class='k'>{escape(k)}</div>"
        f"<div class='v {cls}'>{v}</div>{foot}</div>"
    )


def _span_caption(d: Dict[str, Any]) -> str:
    """The dates behind the scope button, so "30d" is never taken on faith.

    A window label is a promise about a sample; the caption is the sample. When the two
    disagree — a 30d scope reaching back only 11 days — the caption says so, because every
    per-month figure below is extrapolated from exactly this span.
    """
    sp = d.get("span") or {}
    first, last = sp.get("first_ts"), sp.get("last_ts")
    if not (first and last):
        return "no transcripts in this scope"
    key = d.get("range")
    # Sub-day scopes need the clock; a bare date would read as a whole day of data.
    fmt = "%Y-%m-%d %H:%M" if key in ("24h", "session") else "%Y-%m-%d"
    a = datetime.datetime.fromtimestamp(first).strftime(fmt)
    z = datetime.datetime.fromtimestamp(last).strftime(fmt)
    days = sp.get("days") or 0.0
    if days < 1.0:
        length = f"{days * 24:.1f}h"
    else:
        length = f"{days:.1f} days" if days < 10 else f"{days:.0f} days"
    tail = f" · {length}"
    if sp.get("partial"):
        tail += " of data — the window is not full"
    return f"{a} → {z}{tail}"


def _mask_home(p: Any) -> str:
    """Render a filesystem path with the home directory collapsed to ``~``.

    Every absolute path on this page starts inside the reader's home directory, so printing
    it in full publishes their account name — and, in the session list, the names of private
    repositories alongside it. This dashboard is the thing people screenshot when they want
    to show what their agents cost, which makes an unmasked path a leak with a very short
    path to a public timeline. Masking is applied at render time only; nothing downstream
    reads these strings back as paths.
    """
    if not isinstance(p, str):
        return str(p)
    home = os.path.expanduser("~")
    if not home or home == "/":
        return p
    # Claude Code encodes a project's path into its transcript filename by replacing every
    # separator with a dash, so the home directory shows up there in a form the plain
    # replace above cannot see: /Users/alex -> -Users-alex.
    return p.replace(home, "~").replace(home.replace(os.sep, "-"), "~")


def _sec(num: str, label: str, head: str, tail: str, badge: str = "LIVE") -> str:
    cls = "live" if badge == "LIVE" else "live calc"
    # The id is what the rail nav targets. Keyed off the section number so a section and its
    # nav entry cannot drift apart -- see _rail().
    return (
        f"<div class='sec' id='s{num}'>§ {num} / {escape(label)}</div>"
        f"<div class='hd'><span><b>{escape(head)}</b> <span class='g'>{escape(tail)}</span></span>"
        f"<span class='{cls}'><i></i>{escape(badge)}</span></div>"
    )


def _hours(sec: Any) -> str:
    """Duration at the scale a reader can hold: 42s, 18m, 3.4h.

    Seconds below a minute and a half, minutes below an hour and a half, hours after that.
    A session-time page that reports "153180s" is technically complete and practically
    useless.
    """
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return "—"
    if v < 90:
        return f"{v:.0f}s"
    if v < 5400:
        return f"{v / 60:.0f}m"
    return f"{v / 3600:,.1f}h"


def _parked_alarm(pk: Dict[str, Any]) -> str:
    """The one thing on this page that is about *now* rather than about history.

    Placed above § 01 rather than inside the time section, because an alarm a reader has to
    scroll to is not an alarm. It renders only when something is actually waiting.
    """
    live = (pk or {}).get("live")
    if not live:
        return ""
    tools = ", ".join(live.get("tools") or []) or "a tool call"
    mean = pk.get("mean_s") or 0.0
    ref = f" Your own parked stretches average {_hours(mean)}." if mean else ""
    return (
        f"<div class='rec HIGH'><h3>An agent has been waiting "
        f"{_hours(live.get('since_s'))} for you</h3>"
        f"<p>The most recent session's last turn is still holding "
        f"<code>{escape(tools)}</code> — nothing has run since, so whatever it was doing has "
        f"not progressed.{escape(ref)}</p>"
        f"<div class='tags'><span class='tg HIGH'>parked now</span>"
        f"<span class='tg'>read from transcripts, not the request path</span></div></div>"
    )


def _time(tb: Dict[str, Any], pk: Dict[str, Any]) -> str:
    """Session wall clock, decomposed, with the parked total beside it.

    Every section above this one prices tokens. This one prices the clock, and it leads with
    the idle share because that is the number which decides whether any time-saving lever is
    worth building: a saving quoted against total session time and the same saving quoted
    against active time differ by roughly 9x.
    """
    if not tb.get("available"):
        return (
            "<div class='note'>No transcript timestamps in this scope, so there is no "
            "elapsed time to account for.</div>"
        )
    acc = tb.get("accounted_s") or 0.0
    idle = tb.get("idle_s") or 0.0
    thresh = tb.get("idle_threshold_s") or 300.0
    tiles = [
        _st(
            "wall_clock",
            _hours(acc),
            "elapsed, this scope",
            delta=f"{tb.get('sessions') or 0} sessions",
            title=(
                "Summed gaps between consecutive events in each transcript.\n"
                "Not a sum of session durations: overlapping sessions are counted once "
                "each, so this is time-in-sessions, not calendar time."
            ),
        ),
        _st(
            "active",
            _hours(tb.get("active_s")),
            "everything not idle",
            delta=_pct((tb.get("active_s") or 0.0) / acc if acc else 0.0),
        ),
        _st(
            "idle",
            _hours(idle),
            f"gaps over {_hours(thresh)}",
            delta=_pct(idle / acc if acc else 0.0),
            dcls="warn",
            title=(
                f"A gap longer than {_hours(thresh)} is read as nobody being at the "
                "keyboard rather than somebody deciding. The cut is arguable — both sides "
                "of it are reported, here and in the parked figure."
            ),
        ),
        _st("median_session", _hours(tb.get("median_span_s")), "first to last event"),
    ]
    if pk.get("available"):
        share = pk.get("share_of_idle") or 0.0
        tiles.append(
            _st(
                "parked_on_approval",
                _hours(pk.get("total_s")),
                f"{pk.get('events') or 0} times, {_hours(pk.get('mean_s'))} each",
                delta=f"{_pct(share)} of idle",
                dcls="crit" if share >= 0.2 else "warn",
                title=(
                    "Idle stretches that began with a turn holding a tool call and ended "
                    "when that call finally ran — an agent waiting to be let through.\n"
                    "An UPPER BOUND: a transcript cannot tell a human who would have come "
                    "back sooner from one who left for unrelated reasons."
                ),
            )
        )
    out = ["<div class='grid'>" + "".join(tiles) + "</div>"]

    colors = {
        "idle": "var(--ink-4)",
        "tool execution + approval": "var(--gold)",
        "model thinking after a tool": "var(--blue)",
        "model generating": "var(--mint)",
        "human composing a prompt": "var(--accent-2)",
    }
    bars = []
    for p in tb.get("phases") or []:
        name = p["name"]
        label = f"idle over {_hours(thresh)}" if name == "idle" else name
        bars.append(
            _bar(
                label,
                f"{_hours(p['seconds'])}  {_pct(p['share'])}",
                p["share"],
                colors.get(name, "var(--ink-3)"),
            )
        )
    out.append("<div class='pan'><div class='pb'>" + "".join(bars) + "</div></div>")

    if pk.get("available"):
        tools = pk.get("by_tool") or []
        top = ", ".join(f"{escape(str(n))} ({c})" for n, c in tools[:5]) or "—"
        ref = pk.get("reference") or {}
        out.append(
            "<div class='note'><b>Parked on:</b> "
            + top
            + ".<br>Measured baseline for comparison: "
            + f"{_pct(ref.get('share_of_idle') or 0)} of idle at "
            + f"{_hours(ref.get('mean_s'))} per stretch. "
            + "<b>This is a ceiling, not a saving</b> — the realised figure only exists "
            + "after something actually approves those calls, as a before/after on this "
            + "same number. Nothing in this release does."
            + "</div>"
        )
    out.append(
        "<div class='note'><code>tool execution + approval</code> is deliberately one "
        "bucket. Nothing in a transcript separates the time a tool spent running from the "
        "time it spent waiting to be allowed to run, so splitting it would bill "
        "<code>pytest</code> to an approval prompt."
        "</div>"
    )
    return "".join(out)


def _bar(label: str, val: str, frac: float, color: str) -> str:
    return (
        f"<div class='bar'><span class='l'>{escape(label)}</span>"
        f"<span class='t'><i style='width:{max(0.5, min(100, frac * 100)):.1f}%;"
        f"background:{color}'></i></span>"
        f"<span class='v'>{escape(val)}</span></div>"
    )


def _compact(n: Any) -> str:
    """Axis-scale number: 412M, 6.5M, 85k. Full precision belongs in the tables."""
    try:
        v = float(n)
    except Exception:
        return "—"
    for lim, div, u in ((1e9, 1e9, "B"), (1e6, 1e6, "M"), (1e3, 1e3, "k")):
        if abs(v) >= lim:
            return f"{v/div:,.1f}{u}" if abs(v) < lim * 10 else f"{v/div:,.0f}{u}"
    return f"{v:,.0f}"


def _pctl_row(label: str, p: Dict[str, Any], fmt: Any = _f) -> str:
    return (
        f"<tr><td class='m'>{escape(label)}</td>"
        f"<td class='num'>{fmt(p.get('p25'))}</td>"
        f"<td class='num'><b>{fmt(p.get('p50'))}</b></td>"
        f"<td class='num'>{fmt(p.get('p99'))}</td>"
        f"<td class='num' style='color:var(--ink-4)'>{fmt(p.get('max'))}</td></tr>"
    )


def _activity_svg(daily: List[Dict[str, Any]], commits: bool) -> str:
    """Daily token volume as bars, with commits on a second scale beneath.

    Idle days are drawn as a flat tick rather than a gap: omitting them compresses a
    four-day silence into one step and makes the workload look steadier than it is. The
    commit strip uses a *separate* scale — tokens and commits differ by six orders of
    magnitude, so a shared axis would flatten one to nothing.
    """
    if not daily:
        return "<div class='exp'>No daily activity in this scope.</div>"

    width, top, t_h, c_h = 1000.0, 14.0, 130.0, 24.0
    base = top + t_h
    c_top = base + 14.0
    height = (c_top + c_h + 20.0) if commits else (base + 20.0)

    peak_t = max((r.get("tokens") or 0) for r in daily) or 1
    peak_c = max((r.get("commits") or 0) for r in daily) or 1
    band = width / len(daily)
    bar_w = max(1.0, band * 0.7)

    parts: List[str] = []
    for i, r in enumerate(daily):
        x = i * band + (band - bar_w) / 2.0
        tok = r.get("tokens") or 0
        if tok:
            h = max(1.5, tok / peak_t * t_h)
            fill = "var(--mint)"
        else:
            h, fill = 1.5, "var(--line-2)"
        day = escape(str(r.get("day") or ""))
        title = f"{day} · {_compact(tok)} tokens · {_usd(r.get('cost'))}"
        parts.append(
            f"<rect x='{x:.2f}' y='{base - h:.2f}' width='{bar_w:.2f}' "
            f"height='{h:.2f}' fill='{fill}'><title>{escape(title)}</title></rect>"
        )
        if commits:
            c = r.get("commits") or 0
            ch = max(1.5, c / peak_c * c_h) if c else 1.5
            cfill = "var(--gold)" if c else "var(--line-2)"
            parts.append(
                f"<rect x='{x:.2f}' y='{c_top:.2f}' width='{bar_w:.2f}' "
                f"height='{ch:.2f}' fill='{cfill}'>"
                f"<title>{escape(day)} · {c} commits</title></rect>"
            )

    parts.append(
        f"<line class='ax' x1='0' y1='{base:.2f}' x2='{width:.0f}' y2='{base:.2f}'/>"
    )
    parts.append(f"<text x='0' y='10'>peak {_compact(peak_t)} tok/day</text>")
    parts.append(
        f"<text x='0' y='{height - 6:.2f}'>{escape(str(daily[0].get('day') or ''))}</text>"
    )
    if len(daily) > 1:
        parts.append(
            f"<text x='{width:.0f}' y='{height - 6:.2f}' text-anchor='end'>"
            f"{escape(str(daily[-1].get('day') or ''))}</text>"
        )
    if commits:
        parts.append(
            f"<text x='{width:.0f}' y='{c_top - 4:.2f}' text-anchor='end'>"
            f"commits · peak {peak_c}</text>"
        )

    legend = (
        "<div class='lg'><span><i style='background:var(--mint)'></i>tokens/day</span>"
        + (
            "<span><i style='background:var(--gold)'></i>commits/day</span>"
            if commits
            else ""
        )
        + "<span><i style='background:var(--line-2)'></i>idle day</span></div>"
    )
    return (
        f"<svg class='chart' viewBox='0 0 {width:.0f} {height:.0f}' "
        f"preserveAspectRatio='xMidYMid meet' role='img' "
        f"aria-label='Daily token volume and commits'>{''.join(parts)}</svg>{legend}"
    )


def _quality(qm: Optional[Dict[str, Any]]) -> str:
    """§ 02 — Code quality, verification hygiene, and agent execution reliability."""
    if not qm or not qm.get("available"):
        return (
            "<div class='pan'><div class='ph'><span>~/ace/code_quality</span>"
            "<span class='live calc'><i></i>NO SESSIONS</span></div><div class='pb'>"
            "<div class='exp'>No session data available in this scope to compute code quality metrics. "
            "Metrics will populate as coding agent sessions run and edit workspace files.</div></div></div>"
        )

    score = qm.get("quality_score", 100)
    grade = qm.get("grade", "A")
    v_rate = qm.get("verification_rate_pct", 100.0)
    fsr = qm.get("first_pass_success_rate_pct", 100.0)
    err_rate = qm.get("tool_error_rate_pct", 0.0)
    thrash_cnt = qm.get("thrashed_files_count", 0)
    recovery_turns = qm.get("avg_error_recovery_turns", 1.0)
    redundant_reads = qm.get("redundant_reads_count", 0)
    test_code_ratio = qm.get("test_to_code_ratio", 1.0)
    sessions_edits = qm.get("sessions_with_edits", 0)
    sessions_tests = qm.get("sessions_with_tests", 0)

    score_color = (
        "var(--mint)"
        if score >= 80
        else ("var(--gold)" if score >= 60 else "var(--crit)")
    )
    v_cls = "" if v_rate >= 75 else ("warn" if v_rate >= 50 else "crit")
    fsr_cls = "" if fsr >= 85 else ("warn" if fsr >= 70 else "crit")
    thrash_cls = "" if thrash_cnt == 0 else ("warn" if thrash_cnt <= 2 else "crit")

    tiles = [
        _st(
            "quality_score",
            f"<span style='color:{score_color}'>{score}</span><span style='font-size:0.6em;color:var(--ink-3);margin-left:4px'>/ 100</span>",
            f"Grade {grade}",
            delta="COMPOSITE",
            title="Weighted reliability index across verification hygiene (35%), first-pass tool success (35%), edit stability (15%), and test balance (15%).",
        ),
        _st(
            "verification_rate",
            f"{v_rate}%",
            f"{sessions_tests} of {sessions_edits} edit sessions",
            delta="TEST HYGIENE",
            dcls=v_cls,
            title="Percentage of sessions containing file modifications that executed an automated test runner or linter (pytest, npm test, ruff, etc.).",
        ),
        _st(
            "first_pass_success",
            f"{fsr}%",
            f"{err_rate}% error rate",
            delta="TOOL RELIABILITY",
            dcls=fsr_cls,
            title="Share of tool executions that succeeded on their first attempt without returning execution errors or non-zero exit codes.",
        ),
        _st(
            "edit_thrash_files",
            f"{thrash_cnt}",
            f"{qm.get('total_edits', 0)} total file edits",
            delta="REWORK CHURN",
            dcls=thrash_cls,
            title="Files edited 3 or more times within the same session, indicating thrashing or lack of convergence.",
        ),
        _st(
            "healing_latency",
            f"{recovery_turns} turns",
            "avg turns to recover",
            delta="ERROR HEALING",
            title="Average number of conversation turns required for the agent to resolve a failed tool execution and resume forward progress.",
        ),
        _st(
            "context_waste",
            f"{redundant_reads} reads",
            f"test/code ratio: {test_code_ratio}x",
            delta="REDUNDANCY",
            title="Consecutive duplicate reads of identical files without intervening edits.",
        ),
    ]

    thrashed_files_list = qm.get("thrashed_files_list") or []
    thrash_html = ""
    if thrashed_files_list:
        thrashed_items = "".join(
            f"<li><code>{escape(_mask_home(f))}</code></li>"
            for f in thrashed_files_list
        )
        thrash_html = (
            f"<div style='margin-top:12px;padding:10px 14px;background:var(--warn-bg);border:1px solid #3d3014;border-radius:4px;'>"
            f"<b style='color:var(--gold);font-size:12px;'>⚠️ Repeatedly Modified Files (Thrashing Detected):</b>"
            f"<ul style='margin:6px 0 0 16px;padding:0;font-size:12px;color:var(--ink-2);'>{thrashed_items}</ul>"
            f"</div>"
        )

    breakdown_rows = []
    # Agent breakdown rows
    by_agent = qm.get("by_agent") or {}
    for ak, a_info in by_agent.items():
        a_score = a_info.get("quality_score", 100)
        a_grade = a_info.get("grade", "A")
        a_v_rate = a_info.get("verification_rate_pct", 100.0)
        a_fsr = a_info.get("first_pass_success_rate_pct", 100.0)
        a_thrash = a_info.get("thrashed_files_count", 0)
        a_rec = a_info.get("avg_error_recovery_turns", 1.0)
        a_sess = a_info.get("sessions", 0)
        badge_style = (
            "color:var(--mint);border-color:#1d3b2e;background:#0F231A"
            if ak == "antigravity"
            else "color:var(--blue);border-color:#1e355b;background:#0d1c33"
        )
        score_badge = (
            "color:var(--mint);border-color:#1d3b2e;background:#0F231A"
            if a_score >= 80
            else (
                "color:var(--gold);border-color:#3d3014;background:#241D0E"
                if a_score >= 60
                else "color:var(--crit);border-color:#4a1e17;background:#2a110e"
            )
        )
        breakdown_rows.append(
            f"<tr>"
            f"<td><span class='pill' style='{badge_style};font-weight:600;'>{escape(a_info.get('label', ak))}</span></td>"
            f"<td><span class='pill' style='{score_badge};font-weight:700;'>{a_score} ({a_grade})</span></td>"
            f"<td class='num'><b>{a_v_rate}%</b></td>"
            f"<td class='num'><b>{a_fsr}%</b></td>"
            f"<td class='num'>{'<span style=\"color:var(--gold)\">' + str(a_thrash) + '</span>' if a_thrash > 0 else '0'}</td>"
            f"<td class='num'>{a_rec} turns</td>"
            f"<td class='num' style='color:var(--ink-3);'>{a_sess}</td>"
            f"</tr>"
        )

    # Model breakdown rows
    by_model = qm.get("by_model") or []
    for m_info in by_model:
        m_name = m_info.get("model", "unknown")
        m_score = m_info.get("quality_score", 100)
        m_grade = m_info.get("grade", "A")
        m_v_rate = m_info.get("verification_rate_pct", 100.0)
        m_fsr = m_info.get("first_pass_success_rate_pct", 100.0)
        m_thrash = m_info.get("thrashed_files_count", 0)
        m_rec = m_info.get("avg_error_recovery_turns", 1.0)
        m_sess = m_info.get("sessions", 0)
        score_badge = (
            "color:var(--mint);border-color:#1d3b2e;background:#0F231A"
            if m_score >= 80
            else (
                "color:var(--gold);border-color:#3d3014;background:#241D0E"
                if m_score >= 60
                else "color:var(--crit);border-color:#4a1e17;background:#2a110e"
            )
        )
        breakdown_rows.append(
            f"<tr>"
            f"<td class='m'><code style='color:var(--ink);'>{escape(m_name)}</code></td>"
            f"<td><span class='pill' style='{score_badge};font-weight:700;'>{m_score} ({m_grade})</span></td>"
            f"<td class='num'>{m_v_rate}%</td>"
            f"<td class='num'>{m_fsr}%</td>"
            f"<td class='num'>{'<span style=\"color:var(--gold)\">' + str(m_thrash) + '</span>' if m_thrash > 0 else '0'}</td>"
            f"<td class='num'>{m_rec} turns</td>"
            f"<td class='num' style='color:var(--ink-3);'>{m_sess}</td>"
            f"</tr>"
        )

    matrix_table = ""
    if breakdown_rows:
        matrix_table = (
            f"<div style='margin-top:14px;'>"
            f"<table style='margin-top:6px;'>"
            f"<tr><th>engine / model</th><th>score</th><th class='num'>verification</th><th class='num'>first-pass success</th>"
            f"<th class='num'>thrash files</th><th class='num'>healing turns</th><th class='num'>sessions</th></tr>"
            f"{''.join(breakdown_rows)}"
            f"</table>"
            f"</div>"
        )

    return (
        f"<div class='grid'>{''.join(tiles)}</div>"
        f"<div class='pan' style='margin-top:14px;'>"
        f"<div class='ph'><span>~/ace/quality_breakdown</span><span class='live'><i></i>LOCAL VERIFIED</span></div>"
        f"<div class='pb'>"
        f"<div style='display:flex;gap:20px;flex-wrap:wrap;font-size:13px;color:var(--ink-2);'>"
        f"<div><b style='color:var(--ink);'>{sessions_tests}</b> test-verified sessions</div>"
        f"<div><b style='color:var(--ink);'>{sessions_edits}</b> editing sessions</div>"
        f"<div><b style='color:var(--ink);'>{qm.get('total_tool_calls', 0)}</b> total tool executions</div>"
        f"<div><b style='color:var(--ink);'>{redundant_reads}</b> redundant duplicate file reads</div>"
        f"</div>"
        f"{thrash_html}"
        f"{matrix_table}"
        f"<div class='exp'>Measures how safely and stably coding agents operate in your repository. Correlates cost against first-pass tool correctness and test diligence.</div>"
        f"</div></div>"
    )


def _fleet(f: Optional[Dict[str, Any]]) -> str:
    """§ 01 — the eleven fleet metrics from docs/22 §0, on this machine's transcripts.

    Rendered even when empty: it is a rail destination, and a section that disappears would
    turn "Overview" into a dead link on a fresh install. Same reasoning as § 08.
    """
    head = _sec(
        "01",
        "FLEET METRICS",
        "What the fleet did.",
        "Eleven headline metrics, measured on your transcripts.",
        "LOCAL",
    )
    if not f:
        return head + (
            "<div class='pan'><div class='ph'><span>~/.claude/projects</span>"
            "<span class='live calc'><i></i>NO DATA</span></div><div class='pb'>"
            "<div class='exp'>No transcripts in this scope, so there is nothing to "
            "count. Widen the scope above, or use Claude Code on this machine and "
            "these figures fill in.</div></div></div>"
        )

    tok = f.get("tokens") or {}
    cm = f.get("commits") or {}
    tr = f.get("trend") or {}
    cps = f.get("cost_per_session") or {}
    prompt, out = tok.get("prompt") or 0, tok.get("output") or 0
    has_commits = bool(cm.get("available"))

    b = [head]

    # [1][2][3] — volume.
    b.append(
        "<div class='grid'>"
        + "".join(
            [
                _st(
                    "tokens_in [1]",
                    _f(prompt),
                    "prompt tokens",
                    delta=f"{prompt/out:,.0f}:1 in:out" if out else "",
                ),
                _st("tokens_out [1]", _f(out), "generated by the model"),
                _st(
                    "api_requests [2]",
                    _f(f.get("requests")),
                    "developer sessions",
                    delta=_f(f.get("sessions")),
                ),
                _st(
                    "conversation_turns [3]",
                    _f(f.get("handbacks")),
                    "API requests each",
                    delta=f"{f.get('requests_per_handback') or 0:,.1f}x",
                ),
                _st(
                    "cost_per_turn [3]",
                    _usd(f.get("cost_per_handback")),
                    f"{_f(f.get('interrupted'))} interrupted",
                    title=(
                        "list-price cost in scope / conversation turns\n"
                        "A turn is one return of control to you (stop_reason=end_turn), "
                        "so this is what one instruction cost — across all the API "
                        "requests it took. Rates: see the rate card under § 02."
                    ),
                ),
            ]
        )
        + "</div>"
    )

    # [6][9][10] — economics and yield. The total leads: every per-something dollar here is a
    # slice of it. Repeated from § 02 so the denominators sit next to the rates they divide.
    b.append(
        "<div class='grid'>"
        + "".join(
            [
                _st(
                    "list_price_cost",
                    _usd(f.get("cost")),
                    "this scope",
                    title=(
                        "Every request priced at its own model's published rate, summed:\n"
                        "  fresh/1e6 x input\n"
                        "+ cache_read/1e6 x cache_read rate\n"
                        "+ cache_write_5m/1e6 x input x 1.25\n"
                        "+ cache_write_1h/1e6 x input x 2.0\n"
                        "+ output/1e6 x output rate\n"
                        "Same total as § 02. Rates: see the rate card under § 02."
                    ),
                ),
                _st(
                    "cost_per_session [6]",
                    _usd(cps.get("mean")),
                    "median",
                    delta=_usd(cps.get("p50")),
                ),
                _st(
                    "commits_per_session [9]",
                    f"{cm.get('per_session_mean') or 0:,.1f}" if has_commits else "—",
                    (
                        f"{_f(cm.get('sessions_with_commits'))} of "
                        f"{_f(f.get('sessions'))} sessions committed"
                        if has_commits
                        else "git join unavailable on this machine"
                    ),
                    delta=(f"max {_f(cm.get('max'))}" if has_commits else ""),
                ),
                _st(
                    # Full precision, not _compact: this is the number the analysis quotes,
                    # and "11M" hides the difference between 11.0M and 11.9M.
                    "tokens_per_commit [10]",
                    _f(cm.get("tokens_per_commit")) if has_commits else "—",
                    (
                        f"{_f(cm.get('output_tokens_per_commit'))} of them output"
                        if has_commits
                        else "needs the repositories, not just transcripts"
                    ),
                ),
                _st(
                    "cost_per_commit [10]",
                    _usd(cm.get("cost_per_commit")) if has_commits else "—",
                    (
                        f"{_f(cm.get('attributed'))} of {_f(cm.get('in_window'))} "
                        "commits attributed"
                        if has_commits
                        else "no repositories resolved"
                    ),
                ),
            ]
        )
        + "</div>"
    )

    b.append(
        "<div class='lede' style='margin:11px 0 0'>Per-session figures use "
        f"<b>developer sessions</b> — a main transcript plus the subagents it spawned: "
        f"{_f(f.get('sessions'))} of them across {_f(f.get('transcripts'))} transcript "
        "files. Commit attribution is time-window based, not causal: a commit that landed "
        "inside a session's window was not necessarily produced by that session.</div>"
    )

    # [4][5] — distributions.
    b.append(
        "<div class='pan'><div class='ph'><span>~/ace/percentiles [4] [5]</span>"
        "<span class='live'><i></i>LOCAL</span></div><div class='pb'><table>"
        "<tr><th>distribution</th><th class='num'>p25</th><th class='num'>p50</th>"
        "<th class='num'>p99</th><th class='num'>max</th></tr>"
        + _pctl_row("context per request [4]", f.get("context") or {})
        + _pctl_row("requests per session [5]", f.get("requests_per_session") or {})
        + _pctl_row(
            "cost per session [6]",
            {
                "p25": cps.get("mean"),
                "p50": cps.get("p50"),
                "p99": cps.get("p99"),
                "max": cps.get("max"),
            },
            _usd,
        )
        + "</table><div class='exp'>The p25 column is the point: there is no cheap "
        "quartile of requests, because even a quarter-way-down request carries most of a "
        "session's context. Cost per session reports the <i>mean</i> in the p25 column — "
        "the spread between it and the median is the concentration.</div>"
        "</div></div>"
    )

    # [8] — model mix.
    models = f.get("by_model") or []
    if models:
        bars = "".join(
            _bar(
                m.get("model") or "—",
                f"{_compact(m.get('prompt_tokens'))} · {_pct(m.get('token_share'))}",
                m.get("token_share") or 0.0,
                "var(--mint)",
            )
            for m in models
        )
        rows = "".join(
            f"<tr><td class='m'><b>{escape(m.get('model') or '—')}</b></td>"
            f"<td class='num'>{_f(m.get('prompt_tokens'))}</td>"
            f"<td class='num'>{_pct(m.get('token_share'))}</td>"
            f"<td class='num'>{_f(m.get('output_tokens'))}</td>"
            f"<td class='num'>{_f(m.get('requests'))}</td>"
            f"<td class='num'><b>{_usd(m.get('cost'))}</b></td>"
            f"<td class='num'>{_pct(m.get('cost_share'))}</td></tr>"
            for m in models
        )
        b.append(
            "<div class='pan'><div class='ph'><span>~/ace/model_mix [8]</span>"
            "<span class='live'><i></i>LOCAL</span></div><div class='pb'>"
            + bars
            + "<table style='margin-top:12px'><tr><th>model</th>"
            "<th class='num'>prompt tokens</th><th class='num'>%tok</th>"
            "<th class='num'>output</th><th class='num'>requests</th>"
            "<th class='num'>cost</th><th class='num'>%cost</th></tr>"
            + rows
            + "</table><div class='exp'>Token share and cost share are different "
            "columns for a reason: cache-read rates span 5x across the tier, so a model "
            "can be a small part of the volume and a large part of the bill. Each "
            "<b>cost</b> cell is that model's own tokens at that model's own published "
            "rates — <code>fresh&times;input + cache_read&times;cache_read_rate + "
            "writes&times;input&times;TTL_multiplier + output&times;output_rate</code>. "
            "The rates, and the vendor page they came from, are in the rate card under "
            "§ 02.</div>"
            "</div></div>"
        )

    # [7] — trend.
    weeks = tr.get("weeks") or []
    if weeks:
        rows = "".join(
            f"<tr><td class='m'>week {escape(str(w.get('week')))}</td>"
            f"<td class='num' style='color:var(--ink-4)'>{_f(w.get('active_days'))}"
            f"/{_f(w.get('days'))}</td>"
            f"<td class='num'>{_f(w.get('tokens'))}</td>"
            f"<td class='num'>{_usd(w.get('cost'))}</td>"
            f"<td class='num'>{_f(w.get('commits')) if has_commits else '—'}</td>"
            f"<td class='num'>"
            + (
                f"<span class='d'>{_pct(w.get('wow'))}</span>"
                if w.get("wow") is not None
                else "—"
            )
            + "</td></tr>"
            for w in weeks
        )
        b.append(
            "<div class='pan'><div class='ph'><span>~/ace/trend [7]</span>"
            "<span class='live'><i></i>LOCAL</span></div><div class='pb'><table>"
            "<tr><th>week</th><th class='num'>active days</th><th class='num'>tokens</th>"
            "<th class='num'>cost</th><th class='num'>commits</th>"
            f"<th class='num'>WoW tokens</th></tr>{rows}</table>"
            "<div class='exp'>Day over day the median change is "
            f"<b>{_pct(tr.get('dod_median'))}</b>, with an interquartile range of "
            f"{_pct(tr.get('dod_p25'))} to {_pct(tr.get('dod_p75'))} — volume routinely "
            "halves or doubles between consecutive days. That rules out any budget or "
            "alert built on a rate of change; a period cap on cumulative spend is the "
            "only thing this shape supports.</div></div></div>"
        )

    # [11] — activity chart.
    b.append(
        "<div class='pan'><div class='ph'><span>~/ace/coding_activity [11]</span>"
        "<span class='live'><i></i>LOCAL</span></div><div class='pb'>"
        + _activity_svg(f.get("daily") or [], has_commits)
        + "</div></div>"
    )
    return "".join(b)


def _rate(n: Any) -> str:
    """A published rate. Two decimals, because $0.20 and $0.08 are both real prices."""
    try:
        return f"${float(n):,.2f}"
    except Exception:
        return "—"


def _rate_card(rc: Optional[Dict[str, Any]]) -> str:
    """The published prices every dollar on this page was computed from, with their source.

    **Which price list.** The vendor page and the date it was last checked are linked from
    the header, so cent-level figures above are checkable rather than merely precise.

    **Which numbers are quoted and which are derived.** Anthropic publishes one input price
    per model; the cache-write columns are that price times a multiplier fixed by the
    requested TTL (1.25x at 5m, 2x at 1h) — the one assumption in the cost model.
    """
    rows = (rc or {}).get("rows") or []
    if not rows:
        return ""

    src = (rc or {}).get("source") or ""
    as_of = (rc or {}).get("as_of") or ""
    m5 = (rc or {}).get("write_multiplier_5m") or 1.25
    m1h = (rc or {}).get("write_multiplier_1h") or 2.0

    link = (
        f"<a href='{escape(src)}' target='_blank' rel='noopener'>{escape(src)}</a>"
        if src
        else "<span class='cm'>no source recorded</span>"
    )
    stamp = f" · checked {escape(as_of)}" if as_of else ""

    body = []
    for r in rows:
        model = escape(str(r.get("model") or "—"))
        if not r.get("priced"):
            # An unpriced model contributes $0 to every total on this page. A row of zeros
            # would read as "this model is free" rather than "we could not price it".
            body.append(
                f"<tr><td class='m'><b>{model}</b></td>"
                "<td class='num' colspan='6' style='text-align:left;color:var(--gold)'>"
                "unpriced — counted as $0, not free</td></tr>"
            )
            continue
        body.append(
            f"<tr><td class='m'><b>{model}</b></td>"
            f"<td class='num'>{_rate(r.get('input'))}</td>"
            f"<td class='num'>{_rate(r.get('output'))}</td>"
            f"<td class='num'><b>{_rate(r.get('cache_read'))}</b></td>"
            f"<td class='num' style='color:var(--mint)'>"
            f"{r.get('cache_read_ratio') or 0:.2f}x</td>"
            f"<td class='num' style='color:var(--ink-4)'>"
            f"{_rate(r.get('cache_write_5m'))}</td>"
            f"<td class='num' style='color:var(--ink-4)'>"
            f"{_rate(r.get('cache_write_1h'))}</td></tr>"
        )

    return (
        "<div class='pan'><div class='ph'><span>~/ace/rate_card · "
        f"{escape((rc or {}).get('unit') or 'USD per 1M tokens')}</span>"
        "<span class='live'><i></i>CATALOG</span></div><div class='pb'>"
        "<table><tr><th>model</th><th class='num'>input</th><th class='num'>output</th>"
        "<th class='num'>cache read</th><th class='num'>read vs input</th>"
        "<th class='num'>write 5m *</th><th class='num'>write 1h *</th></tr>"
        + "".join(body)
        + "</table>"
        "<div class='calcbox'>"
        "<b>cost</b> = fresh/1e6 &times; input + cache_read/1e6 &times; cache_read<br>"
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; + write_5m/1e6 &times; input &times; "
        f"{m5:g} + write_1h/1e6 &times; input &times; {m1h:g}<br>"
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; + output/1e6 &times; output_rate<br>"
        "<b>cache_savings</b> = cache_read/1e6 &times; (input &minus; cache_read) "
        "<span class='cm'>&larr; counterfactual, never invoiced</span>"
        "</div>"
        f"<div class='exp'><b>*</b> the two write columns are <b>derived</b>, not quoted: "
        f"Anthropic publishes one input price per model, and a cache write bills at "
        f"{m5:g}&times; that price at the 5-minute TTL and {m1h:g}&times; at the 1-hour TTL. "
        "Every other column is read straight from the vendor's table. Cache reads land at "
        "roughly a tenth of input across the whole tier, which is the single fact the rest "
        "of this page is about.</div>"
        f"<div class='src' style='margin-top:9px'>source: {link}{stamp}</div>"
        "</div></div>"
    )


def _qa_item(q: str, body: str, open_first: bool = False) -> str:
    return (
        f"<details class='qa'{' open' if open_first else ''}>"
        f"<summary>{escape(q)}</summary><div class='a'>{body}</div></details>"
    )


def _qa(f: Optional[Dict[str, Any]], rc: Dict[str, Any]) -> str:
    """§ 13 — the questions this dashboard reliably provokes, answered with the reader's own
    numbers.

    Every figure is recomputed from the scope above rather than written into the prose. The
    one exception is the latency sample, labelled as a sample because this release does not
    measure per-request latency.
    """
    head = _sec(
        "13",
        "COMMON QUESTIONS",
        "Why the token counts look the way they do.",
        "Mechanism, priced against your scope.",
        "LOCAL",
    )

    tok = (f or {}).get("tokens") or {}
    p = tok.get("prompt") or 0
    reqs = (f or {}).get("requests") or 0
    cr, fresh, cw = (
        tok.get("cache_read") or 0,
        tok.get("fresh") or 0,
        tok.get("cache_write") or 0,
    )
    mean_ctx = (p / reqs) if reqs else 0
    new_per_req = ((fresh + cw) / reqs) if reqs else 0

    # Rates come from the dominant model in scope — the same row § 02's rate card shows first.
    priced = [r for r in (rc.get("rows") or []) if r.get("priced")]
    r0 = priced[0] if priced else {}
    model = r0.get("model") or "your model"
    in_rate, cr_rate = r0.get("input") or 0.0, r0.get("cache_read") or 0.0
    w1h = r0.get("cache_write_1h") or 0.0
    cr_cost, fresh_cost = cr / 1e6 * cr_rate, cr / 1e6 * in_rate

    b = [head]

    # -- 1. the number that starts every one of these conversations.
    box = (
        "<div class='calcbox'>"
        f"<span class='cm'>API requests in scope</span> &nbsp; <b>{_f(reqs)}</b><br>"
        f"<span class='cm'>x mean context carried</span> &nbsp; <b>{_f(mean_ctx)}</b><br>"
        f"<span class='cm'>= tokens_in [1]</span> &nbsp; <b>{_f(p)}</b><br>"
        f"&nbsp;&nbsp;<span class='cm'>fresh</span> {_pct(fresh / p if p else 0, 2)} "
        f"&nbsp;<span class='cm'>cache write</span> {_pct(cw / p if p else 0, 2)} "
        f"&nbsp;<span class='cm'>cache read</span> {_pct(cr / p if p else 0, 2)}"
        "</div>"
        if p
        else ""
    )
    b.append(
        _qa_item(
            "tokens_in is in the hundreds of millions. Is something re-reading my "
            "repository on every turn?",
            "<p>No. <b>tokens_in</b> sums the whole prompt every API request carried, and "
            "the API is stateless — each request resends the conversation so far. It is a "
            "measure of <b>context carried</b>, not content read: requests x context, "
            "counted again on every request.</p>"
            + box
            + "<p>The share split is the tell. "
            "Genuinely new material — your messages, tool output, files actually opened — "
            "enters as a <b>cache write</b>. Everything else is replay of tokens the model "
            "has already seen. A run that really did re-read a repository every turn would "
            "show a fresh or write share in the tens of percent, not a fraction of one.</p>",
            open_first=True,
        )
    )

    # -- 2. the objection that follows immediately.
    b.append(
        _qa_item(
            "If every request carries the entire conversation, why is latency low?",
            "<p>Because resent is not recomputed. A cached prefix is a KV-cache hit: the "
            "attention states for those tokens already exist server-side, so prefill work "
            "scales with the <b>new suffix</b>, not the whole prompt."
            + (
                " In this scope that suffix averages "
                f"<b>{_f(new_per_req)}</b> tokens per request against "
                f"<b>{_f(mean_ctx - new_per_req)}</b> replayed."
                if reqs
                else ""
            )
            + "</p>"
            "<p>Generation is the sequential part — one forward pass per output token — so "
            "wall-clock tracks what the model <b>writes</b>, not what it carries. Measured "
            "on one machine over 24h (857 request/response pairs, timed client-side from "
            "transcript timestamps, so local overhead is included):</p>"
            "<table><tr><th>holding output at 100-400 tok</th><th class='num'>p50 latency</th>"
            "<th>at any context, by output</th><th class='num'>p50 latency</th></tr>"
            "<tr><td class='m'>context &lt; 100k</td><td class='num'>3.1s</td>"
            "<td class='m'>output 100-400</td><td class='num'>3.5s</td></tr>"
            "<tr><td class='m'>context 100-250k</td><td class='num'>3.7s</td>"
            "<td class='m'>output 400-1,000</td><td class='num'>5.9s</td></tr>"
            "<tr><td class='m'>context 250-500k</td><td class='num'>3.3s</td>"
            "<td class='m'>output 1,000-3,000</td><td class='num'>8.8s</td></tr>"
            "<tr><td class='m'>context 500k-1.1M</td><td class='num'>9.2s</td>"
            "<td class='m'>output 3,000+</td><td class='num'>22.5s</td></tr></table>"
            "<p>Five times the context costs nothing measurable; seven times the output "
            "costs seven times the wait. Past roughly 500k the cache load itself does start "
            "to show, and that last row is thin enough (n=16) to read as a hint rather than "
            "a result.</p>",
        )
    )

    # -- 3. the over-correction the previous answer invites.
    b.append(
        _qa_item(
            "So tokens_in is not really input — it is just a cache hit?",
            "<p>It is real input, and it is really billed. Cache reads land at roughly a "
            "tenth of the input rate"
            + (
                f" — on <b>{escape(model)}</b>, {_usd(cr_rate)} against {_usd(in_rate)} "
                "per million"
                if in_rate
                else ""
            )
            + " — which is a discount, not an exemption."
            + (
                f" In this scope those reads are <b>{_usd(cr_cost)}</b> of "
                f"{_usd((f or {}).get('cost'))}; sent fresh they would have been "
                f"{_usd(fresh_cost)}."
                if cr and in_rate
                else ""
            )
            + "</p>"
            "<p>What is cached is the <b>key/value tensors</b> prefill produced, not an "
            "answer or a summary. The model still attends over every token; the arithmetic "
            "was simply done once and kept. Output is identical to a fresh send.</p>"
            "<p>The cache key is a <b>prefix hash</b> — exact bytes, longest match wins, "
            "scoped to model, tool definitions, system prompt and the cache breakpoints the "
            "client set. Change one byte early and every block after it re-prefills at full "
            "price. That is the real cost of rewriting history (compaction) and of letting "
            "the TTL lapse.</p>"
            "<p>It also explains the growth. Within a session the prefix only appends, so "
            "request <i>n</i> carries roughly <i>n</i> turns and the session total is "
            "<b>quadratic in its length</b>. Session length is the lever, not file size.</p>",
        )
    )

    # -- 4. where retrieval actually happens.
    carry_box = (
        "<div class='calcbox'>"
        "<span class='cm'>one 600-token file read, 200 requests left in the session</span>"
        f"<br>write once &nbsp; 600 x {_usd(w1h)}/Mtok &nbsp;= &nbsp;"
        f"<b>${600 / 1e6 * w1h:,.4f}</b><br>"
        f"carried &nbsp; 600 x 200 = 120,000 x {_usd(cr_rate)}/Mtok &nbsp;= &nbsp;"
        f"<b>${120_000 / 1e6 * cr_rate:,.4f}</b>"
        "</div>"
        if cr_rate
        else ""
    )
    b.append(
        _qa_item(
            "Where do file reads and other context-gathering enter the prompt, and how are "
            "they billed?",
            "<p>Through the same channel as everything else. A tool result is a "
            "<code>tool_result</code> content block in the <code>messages</code> array, "
            "user role, immediately after the <code>tool_use</code> block that asked for it. "
            "Project instructions, injected reminders and attachments arrive the same way. "
            "There is no embedding index, no retrieval service and no side channel — if the "
            "model can see it, it was serialised into the prompt.</p>"
            "<p>So each read is billed twice over: once as a <b>cache write</b> when it "
            "first appears, then as a <b>cache read</b> on every subsequent request for the "
            "rest of the session.</p>" + carry_box + "<p>The carry dominates the "
            "acquisition, which makes <b>position beat size</b>: a file read early is paid "
            "for by every request after it, and the same read late is nearly free. It also "
            "makes many small tool outputs more expensive than their size suggests — a "
            "few hundred tokens of shell output, repeated across hundreds of calls and then "
            "carried, outweighs a handful of large file reads.</p>"
            "<p>Two levers follow directly: read narrowly (an offset and a limit, a head "
            "rather than a whole file), and delegate breadth to a subagent — its reads land "
            "in its own transcript and only the summary returns to yours.</p>",
        )
    )

    # -- 5. the question the previous two answers together provoke, and the only one on this
    # page whose answer is an instruction rather than a mechanism.
    ramp = (f or {}).get("ramp") or []
    ramp_tbl = ""
    if len(ramp) > 1:
        rows = "".join(
            f"<tr><td class='m'>{escape(r['band'])}</td>"
            f"<td class='num'>{_f(r['requests'])}</td>"
            f"<td class='num'>{_f(r['mean_context'])}</td>"
            # Three decimals, not two: the whole point of the table is the gap between
            # consecutive bands, and at these magnitudes _usd rounds it away.
            f"<td class='num'><b>${r['cost_per_request']:,.3f}</b></td>"
            f"<td class='num'>{_pct(r['cost_share'])}</td></tr>"
            for r in ramp
        )
        first = ramp[0]
        peak = max(ramp, key=lambda r: r["cost_per_request"])
        mult = (
            (peak["cost_per_request"] / first["cost_per_request"])
            if first["cost_per_request"]
            else 0
        )
        # Measured against the PEAK band, not the last one. Past roughly 500k the ramp
        # flattens (compaction and the context ceiling cap the prefix), so a first-to-last
        # ratio reads the plateau as the slope easing and understates a session pinned
        # against the ceiling for its whole tail.
        plateau = (
            "<p>The tail band is not the most expensive one: past a point the prefix stops "
            "growing, because compaction and the context ceiling cap it. That is the ramp "
            "flattening at its maximum, not relenting — those requests carry the largest "
            "context on the page and pay for it on every turn.</p>"
            if peak is not ramp[-1]
            else ""
        )
        # The share of spend that is not in the opening band — the money a shorter session
        # would have had a chance at.
        late = 1.0 - (first.get("cost_share") or 0.0)
        ramp_tbl = (
            "<table><tr><th>request # in its session</th><th class='num'>requests</th>"
            "<th class='num'>mean context</th><th class='num'>$/request</th>"
            f"<th class='num'>% of spend</th></tr>{rows}</table>"
            f"<p>Same work, up to <b>{mult:,.1f}x</b> the price, bought by nothing but "
            f"position. <b>{_pct(late)}</b> of your spend in this scope is requests that "
            "were not in the opening band of their session.</p>" + plateau
        )

    # write_1h / cache_read is the whole decision, and it is a property of the TTL rather
    # than of the price list: a 1h write bills at 2x input and a read at ~0.1x, so the ratio
    # survives any vendor re-pricing that moves both.
    breakeven = (w1h / cr_rate) if cr_rate else 0
    be_box = (
        "<div class='calcbox'>"
        f"<span class='cm'>carry 1,000 tokens one more request</span> &nbsp; "
        f"1,000 x {_usd(cr_rate)}/Mtok &nbsp;=&nbsp; <b>${1000 / 1e6 * cr_rate:,.6f}</b><br>"
        f"<span class='cm'>re-acquire 1,000 tokens after a restart</span> &nbsp; "
        f"1,000 x {_usd(w1h)}/Mtok &nbsp;=&nbsp; <b>${1000 / 1e6 * w1h:,.6f}</b><br>"
        f"<span class='cm'>break-even</span> &nbsp; <b>{breakeven:,.0f} requests</b>"
        "</div>"
        if cr_rate and w1h
        else ""
    )

    b.append(
        _qa_item(
            "Which is cheaper — restarting from a clean session often, or keeping one "
            "session going as long as possible to avoid re-reading everything?",
            "<p><b>Restarting, by a wide margin</b> — but per task, not as often as "
            "possible. The intuition that a long session amortises its context is backwards: "
            "context is not paid once and reused, it is <b>re-billed on every request that "
            "follows it</b>. Length is the thing being charged for.</p>"
            "<p>Your own sessions, by where each request fell in its context window:</p>"
            + ramp_tbl
            + "<p>The arithmetic behind it is one ratio. A token you keep costs a cache read "
            "on every later request; a token you drop and fetch again costs one cache write, "
            f"which at the 1h TTL is 2x the input rate — {breakeven:,.0f}x a read.</p>"
            + be_box
            + f"<p>So <b>any token you would carry for more than ~{breakeven:,.0f} more "
            "requests is cheaper to drop and re-read on demand.</b> In a 400-request "
            "session, nearly everything acquired in the first 380 requests is past that "
            "line — and most of what a long context holds is not re-readable material at "
            "all but conversation, tool chatter and superseded diffs, which a restart drops "
            "for free because nothing ever fetches it back.</p>"
            "<p>Replaying this scope with sessions cut at a fixed length, and charging each "
            "cut for re-acquiring a share of the prefix it discarded:</p>"
            "<table><tr><th>cut every</th><th class='num'>re-read 0%</th>"
            "<th class='num'>re-read 25%</th><th class='num'>re-read 50%</th>"
            "<th class='num'>breaks even at</th></tr>"
            "<tr><td class='m'>400 requests</td><td class='num'>28.8%</td>"
            "<td class='num'>24.5%</td><td class='num'>18.3%</td>"
            "<td class='num'>86%</td></tr>"
            "<tr class='hi'><td class='m'>200 requests</td><td class='num'>48.7%</td>"
            "<td class='num'>42.4%</td><td class='num'>32.4%</td>"
            "<td class='num'>84%</td></tr>"
            "<tr><td class='m'>100 requests</td><td class='num'>58.0%</td>"
            "<td class='num'>50.7%</td><td class='num'>38.3%</td>"
            "<td class='num'>81%</td></tr>"
            "<tr><td class='m'>25 requests</td><td class='num'>64.1%</td>"
            "<td class='num'>56.5%</td><td class='num'>42.4%</td>"
            "<td class='num'>77%</td></tr></table>"
            "<p>Two things to take from that table. Restarting has to be <b>very</b> wasteful "
            "before it loses — you would have to re-read roughly <b>80%</b> of everything you "
            "discarded, every time, for it to cost more than carrying it. And the returns "
            "<b>saturate</b>: going from 200 to 25 buys another 15 points while multiplying "
            "the restarts tenfold, and every restart is a chance to re-read something you did "
            "not need to.</p>"
            "<p><b>The practical rule: one session per task, not per day and not per "
            "question.</b> Restart when the subject changes — the context from the last task "
            "is pure carry cost against the next one. Do not restart inside a task you are "
            "mid-way through: that is the case where re-acquisition is real, and where the "
            "cost of the model re-deriving what it already knew is not on this page at all. "
            "<code>/compact</code> is the middle setting — it is a restart that writes its own "
            "handover, so it pays a full prefix rewrite but keeps the thread.</p>"
            "<p class='lim'><b>Honest limits.</b> The ramp table is measured; the cut table is "
            "a <b>simulation</b> over this scope, and it assumes the work each request does is "
            "unchanged by the restart. Re-derivation — the model re-reasoning to a conclusion "
            "it had already reached — is not modelled and is not free. Read the direction, not "
            "the third digit.</p>",
        )
    )

    # -- 6. the check anyone can run themselves.
    b.append(
        _qa_item(
            "What does the client actually call on each turn?",
            "<p>One endpoint: <code>POST /v1/messages</code> on "
            "<code>api.anthropic.com</code>, streamed as server-sent events "
            "(<code>message_start</code>, <code>content_block_delta</code>, "
            "<code>message_delta</code>). The only sibling in the per-turn path is "
            "<code>/v1/messages/count_tokens</code>, which prices context without "
            "generating. Batches, files and agent endpoints exist in the shipped client but "
            "no conversation turn touches them.</p>"
            "<p>Both were read out of the installed Claude Code binary rather than from "
            "documentation, and you can repeat it: "
            "<code>strings $(which claude) | grep -o '/v1/[a-z_/]*' | sort | uniq -c</code>."
            "</p>"
            "<p>That single request body is why this page works at all: everything the model "
            "saw is in the transcript on disk, so a session can be priced exactly without "
            "anything being sent anywhere.</p>",
        )
    )

    # -- 7. Prometheus metrics & offline telemetry integration
    b.append(
        _qa_item(
            "How do I scrape Prometheus metrics or ingest telemetry into Grafana / Datadog?",
            "<p>The ACE sidecar exposes a standard Prometheus text exposition format endpoint at "
            "<code><a href='/metrics' target='_blank' rel='noopener'>/metrics</a></code> (HTTP GET).</p>"
            "<p>This endpoint streams real-time counter and gauge metrics for offline time-series database scraping "
            "(Prometheus, Grafana Alloy, VictoriaMetrics, OpenTelemetry Collector, Datadog Agent). Metrics emitted include:</p>"
            "<table><tr><th>Metric Name</th><th>Type</th><th>Description</th></tr>"
            "<tr><td class='m'><b>ace_requests_total</b></td><td>Counter</td><td>Total API requests processed split by agent engine, model, and status code.</td></tr>"
            "<tr><td class='m'><b>ace_tokens_input_fresh_total</b></td><td>Counter</td><td>Fresh (uncached) input tokens.</td></tr>"
            "<tr><td class='m'><b>ace_tokens_cache_read_total</b></td><td>Counter</td><td>Input tokens served from Anthropic prompt cache.</td></tr>"
            "<tr><td class='m'><b>ace_tokens_cache_write_total</b></td><td>Counter</td><td>Tokens written to prompt cache.</td></tr>"
            "<tr><td class='m'><b>ace_tokens_output_total</b></td><td>Counter</td><td>Model completion tokens generated.</td></tr>"
            "<tr><td class='m'><b>ace_cost_usd_total</b></td><td>Counter</td><td>List-price USD cost valuation.</td></tr>"
            "<tr><td class='m'><b>ace_session_time_seconds</b></td><td>Counter</td><td>Session duration breakdown (wall clock, active, idle, parked).</td></tr>"
            "<tr><td class='m'><b>ace_tool_bytes</b></td><td>Gauge</td><td>Total vs. unused system tool declaration payload sizes.</td></tr>"
            "</table>"
            "<p>Add the following target to your <code>prometheus.yml</code> scrape configuration:</p>"
            "<div class='calcbox'>"
            "<pre style='color:var(--mint);margin:0;font-family:monospace;font-size:12px;'>scrape_configs:\n  - job_name: 'ace_sidecar'\n    scrape_interval: 15s\n    static_configs:\n      - targets: ['127.0.0.1:8787']</pre>"
            "</div>",
        )
    )

    return "".join(b)



SITE = "https://acefleet.dev"
REPO = "https://github.com/ACE-Engineering/ace-sidecar"
CONTACT = "contact@acefleet.dev"


def _mailto(subject: str) -> str:
    """One inbox, with the errand already in the subject line.

    Encoded rather than left raw: a bare space in an ``href`` is fixed up by browsers but not
    by every mail client, which truncates the subject at the space.
    """
    return f"mailto:{CONTACT}?subject={quote(subject)}"


def _about() -> str:
    """§ 14 — what ACE is, and how to reach the people who build it.

    Answers the two questions the measurements above cannot: what the measuring thing *is*,
    and who to contact about it. One address for questions, feature requests, collaborations,
    and team rollout, with ``?subject=`` prefill as the routing — it degrades to a plain mailto
    if the client ignores it.

    The banner names ACE Fleet because this page is the only thing most readers will ever see
    of it: a reader who arrives through the open-source sidecar has no way to tell that it is
    one vertical of a broader cost-control platform rather than the whole company, and that is
    exactly the impression the section exists to correct.
    """
    cards = (
        (
            "QUESTIONS",
            "Questions, feature requests, collaborations",
            "Something here wrong, missing, or measuring the thing you did not mean? Say so — "
            "the levers this release only scores are prioritised by what people ask for.",
            _mailto("ACE sidecar"),
            CONTACT,
        ),
        (
            "TEAMS",
            "Deploy this sidecar to your whole team",
            "This page sees one machine. The same capture and scoring runs fleet-wide — "
            "per-repo and per-developer attribution, shared baselines, budgets — so efficiency "
            "is managed across the team rather than rediscovered one laptop at a time. Ask for "
            "a rollout.",
            _mailto("ACE team deployment"),
            CONTACT,
        ),
    )
    body = "".join(
        f"<div class='card'><div class='k'><span class='pill on' style='font-weight:600;padding:2px 8px'>{escape(k)}</span></div>"
        f"<h3>{escape(h)}</h3>"
        f"<p>{escape(p)}</p><a class='lk' href='{escape(href)}'><span>✉ {escape(label)}</span><span class='arw'>→</span></a></div>"
        for k, h, p, href, label in cards
    )
    return (
        _sec("14", "ACE SIDECAR", "Who makes this.", "And how to reach them.", "ABOUT")
        + "<div class='tag'>"
        "<div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px;'>"
        "<div class='t'>ACE Sidecar is built by ACE Fleet.</div>"
        # The pills sit beside a headline about ACE Fleet, so they describe the proxy, not the
        # page they are printed on: what it costs to adopt, and what it does to the bill.
        # LOCAL-FIRST and ZERO-CLOUD OVERHEAD were true of the sidecar but read as claims
        # about Fleet in this position, which inverts what it is.
        "<div style='display:flex;gap:6px;flex-wrap:wrap;'>"
        "<span class='pill on' style='background:#0F231A;color:var(--mint);border-color:#1d3b2e;'>⚡ DROP-IN PROXY</span>"
        "<span class='pill on' style='background:#0d1c33;color:var(--blue);border-color:#1e355b;'>📉 CUT AI BILLS</span>"
        "</div></div>"
        "<p>ACE Fleet is a cost-saving proxy for companies scaling AI applications — it sits in "
        "front of the model providers and cuts what an organisation spends on inference as that "
        "spend grows, across every workload rather than any single one.</p>"
        "<p>This sidecar is the coding-agent slice of that work, open-sourced on its own: the "
        "same accounting turned on one developer's machine, where the spend is small enough to "
        "check by hand and the levers are easy to see. It is a showcase of the approach — the "
        "platform behind it is considerably wider in scope.</p>"
        f"<div style='margin-top:16px;'><a class='lk site-lk' target='_blank' rel='noopener' href='{SITE}'><span>🌐 Visit acefleet.dev</span><span class='arw'>↗</span></a></div>"
        "</div>"
        f"<div class='cards'>{body}</div>"
    )


def _prometheus_section(d: Dict[str, Any]) -> str:
    """§ 12 — Prometheus metrics exposition & offline telemetry ingestion guide."""
    head = _sec(
        "12",
        "PROMETHEUS METRICS",
        "Machine-readable telemetry exporter.",
        "Prometheus, Grafana Alloy & OTel scraping.",
        "EXPORTER",
    )

    rows = [
        ("ace_sessions_total", "Counter", "agent", "Total observed developer session transcripts across Claude Code, Antigravity, and Codex."),
        ("ace_turns_total", "Counter", "agent", "Total AI agent API turns / requests processed."),
        ("ace_tokens_input_fresh_total", "Counter", "—", "Fresh (uncached) prompt input tokens (live gateway proxy)."),
        ("ace_tokens_cache_read_total", "Counter", "—", "Input tokens served directly from prompt cache."),
        ("ace_tokens_cache_write_total", "Counter", "—", "Tokens written to prompt cache."),
        ("ace_tokens_output_total", "Counter", "—", "Completion output tokens generated by the model."),
        ("ace_cost_usd_total", "Counter", "agent", "Cumulative list-price cost valuation in USD."),
        ("ace_peak_context_tokens", "Gauge", "—", "Maximum context window depth observed in a single turn."),
        ("ace_cache_read_share", "Gauge", "—", "Ratio of prompt input tokens served from cache (0.0 - 1.0)."),
        ("ace_session_time_seconds", "Counter", "state", "Cumulative session time in seconds (wall_clock, active, idle, parked)."),
        ("ace_model_requests_total", "Counter", "model", "Total API requests processed by model."),
        ("ace_model_prompt_tokens_total", "Counter", "model", "Prompt tokens by model."),
        ("ace_model_output_tokens_total", "Counter", "model", "Output tokens by model."),
        ("ace_model_cost_usd_total", "Counter", "model", "Total USD cost by model."),
        ("ace_tool_bytes", "Gauge", "type", "Tool declaration payload size in bytes (total, unused)."),
        ("ace_installed_skills_total", "Gauge", "—", "Number of active workflow skills installed on disk."),
    ]

    table_rows = "".join(
        f"<tr><td class='m'><b>{escape(name)}</b></td>"
        f"<td><span class='pill on' style='font-size:10px;padding:1px 6px;'>{escape(kind)}</span></td>"
        f"<td class='m' style='color:var(--ink-3);'>{escape(labels)}</td>"
        f"<td>{escape(desc)}</td></tr>"
        for name, kind, labels, desc in rows
    )

    promql_recipes = [
        ("Turn Velocity (5m)", 'sum(rate(ace_turns_total[5m])) by (agent)', "Requests per second per agent engine"),
        ("Cumulative Spend ($)", 'sum(ace_cost_usd_total) by (agent)', "Total cost distribution by coding agent"),
        ("Model Traffic Share", 'sum(ace_model_requests_total) by (model)', "Request count split by model identifier"),
        ("Prompt Cache Hit Rate", 'ace_cache_read_share * 100', "Percentage of input context served from cache"),
        ("Session Time Breakdown", 'sum(rate(ace_session_time_seconds[5m])) by (state)', "Wall clock vs active vs idle vs approval wait time"),
    ]

    promql_rows = "".join(
        f"<tr><td><b>{escape(title)}</b><div style='font-size:11.5px;color:var(--ink-4);margin-top:2px;'>{escape(note)}</div></td>"
        f"<td><code class='promql-tag'>{escape(query)}</code></td>"
        f"<td style='text-align:right;'><button class='btn' style='padding:3px 8px;font-size:11px;cursor:pointer;' onclick='copyText(\"{escape(query)}\", this)'>Copy Query</button></td></tr>"
        for title, query, note in promql_recipes
    )

    return (
        head
        + "<div class='tag' style='margin-bottom:16px;'>"
        "<div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px;'>"
        "<div class='t'>Native Prometheus Exposition Exporter (v0.0.4)</div>"
        "<div style='display:flex;gap:6px;'>"
        "<span class='pill on' style='background:#0F231A;color:var(--mint);border-color:#1d3b2e;'>✓ OPENMETRICS COMPLIANT</span>"
        "<span class='pill on' style='background:#0d1c33;color:var(--blue);border-color:#1e355b;'>⚡ LIVE TEXT STREAM</span>"
        "</div></div>"
        "<p>The ACE sidecar exposes real-time Prometheus text exposition metrics at <code>/metrics</code> (HTTP GET). "
        "Standardized for continuous time-series scraping into Prometheus, Grafana Alloy, OpenTelemetry Collector, VictoriaMetrics, Datadog, or ClickHouse.</p>"
        "<div style='display:flex;gap:10px;margin-top:14px;align-items:center;flex-wrap:wrap;'>"
        "<input type='text' readonly id='metricsUrlSection' value='' style='flex:1;min-width:250px;background:#0b0c0d;border:1px solid #282e30;color:var(--mint);padding:8px 12px;border-radius:6px;font-family:var(--mono);font-size:13px;' />"
        "<button class='lk' style='cursor:pointer;' onclick='copyMetricsSectionUrl(this)'><span>📋 Copy Endpoint URL</span></button>"
        "<a class='lk site-lk' target='_blank' rel='noopener' href='/metrics'><span>🌐 Open Raw Stream</span><span class='arw'>↗</span></a>"
        "</div>"
        "</div>"
        "<div class='pan' style='margin-bottom:16px;'>"
        "<div class='ph' style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;'>"
        "<span>Telemetry Collector Scrape Configurations</span>"
        "<div style='display:flex;gap:6px;' id='collectorTabs'>"
        "<button class='tab-btn on' onclick='switchCollectorTab(\"prometheus\", this)'>Prometheus</button>"
        "<button class='tab-btn' onclick='switchCollectorTab(\"otel\", this)'>OTel Collector</button>"
        "<button class='tab-btn' onclick='switchCollectorTab(\"alloy\", this)'>Grafana Alloy</button>"
        "<button class='tab-btn' onclick='switchCollectorTab(\"datadog\", this)'>Datadog Agent</button>"
        "</div></div>"
        "<div class='pb'>"
        "<div id='collectorConfigBox' class='calcbox' style='margin:0;position:relative;'>"
        "<pre id='collectorCode' style='color:#a3e635;margin:0;font-family:var(--mono);font-size:12px;white-space:pre-wrap;'></pre>"
        "<button class='btn' style='position:absolute;top:10px;right:10px;padding:4px 10px;font-size:11px;cursor:pointer;' onclick='copyCollectorConfig(this)'>Copy Config</button>"
        "</div>"
        "</div></div>"
        "<div class='grid' style='grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px;'>"
        "<div class='pan'><div class='ph'><span>Prometheus Metrics Schema Reference</span>"
        "<span class='live'><i></i>16 METRICS</span></div>"
        "<div class='pb' style='max-height:320px;overflow-y:auto;'><table>"
        "<tr><th>Metric Name</th><th>Type</th><th>Labels</th><th>Description</th></tr>"
        f"{table_rows}"
        "</table></div></div>"
        "<div class='pan'><div class='ph'><span>Grafana & PromQL Query Recipes</span>"
        "<span class='live'><i></i>READY-TO-USE</span></div>"
        "<div class='pb' style='max-height:320px;overflow-y:auto;'><table>"
        "<tr><th>Dashboard Panel</th><th>PromQL Expression</th><th></th></tr>"
        f"{promql_rows}"
        "</table></div></div>"
        "</div>"
        "<div class='pan'><div class='ph'><span>Live Stream Inspector (Syntax-Highlighted)</span>"
        "<button class='btn' style='padding:3px 8px;font-size:11px;cursor:pointer;' onclick='loadSectionLiveMetrics()'>Fetch Live Payload</button></div>"
        "<div class='pb'><div id='sectionMetricsPreviewBox' class='metrics-box' style='margin:0;max-height:240px;'>Click 'Fetch Live Payload' to view live metrics stream...</div></div></div>"
    )






# The rail's destinations, in rail order. Module-level because both the markup and the
# highlight CSS are generated from it -- an entry and its :target rule cannot drift apart,
# the same reasoning that keeps _sec's id keyed off the section number.
_NAV = (
    ("◫", "Overview", "01"),
    ("🎯", "Code Quality", "02"),
    ("⇄", "Strategies", "04"),
    ("✦", "Recommendations", "06"),
    ("⚡", "Workflow Skills", "07"),
    ("✓", "Installed Skills", "08"),
    ("◷", "Sessions", "09"),
    ("⧗", "Time", "10"),
    ("📊", "Prometheus Metrics", "12"),
    ("?", "Common questions", "13"),
    ("◈", "About ACE", "14"),
)
# The numbers are the anchors — `#s<num>` — so they have to match the section each rail item
# means, and each has to be unique across the page. Both failed here: Common questions pointed
# at #s11 and landed on Live Stream, because the questions section was itself numbered 10 and
# collided with Session Time, leaving two id='s10' on one document. Numbers now follow the
# order the sections are emitted in; keep it that way when adding one.



def _nav_css() -> str:
    """No highlight rules: ``.rail .item.on`` in the main stylesheet is the whole marker now.

    This used to generate ``body:has(#sNN:target) .rail .item[href='#sNN']`` and move the
    marker with no JavaScript at all. That was the nicer design and it worked until the
    dashboard grew a scope switcher: swapping the agent filter replaces ``.main``'s innerHTML,
    which destroys and rebuilds every ``.sec``, and Chrome does not re-evaluate a ``:has()``
    against ``:target`` for a subtree rebuilt underneath it. The rail then stayed pinned to
    the server-rendered Overview no matter where the reader navigated — the marker frozen
    while the content moved, which reads as "the tabs stopped working".

    The same swap also dropped the fragment from the URL, so a scope change silently reset
    ``:target`` to nothing and re-lit Overview even when nothing had been clicked.

    Both are DOM-lifecycle problems, so the marker now lives where the DOM lifecycle is
    already handled: :func:`_nav_js` sets the class, and re-sets it after every swap.
    """
    return ""


def _nav_js() -> str:
    """Own both halves of a rail click: which section is shown, and which entry is marked.

    Neither half survives the scope switcher on its own.

    **The marker.** It used to be pure CSS — ``body:has(#sNN:target) .rail .item[href='#sNN']``
    — which is a nicer design and worked until swapping the agent filter began replacing
    ``.main``'s innerHTML. That rebuilds every ``.sec``, and Chrome does not re-evaluate a
    ``:has()`` against ``:target`` for a subtree replaced underneath it, so the marker froze
    on the server-rendered Overview while the content moved.

    **The scroll.** After that same swap the browser stops scrolling on fragment change at
    all: the hash updates, nothing moves. Measured on a rebuilt page, a click on a rail entry
    left ``scrollY`` at 0 for 3.5s with ``scroll-behavior: auto`` and the target 7,094px down.
    The document's fragment target does not survive having its element replaced, and a plain
    ``<a href="#sNN">`` has nothing left to scroll to.

    So the click is handled explicitly rather than delegated to the browser: set the hash,
    scroll the section in ourselves, move the marker. All three are idempotent, which is what
    lets :func:`initScopeNav`'s swap path re-run the binding without stacking handlers.
    """
    return """
function syncRailMarker() {
  const items = document.querySelectorAll('.rail .item');
  if (!items.length) return;
  const hash = window.location.hash;
  let hit = null;
  items.forEach(a => { if (a.getAttribute('href') === hash) hit = a; });
  items.forEach(a => a.classList.remove('on'));
  (hit || items[0]).classList.add('on');
}

function initRailNav() {
  document.querySelectorAll('.rail .item').forEach(a => {
    const href = a.getAttribute('href') || '';
    if (href.charAt(0) !== '#') return;
    // Assigned, not addEventListener: re-running after a swap must replace the handler
    // rather than add a second one that scrolls twice.
    a.onclick = function(e) {
      const el = document.getElementById(href.slice(1));
      if (!el) return;                     // unknown anchor: let the browser try
      e.preventDefault();
      if (window.location.hash !== href) {
        window.history.pushState({}, '', window.location.pathname + window.location.search + href);
      }
      el.scrollIntoView({ block: 'start' });
      syncRailMarker();
    };
  });
}

// Follow the reader, not just their clicks. Without this the marker only ever moved on a
// click, so a reload — which restores scroll position but carries no fragment — left the
// rail lit on Overview while the reader was looking at section 12. Scrolling by hand had
// the same effect.
//
// A scroll listener rather than an IntersectionObserver: these sections are thousands of
// pixels tall, so scrolling through the middle of one crosses no threshold and an observer
// stays silent for the entire section. Measured that way first — the marker never moved at
// any scroll position. Reading positions on scroll always answers the question being asked,
// "which section is at the top of the viewport".
let railSpyTargets = [];
let railSpyQueued = false;
function railSpyUpdate() {
  railSpyQueued = false;
  if (!railSpyTargets.length) return;
  let best = null, bestTop = -Infinity;
  railSpyTargets.forEach(pair => {
    const top = pair[0].getBoundingClientRect().top;
    if (top <= 120 && top > bestTop) { bestTop = top; best = pair[1]; }
  });
  if (!best) best = railSpyTargets[0][1];
  if (best.classList.contains('on')) return;   // no DOM writes on an unchanged marker
  document.querySelectorAll('.rail .item').forEach(a => a.classList.remove('on'));
  best.classList.add('on');
}
function initRailSpy() {
  railSpyTargets = [];
  document.querySelectorAll('.rail .item').forEach(a => {
    const href = a.getAttribute('href') || '';
    if (href.charAt(0) !== '#') return;
    const el = document.getElementById(href.slice(1));
    if (el) railSpyTargets.push([el, a]);
  });
  railSpyUpdate();
}
// Bound once, not per init: re-running initRailSpy after a swap refreshes the targets, and
// a second listener would only duplicate the same work.
window.addEventListener('scroll', () => {
  if (railSpyQueued) return;
  railSpyQueued = true;
  window.requestAnimationFrame(railSpyUpdate);
}, { passive: true });

window.addEventListener('hashchange', syncRailMarker);
"""


_LEVER_NAMES = ("age-out", "bash truncate", "supersede", "read de-dup")


def _lever_rail(d: Dict[str, Any]) -> str:
    """The four volume levers, ranked by measured headroom, with the dollars attached.

    Every row renders **disabled** — Phase 0 ships no lever. Each row carries a number: the
    lever scored *alone* against this machine's sessions over the selected range, ordered by
    what it is worth.

    Standalone is deliberate. Under the tiers in § 04/05 levers apply first-match-wins, so a
    lever's contribution depends on which ran before it — useful for "what does this tier
    save", useless for "which lever should exist". Scored alone the shares overlap and
    **must not be summed**; the note under the list says so, and § 04 has the composed
    totals.
    """
    sc = d.get("scorecards") or {}
    rows = sc.get("standalone") or []
    if not rows:
        # No sessions in scope. The published corpus figures would fit here and would be a
        # lie about this machine, so the rows render with the number withheld.
        return "".join(
            f"<div class='lv{' f' if i == 0 else ''} dis' title='Nothing to score — no "
            f"sessions in the selected range.'><div class='lr'>"
            f"<span class='rk'>{i + 1}</span><span class='ln'>{n}</span>"
            f"<span class='lu z'>—</span></div>"
            f"<div class='lb z'><i style='width:0'></i></div>"
            f"<div class='lm'><span>not scored</span><span>PHASE 2</span></div></div>"
            for i, n in enumerate(_LEVER_NAMES)
        )
    top = max((r["usd"] for r in rows), default=0.0)
    out = []
    for i, r in enumerate(rows):
        # Proportional to the largest lever, so the gap between #1 and #4 is visible rather
        # than something the reader has to compute from four dollar figures.
        w = (r["usd"] / top * 100.0) if top > 0 else 0.0
        z = "" if r["usd"] > 0 else " z"
        risk = escape((r.get("risk") or "").replace("*", ""))
        tip = (
            f"{r['detail']}. {r['caveat']}. "
            f"Scored alone against your own sessions in this range — "
            f"standalone shares overlap and do not sum. "
            f"Not shipped in Phase 0; this release measures only."
        )
        out.append(
            f"<div class='lv{' f' if i == 0 else ''} dis' title='{escape(tip)}'>"
            f"<div class='lr'><span class='rk'>{r['rank']}</span>"
            f"<span class='ln'>{escape(r['label'])}</span>"
            f"<span class='lu{z}'>{_usd(r['usd'])}</span></div>"
            f"<div class='lb{z}'><i style='width:{w:.1f}%'></i></div>"
            # Two decimals, not one: the smallest lever here is 0.05% of the bill and "0.0%"
            # reads as "nothing measured" rather than "measured, and tiny".
            f"<div class='lm'><span>{_pct(r['share'], 2)} of billed</span>"
            f"<span class='{risk}'>{risk or '—'}</span></div></div>"
        )
    return "".join(out)


def _lever_note(d: Dict[str, Any]) -> str:
    sc = d.get("scorecards") or {}
    rows = sc.get("standalone") or []
    if not rows:
        return (
            "none are wired — this release measures only. No sessions in scope, "
            "so there is no headroom to rank."
        )
    return (
        "none are wired — this release measures only. Each lever is scored "
        "<b>alone</b>, so these overlap and do not sum; "
        "<a href='#s04'>§ 04</a> composes them."
    )


# The GitHub mark, inlined rather than linked: the dashboard is served by a local process
# and is expected to render with no network at all, so a CDN <img> would be the one asset
# that breaks offline. currentColor lets the anchor's :hover drive the fill.
_GITHUB_ICON = (
    "<svg viewBox='0 0 16 16' aria-hidden='true' focusable='false'><path d='M8 0C3.58 0 0 "
    "3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37"
    "-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 "
    "1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87"
    ".31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36"
    ".09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 "
    "3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38"
    "A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8z'/></svg>"
)


def _rail(d: Dict[str, Any]) -> str:
    live = d.get("live") or {"turns": 0}
    # Each entry jumps to a section already on the page -- one document, not five views.
    # Anchors rather than divs: clickable without JS.
    nav = "".join(
        f"<a class='item {'on' if t == _NAV[0][2] else ''}' href='#s{t}'>"
        f"<span class='ic'>{i}</span>{n}</a>"
        for i, n, t in _NAV
    )
    levers = _lever_rail(d)
    return f"""<div class='rail'>
<div class='brand'><div class='r'><span class='mark'></span><span class='n'>ACE</span>
<a class='gh' href='{REPO}' target='_blank' rel='noopener noreferrer'
   title='Source on GitHub' aria-label='Source on GitHub'>{_GITHUB_ICON}</a></div>
<div class='s'>local sidecar</div></div>
<h4>Dashboards</h4>{nav}
<h4>Sidecar</h4>
<div class='ctl'><div class='h'>Mode</div>
  <div class='sw' title='The only mode this release has: every request is relayed
 byte-for-byte and only its usage is recorded.'>observe<span class='pill on'>ACTIVE</span></div>
  <div class='sw dis' title='Phase 1 — would score a rewritten prompt alongside the real one
 without sending it.'>shadow<span class='pill soon'>PHASE 1</span></div>
  <div class='sw dis' title='Phase 2 — would apply the levers to the request actually
 sent.'>enforce<span class='pill soon'>PHASE 2</span></div>
</div>
<div class='ctl'><div class='h'>Levers(PHASE 1) &mdash; headroom on your data</div>{levers}
  <div class='note-s'>{_lever_note(d)}</div>
</div>
<div class='ctl'><div class='h'>Data</div>
  <div class='sw'>turns recorded<span class='pill on'>{_f(live.get('turns'))}</span></div>
  <div class='sw'>capture<span class='pill {'on' if d['capture'] else ''}'>
    {'ON' if d['capture'] else 'OFF'}</span></div>
  <button class='btn' style='width:100%;cursor:pointer;text-align:left;margin-bottom:4px;' onclick="document.getElementById('metricsModal').style.display='flex'">📊 Prometheus &amp; Export</button>
  <a class='btn' download='ace-stats-{escape(d['range'])}.json'
     href='/api/stats?range={escape(d['range'])}'>export scrubbed JSON</a>
  <a class='btn' target='_blank' rel='noopener'
     href='/api/report?range={escape(d['range'])}'>share report (numbers only)</a>
</div>

<div class='up'><div class='t'>ACE Sidecar</div>
  <p>Local developer observability sidecar for AI coding agents. Intercepts local Claude Code
  &amp; Antigravity traffic, mines session transcripts, proposes workflow skills, and exposes
  standard Prometheus metrics for time-series scraping.</p>
  <a class='cta' href='#s14'>About &amp; contact</a>
</div>

</div>"""


def _scorecard(tiers: List[Dict[str, Any]], billed: float, ptok: float) -> str:
    rows = []
    for t in tiers:
        names = (
            ", ".join(x["name"] for x in t["levers"] if x["usd"] or x["tokens"]) or "—"
        )
        hi = " class='hi'" if t["tier"] == "SAFE" else ""
        rows.append(
            f"<tr{hi}><td><b>{escape(t['tier'])}</b></td>"
            f"<td class='m' style='color:var(--ink-4)'>{escape(names)}</td>"
            f"<td class='num'><b>{_usd(t['usd_total'])}</b></td>"
            f"<td class='num'>{_pct(t['usd_total']/billed if billed else 0)}</td>"
            f"<td class='num'>{_f(t['tokens_total'])}</td>"
            f"<td class='num'>{_pct(t['tokens_total']/ptok if ptok else 0, 2)}</td>"
            f"<td style='color:var(--ink-4)'>{escape(t['note'])}</td></tr>"
        )
    return (
        "<table><tr><th>tier</th><th>levers</th><th class='num'>saving</th>"
        "<th class='num'>%cost</th><th class='num'>tokens removed</th>"
        "<th class='num'>%tok</th><th>note</th></tr>" + "".join(rows) + "</table>"
    )


def render(d: Dict[str, Any]) -> str:
    h = d["historical"]
    billed = h.get("cost_usd") or 0.0
    ptok = h.get("prompt_tokens") or 0
    share = h.get("cache_share") or 0.0
    cap = d.get("capture") or {}
    b: List[str] = []

    masked_transcripts = [
        _mask_home(p) for p in d.get("sources", {}).get("transcripts", [])
    ]
    b.append(
        "<div class='top'><span><span class='w'>ace</span> / "
        "<b>local</b> / "
        f"{escape(d['range'])}</span><span class='p'>{escape(str(masked_transcripts))}"
        "&nbsp;&nbsp;<span class='live'><i></i>LOCAL ONLY</span></span></div>"
    )
    b.append("<div class='wrap'>")
    b.append(
        "<h1>Heterogeneous Coding Agent Observability</h1><div class='lede'>Unified observability, "
        "cost analysis, and context efficiency across Claude Code, Antigravity (Google), & Codex (OpenAI) agents. Nothing leaves this machine.</div>"
    )
    cur_agent = d.get("agent", "all")
    scope = "".join(
        f"<a class=\"{'on' if k == d['range'] else ''}\" href='/dashboard?range={k}&agent={cur_agent}'>{escape(lbl)}</a>"
        for k, lbl in d["ranges"]
    )
    agents_nav = "".join(
        f"<a class=\"{'on' if k == cur_agent else ''}\" href='/dashboard?range={d['range']}&agent={k}'>{escape(lbl)}</a>"
        for k, lbl in d.get(
            "agents",
            [
                ("all", "All Agents"),
                ("claude", "Claude Code"),
                ("antigravity", "Antigravity (Google)"),
            ],
        )
    )
    b.append(
        f"<div class='scope'><span class='lbl'>Time</span>{scope}"
        f"<span class='lbl' style='margin-left:14px'>Agent Env</span>{agents_nav}"
        f"<span class='span'>{_span_caption(d)}</span></div>"
    )

    # Heterogeneous Agent Environment Section
    ab = d.get("agent_breakdown") or {}
    ab_cards = []
    for ak, av in ab.items():
        if cur_agent not in ("all", None) and ak != cur_agent:
            continue
        sess_c = av.get("sessions", 0)
        turns_c = av.get("turns", 0)
        cost_v = av.get("cost_usd", 0.0)
        toks_v = av.get("prompt_tokens", 0) + av.get("output_tokens", 0)
        models_str = ", ".join(av.get("models") or ["—"])
        badge_style = (
            "color:var(--mint);border-color:#1d3b2e;background:#0F231A"
            if ak == "antigravity"
            else "color:var(--blue);border-color:#1e355b;background:#0d1c33"
        )
        ab_cards.append(
            f"<div class='st'>"
            f"<div class='k'><span class='pill' style='{badge_style};font-weight:600;padding:2px 8px'>{escape(av['label'])}</span></div>"
            f"<div class='v'>{_usd(cost_v)}</div>"
            f"<div class='n'><b class='d'>{_f(sess_c)}</b> sessions · <b class='d'>{_f(turns_c)}</b> turns</div>"
            f"<div class='n' style='margin-top:4px;color:var(--ink-4)'>{_f(toks_v)} tokens · models: {escape(models_str)}</div>"
            f"</div>"
        )

    b.append(
        _sec(
            "00",
            "HETEROGENEOUS AGENT ENV",
            "Observed agent engines.",
            "Multi-agent environment breakdown.",
            "MULTI-AGENT",
        )
    )
    b.append(f"<div class='grid'>{''.join(ab_cards)}</div>")

    # Above everything: if an agent is waiting right now, that outranks every historical
    # figure on the page. Renders to nothing the rest of the time.
    b.append(_parked_alarm(d.get("parked") or {}))

    # § 01 — fleet metrics. First because it answers "what happened" before the page moves
    # on to "what it cost" and "what to do about it".
    b.append(_fleet(d.get("fleet")))

    # § 02 — code quality & reliability
    b.append(
        _sec(
            "02",
            "CODE QUALITY & RELIABILITY",
            "Agent execution stability & test hygiene.",
            "Verification rate, rework thrash, and error recovery.",
            "LOCAL",
        )
    )
    b.append(_quality(d.get("quality")))

    peak = h.get("peak_context") or 0
    # § 03 — spend
    b.append(
        _sec(
            "03",
            "SPEND",
            "Where the money goes.",
            "List price on your transcripts.",
            "LOCAL",
        )
    )
    b.append(
        "<div class='grid'>"
        + "".join(
            [
                _st(
                    "list_price_cost",
                    _usd(billed),
                    "this scope",
                    delta=d["range"],
                    title=(
                        "Every request priced at its own model's published rate, summed:\n"
                        "  fresh/1e6 x input\n"
                        "+ cache_read/1e6 x cache_read rate\n"
                        "+ cache_write_5m/1e6 x input x 1.25\n"
                        "+ cache_write_1h/1e6 x input x 2.0\n"
                        "+ output/1e6 x output rate\n"
                        "Rates and their source are in the rate card below."
                    ),
                ),
                _st(
                    "cache_savings",
                    _usd(h.get("cache_saved_usd")),
                    "already banked, not ours",
                    delta=(
                        f"{(h.get('cache_saved_usd') or 0) / billed:.1f}x"
                        if billed
                        else ""
                    ),
                    title=(
                        "Counterfactual, not an invoice line:\n"
                        "  cache_read/1e6 x (input rate - cache_read rate)\n"
                        "i.e. what those tokens would have cost had they been sent fresh. "
                        "Anthropic's caching already banked this before ACE was involved."
                    ),
                ),
                _st(
                    "turns", _f(h.get("turns")), "sessions", delta=_f(h.get("sessions"))
                ),
                _st(
                    "cache_hit_ratio",
                    _pct(share),
                    "healthy" if share >= 0.9 else "prefix unstable",
                    delta="cached",
                    dcls="" if share >= 0.9 else "crit",
                ),
                _st(
                    "peak_context",
                    _f(peak),
                    "target 200k",
                    delta="over" if peak > 200_000 else "under",
                    dcls="warn" if peak > 200_000 else "",
                ),
            ]
        )
        + "</div>"
    )
    # The rate card sits directly under the money it explains, not in an appendix.
    b.append(_rate_card(d.get("rate_card")))

    # § 03 — token composition
    b.append(
        _sec(
            "03",
            "TOKEN COMPOSITION",
            "Almost nothing you send is new.",
            "Fresh input vs cache.",
            "LOCAL",
        )
    )
    fresh = h.get("fresh_tokens") or 0
    cr, cw = h.get("cache_read_tokens") or 0, h.get("cache_write_tokens") or 0
    b.append(
        "<div class='pan'><div class='ph'><span>~/ace/prompt_composition</span>"
        "<span class='live'><i></i>LOCAL</span></div><div class='pb'>"
        + "".join(
            [
                _bar(
                    "cache read",
                    f"{_f(cr)} · {_pct(cr/ptok if ptok else 0)}",
                    cr / ptok if ptok else 0,
                    "var(--mint)",
                ),
                _bar(
                    "cache write",
                    f"{_f(cw)} · {_pct(cw/ptok if ptok else 0)}",
                    cw / ptok if ptok else 0,
                    "var(--blue)",
                ),
                _bar(
                    "fresh input",
                    f"{_f(fresh)} · {_pct(fresh/ptok if ptok else 0, 2)}",
                    fresh / ptok if ptok else 0,
                    "var(--gold)",
                ),
            ]
        )
        + "</div></div>"
    )

    if cap.get("tool_bytes_total"):
        used, tot = cap.get("tool_bytes_unused", 0), cap["tool_bytes_total"]
        b.append(
            "<div class='pan'><div class='ph'><span>~/ace/assembled_prompt "
            "(from live capture)</span><span class='live'><i></i>LIVE</span></div>"
            "<div class='pb'>"
            + "".join(
                [
                    _bar("tool definitions", f"{_f(tot)} B", 1.0, "var(--gold)"),
                    _bar(
                        "  of which unused",
                        f"{_f(used)} B · {_pct(used/tot)}",
                        used / tot,
                        "var(--crit)",
                    ),
                    _bar(
                        "system",
                        f"{_f(cap.get('system_bytes'))} B",
                        cap.get("system_bytes", 0) / max(1, tot),
                        "var(--blue)",
                    ),
                    _bar(
                        "messages",
                        f"{_f(cap.get('messages_bytes'))} B",
                        cap.get("messages_bytes", 0) / max(1, tot),
                        "var(--mint)",
                    ),
                ]
            )
            + f"<div class='exp'>"
            f"{cap.get('tools_defined')} tools defined · {cap.get('tools_used')} used. "
            f"Definitions render first, so unused ones head the prefix and are re-read "
            f"every turn.</div></div></div>"
        )

    # § 04/05 — strategies. Rendered unconditionally: the rail links to #s04, so a section
    # that only exists once transcripts do would dangle on a fresh install (as with § 01 and
    # § 08). No scorecard is an empty state, not a missing section.
    sc = d.get("scorecards")
    if not sc:
        b.append(
            _sec(
                "04",
                "STRATEGY / ENTERPRISE",
                "Minimise cost.",
                "Nothing to simulate yet.",
                "SIMULATED",
            )
        )
        b.append(
            "<div class='pan'><div class='ph'><span>~/ace/strategies</span>"
            "<span class='live calc'><i></i>NO DATA</span></div><div class='pb'>"
            "<div class='exp'>No sessions in scope, so there is nothing to score. The "
            "strategies simulate levers against your own transcripts; widen the scope "
            "above, or use Claude Code on this machine.</div></div></div>"
        )
        b.append(
            _sec(
                "05",
                "STRATEGY / USER",
                "Maximise headroom under a token cap.",
                "Nothing to simulate yet.",
                "SIMULATED",
            )
        )
    if sc:
        b.append(
            _sec(
                "04",
                "STRATEGY / ENTERPRISE",
                "Minimise cost.",
                "Accounting levers included — they change nothing the model sees.",
                "SIMULATED",
            )
        )
        b.append(_scorecard(sc["enterprise"], billed, ptok))
        b.append(
            _sec(
                "05",
                "STRATEGY / USER",
                "Maximise headroom under a token cap.",
                "Accounting levers excluded — they convert price, not volume.",
                "SIMULATED",
            )
        )
        b.append(_scorecard(sc["user"], billed, ptok))

        safe = next((t for t in sc["enterprise"] if t["tier"] == "SAFE"), None)
        if safe:
            rows = "".join(
                f"<tr><td class='m'>{escape(lv['name'])}</td><td class='num'><b>{_usd(lv['usd'])}</b></td>"
                f"<td class='num'>{_pct(lv['usd']/billed if billed else 0)}</td>"
                f"<td class='num'>{_f(lv['tokens'])}</td>"
                f"<td><span class='tg {escape((lv['risk'] or '').replace('*',''))}'>"
                f"{escape(lv['risk'])}</span></td>"
                f"<td style='color:var(--ink-4)'>{escape(lv['why'])}</td></tr>"
                for lv in safe["levers"]
                if lv["usd"] or lv["tokens"]
            )
            b.append(
                "<div class='pan'><div class='ph'><span>~/ace/safe_tier_levers</span>"
                "<span class='live calc'><i></i>SIMULATED</span></div><div class='pb'>"
                "<table><tr><th>lever</th><th class='num'>saving</th><th class='num'>%cost</th>"
                f"<th class='num'>tokens</th><th>risk</th><th>why</th></tr>{rows}</table>"
                "</div></div>"
            )

    # § 06 — recommendations
    b.append(
        _sec(
            "06",
            "RECOMMENDATIONS",
            "What to change.",
            "Each fires off a measured threshold.",
            "LOCAL",
        )
    )
    for r in d["recommendations"]:
        risk = (r.get("risk") or "").split("—")[0].strip().replace("*", "") or "NONE"
        b.append(
            f"<div class='rec {escape(risk)}'><h3>{escape(r['title'])}</h3>"
            f"<p>{escape(r['detail'])}</p><div class='tags'>"
            f"<span class='tg s'>saves {escape(r['saving'])}</span>"
            f"<span class='tg'>{escape(r['unit'])}</span>"
            f"<span class='tg {escape(risk)}'>risk: {escape(r['risk'])}</span>"
            f"<span class='tg'>{escape(r['evidence'])}</span></div></div>"
        )

    # § 07 — local workflow skills miner & one-click installer
    local_proposals = d.get("local_skill_proposals") or []
    b.append(
        _sec(
            "07",
            "LOCAL WORKFLOW SKILLS",
            "Frictionless local skill miner.",
            "Detects repeated command patterns and generates local SKILL.md rules.",
            "LOCAL",
        )
    )
    if not local_proposals:
        b.append(
            "<div class='pan'><div class='ph'><span>~/.agents/skills</span>"
            "<span class='live calc'><i></i>MINED</span></div><div class='pb'>"
            "<div class='exp'>No repeated multi-step workflow patterns detected yet. "
            "As you use Claude Code or Antigravity for coding tasks, ACE Sidecar mines "
            "recurring command patterns (e.g. build -> test -> commit) and generates one-click SKILL.md rules.</div>"
            "</div></div>"
        )
    else:
        for sk in local_proposals:
            sk_id = escape(sk["id"])
            sk_name = escape(sk["name"])
            sk_desc = escape(sk["description"])
            sk_cmd = escape(sk["trigger_command"])
            sk_md = escape(sk["skill_md"])
            tok_saved = _f(sk["estimated_tokens_saved"])
            is_installed = bool(sk.get("installed"))
            inst_path = escape(sk.get("installed_path") or "")

            if is_installed:
                b.append(
                    f"<details class='pan' style='margin-bottom:12px;border:1px solid #1d3b2e;background:var(--surface-2);'>"
                    f"<summary class='ph' style='cursor:pointer;display:flex;align-items:center;justify-content:space-between;'>"
                    f"<span><b style='color:var(--mint);'>✓ {sk_name}</b> <code style='color:var(--ink-3);margin-left:8px;'>{sk_cmd}</code></span>"
                    f"<span class='pill on' style='background:#0F231A;color:var(--mint);border-color:#1d3b2e;'>✓ INSTALLED ({inst_path})</span>"
                    f"</summary><div class='pb' style='padding:14px;'>"
                    f"<p style='margin:0 0 10px;color:var(--ink-2);font-size:12.5px;'>{sk_desc}</p>"
                    f"<pre style='margin:0;font-family:var(--mono);font-size:11.5px;color:var(--ink-3);white-space:pre-wrap;background:var(--paper);padding:10px;border-radius:4px;'>{sk_md}</pre>"
                    f"</div></details>"
                )
            else:
                b.append(
                    f"<div class='pan' id='skill-{sk_id}' style='margin-bottom:16px;border:1px solid var(--line-2);'>"
                    f"<div class='ph' style='display:flex;align-items:center;justify-content:space-between;'>"
                    f"<span><b style='color:var(--mint);'>{sk_name}</b> <code style='color:var(--ink-3);margin-left:8px;'>{sk_cmd}</code></span>"
                    f"<span class='pill on'>{sk['occurrences']}x detected</span></div>"
                    f"<div class='pb' style='padding:16px;'>"
                    f"<p style='margin:0 0 12px;color:var(--ink-2);font-size:13px;'>{sk_desc}</p>"
                    f"<div style='background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:12px;margin-bottom:14px;'>"
                    f"<div style='font-family:var(--mono);font-size:11px;color:var(--ink-4);margin-bottom:6px;'>PROPOSED SKILL CONTENTS</div>"
                    f"<pre style='margin:0;font-family:var(--mono);font-size:12px;color:var(--ink-2);white-space:pre-wrap;'>{sk_md}</pre>"
                    f"</div>"
                    f"<div style='display:flex;align-items:center;justify-content:space-between;'>"
                    f"<span style='font-family:var(--mono);font-size:11.5px;color:var(--mint);'>Saves ~{tok_saved} input tokens per run</span>"
                    f"<button onclick='installSkill(\"{sk_id}\", this)' data-sk='{sk_md}' class='btn' style='background:var(--mint);color:#04120B;font-weight:600;padding:6px 16px;border:0;border-radius:4px;cursor:pointer;'>"
                    f"⚡ Install Skill ({sk_cmd})</button>"
                    f"</div>"
                    f"</div></div>"
                )

    # § 08 — installed coding agent skills
    installed_skills = d.get("installed_skills") or []
    b.append(
        _sec(
            "08",
            "INSTALLED CODING AGENT SKILLS",
            "Active skills on disk.",
            "Scanned across workspace (.agents/skills) and coding agent roots.",
            "LOCAL",
        )
    )
    if not installed_skills:
        b.append(
            "<div class='pan'><div class='ph'><span>~/.agents/skills</span>"
            "<span class='live calc'><i></i>NONE</span></div><div class='pb'>"
            "<div class='exp'>No installed skills found on disk yet. Click '⚡ Install Skill' "
            "above to install a mined workflow directly to your workspace.</div></div></div>"
        )
    else:
        inst_rows = "".join(
            f"<tr><td class='m'><b style='color:var(--ink);'>{escape(s['name'])}</b></td>"
            f"<td class='m'><code style='color:var(--mint);'>{escape(s['trigger_command'])}</code></td>"
            f"<td><span class='pill on'>{escape(s['agent_type'].upper())}</span></td>"
            f"<td class='m' style='color:var(--ink-3)'>{escape(_mask_home(s['installed_path']))}</td>"
            f"<td style='color:var(--ink-2);font-size:12.5px;'>{escape(s['description'])}</td></tr>"
            for s in installed_skills
        )
        b.append(
            "<div class='pan'><div class='ph'><span>Installed Assistant Skills</span>"
            f"<span class='live'><i></i>{len(installed_skills)} SKILLS INSTALLED</span></div><div class='pb'>"
            "<table><tr><th>skill name</th><th>trigger</th><th>agent</th>"
            "<th>disk location</th><th>description</th></tr>"
            f"{inst_rows}</table></div></div>"
        )

    # § 09 — session storage
    b.append(
        _sec(
            "09",
            "SESSIONS",
            "Observed agent session transcripts.",
            "On disk, this machine only — read from Claude Code, Antigravity, & Codex logs.",
            "LOCAL",
        )
    )
    files = d.get("files") or []
    rows = "".join(
        f"<tr><td class='m' style='color:var(--ink-3)'>{escape(_mask_home(fi['path']))}</td>"
        f"<td><span class='pill' style='{('color:var(--mint);border-color:#1d3b2e;background:#0F231A' if fi.get('agent_type')=='antigravity' else 'color:#10a37f;border-color:#14532d;background:#052e16' if fi.get('agent_type')=='codex' else 'color:var(--blue);border-color:#1e355b;background:#0d1c33')}'>{escape(fi.get('agent_type', 'claude'))}</span></td>"
        f"<td class='m' style='color:var(--ink-4)'>{escape(_mask_home(fi['project'] or '—'))}</td>"
        f"<td>{escape(fi['kind'])}</td><td class='num'>{_f(fi['turns'])}</td>"
        f"<td class='num'>{_kb(fi['bytes'])}</td><td class='num'>{_ago(fi['mtime'])}</td>"
        # The snippet is the session's opening prompt verbatim, so it carries whatever paths
        # the user happened to type or paste — Antigravity artifact URIs in particular.
        f"<td class='snip'>{escape(_mask_home(fi['snippet'] or '—'))}</td></tr>"
        for fi in files
    )
    b.append(
        "<div class='pan'><div class='ph'><span>~/.claude/projects, ~/.gemini/antigravity/brain &amp; ~/.codex/sessions</span>"
        f"<span class='live'><i></i>{len(files)} FILES</span></div><div class='pb'>"
        "<table><tr><th>transcript</th><th>agent</th><th>working directory</th><th>kind</th>"
        "<th class='num'>turns</th><th class='num'>size</th><th class='num'>modified</th>"
        f"<th>first prompt</th></tr>{rows}</table>"
        "<div class='exp'>Scans local session transcripts across Claude Code, Antigravity (Google), and Codex (OpenAI) agent logs on disk. "
        "Session telemetry and token metrics are normalized across heterogeneous agent engines.</div></div></div>"
    )

    # § 10 — the clock. After the money and before the live stream: historical analysis like
    # everything above, but the only figure on the page about a person's time.
    b.append(
        _sec(
            "10",
            "SESSION TIME",
            "Where the clock goes.",
            "Elapsed time in your transcripts, and who was waiting on whom.",
            "LOCAL",
        )
    )
    b.append(_time(d.get("time") or {}, d.get("parked") or {}))

    # § 11 — live turns. Rendered unconditionally: a rail destination, so vanishing when
    # empty would turn "Sessions" into a dead link on a new install.
    b.append(
        _sec(
            "11",
            "LIVE STREAM",
            "Turns through this sidecar.",
            "Recorded to local SQLite.",
            "LIVE",
        )
    )
    if d.get("recent"):
        rows = "".join(
            f"<tr><td class='m'>{datetime.datetime.fromtimestamp(r['ts']).strftime('%H:%M:%S')}</td>"
            f"<td class='m'>{escape(r.get('model') or '')}</td><td class='num'>{_f(r.get('tokens_in'))}</td>"
            f"<td class='num'>{_f(r.get('cache_read_tokens'))}</td>"
            f"<td class='num'>{_f(r.get('cache_write_tokens'))}</td>"
            f"<td class='num'>{_f(r.get('tokens_out'))}</td>"
            f"<td class='num'>{_usd(r.get('cost_usd'))}</td></tr>"
            for r in d["recent"]
        )
        b.append(
            "<div class='pan'><div class='ph'><span>~/ace/live_turns</span>"
            "<span class='live'><i></i>LIVE</span></div><div class='pb'><table>"
            "<tr><th>time</th><th>model</th><th class='num'>fresh</th>"
            "<th class='num'>cache read</th><th class='num'>cache write</th>"
            f"<th class='num'>out</th><th class='num'>cost</th></tr>{rows}</table>"
            "</div></div>"
        )
    else:
        b.append(
            "<div class='pan'><div class='ph'><span>~/ace/live_turns</span>"
            "<span class='live calc'><i></i>NO TRAFFIC</span></div><div class='pb'>"
            "<div class='exp'>Nothing has been relayed through this sidecar yet. Point Claude "
            "Code at it with <code>ANTHROPIC_BASE_URL=http://127.0.0.1:8787</code> and this "
            "table fills in as turns land. The sections above read your existing transcripts "
            "on disk and do not need the sidecar to have seen any traffic.</div>"
            "</div></div>"
        )

    # § 13 — Prometheus Metrics Exporter & Ingestion section
    b.append(_prometheus_section(d))

    # § 11 — common questions. Last because it explains the page above it, and unconditional
    # for the same reason as § 01 and § 08: it is a rail destination.
    b.append(_qa(d.get("fleet"), d.get("rate_card") or {}))

    # § 12 — the product and the people behind it. Below the Q&A: the one section not about
    # this machine's numbers, reachable directly from the rail.
    b.append(_about())


    src = d["sources"]
    b.append(
        f"<div class='foot'>transcripts <code>{escape(str([_mask_home(p) for p in src['transcripts']]))}</code> · "
        f"telemetry <code>{escape(_mask_home(src['telemetry_db']) if src['telemetry_db'] else 'not wired')}</code> · "
        f"external <b>none</b><br>Costs are Anthropic list-price valuations — on a "
        f"subscription no dollars are actually billed. Strategy figures are simulations with "
        f"stated assumptions, not measurements. Refreshes every {REFRESH_SECONDS}s.</div>"
    )
    b.append("</div>")

    metrics_modal = """
<div id='metricsModal' class='modal-overlay' onclick='if(event.target===this)this.style.display="none"'>
  <div class='modal-content'>
    <div class='modal-header'>
      <h2>📊 Prometheus Metrics &amp; Telemetry Export</h2>
      <button class='modal-close' onclick="document.getElementById('metricsModal').style.display='none'">&times;</button>
    </div>
    <div style='margin-bottom:16px;'>
      <span class='pill on' style='background:#0F231A;color:var(--mint);border-color:#1d3b2e;'>PROMETHEUS TEXT FORMAT (v0.0.4)</span>
      <span class='pill on' style='background:#0d1c33;color:var(--blue);border-color:#1e355b;margin-left:6px;'>OPENMETRICS COMPLIANT</span>
    </div>
    <p style='color:var(--ink-2);font-size:13.5px;line-height:1.55;margin-bottom:14px;'>
      The <code>/metrics</code> endpoint streams real-time counters and gauges for time-series database scraping (Prometheus, Grafana Alloy, VictoriaMetrics, OpenTelemetry Collector, Datadog Agent).
    </p>
    <div style='display:flex;gap:10px;margin-bottom:16px;align-items:center;'>
      <input type='text' readonly id='metricsUrlInput' value='' style='flex:1;background:#0b0c0d;border:1px solid #282e30;color:var(--mint);padding:8px 12px;border-radius:4px;font-family:var(--mono);font-size:13px;' />
      <button class='btn' style='margin:0;cursor:pointer;' onclick='copyMetricsUrl(this)'>Copy URL</button>
      <a class='btn' style='margin:0;' target='_blank' rel='noopener' href='/metrics'>Open Raw</a>
    </div>
    <div style='font-weight:600;font-size:13.5px;margin-bottom:8px;'>Scrape Configuration</div>
    <div id='modalCollectorConfig' class='calcbox' style='margin:0 0 16px 0;position:relative;'>
      <pre id='modalCollectorCode' style='color:#a3e635;margin:0;font-family:var(--mono);font-size:12px;white-space:pre-wrap;'></pre>
      <button class='btn' style='position:absolute;top:10px;right:10px;padding:4px 10px;font-size:11px;cursor:pointer;' onclick='copyModalCollectorConfig(this)'>Copy Config</button>
    </div>
    <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;'>
      <span style='font-weight:600;font-size:13.5px;'>Live Stream Preview</span>
      <button class='btn' style='padding:4px 10px;font-size:12px;cursor:pointer;' onclick='loadLiveMetrics()'>Fetch Live Stream</button>
    </div>
    <div id='metricsPreviewBox' class='metrics-box' style='max-height:260px;'>Click 'Fetch Live Stream' to load current payload...</div>
  </div>
</div>
"""

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>"
        "<title>ACE — Local Coding Dashboard</title>"
        + FAVICON_LINK
        + f"<style>{_CSS}{_nav_css()}</style></head><body>"
        + _rail(d)
        + "<div class='main'>"
        + "".join(b)
        + "</div>"
        + metrics_modal
        + "<script>"
        + _nav_js()
        + """
let currentCollector = 'prometheus';

function getCollectorSnippet(type, host) {
  const h = host || window.location.host || '127.0.0.1:8787';
  if (type === 'prometheus') {
    return `# prometheus.yml
scrape_configs:
  - job_name: 'ace_sidecar'
    scrape_interval: 15s
    static_configs:
      - targets: ['` + h + `']`;
  } else if (type === 'otel') {
    return `# otel-collector.yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: 'ace_sidecar'
          scrape_interval: 15s
          static_configs:
            - targets: ['` + h + `']`;
  } else if (type === 'alloy') {
    return `// config.alloy (Grafana Alloy)
prometheus.scrape "ace_sidecar" {
  targets = [{"__address__" = "` + h + `"}]
  forward_to = [prometheus.remote_write.default.receiver]
  scrape_interval = "15s"
}`;
  } else if (type === 'datadog') {
    return `# openmetrics.d/conf.yaml (Datadog Agent)
init_config:
instances:
  - openmetrics_endpoint: http://` + h + `/metrics
    namespace: "ace"
    metrics:
      - ace_.*`;
  }
  return '';
}

function switchCollectorTab(type, btn) {
  currentCollector = type;
  const container = document.getElementById('collectorTabs');
  if (container) {
    container.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
  }
  const codeEl = document.getElementById('collectorCode');
  if (codeEl) {
    codeEl.innerText = getCollectorSnippet(type, window.location.host);
  }
}

function copyCollectorConfig(btn) {
  const codeEl = document.getElementById('collectorCode');
  if (!codeEl) return;
  navigator.clipboard.writeText(codeEl.innerText);
  const orig = btn.innerText;
  btn.innerText = '✓ Copied!';
  setTimeout(() => btn.innerText = orig, 2000);
}

function copyModalCollectorConfig(btn) {
  const codeEl = document.getElementById('modalCollectorCode');
  if (!codeEl) return;
  navigator.clipboard.writeText(codeEl.innerText);
  const orig = btn.innerText;
  btn.innerText = '✓ Copied!';
  setTimeout(() => btn.innerText = orig, 2000);
}

function copyText(txt, btn) {
  navigator.clipboard.writeText(txt);
  const orig = btn.innerText;
  btn.innerText = '✓ Copied!';
  setTimeout(() => btn.innerText = orig, 2000);
}

function copyMetricsUrl(btn) {
  const input = document.getElementById('metricsUrlInput');
  input.select();
  navigator.clipboard.writeText(input.value);
  const orig = btn.innerText;
  btn.innerText = '✓ Copied!';
  setTimeout(() => btn.innerText = orig, 2000);
}

function copyMetricsSectionUrl(btn) {
  const input = document.getElementById('metricsUrlSection');
  input.select();
  navigator.clipboard.writeText(input.value);
  const orig = btn.innerHTML;
  btn.innerHTML = '<span>✓ Copied!</span>';
  setTimeout(() => btn.innerHTML = orig, 2000);
}

function formatPrometheusOutput(rawText) {
  const lines = rawText.split('\\n');
  const out = [];
  for (let line of lines) {
    if (!line.trim()) {
      out.push('');
      continue;
    }
    if (line.startsWith('# HELP')) {
      out.push('<span style=\"color:var(--ink-3);font-style:italic;\">' + escapeHtml(line) + '</span>');
    } else if (line.startsWith('# TYPE')) {
      out.push('<span style=\"color:var(--blue);font-weight:600;\">' + escapeHtml(line) + '</span>');
    } else {
      // Metric line: e.g. ace_turns_total{agent="claude"} 33647
      const match = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(\\{[^}]*\\})?\\s+(.+)$/);
      if (match) {
        const name = match[1];
        const labels = match[2] || '';
        const val = match[3];
        let row = '<span style=\"color:var(--mint);font-weight:600;\">' + escapeHtml(name) + '</span>';
        if (labels) {
          row += '<span style=\"color:#a78bfa;\">' + escapeHtml(labels) + '</span>';
        }
        row += ' <span style=\"color:#fde047;font-weight:600;\">' + escapeHtml(val) + '</span>';
        out.push(row);
      } else {
        out.push(escapeHtml(line));
      }
    }
  }
  return out.join('\\n');
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function loadLiveMetrics() {
  const box = document.getElementById('metricsPreviewBox');
  box.innerHTML = '<span style=\"color:var(--ink-3);\">Loading metrics stream...</span>';
  try {
    const res = await fetch('/metrics');
    const text = await res.text();
    box.innerHTML = formatPrometheusOutput(text);
  } catch (err) {
    box.innerText = 'Failed to load metrics stream: ' + err.message;
  }
}

async function loadSectionLiveMetrics() {
  const box = document.getElementById('sectionMetricsPreviewBox');
  box.innerHTML = '<span style=\"color:var(--ink-3);\">Loading metrics stream...</span>';
  try {
    const res = await fetch('/metrics');
    const text = await res.text();
    box.innerHTML = formatPrometheusOutput(text);
  } catch (err) {
    box.innerText = 'Failed to load metrics stream: ' + err.message;
  }
}

function initMetricsUI() {
  const fullUrl = (window.location.origin || 'http://127.0.0.1:8787') + '/metrics';
  const inputSection = document.getElementById('metricsUrlSection');
  if (inputSection) inputSection.value = fullUrl;
  const inputModal = document.getElementById('metricsUrlInput');
  if (inputModal) inputModal.value = fullUrl;

  const codeEl = document.getElementById('collectorCode');
  if (codeEl) {
    codeEl.innerText = getCollectorSnippet(currentCollector, window.location.host);
  }
  const modalCodeEl = document.getElementById('modalCollectorCode');
  if (modalCodeEl) {
    modalCodeEl.innerText = getCollectorSnippet('prometheus', window.location.host);
  }
}

function initScopeNav() {
  document.querySelectorAll('.scope a').forEach(a => {
    a.onclick = async function(e) {
      const href = this.getAttribute('href');
      if (!href || !href.startsWith('/dashboard')) return;
      e.preventDefault();
      const mainEl = document.querySelector('.main');
      if (mainEl) mainEl.style.opacity = '0.7';
      try {
        const res = await fetch(href);
        if (res.ok) {
          const html = await res.text();
          const doc = new DOMParser().parseFromString(html, 'text/html');
          const newMain = doc.querySelector('.main');
          if (newMain && mainEl) {
            mainEl.innerHTML = newMain.innerHTML;
            mainEl.style.opacity = '1';
            // Preserve the fragment: the scope switcher changes WHICH data is shown,
            // never WHICH section the reader is in. Dropping it here scrolled them
            // back to Overview on every agent change.
            window.history.pushState({}, '', href + window.location.hash);
            initScopeNav();
            initRailNav();
            syncRailMarker();
            initRailSpy();
            initMetricsUI();
            return;
          }
        }
      } catch (err) {
        console.error('Fast filter switch failed:', err);
      }
      window.location.href = href;
    };
  });
}

window.addEventListener('popstate', async () => {
  try {
    const res = await fetch(window.location.href);
    if (res.ok) {
      const html = await res.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const newMain = doc.querySelector('.main');
      const mainEl = document.querySelector('.main');
      if (newMain && mainEl) {
        mainEl.innerHTML = newMain.innerHTML;
        initScopeNav();
        initMetricsUI();
        initRailNav();
        syncRailMarker();
        initRailSpy();
      }
    }
  } catch (e) {
    window.location.reload();
  }
});

window.addEventListener('DOMContentLoaded', () => {
  initMetricsUI();
  initScopeNav();
  initRailNav();
  syncRailMarker();
  initRailSpy();
});
if (document.readyState === 'complete' || document.readyState === 'interactive') {
  initMetricsUI();
  initScopeNav();
}

async function installSkill(skillId, btn) {
  const originalText = btn.innerText;
  btn.disabled = true;
  btn.innerText = 'Installing...';
  try {
    const rawMd = btn.getAttribute('data-sk');
    const res = await fetch('/api/skills/install', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({skill_id: skillId, skill_md: rawMd})
    });
    const data = await res.json();
    if (res.ok) {
      btn.style.background = '#3ECF8E';
      btn.innerText = '✓ Installed (' + data.installed_path + ')';
      alert('✅ Skill Installed Successfully!\\n\\nLocation: ' + data.installed_path + '\\n\\nHow to Trigger:\\n' + data.trigger_instruction);
    } else {
      btn.innerText = 'Installation failed';
      btn.disabled = false;
      alert('Error installing skill: ' + (data.detail || 'Unknown error'));
    }
  } catch (err) {
    btn.innerText = originalText;
    btn.disabled = false;
    alert('Failed to connect to local sidecar: ' + err.message);
  }
}
"""
        + "</script></body></html>"
    )

