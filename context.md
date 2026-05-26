# ClawClinic — Session Context (handoff)

Single-source recap so you can compact this chat and resume on ElevenLabs voice integration. As of 2026-05-26 ~14:30.

---

## What ClawClinic is

AI receptionist for dental clinics and independent pharmacies, deployed as a Telegram bot. Books appointments, verifies insurance, answers FAQs, processes refills. Charges clinics **$1.00 USDC per confirmed booking** via x402 on GOAT Mainnet. Has an ERC-8004 on-chain identity. Parent startup: **MedPortAI**. Built for OpenClaw Hack Toronto, submission due 5:45 PM today.

Pitch line: *"Clinics lose $50–150 per missed call; 30% of calls go unanswered. We validated this with 30+ Toronto clinics through MedPortAI."*

---

## What's working

| Layer | Status |
|---|---|
| Telegram bot `@ClawClinic` | ✅ live |
| Hermes Agent (local) — organizers waived ClawUp gate | ✅ |
| ERC-8004 testnet3 (chain 48816), AgentID **304** | ✅ |
| ERC-8004 mainnet (chain 2345), AgentID **29** | ✅ `https://8004scan.io/agents/goat/29` |
| x402 mainnet merchant `clawclinic` approved | ✅ |
| x402 API keys + receiving address configured | ✅ |
| Slash commands (real Hermes plugin) | ✅ `/book /cancel /hours /insurance /refill /identity` |
| x402 order create + status flow (live mainnet) | ✅ |
| Hard-gate guardrail script (literal confirmation tokens) | ✅ |
| Telegram tool-call noise hidden (`display.platforms.telegram.tool_progress: off`) | ✅ |
| Submission form | ⏳ filling now |
| Demo rehearsal | ⏳ |
| **ElevenLabs + Twilio voice (next focus)** | ❌ not started |

---

## Wallet + on-chain facts

- **EOA wallet:** `0x9deEC91428b2637c9Bdb8B74aa8c0C0baFC88592`
- **Private key:** in `~/.evm-wallet.json` — never commit
- **Mainnet AgentID:** 29 — tx `0x2582a7ad8d268e4665ffe6bc952cfc14435dde5b3ac3141480e44607892da7a9`
- **Identity Registry (mainnet):** `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`
- **Reputation Registry (mainnet):** `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`
- **USDC mainnet:** `0x3022b87ac063DE95b1570F46f5e470F8B53112D8` (6 decimals)
- **GOAT Mainnet RPC:** `https://rpc.goat.network` · Explorer: `https://explorer.goat.network`

### Gas gotcha (already fixed)
The `evm-wallet-skill`'s `src/lib/gas.js` had a hardcoded **0.1 gwei priority-fee floor** that made tx cost ~0.0000472 BTC vs the actual ~0.00000006 BTC needed. We patched it to drop the floor when base fee is `< 0.001 gwei` (GOAT mainnet sits at ~7 wei). Without this patch, register() fails with "insufficient funds." File: `/Users/aaryaprakash/Development/random_projs/GOAT-Hackathon-2026/.claude/skills/evm-wallet-skill/src/lib/gas.js`.

---

## x402 merchant facts

- **merchant_id:** `clawclinic`
- **email:** `aaryaprakash2022@gmail.com`
- **mode:** DIRECT (event-matched ERC20 transfer)
- **API base:** `https://x402-api.goat.network`
- **API keys:** in `~/.hermes/.env` as `X402_API_KEY` / `X402_API_SECRET` (already rotated once after one leak; current set is the live one)
- **Fee balance:** funded (verified by successful order creation)
- **Order create flow returns HTTP 402** with `order_id`, `payTo`, `amount`, `accepts[]`. The `x402.py` helper treats 402 as success — that IS the x402 protocol.

---

## File map (everything we built or modified)

| Path | Purpose |
|---|---|
| `~/.hermes/SOUL.md` | Persona — "You are ClawClinic …" |
| `~/.hermes/hermes-agent/HERMES.md` | Project context — loaded every turn (gateway cwd = `~/.hermes/hermes-agent`) |
| `~/.hermes/plugins/clawclinic/__init__.py` | Hermes plugin registering 6 slash commands |
| `~/.hermes/plugins/clawclinic/plugin.yaml` | Plugin manifest |
| `~/.hermes/skills/clawclinic/SKILL.md` | Skill instructions for the booking flow |
| `~/.hermes/skills/clawclinic/x402.py` | HMAC-signed x402 helper (create/status). Returns clean error JSON. |
| `~/.hermes/skills/clawclinic/guardrails.py` | Hard-gate script (literal confirmation tokens) |
| `~/.hermes/skills/clawclinic/slots.json` | Fake clinic backend |
| `~/.hermes/config.yaml` | `plugins.enabled: [clawclinic]`, `display.platforms.telegram.tool_progress: off` |
| `~/.hermes/.env` | `X402_API_KEY`, `X402_API_SECRET`, Anthropic key |
| `/Users/aaryaprakash/Development/random_projs/GOAT-Hackathon-2026/.claude/skills/evm-wallet-skill/` | Wallet skill (registration TXs run from here) |

### Things you must NOT commit
`~/.evm-wallet.json` (private key) · `~/.hermes/.env` (API secrets) · anything from `~/.hermes/` that contains secrets.

---

## Hermes gotchas learned the hard way

1. **Prompt-injection scanner blocks "pretend" in SOUL.md / HERMES.md** — the regex hits `pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)`. Wrote "Never pretend to be human" → blocked. Use "Never claim to be human" instead.
2. **Gateway intercepts `/command` before the LLM sees it.** Slash commands need either built-in resolution, a skill, or a `register_command`-style plugin. Free text goes straight to the LLM.
3. **Plugin handler return values are sent DIRECTLY to the user** (gateway/run.py:7666). NOT injected into LLM history. So either your handler does the whole job (static commands), or you give the user enough info to make their *next* message self-sufficient (dynamic commands).
4. **`tool_progress` for Telegram defaults to `"new"`** which leaks `terminal: "python3 ..."` lines. Set to `"off"` under `display.platforms.telegram`. Also note: there's a duplicate `platforms: {}` line later in `display:` that overrides ours — delete it.
5. **User plugins require opt-in.** Add `plugins: { enabled: [clawclinic] }` at the top level of `config.yaml`. Default state is "discovered but skipped."
6. **HERMES.md is loaded from cwd up to git root.** The gateway runs from `~/.hermes/hermes-agent`. That's where the file lives.
7. **`hermes gateway restart`** picks up plugin code + display config changes. SOUL.md and HERMES.md hot-reload per turn, plugin code does NOT.

---

## Submission form answers (for `https://bit.ly/openclaw-hackathon-submission`)

1. **Agent Name:** `ClawClinic`
2. **Wallet:** `0x9deEC91428b2637c9Bdb8B74aa8c0C0baFC88592`
3. **Bot Telegram Handle:** `@ClawClinic`
4. **x402 Merchant Name:** `clawclinic`
5. **Business Use Case:** *"ClawClinic is an AI receptionist for dental clinics and independent pharmacies, deployed on Telegram. It books appointments, verifies insurance, answers FAQs, processes cancellations and refill requests. Clinics lose $50–150 per missed call; 30% of calls go unanswered. We validated this pain point with 30+ Toronto clinics through MedPortAI customer development. Clinics pay $1.00 USDC per confirmed booking via x402 — pay-for-outcome, no subscriptions."*
6. **x402 Use Case:** *"Clinics pay ClawClinic per confirmed booking. Patient picks a slot → bot creates an x402 order on GOAT Mainnet (chain 2345, $1.00 USDC DIRECT mode) → bot returns payment instructions → on PAYMENT_CONFIRMED the bot issues a BK- confirmation. x402 is the right primitive because billing is per-outcome and machine-to-machine: Stripe assumes a human checkout, subscriptions misprice low- vs high-volume clinics, x402 meters atomically per booking with a cryptographic receipt. Business case: ~16,000 Canadian dental clinics + ~11,000 pharmacies × ~5 outcomes/day × $1 = ~$1,800/year/location."*
7. **Team Leader Name:** Aarya Prakash
8. **Team Leader Telegram:** (your handle)
9. **Team Leader Email:** aaryaprakash2022@gmail.com
10–11. **Other team members:** (blank or filled)
12. **GitHub repo:** (paste after pushing — see "Repo prep" below)
13. **Continuing as business:** (your handle — yes, this is MedPortAI)
14. **Internship interest:** (your handle if yes)

---

## Repo prep (5-min job)

```bash
mkdir -p ~/projects/clawclinic && cd ~/projects/clawclinic
git init -b main
mkdir -p plugin skill
cp ~/.hermes/plugins/clawclinic/__init__.py    plugin/__init__.py
cp ~/.hermes/plugins/clawclinic/plugin.yaml    plugin/plugin.yaml
cp ~/.hermes/skills/clawclinic/x402.py         skill/x402.py
cp ~/.hermes/skills/clawclinic/guardrails.py   skill/guardrails.py
cp ~/.hermes/skills/clawclinic/slots.json      skill/slots.json
cp ~/.hermes/skills/clawclinic/SKILL.md        skill/SKILL.md
cp ~/.hermes/hermes-agent/HERMES.md            HERMES.md
cp ~/.hermes/SOUL.md                           SOUL.md
echo -e ".env\n.env.*\n__pycache__/\n*.log" > .gitignore
# Write README.md — see chat transcript for full text
git add -A && git commit -m "Initial: ClawClinic — AI receptionist on Hermes + GOAT (x402 + ERC-8004)"
gh repo create clawclinic --public --source=. --push
```

---

## Next: ElevenLabs + Twilio voice integration

**Goal:** Patients can *call a real phone number* and have a voice conversation with ClawClinic. The Twilio number routes audio to ElevenLabs (STT + voice), which calls Hermes' `/v1/chat/completions` endpoint as a "Custom LLM" for the brain. Outputs are spoken back to the caller.

**Target architecture:**
```
Phone call → Twilio number → ElevenLabs Agent → ngrok https://… → Hermes /v1/chat/completions → reply → ElevenLabs TTS → caller hears
```

### Prerequisites

- ngrok account + CLI installed
- ElevenLabs account (free tier OK)
- Twilio account + a US/CA phone number (~$1/month)
- Hermes' `api_server` gateway platform enabled

### Step 1 — Enable Hermes API server

Hermes has an `api_server` platform per `hermes-agent/hermes_cli/platforms.py:41`. It's not on by default — currently only Telegram is enabled.

```bash
hermes gateway          # find which platforms are listed
# OR edit config directly: add api_server under whatever block lists enabled gateways
```

Look for `messaging_platforms` or similar in `~/.hermes/config.yaml`. Then:

```bash
hermes gateway restart
# Confirm the API server is listening
curl http://127.0.0.1:18789/v1/chat/completions    # or whatever port it claims
```

### Step 2 — Tunnel with ngrok

```bash
ngrok http 18789      # replace 18789 with the actual port
# Note the https://abc123.ngrok-free.app URL
```

### Step 3 — Create ElevenLabs Custom-LLM agent

Two routes:

**UI route (simpler):**
- ElevenLabs dashboard → Conversational AI → new Agent
- LLM section → "Custom LLM"
- URL: `https://YOUR_NGROK_URL/v1/chat/completions`
- Auth: create a secret with your Hermes gateway token; reference it as the API key
- System prompt: leave empty or put a one-liner — Hermes will inject its own via HERMES.md/SOUL.md
- Voice: any natural-sounding voice; turn on interruptions

**API route (scriptable):** see ElevenLabs `/v1/convai/agents/create` — full curl example was in our chat.

### Step 4 — Connect Twilio

- Buy a number on Twilio (~$1)
- ElevenLabs Agent → Phone tab → enter Twilio Account SID + Auth Token + the number
- Pair it

### Step 5 — Test

Call the Twilio number. Expected:
- ClawClinic answers
- You can ask "what do you do" / "book me an appointment for tomorrow"
- The agent should reply using HERMES.md context (clinic name, slot list, insurance providers)
- Booking flow probably needs adjustment for voice: "slot 1 and your wallet" doesn't work on a phone call. **Consider a simplified voice flow**: agent collects slot preference + callback number, sends booking link via SMS. The x402 payment can still happen in-band via SMS link or via Telegram.

### Known unknowns / risks

1. **`/v1/chat/completions` shape compatibility** — ElevenLabs expects OpenAI's exact JSON shape. Hermes' server should match but verify with a curl test before wiring Twilio.
2. **Streaming** — ElevenLabs prefers streamed responses for low TTFB. Check Hermes streaming behavior on the api_server platform.
3. **State across messages** — voice calls are one session; if the agent loses context between turns the conversation breaks. Hermes' session model handles this for Telegram; confirm it does the same when the "user" is ElevenLabs.
4. **Authentication** — make sure the ngrok URL is gated by the API key ElevenLabs sends, or you'll have a wide-open LLM endpoint.
5. **Cost** — Twilio per-minute + ElevenLabs per-character + Anthropic per-token. Demo will be fine, production needs metering.

### What you can demo even if voice isn't perfect

Even a 30-second clip of "hello, ClawClinic, what are your hours" → bot speaks hours back is a massive judge moment. Don't over-build before submission.

---

## Codex scope warning

When the session resumes, **Codex was working on x402 hardening + insurance flow + 8004scan name** at the time this file was written. Files Codex was touching:

- `~/.hermes/skills/clawclinic/x402.py`
- `~/.hermes/plugins/clawclinic/__init__.py`
- `~/.hermes/SOUL.md`
- `~/.hermes/hermes-agent/HERMES.md`
- `~/.hermes/skills/clawclinic/SKILL.md`

Before resuming, **diff those files against this context** and reconcile. The handoff version shown in the chat may be slightly behind Codex's latest.

---

## Demo plan (Telegram-only baseline; voice = stretch)

1. **Problem (15 sec):** "Clinics lose $50–150 per missed call. 30% unanswered. 30+ Toronto clinics validated."
2. **What it does (15 sec):** "Telegram receptionist that books, answers FAQs, charges per booking via x402 on GOAT."
3. **Booking + x402 (40 sec):** `/book` → `1 0xPAYER` → bot creates order → send USDC from MetaMask → `status` → BK- confirmation → show tx on explorer.goat.network.
4. **Guardrails (20 sec):** "cancel all today's appointments" → bot halts with `CONFIRM CANCEL ALL`. Type "yes do it" → rejected. Type the literal → proceeds.
5. **Self-disclosure (10 sec):** "what are you?" → structured response with on-chain identity.
6. **Identity (10 sec):** Show https://8004scan.io/agents/goat/29 — agent registered on mainnet.

Total: ~110 sec. Voice version adds a 30-sec phone-call segment in slot 3.

---

## Critical-path checklist

- [ ] Submit https://bit.ly/openclaw-hackathon-submission **before 5:45 PM**
- [ ] Push GitHub repo, paste URL into form #12 (re-submit if needed)
- [ ] Rehearse 2-min demo twice end-to-end on Telegram
- [ ] Stretch: ElevenLabs + Twilio voice
- [ ] Stretch: agent-to-agent payment demo (would populate the empty panel on `https://goat-hackathon-2026.vercel.app`)

The submission is the only hard wall. Everything else is upside.
