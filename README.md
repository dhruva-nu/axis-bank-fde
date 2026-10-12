# Axis Bank Assistant — POC

An AI customer-support assistant for retail banking. **Everything is mocked except the AI**,
which is a real [Google ADK](https://google.github.io/adk-docs/) agent doing real
function-calling against a simulated core banking system.

The point of the build is that **it is fast to change while someone is watching**. Prompt,
data, branding and capabilities are all editable at runtime — no restart, no redeploy.

---

## Run it

```bash
cp .env.example .env          # then set VERTEX_AI_KEY (Vertex AI express) or GOOGLE_API_KEY (https://aistudio.google.com/apikey)
./run.sh                      # → http://localhost:8000
```

Test without the browser:

```bash
.venv/bin/python chat.py                        # interactive terminal chat
.venv/bin/python chat.py --scenario fraud       # run a canned scenario
```

---

## What's on screen

| | |
|---|---|
| **Left** | The customer's chat, styled as an Axis product. Quick-action chips, live typing. |
| **Right — Bank systems** | Live core-banking state. Block a card and the row flips to *Hotlisted* in front of the customer. Balances move, disputes appear, SR numbers land. |
| **Right — Agent trace** | Every tool call and its raw JSON result, as it happens. This is the "how does it actually work" tab. |
| **Top bar** | Switch customer profile, see the model and tool count, the **Voice** language, **Hands-free**, **Hide panel**, **Reload config**, **New chat**, and a **Tools** list. |
| **Composer** | Type, or press the **mic** and talk. Replies are read back aloud. |

**Hide panel** (or the `›` on the panel, or **Ctrl/Cmd+B**) collapses the whole right side and
centres the chat — so you can present the clean customer-facing product, then bring the
panel back the moment someone asks what's happening underneath. The choice is remembered
across refreshes, so you can start the meeting already in demo mode.

The OTP in this demo is always **123456**, and it's shown in the right-hand panel when sent —
so the flow is realistic without anyone having to fake an SMS.

---

## The second page: agent assist (`/assist`)

The chat page puts the AI *on the phone*. This page puts it *on the human agent's screen*
instead — the ROI deck's **Lever 1**. Two people talk; the assist watches the transcript and
surfaces the answer and the next action before the agent could have looked it up.

Click **Agent assist** in the top bar, hit **Start listening**, and talk. One microphone hears
both sides of the call.

| | |
|---|---|
| **Left — the call** | Live transcript, each line tagged `customer` or `agent`. Click a tag to correct it. |
| **Middle — agent assist** | Cards, grouped under whatever the customer just said. |
| **Right — core banking** | Balances, card status and the audit trail, moving as tools fire. |

Five kinds of card, which map onto the deck:

| Card | What it is |
|---|---|
| **answer** | The fact that answers the question, looked up from core banking. Lever 1. |
| **action** | What to say or do next. |
| **confirm** (`[DO]`) | A button. The assist **cannot** change anything itself — see below. |
| **offer** | A pre-approved product, on calm calls only. Lever 2. |
| **risk** | Fraud, compliance or escalation worth flagging. |

**End call & wrap up** generates the summary, disposition tag, CRM note and follow-ups from
the transcript — the "deletes wrap-up time" claim, done rather than asserted.

### The one thing to be honest about on stage

Speaker attribution is a **judgement, not a measurement**. Browser speech recognition gives a
single undiarised stream, so a model decides who spoke from the wording. It is right most of
the time and wrong sometimes — every line is one click to correct, and the correction feeds
the wrap-up. Real speaker separation is what Deepgram is for on slide 3; it isn't wired here.

If the room is too loud to demo in, the box under the transcript takes typed lines with an
explicit speaker — same pipeline, no microphone.

### Why the assist can't touch anything

The 71 tools are split by name: the 31 that only read (`get_`, `list_`, `check_`, `search_`,
`find_`, `track_`) go to the assist agent. The other 40 — blocking a card, raising a dispute,
moving money — are **held back**, and the model can only *propose* one as a `[DO]` card that a
human clicks. That's slide 6's human-in-the-loop rule enforced by the tool list rather than by
the prompt, which is the version that survives someone asking "but what if it hallucinates?".

Two guards sit behind it: a `[DO]` naming a tool that doesn't exist, or missing a required
argument, is downgraded to plain advice rather than rendered as a button that would fail. The
**Tools** list on the chat page marks every tool `read` or `write`.

Rough timings on `gemini-2.5-flash`: ~0.8s to attribute the speaker, first card at **2–3s**,
tool-backed answers by 4s.

---

## Talking to it

Press the **mic** in the composer (or **Ctrl/Cmd+M**) and speak. The transcript appears in the
box as you talk, sends itself when you stop, and the reply is read back aloud — starting on
the first sentence, so it answers while it is still being written rather than after.

**Then it keeps going.** The moment it finishes speaking, the mic reopens by itself and it is
your turn again — talk, answer, talk, answer, no clicking. **Hands-free** in the top bar is
**on by default**; that one mic tap at the start is only there because the browser will not
grant microphone permission without a click.

```
   you speak ─► agent replies (out loud) ─► mic reopens ─┐
        ▲                                                │
        └────────────────────────────────────────────────┘
```

**Esc** stops the voice. Tapping the mic while it is talking cuts it off and starts listening,
so you can interrupt it mid-sentence.

Typing does *not* pull the mic open — if you type a question, you get the reply spoken but the
mic stays shut, so a keyboard-driven demo never has the mic popping open behind you. Set
`hands_free_after_typing: true` if you want the loop to run after typed turns too.

The **Voice** dropdown sets the language for both directions. Ask a question in Hindi and it
replies in Hindi *in a Hindi voice* — the reply is scanned for Devanagari and the voice follows
it, so `मेरा कार्ड खो गया है` demos end-to-end.

The **mic** is the browser's own [Web Speech API](https://developer.mozilla.org/docs/Web/API/Web_Speech_API) —
free, no key, needs **Chrome or Edge**. The **voice** is server-side, and which one you use is
the single biggest decision here.

### Which voice — read this before demoing

`branding.json` → `voice.engine`, and `voice.provider` when the engine is `gemini`:

| | sounds like | time to first word | cost | needs |
|---|---|---|---|---|
| `engine: "browser"` | robotic on Linux, good on macOS | **instant** | free | a local speech engine |
| `engine: "gemini"`, `provider: "gemini"` *(default)* | **excellent** | **6–8s** ⚠️ | ~$0.015/min | your existing key |
| `engine: "gemini"`, `provider: "cloud"` | excellent | **~0.3s** | ~$0.016/min | GCP setup, below |

Those latencies are measured, not estimated. **Gemini's TTS is a batch model** — it does not
stream (`generate_content_stream` returns one chunk at the end), and its floor is ~2.2s even
for the single word "Done." Every sentence is requested the instant the agent finishes writing
it, several in flight at once, so synthesis overlaps generation and there are no gaps *between*
sentences — but the first sentence still leaves the customer sitting in 6–8 seconds of silence.
It sounds beautiful and it does not feel like a conversation.

**For a live demo, switch `provider` to `"cloud"`.** Google Cloud TTS returns comparable
Chirp3-HD voices in a few hundred milliseconds. It needs its own key, because an AI Studio key
is refused with `PERMISSION_DENIED`:

1. In a GCP project, enable the **Cloud Text-to-Speech API**
2. Create an API key for it, put `GOOGLE_TTS_API_KEY=...` in `.env`
3. `"provider": "cloud"` in `branding.json` → **Reload config**

Pick a voice from `gcloud ml speech`… or just try the `en-IN-Chirp3-HD-*` family; the default
is `en-IN-Chirp3-HD-Achernar`.

**The Gemini TTS model fails about 1 request in 9** — a 500 INTERNAL, or a response with no
audio in it. Measured, and independent of load: firing sentences concurrently and one at a
time fail at the same rate, so it is the API, not the caller. `assistant/tts.py` retries up to
four times with backoff, which takes it to roughly 1 in 60,000 and costs nothing on the common
path because most calls never retry. Bad requests (unknown voice name, revoked key) are not
retried — they fail in half a second.

Whatever you choose, **failure is never silence**: a TTS error returns 503 and that one
sentence is spoken by the browser voice, with a note in the chat. The next sentence tries the
server voice again. Identical text is cached server-side, so the greeting and any re-run
scenario are instant and free.

Because the voice is now server-side, **Linux no longer needs `speech-dispatcher`** — that is
only the fallback path. If you want the fallback to work too:
`sudo pacman -S speech-dispatcher espeak-ng` (Arch) or `sudo apt install speech-dispatcher
espeak-ng`, then fully restart the browser.

Three things worth knowing if it's questioned:

- **The agent is told when it's on a voice call.** `/api/chat` takes a `voice` flag, which
  flips `agent.VOICE_MODE`, which the per-turn instruction picks up — so spoken replies come
  back as short plain sentences instead of the markdown bullet lists that sound terrible read
  aloud. The same question typed gets the normal formatted answer. Edit the wording via
  `voice.agent_hint` in `branding.json`.
- **Written text and spoken text are not the same text.** `web/voice.js` rewrites the reply
  before it reaches the speaker: `₹47,320.50` → "47,320 rupees 50 paise",
  `XXXX XXXX XXXX 4291` → "ending 4291", `TAT` → "turnaround time", markdown and URLs
  dropped. Sentence splitting knows a decimal point and a URL are not full stops.
- **On a voice call the agent is told to shut up sooner.** Two or three sentences, 35 words,
  a very short opening sentence (it is spoken before the rest is written), and *no reading out
  reference numbers* — "BLK202607300001" takes four seconds aloud and helps nobody, so it says
  the reference is on screen instead. That one instruction cut a measured reply from 34
  seconds of audio to 7.

Tune it in `config/branding.json` → `voice` (then **Reload config**, no restart):

| Key | |
|---|---|
| `enabled` | master switch — `false` hides voice entirely |
| `engine` | `"gemini"` = server voice, `"browser"` = local Web Speech voice |
| `provider` | with `engine: "gemini"` — `"gemini"` (slow, lovely) or `"cloud"` (fast, lovely) |
| `gemini.voice_name` | `Kore`, `Puck`, `Aoede`, `Charon`… ~30 prebuilt voices |
| `gemini.style` | plain-English direction, e.g. *"warm, calm, like a bank agent"* — obeyed, not spoken |
| `cloud.voice_name` | e.g. `en-IN-Chirp3-HD-Achernar`; `cloud.api_key_env` names the env var |
| `lang`, `languages` | default language, and what's in the dropdown |
| `rate`, `pitch`, `volume` | `rate` is playback speed for the server voice; 1.12 default |
| `speak_replies` | `false` = dictation only, assistant stays silent |
| `auto_send` | `false` = transcribe into the box but wait for Send |
| `hands_free` | **default `true`** — reopen the mic after each spoken reply |
| `hands_free_after_typing` | `true` = a typed turn also reopens the mic |
| `voice_hint` | substring of a preferred system voice name, e.g. `"Natural"` |
| `say` | extra pronunciation fixes, `{"NRI": "N R I"}` |
| `agent_hint` | the instruction the agent gets while on a voice call |

**If someone asks for real-time speech-to-speech** — barge-in, no transcription step, natural
prosody — that is Gemini's Live API, which ADK already supports via `Runner.run_live()`. It
means a WebSocket in `server.py`, PCM capture through an AudioWorklet in the browser, and
Live-API audio pricing. Same agent, same 71 tools, roughly a day of plumbing. This build is the
free version of that, and for a demo the difference is latency and polish, not capability.

---

## Changing things mid-meeting

Everything below takes effect on the **next message** or after one click of **Reload config**.
Nothing needs a restart.

### "Can it be less formal / handle X differently / stop doing Y?"

Edit **`config/persona.md`**. It is re-read on *every single turn* — you don't even need to
click Reload. Change a line, send the next message, the behaviour has changed.

### "What if the customer had a different profile?"

Edit **`config/data/customers.json`** → Reload. Balances, card states, overdue loans, open
disputes — all of it. Add a whole new customer to the list and it appears in the profile
switcher at the top.

### "Does it know our actual charges / TAT / policy?"

Edit **`config/data/knowledge_base.json`** → Reload. Add an article with sensible `tags` and
the agent can answer that question. Complaint categories and their TATs live in the same file.

### "Can it also do «something it can't do yet»?"

Add a function to any file in **`assistant/tools/`** → Reload. That's the whole process —
tools are auto-discovered, there is no registry to update.

```python
def close_account(account_no: str, reason: str) -> dict:
    """Close a savings account. Requires verified OTP."""     # <- the model reads this
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    if not BANK.otp_ok():
        return need_otp("account closure")
    a["status"] = "Closed"
    BANK.consume_otp()
    ref = BANK.ref("CLS")
    BANK.log("account_closed", account_no, ref=ref)
    return {"ok": True, "reference": ref, "status": "Closed", "tat": "Same day"}
```

Rules: type-hinted arguments, no default values, return a dict, and write the docstring
*for the model* — that first line is the tool description it sees. Prefix a name with `_`
to keep it a private helper.

### "Make it our colours / rename it / different quick buttons"

Edit **`config/branding.json`** → Reload. Product name, logo letter, every colour, the
greeting, and the quick-action chips.

### "Can we see it on a harder/cheaper model?"

`MODEL=gemini-2.5-pro` in `.env`, restart. Default is `gemini-2.5-flash` because a demo
wants low latency.

### "This is too much — show me a focused version"

`TOOL_MODULES=accounts,cards,disputes,servicing` in `.env`, restart. Narrows the agent to
those modules only.

---

## What the agent can do — 71 tools

| Module | Covers |
|---|---|
| `accounts` (12) | Balances, mini statement, transaction search, e-statements, interest certificates, contact detail updates, nominee, cheque book, stop payment, IFSC |
| `cards` (16) | List/detail, block & hotlist, unblock, reissue, delivery tracking, limits, international/contactless/e-com/ATM switches, PIN reset, credit card bill, bill payment, EMI conversion, autopay, limit increase, reward points, redemption |
| `payments` (11) | Beneficiaries, IMPS/NEFT/RTGS transfers, transaction status, bill pay, FASTag, standing instructions, UPI status and deregistration |
| `disputes` (5) | Chargebacks with provisional credit, fraud reporting with instant hotlisting, failed-transaction reversal TATs, dispute status, withdrawal |
| `lending` (14) | Loan details, amortisation, overdue EMI payment, foreclosure quotes, loan documents, EMI date change, pre-approved offers, loan application; FD/RD listing, rates, booking, premature closure quotes and closure |
| `servicing` (11) | OTP send/verify, complaint categories, service requests with committed TATs, SR status, human escalation, callbacks, branch appointments, goodwill fee waivers, KYC status, video KYC |
| `knowledge` (2) | Policy/charges/rates lookup, branch and ATM finder |

Click **Tools** in the top bar for the full list with descriptions.

---

## Scenarios that demo well

| Say this | What happens |
|---|---|
| *"There's a ₹47,320 charge from GlobalPay London I didn't make"* | Hotlists the Magnus card **without asking for OTP** (fraud policy), opens a fraud case, quotes provisional credit in 7 working days, cites RBI zero-liability. Right panel flips to *Hotlisted* live. |
| *"My UPI payment of 12,499 to Amazon failed but money was debited"* | Finds the actual failed transaction, logs an SR, quotes the RBI T+1 auto-reversal date and the ₹100/day late-reversal compensation. |
| *"I'm travelling to Singapore, enable international on my debit card"* | Sends OTP → asks for the code → enables it → warns about DCC and forex markup. Try answering with a wrong OTP first. |
| *"Why was I charged ₹590 on my personal loan?"* | Explains the bounce, offers to clear the overdue EMI, then waives the bounce charge as a one-time goodwill gesture — and the balance actually changes. |
| *"Book an FD for 1 lakh for 2 years"* | Quotes the rate from the KB, OTPs, books it, shows the maturity amount. New FD appears in the right panel. |
| Switch to **Meera Iyer** → *"Why was ₹649 taken from my account?"* | Different customer, different data: AMB non-maintenance charge, re-KYC due, one-time waiver available. |
| *"मेरा कार्ड खो गया है"* | Replies in Hindi and blocks the card. |
| *"I want to speak to a human"* | Escalates with a case ID, queue and callback window — and passes the conversation summary across. |

---

## How it's built

```
config/                     ← everything you edit in the meeting
  persona.md                  the agent's instructions (re-read every turn)
  branding.json               colours, name, greeting, quick chips
  data/customers.json         the mock core banking data
  data/knowledge_base.json    policies, charges, rates, branches, SR categories

assistant/
  agent.py                  builds the ADK agent; injects live customer context per turn
  bank.py                   the mock CBS — state, OTP, reference numbers, audit trail
  config.py                 config loading + hot reload
  tts.py                    server-side voice: Gemini or Cloud TTS, cache, silence trim
  tools/*.py                the 71 tools, auto-discovered

server.py                   FastAPI: SSE chat stream + the demo control endpoints
web/                        the UI — no build step
  index.html, styles.css
  app.js                    chat, SSE, the live state panel
  voice.js                  mic, sentence queue, playback, fallback to the local voice

chat.py                     terminal client, for testing without the browser
```

The only real AI is `assistant/agent.py` → `google.adk.agents.Agent` running Gemini with
those 71 tools. Everything a tool returns is fabricated from `config/data/`; the *decision*
about which tool to call, in what order, with what arguments, and what to say — all of that
is the model.

### The Jev engine (chat page default — `CHAT_ENGINE=jev`)

The chat page can run with **no LLM tool calling at all**. [Jev](../jev-sdk) (TypeSafe System
One) answers typed questions — pick one label, yes/no — so every decision becomes a choice:

| Step | Who decides | How |
|---|---|---|
| Which tool | Jev | one `Choice` over the 66 routable tools' docstrings, plus `none` |
| Which card / account / loan / transaction / payee | Jev | a `Choice` built per turn from the customer's own records |
| Limit type, feature, reason, category, KB article, city… | Jev | a `Choice` over the fixed options |
| On or off | Jev | a `Noul` |
| Amount, tenure, OTP, IFSC, name | Jev | the message is cut into words (`jevchat/extract.py`); Jev picks which one it is, or none — and, for amounts and tenures, the unit (₹/lakh, months/years) |
| Descriptions, reasons, summaries | the customer's own words, verbatim |
| The reply text | Gemini, given the message + what ran + the tool output — and **no tools** |

All of a tool's parameter questions go in one Jev request. When Jev is unsure (probability
below 0.5, or confidence below 0.3) or a value was never said, nothing runs: the customer is asked, offering Jev's top
two picks, and the next message is checked with "is this the answer to what we asked?".
OTP is a small state machine: a tool's `otp_required` refusal sends the OTP and holds the
call; next turn Jev picks the code out of the reply, and verify_otp runs it.

Every Jev decision appears in the **Agent trace** as `jev · route` / `jev · params` with its
probabilities. Parameter rules live in `assistant/jevchat/slots.py`, matched by parameter
name, so a new tool with familiar parameter names is routable with no extra work. Out of
scope for now: the date tools (`email_statement`, `request_callback`, `book_appointment`)
and calling more than one tool per turn.

Two deliberate design choices worth mentioning if asked:

- **The instruction is a callable, not a string.** ADK re-evaluates it on every turn, which
  is what lets `persona.md` be hot-editable and lets the customer's live balances, card
  statuses and OTP state be injected fresh each time.
- **Tools return structured refusals, not exceptions.** A sensitive tool called without OTP
  returns `{"ok": false, "error": "otp_required", ...}` with an instruction for the model.
  The security gate is enforced in code, not in the prompt — so it holds even if someone
  talks the model into trying.

---

*Demo environment. All customers, accounts, cards and transactions are simulated.*
