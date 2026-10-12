/* Chat UI. Talks to /api/*; all branding comes from config/branding.json. */

const $ = (id) => document.getElementById(id);
const messages = $('messages');

let busy = false;
let lastState = '';

/* ------------------------------------------------------------------ boot */

async function boot() {
  const cfg = await (await fetch('/api/config')).json();
  applyConfig(cfg);
  addBot(cfg.greeting);
  refreshState();
}

function applyConfig(cfg) {
  const b = cfg.branding;
  const t = b.theme || {};
  const root = document.documentElement.style;
  const map = {
    burgundy: '--burgundy', burgundy_dark: '--burgundy-dark', burgundy_light: '--burgundy-light',
    ink: '--ink', muted: '--muted', line: '--line', surface: '--surface', canvas: '--canvas',
    bot_bubble: '--bot-bubble', user_bubble: '--user-bubble', user_bubble_text: '--user-bubble-text',
    radius: '--radius', font: '--font',
  };
  for (const [k, v] of Object.entries(map)) if (t[k]) root.setProperty(v, t[k]);

  $('logo-mark').textContent = b.logo_mark || 'A';
  $('avatar-mark').textContent = b.logo_mark || 'A';
  $('logo-text').textContent = b.logo_text || '';
  $('product-name').textContent = b.product_short_name || b.product_name || '';
  $('chat-title').textContent = b.product_name || '';
  $('chat-sub').textContent = b.product_tagline || 'Online';
  $('disclaimer').textContent = b.disclaimer || '';
  document.title = `${b.product_name} — POC`;

  $('model-name').textContent = cfg.model;
  $('tool-count').textContent = cfg.tool_count;

  $('chips').innerHTML = '';
  (b.quick_chips || []).forEach((c) => {
    const el = document.createElement('button');
    el.className = 'chip';
    el.textContent = c.label;
    el.onclick = () => send(c.text);
    $('chips').append(el);
  });

  const sel = $('customer-select');
  sel.innerHTML = '';
  (cfg.roster || []).forEach((c) => {
    const o = document.createElement('option');
    o.value = c.id;
    o.textContent = `${c.name} · ${c.segment}`;
    o.selected = c.id === cfg.customer.id;
    sel.append(o);
  });

  $('tab-trace').classList.toggle('hidden', false);

  applyVoiceConfig(b.voice);
}

/* ----------------------------------------------------------------- voice */

/* Wired here rather than in voice.js so the voice layer stays a self-contained
 * thing you can lift out. It only knows about callbacks, not about this DOM. */

let pendingSpeech = null;   // heard while a reply was still streaming

function applyVoiceConfig(voiceCfg) {
  const info = Voice.init(voiceCfg, {
    isBusy: () => busy,
    onTranscript: (text, isFinal) => {
      $('input').value = text.trim();
      $('input').classList.toggle('hearing', !isFinal);
    },
    onFinal: (text) => {
      $('input').classList.remove('hearing');
      if (busy) pendingSpeech = text;   // let the current reply finish, then ask
      else send(text, true);
    },
    onError: (msg) => toast(msg),
    onStateChange: paintVoice,
  });

  const sel = $('voice-lang');
  sel.innerHTML = '';
  const options = [{ code: 'off', label: 'Off' }, ...(Voice.languages() || [])];
  options.forEach((l) => {
    const o = document.createElement('option');
    o.value = l.code;
    o.textContent = l.label;
    o.selected = l.code === Voice.lang();
    sel.append(o);
  });

  if (!info.supported.listen) {
    $('mic').classList.add('unsupported');
    $('mic').title = Voice.unsupportedReason();
  }
  paintVoice({ listening: false, speaking: false, handsFree: Voice.handsFree(), canListen: info.supported.listen });
}

function paintVoice(s) {
  const mic = $('mic');
  mic.classList.toggle('listening', Boolean(s.listening));
  mic.classList.toggle('speaking', Boolean(s.speaking));
  mic.classList.toggle('unsupported', s.canListen === false);
  mic.title = s.canListen === false ? Voice.unsupportedReason()
    : s.speaking ? 'Interrupt and speak (Esc just stops the voice)'
    : s.listening ? 'Listening — click to stop'
    : 'Speak (Ctrl+M)';
  $('btn-handsfree').classList.toggle('on', Boolean(s.handsFree));
}

$('mic').onclick = () => Voice.toggle();

$('voice-lang').onchange = (e) => {
  Voice.setLang(e.target.value);
  if (e.target.value === 'off') $('btn-handsfree').classList.remove('on');
};

$('btn-handsfree').onclick = () => {
  const on = !Voice.handsFree();
  Voice.setHandsFree(on);
  if (on) Voice.start();   // the click is the user gesture the browser wants
};

/* -------------------------------------------------------------- messages */

function bubble(cls, html) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${cls}`;
  const b = document.createElement('div');
  b.className = 'bubble';
  b.innerHTML = html;
  wrap.append(b);
  messages.append(wrap);
  scroll();
  return b;
}

function addUser(text) { bubble('user', esc(text)); }
function addBot(text) { return bubble('bot', md(text)); }

function addToolLine(name, args) {
  const el = document.createElement('div');
  el.className = 'tool-line';
  const a = Object.keys(args || {}).length ? ` (${esc(shortArgs(args))})` : '';
  el.innerHTML = `<svg viewBox="0 0 24 24" width="12" height="12"><path fill="currentColor" d="M22 19l-6-6 2-2-4-4-2 2-6-6-4 4 6 6-2 2 4 4 2-2 6 6z"/></svg> <b>${esc(name)}</b>${a}`;
  messages.append(el);
  scroll();
}

function shortArgs(args) {
  return Object.entries(args)
    .map(([k, v]) => `${k}=${String(v).slice(0, 26)}`)
    .join(', ')
    .slice(0, 90);
}

function scroll() { messages.scrollTop = messages.scrollHeight; }

const esc = (s) => String(s ?? '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

/* Deliberately tiny markdown: bold, inline code, bullets, paragraphs. */
function md(text) {
  const lines = esc(text).split('\n');
  let out = '', inList = false;
  for (let line of lines) {
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    if (bullet) {
      if (!inList) { out += '<ul>'; inList = true; }
      out += `<li>${inline(bullet[1])}</li>`;
    } else {
      if (inList) { out += '</ul>'; inList = false; }
      out += inline(line) + '\n';
    }
  }
  if (inList) out += '</ul>';
  return out.replace(/\n+$/, '');
}
function inline(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

/* ------------------------------------------------------------------ send */

async function send(text, fromVoice) {
  if (busy || !text.trim()) return;
  busy = true;
  $('send').disabled = true;
  $('input').value = '';
  $('input').classList.remove('hearing');
  addUser(text);

  // The mic must be shut while the reply is spoken, or it transcribes the
  // assistant. Voice reopens it itself in hands-free mode once the reply ends.
  Voice.stopListening();
  Voice.stopSpeaking();
  Voice.replyStarted(fromVoice);

  const typing = bubble('bot', '<span class="typing"><i></i><i></i><i></i></span>');
  let target = null, buffer = '';

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, voice: Voice.willSpeak() }),
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

        if (ev === 'tool_call') {
          if (typing.parentElement && !target) typing.parentElement.remove();
          addToolLine(data.name, data.args);
          traceAdd(data.name, data.args, null);
        } else if (ev === 'jev') {
          // A Jev decision (route / params / slot answer): trace panel only, not the chat.
          traceAdd(data.name, data.args);
          traceResult(data.name, data.result);
        } else if (ev === 'tool_result') {
          traceResult(data.name, data.result);
          refreshState();
        } else if (ev === 'delta') {
          if (!target) {
            if (typing.parentElement) typing.parentElement.remove();
            target = addBot('');
          }
          buffer += data.text;
          target.innerHTML = md(buffer);
          Voice.speakChunk(data.text);   // speaks per sentence, not per reply
          scroll();
        } else if (ev === 'error') {
          if (typing.parentElement && !target) typing.parentElement.remove();
          const e = document.createElement('div');
          e.className = 'tool-line err';
          e.textContent = data.message;
          messages.append(e);
          scroll();
        }
      }
    }
  } catch (err) {
    if (typing.parentElement && !target) typing.parentElement.remove();
    const e = document.createElement('div');
    e.className = 'tool-line err';
    e.textContent = String(err);
    messages.append(e);
  } finally {
    if (typing.parentElement && !target) typing.parentElement.remove();
    busy = false;
    $('send').disabled = false;
    $('input').focus();
    refreshState();
    Voice.replyEnded();   // flush the last part-sentence, then re-open the mic
    if (pendingSpeech) {  // something was said over the top of the reply
      const q = pendingSpeech;
      pendingSpeech = null;
      send(q, true);
    }
  }
}

/* ----------------------------------------------------------------- trace */

const traceEl = () => $('trace-content');

function traceAdd(name, args) {
  if (traceEl().querySelector('.empty')) traceEl().innerHTML = '';
  const item = document.createElement('div');
  item.className = 'trace-item';
  item.dataset.name = name;
  item.dataset.open = 'pending';
  item.innerHTML = `
    <div class="trace-head">
      <span class="trace-name">${esc(name)}</span>
      <span class="trace-time">${new Date().toLocaleTimeString()}</span>
    </div>
    <div class="trace-body hidden">
      <div class="trace-sub">arguments</div>${esc(JSON.stringify(args, null, 2))}
      <div class="trace-sub result-label">result</div><span class="result">waiting…</span>
    </div>`;
  item.querySelector('.trace-head').onclick = () => item.querySelector('.trace-body').classList.toggle('hidden');
  traceEl().prepend(item);
}

function traceResult(name, result) {
  const item = [...traceEl().children].find((el) => el.dataset.name === name && el.dataset.open === 'pending');
  if (!item) return;
  item.dataset.open = 'done';
  item.querySelector('.result').textContent = JSON.stringify(result, null, 2);
}

/* ----------------------------------------------------------- state panel */

async function refreshState() {
  if (document.body.classList.contains('panel-hidden')) return;  // nothing to paint
  const s = await (await fetch('/api/state')).json();
  const html = renderState(s);
  if (html === lastState) return;
  const prev = lastState;
  lastState = html;
  $('state-content').innerHTML = html;
  if (prev) flashChanged();
}

function flashChanged() {
  $('state-content').querySelectorAll('.row').forEach((r) => {
    r.classList.add('flash');
    setTimeout(() => r.classList.remove('flash'), 1400);
  });
}

function statusPill(status) {
  const s = String(status || '').toLowerCase();
  const bad = ['hotlisted', 'blocked', 'closed', 'overdue', 'cancelled', 'deregistered', 'fraud'];
  const warn = ['dispatched', 'pending', 'under', 'open', 'due', 're-kyc', 'cooling', 'investigation'];
  const cls = bad.some((w) => s.includes(w)) ? 'bad' : warn.some((w) => s.includes(w)) ? 'warn' : '';
  return `<span class="pill ${cls}">${esc(status)}</span>`;
}

function group(title, rows) {
  if (!rows || !rows.length) return '';
  return `<div class="state-group"><h3>${title}</h3>${rows.map((r) => `
    <div class="row">
      <div class="row-main">
        <div class="row-label">${esc(r.label)}</div>
        <div class="row-ref">${esc(r.ref)}</div>
      </div>
      <div class="row-right">
        <div class="row-value">${esc(r.value)}</div>
        ${statusPill(r.status)}
      </div>
    </div>`).join('')}</div>`;
}

function renderState(s) {
  let html = '';
  if (s.otp) {
    html += `<div class="otp-banner">OTP sent to the customer's phone for <b style="font-size:12px">${esc(s.otp.purpose)}</b><br>
      Code: <b>${esc(s.otp.code)}</b> — ${s.otp.verified ? 'verified ✓' : 'awaiting entry'}</div>`;
  }
  html += group('Accounts', s.accounts);
  html += group('Cards', s.cards);
  html += group('Loans', s.loans);
  html += group('Deposits', s.deposits);

  if (s.disputes?.length) {
    html += group('Disputes', s.disputes.map((d) => ({
      label: d.description, ref: `${d.dispute_id} · resolve by ${d.resolution_by}`,
      value: '', status: d.status,
    })));
  }
  if (s.service_requests?.length) {
    html += group('Service requests', s.service_requests.map((r) => ({
      label: r.category, ref: `${r.sr_no} · due ${r.tat_date}`, value: '', status: r.status,
    })));
  }
  if (s.audit?.length) {
    html += `<div class="state-group"><h3>Audit trail</h3>${s.audit.map((a) => `
      <div class="row"><div class="row-main">
        <div class="row-label">${esc(a.action)}</div>
        <div class="row-ref">${esc(a.detail)}</div>
      </div><div class="row-right"><div class="row-ref">${esc(a.at)}</div></div></div>`).join('')}</div>`;
  }
  return html || '<div class="empty">No data.</div>';
}

/* -------------------------------------------------------------- controls */

$('composer').onsubmit = (e) => { e.preventDefault(); send($('input').value); };

$('btn-new').onclick = async () => {
  Voice.stopSpeaking();
  Voice.stopListening();
  await fetch('/api/new-chat', { method: 'POST' });
  messages.innerHTML = '';
  traceEl().innerHTML = '<div class="empty">No tool calls yet. Ask something.</div>';
  const cfg = await (await fetch('/api/config')).json();
  addBot(cfg.greeting);
  Voice.say(cfg.greeting);   // a click got us here, so the browser allows speech
  refreshState();
};

$('btn-reload').onclick = async () => {
  Voice.stopSpeaking();
  Voice.stopListening();
  $('btn-reload').disabled = true;
  $('btn-reload').textContent = 'Reloading…';
  try {
    const r = await (await fetch('/api/reload', { method: 'POST' })).json();
    const cfg = await (await fetch('/api/config')).json();
    applyConfig(cfg);
    messages.innerHTML = '';
    traceEl().innerHTML = '<div class="empty">No tool calls yet. Ask something.</div>';
    addBot(cfg.greeting);
    lastState = '';
    refreshState();
    toast(`Reloaded · ${r.tool_count} tools · ${r.model}`);
  } finally {
    $('btn-reload').disabled = false;
    $('btn-reload').textContent = 'Reload config';
  }
};

$('customer-select').onchange = async (e) => {
  Voice.stopSpeaking();
  Voice.stopListening();
  await fetch('/api/customer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: e.target.value }),
  });
  messages.innerHTML = '';
  traceEl().innerHTML = '<div class="empty">No tool calls yet. Ask something.</div>';
  const cfg = await (await fetch('/api/config')).json();
  addBot(cfg.greeting);
  lastState = '';
  refreshState();
};

/* Panel visibility — remembered across reloads so demo mode survives an F5. */
function setPanel(hidden) {
  document.body.classList.toggle('panel-hidden', hidden);
  $('btn-panel').textContent = hidden ? 'Show panel' : 'Hide panel';
  localStorage.setItem('panelHidden', hidden ? '1' : '');
  if (!hidden) refreshState();  // it went stale while it was off screen
}
setPanel(localStorage.getItem('panelHidden') === '1');

const togglePanel = () => setPanel(!document.body.classList.contains('panel-hidden'));
$('btn-panel').onclick = togglePanel;
$('btn-collapse').onclick = togglePanel;
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') { e.preventDefault(); togglePanel(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'm') { e.preventDefault(); Voice.toggle(); }
  if (e.key === 'Escape') { Voice.stopSpeaking(); Voice.stopListening(); }
});

$('btn-tools').onclick = async () => {
  const t = await (await fetch('/api/tools')).json();
  $('tools-body').innerHTML = Object.entries(t.modules).map(([mod, tools]) => `
    <div class="tool-module"><h4>${esc(mod)} — ${tools.length}</h4>
      ${tools.map((x) => `<div class="tool-row"><code>${esc(x.name)}</code><span>${esc(x.doc)}</span>
        <em class="tool-mode ${x.mode}">${x.mode}</em></div>`).join('')}
    </div>`).join('');
  $('tools-modal').classList.remove('hidden');
};
$('btn-close-tools').onclick = () => $('tools-modal').classList.add('hidden');
$('tools-modal').onclick = (e) => { if (e.target.id === 'tools-modal') $('tools-modal').classList.add('hidden'); };

document.querySelectorAll('.tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    $('tab-state').classList.toggle('hidden', tab.dataset.tab !== 'state');
    $('tab-trace').classList.toggle('hidden', tab.dataset.tab !== 'trace');
  };
});

function toast(msg) {
  const el = document.createElement('div');
  el.className = 'tool-line';
  el.textContent = msg;
  messages.append(el);
  scroll();
}

boot();
