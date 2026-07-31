/* Agent assist — page 2.
 *
 * One microphone hears both people on a call. Each finished utterance goes to
 * /api/assist/turn, which decides who said it and, if it was the customer,
 * streams cards onto the agent's screen.
 *
 * Nothing here is scripted: the cards come from the same ADK agent and the same
 * mock core-banking system as the chat page. The one honest caveat is speaker
 * attribution — a single undiarised stream means "who spoke" is a judgement, so
 * every line can be corrected with a click.
 */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

let recog = null;
let listening = false;
let started = null;        // when the call began, for the timer
let turnSeq = 0;
const queue = [];
let working = false;

/* ------------------------------------------------------------------- boot */

async function boot() {
  const cfg = await (await fetch('/api/config')).json();
  const t = cfg.branding.theme || {};
  const root = document.documentElement.style;
  const map = {
    burgundy: '--burgundy', burgundy_dark: '--burgundy-dark', burgundy_light: '--burgundy-light',
    ink: '--ink', muted: '--muted', line: '--line', surface: '--surface', canvas: '--canvas',
    radius: '--radius', font: '--font',
  };
  for (const [k, v] of Object.entries(map)) if (t[k]) root.setProperty(v, t[k]);

  $('logo-mark').textContent = cfg.branding.logo_mark || 'A';
  $('logo-text').textContent = cfg.branding.logo_text || '';
  $('call-name').textContent = `${cfg.customer.name} · ${cfg.customer.segment}`;

  const sel = $('customer-select');
  sel.innerHTML = '';
  (cfg.roster || []).forEach((c) => {
    const o = document.createElement('option');
    o.value = c.id;
    o.textContent = `${c.name} · ${c.segment}`;
    o.selected = c.id === cfg.customer.id;
    sel.append(o);
  });

  const tools = await (await fetch('/api/tools')).json();
  $('tool-count').textContent = `${tools.read_count} of ${tools.count}`;
  $('tool-count').title =
    `The assist agent holds the ${tools.read_count} read-only tools. The other ` +
    `${tools.count - tools.read_count} change something, so they need a human click.`;

  if (!SR) {
    $('btn-listen').disabled = true;
    $('listen-label').textContent = 'Mic unsupported';
    $('caveat').innerHTML =
      'This browser has no speech recognition — use Chrome or Edge. ' +
      'You can still drive the demo with the line box below.';
  }
  refreshBank();
}

/* -------------------------------------------------------------- listening */

function setupRecognition() {
  const r = new SR();
  r.lang = 'en-IN';
  r.continuous = true;
  r.interimResults = true;

  r.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const res = e.results[i];
      const text = res[0].transcript.trim();
      if (res.isFinal) {
        if (text) enqueue(text, null);
      } else {
        interim += res[0].transcript;
      }
    }
    $('interim').textContent = interim;
  };

  r.onerror = (e) => {
    if (e.error === 'no-speech' || e.error === 'aborted') return;   // routine
    $('call-status').textContent = `mic error: ${e.error}`;
  };

  // Chrome stops continuous recognition on its own every so often. Reopen it,
  // or the call goes deaf a minute in without saying anything.
  r.onend = () => { if (listening) { try { r.start(); } catch (_) {} } };
  return r;
}

function toggleListening() {
  listening = !listening;
  if (listening) {
    recog = recog || setupRecognition();
    try { recog.start(); } catch (_) {}
    started = started || Date.now();
    $('btn-listen').classList.add('on');
    $('listen-label').textContent = 'Stop listening';
    $('live-dot').classList.add('on');
    $('call-status').textContent = 'listening';
  } else {
    if (recog) recog.stop();
    $('btn-listen').classList.remove('on');
    $('listen-label').textContent = 'Start listening';
    $('live-dot').classList.remove('on');
    $('call-status').textContent = 'paused';
    $('interim').textContent = '';
  }
}

setInterval(() => {
  if (!started) return;
  const s = Math.floor((Date.now() - started) / 1000);
  $('call-timer').textContent =
    `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}, 1000);

/* ------------------------------------------------------------------ turns */

function enqueue(text, speaker) {
  queue.push({ text, speaker });
  pump();
}

async function pump() {
  if (working) return;
  working = true;
  while (queue.length) {
    const item = queue.shift();
    try { await runTurn(item.text, item.speaker); } catch (err) { console.error(err); }
  }
  working = false;
}

function addTurn(text, speaker) {
  if ($('transcript').querySelector('.empty')) $('transcript').innerHTML = '';
  const el = document.createElement('div');
  el.className = `turn ${speaker || 'unknown'}`;
  el.innerHTML = `
    <button class="who" title="Wrong speaker? Click to flip.">${speaker || '…'}</button>
    <div class="said">${esc(text)}</div>`;
  $('transcript').append(el);
  $('transcript').scrollTop = $('transcript').scrollHeight;
  return el;
}

async function runTurn(text, forced) {
  const line = addTurn(text, forced);
  const seq = ++turnSeq;
  let group = null;

  $('rail-sub').textContent = 'thinking…';

  const res = await fetch('/api/assist/turn', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, speaker: forced || undefined }),
  });

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let carry = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    carry += dec.decode(value, { stream: true });
    const frames = carry.split('\n\n');
    carry = frames.pop();

    for (const frame of frames) {
      const ev = /event: (.+)/.exec(frame)?.[1];
      const raw = frame.split('\n').filter((l) => l.startsWith('data: ')).map((l) => l.slice(6)).join('\n');
      if (!ev || !raw) continue;
      const data = JSON.parse(raw);

      if (ev === 'speaker') {
        line.className = `turn ${data.speaker}`;
        line.dataset.index = data.index;
        line.querySelector('.who').textContent = data.speaker;
        line.querySelector('.who').onclick = () => flip(line);
        if (data.speaker === 'customer') group = openGroup(text, seq);
      } else if (ev === 'card') {
        addCard(group, data);
      } else if (ev === 'tool_call') {
        addTool(group, data.name, data.args);
      } else if (ev === 'tool_result') {
        resolveTool(group, data.name);
        refreshBank();
      } else if (ev === 'tool_error') {
        resolveTool(group, data.name, 'failed');
      } else if (ev === 'error') {
        addCard(group, { kind: 'risk', title: 'Assist failed', body: data.message });
      } else if (ev === 'done') {
        $('rail-sub').textContent = data.assisted ? 'ready' : 'waiting for the customer';
        if (group && !group.querySelector('.card')) group.remove();
      }
    }
  }
}

async function flip(line) {
  const now = line.classList.contains('customer') ? 'agent' : 'customer';
  line.className = `turn ${now}`;
  line.querySelector('.who').textContent = now;
  await fetch('/api/assist/correct', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index: Number(line.dataset.index), speaker: now }),
  });
}

/* ------------------------------------------------------------------ cards */

function openGroup(heard, seq) {
  if ($('cards').querySelector('.empty')) $('cards').innerHTML = '';
  const g = document.createElement('div');
  g.className = 'group';
  g.innerHTML = `<div class="group-head"><span class="group-n">${seq}</span>${esc(heard)}</div>`;
  $('cards').prepend(g);
  return g;
}

const ICON = {
  answer: '&#9679;', action: '&#8594;', do: '&#9889;', offer: '&#9733;', risk: '&#9888;',
};

function addCard(group, card) {
  if (!group) return;

  // The model re-proposes an outstanding action every turn until it is run.
  // Move the live button up to the newest turn rather than stacking copies.
  if (card.kind === 'do') {
    const key = `${card.tool}:${JSON.stringify(card.args || {})}`;
    const existing = $('cards').querySelector(`.card.do[data-do="${CSS.escape(key)}"]`);
    if (existing) { group.append(existing); return; }
  }

  const el = document.createElement('div');
  el.className = `card ${card.kind}`;

  if (card.kind === 'do') {
    el.dataset.do = `${card.tool}:${JSON.stringify(card.args || {})}`;
    el.innerHTML = `
      <div class="card-top"><span class="ic">${ICON.do}</span><span class="kind">confirm</span></div>
      <div class="card-title">${esc(card.title)}</div>
      <code class="card-call">${esc(card.tool)}(${esc(JSON.stringify(card.args || {}))})</code>
      <button class="do-btn">Run this</button>
      <div class="do-out"></div>`;
    el.querySelector('.do-btn').onclick = () => runAction(el, card);
  } else {
    el.innerHTML = `
      <div class="card-top"><span class="ic">${ICON[card.kind] || ''}</span><span class="kind">${card.kind}</span></div>
      <div class="card-title">${esc(card.title)}</div>
      ${card.body ? `<div class="card-body">${esc(card.body)}</div>` : ''}`;
  }
  group.append(el);
}

async function runAction(el, card) {
  const btn = el.querySelector('.do-btn');
  btn.disabled = true;
  btn.textContent = 'Running…';
  const out = el.querySelector('.do-out');
  try {
    const r = await fetch('/api/assist/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool: card.tool, args: card.args }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'failed');
    el.classList.add('ran');
    btn.textContent = 'Done';
    out.textContent = summarise(data.result);
    refreshBank();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Retry';
    out.textContent = String(err.message || err);
  }
}

/* The interesting fields of a tool result, for a one-line confirmation. */
function summarise(result) {
  if (!result || typeof result !== 'object') return String(result ?? '');
  const keys = ['message', 'status', 'sr_no', 'dispute_id', 'reference', 'txn_id', 'tat_date', 'new_status'];
  const bits = keys.filter((k) => result[k] != null).map((k) => `${k}: ${result[k]}`);
  return bits.length ? bits.join(' · ') : JSON.stringify(result).slice(0, 180);
}

function addTool(group, name, args) {
  if (!group) return;
  const el = document.createElement('div');
  el.className = 'card tool pending';
  el.dataset.name = name;
  el.innerHTML = `<code>${esc(name)}(${esc(shortArgs(args))})</code><span class="spin">looking up…</span>`;
  group.append(el);
}

function resolveTool(group, name, state = 'ok') {
  if (!group) return;
  const el = [...group.querySelectorAll('.card.tool.pending')].find((x) => x.dataset.name === name);
  if (!el) return;
  el.classList.remove('pending');
  if (state !== 'ok') el.classList.add('failed');
  el.querySelector('.spin').textContent = state;
}

function shortArgs(args) {
  return Object.entries(args || {}).map(([k, v]) => `${k}=${String(v).slice(0, 20)}`).join(', ').slice(0, 70);
}

/* ------------------------------------------------------------ bank systems */

async function refreshBank() {
  const s = await (await fetch('/api/state')).json();
  const rows = [
    ...(s.accounts || []).map((a) => ({ ...a, group: 'Accounts' })),
    ...(s.cards || []).map((c) => ({ ...c, group: 'Cards' })),
  ];
  let html = rows.map((r) => `
    <div class="brow">
      <div><div class="bl">${esc(r.label)}</div><div class="br">${esc(r.ref)}</div></div>
      <div class="bv">${esc(r.value)}<span class="bs ${bad(r.status)}">${esc(r.status)}</span></div>
    </div>`).join('');

  if (s.audit?.length) {
    html += `<h3>Audit trail</h3>` + s.audit.slice(0, 12).map((a) => `
      <div class="arow"><b>${esc(a.action)}</b><span>${esc(a.detail)}</span></div>`).join('');
  }
  $('bank-body').innerHTML = html || '<div class="empty">No data.</div>';
}

function bad(status) {
  const s = String(status || '').toLowerCase();
  return ['hotlisted', 'blocked', 'closed', 'overdue', 'fraud'].some((w) => s.includes(w)) ? 'bad'
    : ['pending', 'open', 'under', 'dispatched'].some((w) => s.includes(w)) ? 'warn' : '';
}

/* --------------------------------------------------------------- controls */

$('btn-listen').onclick = toggleListening;

$('type-turn').onsubmit = (e) => {
  e.preventDefault();
  const text = $('type-text').value.trim();
  if (!text) return;
  $('type-text').value = '';
  enqueue(text, $('type-speaker').value || null);
};

$('customer-select').onchange = async (e) => {
  await fetch('/api/customer', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: e.target.value }),
  });
  await newCall();
  $('call-name').textContent = e.target.selectedOptions[0].textContent;
};

async function newCall() {
  await fetch('/api/assist/reset', { method: 'POST' });
  $('transcript').innerHTML = '<div class="empty">Start listening, then talk. Both sides of the call land here.</div>';
  $('cards').innerHTML = '<div class="empty">Whatever the customer asks for shows up here — looked up, not guessed.</div>';
  $('rail-sub').textContent = 'waiting for the customer';
  started = null;
  $('call-timer').textContent = '00:00';
  refreshBank();
}

$('btn-reset').onclick = newCall;

$('btn-wrapup').onclick = async () => {
  if (listening) toggleListening();
  $('btn-wrapup').disabled = true;
  $('btn-wrapup').textContent = 'Writing…';
  try {
    const r = await fetch('/api/assist/wrapup', { method: 'POST' });
    const d = await r.json();
    $('wrap-body').innerHTML = r.ok ? renderWrap(d) : `<div class="empty">${esc(d.error)}</div>`;
    $('wrap-modal').classList.remove('hidden');
  } finally {
    $('btn-wrapup').disabled = false;
    $('btn-wrapup').textContent = 'End call & wrap up';
  }
};

function renderWrap(d) {
  return `
    <div class="wrap-grid">
      <div class="wrap-tag">${esc(d.disposition)}</div>
      <div class="wrap-tag alt">${esc(d.sentiment)}</div>
      <div class="wrap-tag alt">${d.turns} turns heard</div>
    </div>
    <h4>Summary</h4><p>${esc(d.summary)}</p>
    <h4>CRM note</h4><p class="crm">${esc(d.crm_note)}</p>
    ${d.follow_ups?.length ? `<h4>Follow-ups</h4><ul>${d.follow_ups.map((f) =>
      `<li><b>${esc(f.due)}</b> — ${esc(f.what)}</li>`).join('')}</ul>` : ''}
    ${d.actions?.length ? `<h4>Executed in core banking</h4><ul>${d.actions.map((a) =>
      `<li><code>${esc(a.tool)}(${esc(JSON.stringify(a.args))})</code></li>`).join('')}</ul>`
      : '<h4>Executed in core banking</h4><p class="crm">Nothing — this call was enquiry only.</p>'}
    <p class="wrap-foot">Generated from the transcript. On slide 3 this is the wrap-up time that disappears.</p>`;
}

$('btn-close-wrap').onclick = () => $('wrap-modal').classList.add('hidden');
$('wrap-modal').onclick = (e) => { if (e.target.id === 'wrap-modal') $('wrap-modal').classList.add('hidden'); };

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'm') { e.preventDefault(); toggleListening(); }
});

boot();
