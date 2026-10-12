"""Render a benchmark run (bench/results/<ts>.json) as a side-by-side HTML report.

    .venv/bin/python bench/report.py bench/results/20261010-190703.json

Picks up <ts>.verdicts.json beside it (hand-judged correctness per query) when present, and
every other run in the folder for the run-to-run consistency table. Writes <ts>.html.
"""

from __future__ import annotations

import html
import json
import re
import statistics
import sys
from pathlib import Path

VERDICT = {"ok": ("Correct", "ok"), "partial": ("Partial", "warn"), "wrong": ("Wrong", "bad"), "fail": ("Failed", "bad")}


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def md(text: str) -> str:
    """Just enough markdown for the replies: paragraphs, bullets, **bold**."""
    out, items = [], []

    def inline(s: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(s))

    def flush() -> None:
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^[*-]\s+", s):
            items.append(re.sub(r"^[*-]\s+", "", s))
        elif s:
            flush()
            out.append(f"<p>{inline(s)}</p>")
        else:
            flush()
    flush()
    return "".join(out) or '<p class="empty">(no reply)</p>'


def cost(side: dict) -> float:
    return side["gemini_cost"] + side.get("jev_cost", 0)


def chain(side: dict) -> str:
    return " → ".join(t["tool"] for t in side["tools"]) or "no tool"


def tool_chip(t: dict) -> str:
    args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in t["args"].items())
    res = t.get("result") or {}
    state = "ok" if res.get("ok") else ("hold" if res.get("error") == "otp_required" else "err")
    note = {"ok": "", "hold": "needs OTP", "err": res.get("error", "error")}[state]
    return (f'<li class="tool {state}"><code><b>{esc(t["tool"])}</b>({esc(args)})</code>'
            + (f'<span class="tnote">{esc(note)}</span>' if note else "") + "</li>")


def secs(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f}s"


def avg(xs: list) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0


def side_panel(name: str, label: str, s: dict, v: list | None) -> str:
    verdict = ""
    if v:
        text, cls = VERDICT[v[0]]
        verdict = f'<span class="pill {cls}">{text}</span>'
    why = f'<p class="why">{esc(v[1])}</p>' if v and v[1] else ""
    tools = "".join(tool_chip(t) for t in s["tools"]) or '<li class="tool none"><code>no tool called</code></li>'
    calls = f'{s["llm_calls"]} Gemini call{"s" if s["llm_calls"] != 1 else ""}'
    if name == "jev":
        calls = f'{s["jev_calls"]} Jev + ' + calls
    err = f'<p class="err">{esc(s["error"])}</p>' if s.get("error") else ""
    return f"""
    <div class="side {name}">
      <div class="side-head"><span class="eng">{label}</span>{verdict}</div>
      {why}
      <ul class="tools">{tools}</ul>
      <div class="reply">{md(s["reply"])}</div>
      {err}
      <dl class="metrics">
        <div><dt>Total</dt><dd>{secs(s["total_ms"])}</dd></div>
        <div><dt>First token</dt><dd>{secs(s["first_token_ms"])}</dd></div>
        <div><dt>Cost</dt><dd>${cost(s):.4f}</dd></div>
        <div><dt>Calls</dt><dd>{calls}</dd></div>
      </dl>
    </div>"""


def main() -> None:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text())
    meta, rows = data["meta"], data["rows"]
    vpath = path.with_suffix(".verdicts.json")
    verdicts = json.loads(vpath.read_text()) if vpath.exists() else {}
    others = sorted(p for p in path.parent.glob("*.json") if not p.name.endswith(".verdicts.json"))
    runs = [(p.stem, json.loads(p.read_text())) for p in others]
    n = len(rows)

    L = [r["llm"] for r in rows]
    J = [r["jev"] for r in rows]
    llm_t, jev_t = avg([s["total_ms"] for s in L]), avg([s["total_ms"] for s in J])
    llm_f, jev_f = avg([s["first_token_ms"] for s in L]), avg([s["first_token_ms"] for s in J])
    llm_med, jev_med = statistics.median(s["total_ms"] for s in L), statistics.median(s["total_ms"] for s in J)
    llm_c, jev_c = sum(cost(s) for s in L) / n, sum(cost(s) for s in J) / n

    def count(side: str, kind: str) -> int:
        return sum(1 for v in verdicts.values() if v[side][0] == kind)

    def tile(title: str, lv: float, jv: float, fmt, delta: str, sub: str = "") -> str:
        top = max(lv, jv) or 1
        return f"""
      <div class="tile">
        <h3>{title}</h3>
        <div class="bar-row llm"><span class="eng">LLM</span><span class="bar"><i style="width:{lv / top * 100:.1f}%"></i></span><span class="val">{fmt(lv)}</span></div>
        <div class="bar-row jev"><span class="eng">Jev</span><span class="bar"><i style="width:{jv / top * 100:.1f}%"></i></span><span class="val">{fmt(jv)}</span></div>
        <p class="delta">{delta}</p>{f'<p class="sub">{sub}</p>' if sub else ''}
      </div>"""

    tiles = (
        tile("Average response time", llm_t, jev_t, secs, f"Jev {llm_t / jev_t:.1f}× faster",
             f"Median {secs(llm_med)} vs {secs(jev_med)}")
        + tile("Time to first token", llm_f, jev_f, secs, f"Jev {llm_f / jev_f:.1f}× sooner",
               "When the reply starts appearing")
        + tile("Cost per query", llm_c, jev_c, lambda c: f"${c:.4f}",
               f"Jev {(1 - jev_c / llm_c) * 100:.0f}% cheaper",
               f"${llm_c * 1000:.2f} vs ${jev_c * 1000:.2f} per 1,000 queries")
    )
    if verdicts:
        tiles += f"""
      <div class="tile">
        <h3>Did the right thing</h3>
        <div class="score llm"><span class="eng">LLM</span><b>{count('llm', 'ok')}</b>/{n}<span class="split">{count('llm', 'partial')} partial · {count('llm', 'fail') + count('llm', 'wrong')} failed</span></div>
        <div class="score jev"><span class="eng">Jev</span><b>{count('jev', 'ok')}</b>/{n}<span class="split">{count('jev', 'partial')} partial · {count('jev', 'fail') + count('jev', 'wrong')} wrong</span></div>
        <p class="sub">Judged by hand from the tool calls and reply</p>
      </div>"""

    top = max(max(s["total_ms"] for s in L), max(s["total_ms"] for s in J))
    step = 2000 if top <= 12000 else 5000
    ticks = list(range(0, int(top) + step, step))
    scale = ticks[-1]
    tick_html = "".join(f'<span style="left:{t / scale * 100:.2f}%">{t // 1000}s</span>' for t in ticks)
    grid = "".join(f'<i style="left:{t / scale * 100:.2f}%"></i>' for t in ticks)
    chart_rows = "".join(
        f"""<div class="crow"><a class="cq" href="#q{r['n']}" title="{esc(r['query'])}">Q{r['n']}</a>
          <div class="ctrack">{grid}
            <span class="cbar llm" style="width:{r['llm']['total_ms'] / scale * 100:.2f}%"><em>{secs(r['llm']['total_ms'])}</em></span>
            <span class="cbar jev" style="width:{r['jev']['total_ms'] / scale * 100:.2f}%"><em>{secs(r['jev']['total_ms'])}</em></span>
          </div></div>"""
        for r in rows
    )

    def tok(side: list, k: str) -> int:
        return sum(s["gemini_usage"].get(k, 0) for s in side)

    gp = meta["gemini_price_per_m"]
    jev_tokens = sum(s["jev_input_tokens"] for s in J)
    cost_rows = f"""
      <tr><th>Gemini input tokens</th><td>{tok(L, 'prompt_token_count'):,}</td><td>{tok(J, 'prompt_token_count'):,}</td><td>${gp['input']}/1M</td></tr>
      <tr><th class="ind">of which cache hits</th><td>{tok(L, 'cached_content_token_count'):,}</td><td>{tok(J, 'cached_content_token_count'):,}</td><td>${gp['cached']}/1M</td></tr>
      <tr><th>Gemini output tokens</th><td>{tok(L, 'candidates_token_count'):,}</td><td>{tok(J, 'candidates_token_count'):,}</td><td>${gp['output']}/1M</td></tr>
      <tr><th>Gemini thinking tokens</th><td>{tok(L, 'thoughts_token_count'):,}</td><td>{tok(J, 'thoughts_token_count'):,}</td><td>${gp['output']}/1M</td></tr>
      <tr><th>Jev input tokens</th><td>—</td><td>{jev_tokens:,}</td><td>${meta['jev_price_per_m_input']}/1M*</td></tr>
      <tr><th>Gemini calls</th><td>{sum(s['llm_calls'] for s in L)}</td><td>{sum(s['llm_calls'] for s in J)}</td><td></td></tr>
      <tr><th>Jev calls</th><td>—</td><td>{sum(s['jev_calls'] for s in J)}</td><td></td></tr>
      <tr class="total"><th>Total for {n} queries</th><td>${sum(cost(s) for s in L):.4f}</td><td>${sum(cost(s) for s in J):.4f}</td><td></td></tr>
      <tr class="sub"><th class="ind">Jev part of that</th><td></td><td>${sum(s.get('jev_cost', 0) for s in J):.4f}</td><td></td></tr>"""

    queries = "".join(
        f"""
    <article class="q" id="q{r['n']}">
      <header><span class="qn">Q{r['n']}</span><h3>{esc(r['query'])}</h3></header>
      <div class="pair">
        {side_panel('llm', 'LLM', r['llm'], verdicts.get(str(r['n']), {}).get('llm'))}
        {side_panel('jev', 'Jev', r['jev'], verdicts.get(str(r['n']), {}).get('jev'))}
      </div>
    </article>"""
        for r in rows
    )

    run_heads = "".join(f"<th>{esc(stem[-6:-4])}:{esc(stem[-4:-2])}</th>" for stem, _ in runs)

    def cell(side: dict) -> str:
        bad = side.get("error")
        return f'<td class="{"bad" if bad else ""}" title="{esc(bad or "")}">{esc("empty reply" if bad and "empty" in bad else ("network error" if bad else chain(side)))}</td>'

    consist = ""
    for i, r in enumerate(rows):
        for side, label in (("llm", "LLM"), ("jev", "Jev")):
            cells = "".join(cell(run["rows"][i][side]) if i < len(run["rows"]) else "<td></td>" for _, run in runs)
            first = f'<th rowspan="2"><a href="#q{r["n"]}">Q{r["n"]}</a></th>' if side == "llm" else ""
            consist += f'<tr class="{side}">{first}<td class="eng">{label}</td>{cells}</tr>'
    run_avgs = "".join(
        f"<tr><th>{esc(stem[-6:-4])}:{esc(stem[-4:-2])}</th>"
        f"<td>{secs(avg([x['llm']['total_ms'] for x in run['rows']]))}</td><td>{secs(avg([x['jev']['total_ms'] for x in run['rows']]))}</td>"
        f"<td>${sum(cost(x['llm']) for x in run['rows']) / len(run['rows']):.4f}</td><td>${sum(cost(x['jev']) for x in run['rows']) / len(run['rows']):.4f}</td></tr>"
        for stem, run in runs
    )

    page = TEMPLATE.format(
        time=esc(meta["time"]), model=esc(meta["model"]), jev_model=esc(meta["jev_model"]), customer=esc(meta["customer"]),
        n=n, tiles=tiles, ticks=tick_html, chart_rows=chart_rows, cost_rows=cost_rows, queries=queries,
        run_heads=run_heads, consist=consist, run_avgs=run_avgs, nruns=len(runs),
        jev_ms=secs(avg([s.get("jev_ms") for s in J])), jev_llm_ms=secs(avg([s.get("llm_ms") for s in J])),
        jev_price=meta["jev_price_per_m_input"],
    )
    out = path.with_suffix(".html")
    out.write_text(page, encoding="utf-8")
    print(out)


TEMPLATE = """<title>LLM vs Jev Benchmark</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@500;700;800&family=Public+Sans:ital,wght@0,400;0,600;1,400&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: a lab report. Summary tiles, one to-scale latency chart, then each query as an LLM | Jev pair. */
:root {{
  --bg: #f4f6f8; --surface: #ffffff; --ink: #17212b; --muted: #5a6573; --line: #dce1e7;
  --llm: #a85d14; --llm-soft: #f7ebdd; --jev: #0d7169; --jev-soft: #dcefec;
  --ok: #2d7a33; --ok-soft: #e1f1e2; --warn: #9a6400; --warn-soft: #f8edd6; --bad: #b0281f; --bad-soft: #f9e2df;
  --display: "Schibsted Grotesk", "Helvetica Neue", Arial, sans-serif;
  --body: "Public Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #0e1318; --surface: #151c23; --ink: #e3e8ee; --muted: #94a0ae; --line: #28323d;
  --llm: #e49b55; --llm-soft: #35261a; --jev: #4fc1b5; --jev-soft: #11312f;
  --ok: #73c378; --ok-soft: #17301a; --warn: #e3ab45; --warn-soft: #362a12; --bad: #f2867d; --bad-soft: #3a1c1a;
  color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #0e1318; --surface: #151c23; --ink: #e3e8ee; --muted: #94a0ae; --line: #28323d;
  --llm: #e49b55; --llm-soft: #35261a; --jev: #4fc1b5; --jev-soft: #11312f;
  --ok: #73c378; --ok-soft: #17301a; --warn: #e3ab45; --warn-soft: #362a12; --bad: #f2867d; --bad-soft: #3a1c1a;
  color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--ink); font: 15px/1.55 var(--body); }}
.wrap {{ max-width: 1180px; margin: 0 auto; padding-inline: 20px; padding-block: 40px 72px; display: grid; gap: 44px; }}
.wrap > *, section {{ min-width: 0; }}
h1, h2, h3 {{ font-family: var(--display); text-wrap: balance; margin: 0; }}
h1 {{ font-size: clamp(30px, 5vw, 44px); font-weight: 800; letter-spacing: -0.02em; }}
h2 {{ font-size: 21px; font-weight: 700; margin-bottom: 14px; }}
.eyebrow {{ font: 600 12px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 0 0 12px; }}
.lede {{ max-width: 68ch; color: var(--muted); margin: 10px 0 0; }}
.meta {{ font: 12.5px/1.5 var(--mono); color: var(--muted); margin-top: 14px; display: flex; flex-wrap: wrap; gap: 6px 18px; }}
code, .mono {{ font-family: var(--mono); }}
a {{ color: inherit; }}

.engines {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-top: 22px; }}
.engines div {{ border-top: 3px solid var(--c); padding-top: 10px; }}
.engines .llm {{ --c: var(--llm); }} .engines .jev {{ --c: var(--jev); }}
.engines b {{ font-family: var(--display); font-size: 16px; color: var(--c); }}
.engines p {{ margin: 4px 0 0; color: var(--muted); max-width: 60ch; }}

.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }}
.tile {{ background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 18px; display: grid; gap: 9px; align-content: start; }}
.tile h3 {{ font-size: 13px; font-weight: 600; color: var(--muted); font-family: var(--body); }}
.bar-row {{ display: grid; grid-template-columns: 34px 1fr auto; gap: 10px; align-items: center; }}
.bar {{ height: 10px; background: var(--bg); border-radius: 5px; overflow: hidden; }}
.bar i {{ display: block; height: 100%; background: var(--c); border-radius: 5px; }}
.llm {{ --c: var(--llm); --soft: var(--llm-soft); }} .jev {{ --c: var(--jev); --soft: var(--jev-soft); }}
.eng {{ font: 600 11.5px/1 var(--mono); letter-spacing: .05em; color: var(--c); text-transform: uppercase; }}
.val {{ font: 600 17px/1 var(--mono); font-variant-numeric: tabular-nums; }}
.delta {{ margin: 2px 0 0; font-family: var(--display); font-weight: 700; font-size: 17px; color: var(--jev); }}
.sub {{ margin: 0; font-size: 12.5px; color: var(--muted); }}
.score {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; font-family: var(--mono); color: var(--muted); }}
.score .eng {{ width: 34px; }}
.score b {{ font-size: 24px; color: var(--ink); font-variant-numeric: tabular-nums; }}
.split {{ font-size: 12px; }}

.chart {{ background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 18px 18px 12px; }}
.legend {{ display: flex; gap: 18px; font-size: 13px; color: var(--muted); margin-bottom: 12px; flex-wrap: wrap; }}
.legend span::before {{ content: ""; display: inline-block; width: 12px; height: 10px; border-radius: 2px; background: var(--c); margin-right: 6px; vertical-align: -1px; }}
.crow {{ display: grid; grid-template-columns: 38px 1fr; gap: 8px; align-items: center; padding-block: 3px; }}
.cq {{ font: 600 12px var(--mono); color: var(--muted); text-decoration: none; }}
.cq:hover {{ color: var(--ink); }}
.ctrack {{ position: relative; display: grid; gap: 2px; }}
.ctrack > i {{ position: absolute; top: -3px; bottom: -3px; width: 1px; background: var(--line); }}
.cbar {{ position: relative; height: 11px; background: var(--c); border-radius: 0 3px 3px 0; min-width: 2px; }}
.cbar em {{ position: absolute; left: calc(100% + 6px); top: 50%; transform: translateY(-50%); font: 10.5px var(--mono); font-style: normal; color: var(--muted); white-space: nowrap; }}
.axis {{ position: relative; height: 18px; margin-left: 46px; margin-right: 0; }}
.axis span {{ position: absolute; transform: translateX(-50%); font: 11px var(--mono); color: var(--muted); top: 4px; }}

.table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }}
table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ text-align: left; padding: 8px 14px; border-bottom: 1px solid var(--line); font-size: 13.5px; }}
td {{ font-family: var(--mono); font-size: 12.5px; }}
thead th {{ font: 600 11.5px var(--mono); text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }}
th.ind {{ padding-left: 28px; font-weight: 400; color: var(--muted); }}
tr.total th, tr.total td {{ font-weight: 700; border-top: 2px solid var(--line); }}
tr:last-child th, tr:last-child td {{ border-bottom: 0; }}
.note {{ font-size: 13px; color: var(--muted); margin: 10px 0 0; max-width: 80ch; }}

.queries {{ display: grid; gap: 22px; }}
.q {{ display: grid; gap: 10px; scroll-margin-top: 16px; }}
.q > header {{ display: flex; gap: 12px; align-items: baseline; }}
.qn {{ font: 600 13px var(--mono); color: var(--muted); flex: none; }}
.q h3 {{ font-size: 18px; font-weight: 700; }}
.pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.side {{ background: var(--surface); border: 1px solid var(--line); border-top: 3px solid var(--c); border-radius: 8px; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }}
.side-head {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.side-head .eng {{ font-size: 12.5px; }}
.pill {{ font: 600 11.5px/1 var(--mono); padding: 5px 8px; border-radius: 99px; background: var(--s); color: var(--t); }}
.pill.ok {{ --s: var(--ok-soft); --t: var(--ok); }} .pill.warn {{ --s: var(--warn-soft); --t: var(--warn); }} .pill.bad {{ --s: var(--bad-soft); --t: var(--bad); }}
.why {{ margin: -2px 0 0; font-size: 13px; color: var(--muted); font-style: italic; }}
.tools {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 5px; }}
.tool {{ display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; background: var(--soft); border-radius: 5px; padding: 5px 8px; min-width: 0; }}
.tool code {{ font-size: 12px; overflow-wrap: anywhere; }}
.tool.err {{ background: var(--bad-soft); }}
.tool.none {{ background: var(--bg); color: var(--muted); }}
.tnote {{ font: 600 10.5px var(--mono); text-transform: uppercase; letter-spacing: .04em; color: var(--warn); }}
.tool.err .tnote {{ color: var(--bad); }}
.reply {{ font-size: 14px; flex: 1; }}
.reply p {{ margin: 0 0 8px; }} .reply ul {{ margin: 0 0 8px; padding-left: 18px; }} .reply li {{ margin-bottom: 2px; }}
.reply .empty {{ color: var(--bad); font-style: italic; }}
.err {{ margin: 0; font: 12px var(--mono); color: var(--bad); }}
.metrics {{ display: grid; grid-template-columns: repeat(4, auto); justify-content: start; gap: 4px 18px; margin: 0; padding-top: 10px; border-top: 1px dashed var(--line); }}
.metrics div {{ min-width: 0; }}
.metrics dt {{ font: 600 10.5px var(--mono); text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }}
.metrics dd {{ margin: 0; font: 600 13px var(--mono); font-variant-numeric: tabular-nums; }}

.consist td {{ white-space: nowrap; }}
.consist td.bad {{ color: var(--bad); font-weight: 600; }}
.consist td.eng {{ font-family: var(--mono); }}
.consist tr.llm td, .consist tr.llm th {{ border-bottom-color: transparent; }}
.method {{ display: grid; gap: 8px; max-width: 80ch; }}
.method p, .method li {{ margin: 0; color: var(--muted); }}
.method ul {{ margin: 0; padding-left: 18px; display: grid; gap: 6px; }}
.method strong {{ color: var(--ink); }}

@media (max-width: 760px) {{
  .pair {{ grid-template-columns: 1fr; }}
  .metrics {{ grid-template-columns: repeat(2, auto); }}
  .wrap {{ padding-inline: 16px; }}
}}
</style>

<div class="wrap">
  <header>
    <p class="eyebrow">Axis Bank FDE · chat engine benchmark</p>
    <h1>LLM vs Jev</h1>
    <p class="lede">{n} customer queries, each run through both chat engines as a fresh single-turn conversation against a freshly reset bank. Same customer, same tools, same persona, same Gemini model.</p>
    <div class="meta"><span>{time}</span><span>Gemini {model}</span><span>{jev_model}</span><span>customer {customer}</span></div>
    <div class="engines">
      <div class="llm"><b>LLM</b><p>The ADK agent from <code>master</code>. Gemini holds all 71 tool schemas, decides which to call and with what arguments, loops until done, and writes the reply.</p></div>
      <div class="jev"><b>Jev</b><p>Jev picks the tool and fills every argument as typed choices. The tool runs, then Gemini writes the reply from the result with no tools attached.</p></div>
    </div>
  </header>

  <section>
    <h2>Summary</h2>
    <div class="tiles">{tiles}</div>
  </section>

  <section>
    <h2>Response time per query</h2>
    <div class="chart">
      <div class="legend"><span class="llm">LLM</span><span class="jev">Jev</span></div>
      {chart_rows}
      <div class="axis">{ticks}</div>
    </div>
    <p class="note">Jev's time splits into about {jev_ms} of Jev decisions and {jev_llm_ms} of Gemini writing the reply per query.</p>
  </section>

  <section>
    <h2>Where the cost goes</h2>
    <div class="table-wrap"><table>
      <thead><tr><th></th><th>LLM</th><th>Jev</th><th>Rate</th></tr></thead>
      <tbody>{cost_rows}</tbody>
    </table></div>
    <p class="note">The LLM engine sends all 71 tool schemas on every model call, and calls the model at least twice per query (pick the tool, then write the reply), so it reads about 6,500 input tokens per call. Jev's reply call carries only the tool result and the persona.<br>* Jev has no published price; its tokens are billed here at ${jev_price} per 1M, the same as Gemini Flash input. Rerun with <code>--jev-price</code> to change it.</p>
  </section>

  <section>
    <h2>Side by side</h2>
    <div class="queries">{queries}</div>
  </section>

  <section>
    <h2>Run to run</h2>
    <p class="note" style="margin:0 0 12px">The same {n} queries were run {nruns} times. Tools called in each run:</p>
    <div class="table-wrap"><table class="consist">
      <thead><tr><th></th><th></th>{run_heads}</tr></thead>
      <tbody>{consist}</tbody>
    </table></div>
    <div class="table-wrap" style="margin-top:14px"><table>
      <thead><tr><th>Run</th><th>LLM avg time</th><th>Jev avg time</th><th>LLM $/query</th><th>Jev $/query</th></tr></thead>
      <tbody>{run_avgs}</tbody>
    </table></div>
    <p class="note">The first run counted the LLM's empty-reply call on Q5 as $0. The second run hit a network error on LLM Q7, and its LLM times are inflated by retries.</p>
  </section>

  <section class="method">
    <h2>Method</h2>
    <ul>
      <li><strong>Fresh state.</strong> Before every run the bank is reset and the conversation is new, so a card blocked by one engine is never already blocked for the other.</li>
      <li><strong>Interleaved.</strong> Each query runs on the LLM engine, then on Jev, before the next query, so API latency drift affects both alike.</li>
      <li><strong>Driven like production.</strong> The LLM engine runs through ADK with SSE streaming, as <code>server.py</code> does. Jev runs <code>JevChat.turn()</code>, as the chat page does.</li>
      <li><strong>Time</strong> is wall-clock from sending the message to the last reply token. <strong>First token</strong> is when reply text starts. A query with no reply is left out of the first-token average.</li>
      <li><strong>Cost</strong> uses Gemini 2.5 Flash paid-tier rates, with token counts from each response's usage metadata. Thinking tokens bill as output. Implicit cache hits bill at the cached rate.</li>
      <li><strong>Correctness</strong> is a hand judgement per query: did the engine take the right action, or give the right answer, without needing another turn it did not need.</li>
      <li>Single-turn only. A query that needs an OTP ends with the OTP sent, for both engines.</li>
    </ul>
  </section>
</div>
"""

if __name__ == "__main__":
    main()
