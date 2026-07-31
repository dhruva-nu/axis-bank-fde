/* Voice, done the cheap way: the browser already ships a speech engine.
 *
 * Speech-to-text is the Web Speech API (`SpeechRecognition`), text-to-speech is
 * `speechSynthesis`. Both run in the browser — no API key, no audio leaving over
 * a websocket, no per-minute cost, and `server.py` never learns voice exists.
 *
 * Everything tunable lives in config/branding.json under "voice", so it stays
 * editable mid-demo like the rest of this build.
 *
 * Two things here are load-bearing rather than decorative:
 *   * Replies are spoken sentence-by-sentence as the SSE deltas arrive, so the
 *     voice starts within a beat of the text. Waiting for the full reply would
 *     add seconds of silence, and Chrome truncates long utterances anyway.
 *   * The mic is hard-stopped while the assistant is speaking. Otherwise it
 *     transcribes the assistant and the demo talks to itself.
 */

const Voice = (() => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  const synth = window.speechSynthesis || null;

  const DEFAULTS = {
    enabled: true,
    engine: 'gemini',      // 'gemini' = server voice (natural, ~3s); 'browser' = instant, robotic
    lang: 'en-IN',
    languages: [
      { code: 'en-IN', label: 'English (India)' },
      { code: 'hi-IN', label: 'हिन्दी' },
    ],
    rate: 1.05,
    pitch: 1,
    volume: 1,
    speak_replies: true,   // false = dictation only, assistant stays silent
    auto_send: true,       // send as soon as a sentence is transcribed
    hands_free: true,      // close the loop: after speaking, listen again
    hands_free_after_typing: false,  // true = a typed turn also reopens the mic
    voice_hint: '',        // substring of a preferred system voice name
    say: {},               // extra "written form": "spoken form" substitutions
  };

  /* Acronyms a TTS engine mangles or runs together. Applied on word boundaries. */
  const SAY = {
    TAT: 'turnaround time',
    SR: 'service request',
    AMB: 'average monthly balance',
    FD: 'fixed deposit',
    RD: 'recurring deposit',
    NRE: 'N R E',
    DCC: 'dynamic currency conversion',
    IFSC: 'I F S C',
    MMID: 'M M I D',
    CIF: 'C I F',
    ECS: 'E C S',
    NACH: 'N-ACH',
  };

  const cfg = { ...DEFAULTS };
  let hooks = {};                       // { onTranscript, onFinal, onStateChange }
  let recog = null;
  let listening = false;
  let wantListening = false;            // user asked for the mic; survives auto-restarts
  let handsFree = false;
  let muted = false;                    // TTS off for this session only
  let turnFromVoice = false;            // was the turn being answered started by the mic?

  let buffer = '';                      // reply text not yet chopped into sentences
  let queue = [];                       // {text, lang, audio?} waiting to be played, in order
  let speaking = false;
  let voices = [];
  let ttsProven = false;                // have we ever heard an utterance actually start?
  let warnedSilent = false;
  let warnedFallback = false;
  let inflight = new Set();             // AbortControllers for in-progress /api/tts calls
  let audioEl = null;
  let generation = 0;                   // bumped by stopSpeaking() to strand stale playback

  /* ------------------------------------------------------------ capability */

  const usingServerVoice = () => cfg.engine === 'gemini';
  const canListen = () => Boolean(SR) && cfg.enabled;
  /* The server voice needs no `speechSynthesis` at all — which is the point on a
   * Linux box with no speech engine installed. */
  const canSpeak = () =>
    cfg.enabled && cfg.speak_replies && !muted && (usingServerVoice() || Boolean(synth));

  /* Capability before configuration — "your browser can't" is the more useful
   * message when both are true. */
  function unsupportedReason() {
    if (!SR && !synth) return 'This browser has no Web Speech API. Use Chrome or Edge.';
    if (!SR) return 'This browser can speak but not listen. Use Chrome or Edge for the mic.';
    if (!cfg.enabled) return 'Voice is off — pick a language in the Voice menu.';
    if (!synth && !usingServerVoice()) return 'This browser can listen but not speak.';
    return '';
  }

  /* ------------------------------------------------------ text → speakable */

  /* The chat bubble and the speaker want different text. "₹12,34,567.50" reads
   * as digits-and-dots, "XXXXXXXX4291" as a spelling bee, "**Blocked**" as
   * literal asterisks. Fix all of that before it reaches the synth. */
  function speakable(text) {
    let s = String(text ?? '');

    s = s.replace(/```[\s\S]*?```/g, ' ');
    s = s.replace(/https?:\/\/\S+/g, 'the link');

    // Masked refs, before markdown stripping eats the bullet characters.
    s = s.replace(/(?:[Xx]{2,}[\s-]?)+(\d{3,4})\b/g, 'ending $1');
    s = s.replace(/[•·]{2,}\s?(\d{3,4})\b/g, 'ending $1');

    // Money. Drop a .00, speak real paise.
    s = s.replace(/₹\s?([\d,]+)\.(\d{2})\b/g, (_m, rupees, paise) =>
      paise === '00' ? `${rupees} rupees` : `${rupees} rupees ${Number(paise)} paise`);
    s = s.replace(/₹\s?([\d,]+)/g, '$1 rupees');
    s = s.replace(/\bRs\.?\s?([\d,]+)/g, '$1 rupees');

    // Markdown → nothing. Bullets become sentences so they get a pause.
    s = s.replace(/\*\*(.+?)\*\*/g, '$1');
    s = s.replace(/`([^`]+)`/g, '$1');
    s = s.replace(/^#{1,6}\s*/gm, '');
    s = s.replace(/^\s*[-*•]\s+/gm, '');
    s = s.replace(/[*_#|]/g, ' ');

    const say = { ...SAY, ...(cfg.say || {}) };
    for (const [written, spoken] of Object.entries(say)) {
      s = s.replace(new RegExp(`\\b${written}\\b`, 'g'), spoken);
    }

    return s.replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim();
  }

  /* ------------------------------------------------------------------- TTS */

  function loadVoices() {
    if (synth) voices = synth.getVoices() || [];
  }
  if (synth) {
    loadVoices();
    synth.addEventListener?.('voiceschanged', loadVoices);
  }

  /* Devanagari in the reply means the agent answered in Hindi — follow it,
   * rather than reading Hindi words with an English voice. */
  function langFor(text) {
    if (/[ऀ-ॿ]/.test(text)) return 'hi-IN';
    if (/[஀-௿]/.test(text)) return 'ta-IN';
    if (/[ఀ-౿]/.test(text)) return 'te-IN';
    if (/[ಀ-೿]/.test(text)) return 'kn-IN';
    if (/[ঀ-৿]/.test(text)) return 'bn-IN';
    if (/[઀-૿]/.test(text)) return 'gu-IN';
    return cfg.lang;
  }

  function pickVoice(lang) {
    if (!voices.length) loadVoices();
    const base = lang.split('-')[0];
    const exact = voices.filter((v) => v.lang?.replace('_', '-') === lang);
    const loose = voices.filter((v) => v.lang?.replace('_', '-').startsWith(base));
    const pool = exact.length ? exact : loose;
    if (!pool.length) return null;
    if (cfg.voice_hint) {
      const hinted = pool.find((v) => v.name.toLowerCase().includes(cfg.voice_hint.toLowerCase()));
      if (hinted) return hinted;
    }
    // Google/Microsoft neural voices sound markedly better than the fallbacks.
    return pool.find((v) => /google|natural|neural|online/i.test(v.name)) || pool[0];
  }

  /* `speechSynthesis` existing does not mean the machine can actually speak.
   * On Linux, Chrome delegates to speech-dispatcher; with no engine installed
   * getVoices() is empty and speak() is a silent no-op that reports no error.
   * Say so out loud rather than letting the demo look broken. */
  function reportSilentTts() {
    if (warnedSilent) return;
    warnedSilent = true;
    const onLinux = typeof navigator !== 'undefined' && /Linux/i.test(navigator.userAgent || '');
    const why = voices.length
      ? 'The browser accepted the text but never started speaking.'
      : 'This browser reports no text-to-speech voices installed.';
    const fix = onLinux
      ? ' On Linux, Chrome speaks through speech-dispatcher — install `speech-dispatcher` and `espeak-ng`, then restart the browser.'
      : ' Check the system volume and the OS text-to-speech voices.';
    hooks.onError?.(`Replies are not being spoken. ${why}${fix}`);
  }

  /* Per sentence, not for the session — the next sentence tries the server voice
   * again. Said once so a flaky patch doesn't fill the transcript with warnings. */
  function reportServerFallback(why) {
    if (warnedFallback) return;
    warnedFallback = true;
    hooks.onError?.(`One sentence used the browser voice — the server voice failed for it (${why}).`);
  }

  /* Gemini's TTS takes ~3-5s for a sentence and has no usable time-to-first-byte,
   * so requesting one sentence at a time and waiting would stack those delays end
   * to end. Instead every sentence is requested the instant the agent finishes
   * writing it — several are in flight at once, synthesis overlaps generation —
   * and playback simply consumes them in order. */
  function fetchAudio(text) {
    const ctl = new AbortController();
    inflight.add(ctl);
    return fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      signal: ctl.signal,
    })
      .then(async (r) => {
        if (r.ok) return r.blob();
        const detail = await r.json().catch(() => ({}));
        reportServerFallback(detail.error || `HTTP ${r.status}`);
        return null;
      })
      .catch((e) => {
        if (e.name !== 'AbortError') reportServerFallback(e.message || 'request failed');
        return null;   // null => this sentence falls back to the browser voice
      })
      .finally(() => inflight.delete(ctl));
  }

  function enqueue(text) {
    const clean = speakable(text);
    if (!clean) return;
    const item = { text: clean, lang: langFor(clean) };
    if (usingServerVoice()) item.audio = fetchAudio(clean);   // starts now, not at its turn
    queue.push(item);
    drain();
  }

  function playBlob(blob) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob);
      if (!audioEl) audioEl = new Audio();
      const done = () => { URL.revokeObjectURL(url); resolve(); };
      audioEl.onended = done;
      audioEl.onerror = done;
      audioEl.src = url;
      audioEl.volume = cfg.volume;
      audioEl.playbackRate = Math.min(Math.max(cfg.rate || 1, 0.5), 2);
      audioEl.play().catch((e) => {
        // Autoplay policy: audio needs a prior user gesture. Ours always follows
        // a click or the mic, so this is unexpected enough to surface.
        reportServerFallback(e.name === 'NotAllowedError' ? 'the browser blocked autoplay' : e.message);
        done();
      });
    });
  }

  function speakLocally(item) {
    return new Promise((resolve) => {
      if (!synth) { reportSilentTts(); return resolve(); }
      const u = new SpeechSynthesisUtterance(item.text);
      u.lang = item.lang;
      u.rate = cfg.rate;
      u.pitch = cfg.pitch;
      u.volume = cfg.volume;
      const v = pickVoice(item.lang);
      if (v) u.voice = v;

      // If nothing has started speaking by now, the engine is missing or wedged.
      const watchdog = setTimeout(() => { if (!ttsProven) reportSilentTts(); }, 1600);
      const done = () => { clearTimeout(watchdog); resolve(); };
      u.onstart = () => { ttsProven = true; clearTimeout(watchdog); };
      u.onend = done;
      u.onerror = done;   // 'interrupted' fires here on cancel(); treat it as done
      try {
        // Chrome can leave the queue paused after a cancel(); resuming a synth
        // that isn't paused is a no-op, so this is free insurance.
        synth.resume?.();
        synth.speak(u);
      } catch {
        done();
      }
    });
  }

  async function drain() {
    if (!canSpeak() || speaking || !queue.length) return;
    const item = queue.shift();
    const gen = generation;   // stopSpeaking() bumps this; anything older must not play
    speaking = true;
    emit();

    try {
      let played = false;
      if (item.audio) {
        const blob = await item.audio;
        if (gen !== generation) return;       // barged in on while it was synthesising
        if (blob) { await playBlob(blob); played = true; }
      }
      if (!played && gen === generation && synth) await speakLocally(item);
    } finally {
      if (gen === generation) {
        speaking = false;
        emit();
        if (queue.length) drain();
        else maybeAutoListen();
      }
    }
  }

  /* Not every '.' ends a sentence: ₹47,320.00, https://axisbank.com, 2:30, "Rs."
   * Cutting at those gives you "47,320 rupees" … "00 lands in 7 days", which is
   * exactly the sort of thing that lands badly in front of a client. A terminator
   * only counts if a space or the end of the settled text follows it. */
  const ABBREV = /(?:^|[\s(])(?:Rs|Mr|Mrs|Ms|Dr|Sr|Jr|No|Nos|Ltd|Pvt|vs|etc|approx|a\.m|p\.m|i\.e|e\.g)\.$/i;

  function boundaries(text) {
    const cuts = [];
    const re = /[.!?।:]+|\n+/g;
    let m;
    while ((m = re.exec(text))) {
      let end = m.index + m[0].length;
      while (end < text.length && /["'’)\]]/.test(text[end])) end++;   // let a closing quote ride along
      if (end < text.length && !/\s/.test(text[end])) continue;        // mid-token: decimal, URL, ratio
      if (ABBREV.test(text.slice(0, end))) continue;
      cuts.push(end);
    }
    return cuts;
  }

  /* Chop the streamed buffer into sentences. `।` is the Devanagari full stop.
   * A long clause with no terminator yet gets flushed at a comma, so a rambling
   * reply doesn't sit in silence. */
  function flushBuffer(force) {
    if (!canSpeak()) { buffer = ''; return; }

    // The last token may still be arriving ("₹47,3", "https://axisb"). Sanitizing
    // or cutting half a number or URL gets it wrong, so only treat text up to the
    // final whitespace as settled — unless the reply has ended and this is all of it.
    const ws = Math.max(buffer.lastIndexOf(' '), buffer.lastIndexOf('\n'));
    const settled = force ? buffer : (ws < 0 ? '' : buffer.slice(0, ws + 1));

    if (settled) {
      let from = 0;
      for (const at of boundaries(settled)) { enqueue(settled.slice(from, at)); from = at; }
      if (!from && settled.length > 180) {
        const comma = settled.lastIndexOf(',', 180);
        const at = comma > 60 ? comma + 1 : settled.lastIndexOf(' ', 180);
        if (at > 0) { enqueue(settled.slice(0, at)); from = at; }
      }
      if (from) buffer = buffer.slice(from);
    }

    if (force && buffer.trim()) { enqueue(buffer); buffer = ''; }
  }

  function stopSpeaking() {
    generation++;            // strand any drain() currently awaiting a fetch or playback
    buffer = '';
    queue = [];
    inflight.forEach((c) => { try { c.abort(); } catch {} });   // don't pay for audio nobody hears
    inflight.clear();
    if (audioEl) { try { audioEl.pause(); audioEl.removeAttribute('src'); } catch {} }
    if (synth) { try { synth.cancel(); } catch {} }
    speaking = false;
    emit();
  }

  /* ------------------------------------------------------------------- STT */

  function build() {
    const r = new SR();
    r.lang = cfg.lang;
    r.interimResults = true;
    r.maxAlternatives = 1;
    r.continuous = false;   // one utterance per turn — a pause ends it, like a chat

    r.onstart = () => { listening = true; emit(); };

    r.onresult = (e) => {
      let interim = '', final = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) final += t;
        else interim += t;
      }
      if (interim) hooks.onTranscript?.(interim, false);
      if (final.trim()) {
        hooks.onTranscript?.(final, true);
        if (cfg.auto_send) hooks.onFinal?.(final.trim());
      }
    };

    r.onerror = (e) => {
      // 'no-speech'/'aborted' are the normal ways a turn ends. Only surface real ones.
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        wantListening = false;
        hooks.onError?.('Microphone permission was denied. Allow it in the address bar and try again.');
      } else if (e.error === 'audio-capture') {
        wantListening = false;
        hooks.onError?.('No microphone found.');
      }
    };

    r.onend = () => {
      listening = false;
      const wasWanted = wantListening;
      wantListening = false;
      emit();
      // Hands-free: the turn ended on its own, so open the mic again — but not
      // while a reply is on its way or being spoken, or the mic transcribes the
      // assistant. drain() calls maybeAutoListen() once the speech drains.
      if (wasWanted && handsFree) maybeAutoListen();
    };

    return r;
  }

  function start() {
    if (!canListen()) { hooks.onError?.(unsupportedReason()); return; }
    stopSpeaking();          // barge-in: the user talking wins over the assistant
    wantListening = true;
    if (listening) return;
    if (!recog) recog = build();
    recog.lang = cfg.lang;
    try {
      recog.start();
    } catch {
      // InvalidStateError — a previous session hasn't torn down yet.
      listening = false;
    }
  }

  function stopListening() {
    wantListening = false;
    if (recog && listening) { try { recog.stop(); } catch {} }
  }

  function maybeAutoListen() {
    if (!handsFree || !canListen()) return;
    if (speaking || queue.length || hooks.isBusy?.()) return;   // wait our turn
    // A spoken turn continues as a spoken conversation. A *typed* one doesn't
    // grab the mic uninvited — someone demoing by keyboard shouldn't have the
    // mic pop open after every reply. Flip hands_free_after_typing to opt in.
    if (!turnFromVoice && !cfg.hands_free_after_typing) return;
    start();
  }

  /* ----------------------------------------------------------------- state */

  function emit() {
    hooks.onStateChange?.({
      listening,
      speaking,
      handsFree,
      muted,
      canListen: canListen(),
      canSpeak: cfg.enabled && (usingServerVoice() || Boolean(synth)),
    });
  }

  /* ------------------------------------------------------------------- api */

  return {
    /* `branding.voice` from /api/config, plus the UI callbacks. Re-callable:
     * Reload config swaps the settings without a page refresh. */
    init(voiceCfg, callbacks) {
      Object.assign(cfg, DEFAULTS, voiceCfg || {});
      if (callbacks) hooks = { ...hooks, ...callbacks };
      const saved = localStorage.getItem('voiceLang');
      if (saved && (saved === 'off' || cfg.languages.some((l) => l.code === saved))) {
        if (saved === 'off') cfg.enabled = false;
        else cfg.lang = saved;
      }
      // Tri-state: an explicit earlier choice wins, otherwise take the config
      // default. Reading it as a plain boolean would silently re-enable
      // hands-free for anyone who had deliberately switched it off.
      const savedHF = localStorage.getItem('voiceHandsFree');
      handsFree = savedHF === null ? Boolean(cfg.hands_free) : savedHF === '1';
      stopSpeaking();
      stopListening();
      loadVoices();
      emit();
      return { ...cfg, supported: { listen: Boolean(SR), speak: Boolean(synth) } };
    },

    languages: () => cfg.languages,
    lang: () => (cfg.enabled ? cfg.lang : 'off'),

    setLang(code) {
      if (code === 'off') {
        cfg.enabled = false;
        stopSpeaking();
        stopListening();
      } else {
        cfg.enabled = true;
        cfg.lang = code;
        if (recog) recog.lang = code;
      }
      localStorage.setItem('voiceLang', code);
      emit();
    },

    handsFree: () => handsFree,
    setHandsFree(on) {
      handsFree = Boolean(on);
      localStorage.setItem('voiceHandsFree', handsFree ? '1' : '');
      if (!handsFree) stopListening();
      emit();
    },

    muted: () => muted,
    setMuted(on) {
      muted = Boolean(on);
      if (muted) stopSpeaking();
      emit();
    },

    listening: () => listening,
    speaking: () => speaking,
    /* True when the next reply will actually be read aloud — server.py uses this
     * to tell the agent to answer in short spoken sentences. */
    willSpeak: () => canSpeak(),
    unsupportedReason,

    /* Mic button. Tapping it while the assistant is talking means "I want to
     * talk now" — cut the reply off and open the mic in one tap, rather than
     * making the user tap twice. Escape stops the speech without opening it. */
    toggle() {
      if (listening) stopListening();
      else start();   // start() cancels any speech in progress
    },

    start,
    stopListening,
    stopSpeaking,

    /* Reply lifecycle, driven from the SSE loop in app.js. `fromVoice` says the
     * customer spoke this turn, which is what keeps the hands-free loop going. */
    replyStarted(fromVoice) { buffer = ''; turnFromVoice = Boolean(fromVoice); },
    speakChunk(text) { buffer += text; flushBuffer(false); },
    replyEnded() {
      flushBuffer(true);
      if (!speaking && !queue.length) maybeAutoListen();
    },
    /* One-off line (greeting, error) — only when a click got us here, since
     * browsers ignore speech that isn't downstream of a user gesture. */
    say(text) { if (canSpeak()) enqueue(text); },
  };
})();
