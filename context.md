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
| Slash commands (real Hermes plugin) | ✅ `/book /cancel /hours /insurance /refill /restock /onboard /lookup /identity /menu` |
| x402 order create + status flow (live mainnet) | ✅ |
| Hard-gate guardrail script (literal confirmation tokens) | ✅ |
| Telegram tool-call noise hidden (`display.platforms.telegram.tool_progress: off`) | ✅ |
| **ElevenLabs + Twilio voice** | ✅ live, calls Hermes via local proxy on `:8643`, ngrok-exposed |
| Voice booking + status tools (ElevenLabs server tools → `/book`, `/appointment_status`) | ✅ |
| Shared bookings store between voice + Telegram (`voice/bookings.csv` + `bookings.py` lookup) | ✅ |
| **A2A procurement (PharmaSupply)** — `/restock` autonomous flow with real on-chain settlement | ✅ verified $0.50 USDC tx `0xf86370a8…` |
| Stacked spending guardrails — per-restock + rolling 24h cap | ✅ |
| **`/onboard` clinic configuration** — set spending limits, manage inventory SKUs, edit clinic facts; every write gated by `CONFIRM-ONBOARD-XXXXXX`; abort route via `/onboard abort` | ✅ |
| Audit log of every propose / apply / abort (`procurement/.onboard_audit.log`) | ✅ |
| GitHub repo `Aarya2004/ClawClinic` pushed | ✅ https://github.com/Aarya2004/ClawClinic |
| Submission form | ⏳ |
| Demo rehearsal | ⏳ |

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

### Hermes brain — lives in `~/.hermes/` on the host (snapshotted into `hermes/` in the repo)

| Path | Purpose |
|---|---|
| `~/.hermes/SOUL.md` | Persona — "You are ClawClinic …" |
| `~/.hermes/hermes-agent/HERMES.md` | Project context — loaded every turn (gateway cwd = `~/.hermes/hermes-agent`) |
| `~/.hermes/plugins/clawclinic/__init__.py` | Hermes plugin registering all ClawClinic slash commands (`/book`, `/cancel`, `/hours`, `/insurance`, `/refill`, `/restock`, `/onboard`, `/lookup`, `/identity`, `/menu`). `/restock` and `/onboard` shell out to the procurement client. |
| `~/.hermes/plugins/clawclinic/plugin.yaml` | Plugin manifest |
| `~/.hermes/skills/clawclinic/SKILL.md` | Skill instructions for the booking flow |
| `~/.hermes/skills/clawclinic/x402.py` | HMAC-signed x402 helper (create/status). Returns clean error JSON. |
| `~/.hermes/skills/clawclinic/guardrails.py` | Hard-gate script (literal confirmation tokens) — also used as the model for the `/onboard` propose/confirm pattern |
| `~/.hermes/skills/clawclinic/bookings.py` | Lookup helper that reads `voice/bookings.csv` so Telegram can answer questions about voice bookings |
| `~/.hermes/skills/clawclinic/slots.json` | Fake clinic backend |
| `~/.hermes/hermes-agent/gateway/run.py` | Patched: `_CLAWCLINIC_COMMANDS` allowlist + menu both updated to include `/restock` and `/onboard` |
| `~/.hermes/config.yaml` | `plugins.enabled: [clawclinic]`, `display.platforms.telegram.tool_progress: off`, `streaming.enabled: true` (needed for ElevenLabs SSE), API server env vars in `.env` |
| `~/.hermes/.env` | `X402_API_KEY`, `X402_API_SECRET`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, plus `API_SERVER_ENABLED=true` + `API_SERVER_KEY=<bearer>` for the OpenAI-compatible API server on port 8642 |

### Repo subdirectories — all live in `GOAT-Hackathon-2026/`

| Path | Purpose |
|---|---|
| `voice/voice_proxy.py` | Stdlib-only proxy on `127.0.0.1:8643` between ElevenLabs Conversational AI and the Hermes API server. Injects the ClawClinic persona into every chat completion (ElevenLabs sends its own generic system msg), emits an immediate `role` SSE chunk so ElevenLabs' cascade timer survives Hermes' 3-5 s TTF, and (optionally) emits a short filler if TTF runs long. Also serves `/book` and `/appointment_status` as ElevenLabs server tools. Persists bookings to `voice/bookings.csv`. |
| `voice/README.md` | Voice setup + ElevenLabs agent + Twilio pairing instructions |
| `voice/run.sh` | One-command launcher (`./run.sh` or `./run.sh tunnel`) |
| `voice/bookings.csv` | Shared booking store between voice + Telegram. Gitignored. |
| `procurement/pharmasupply_server.py` | Second local agent on `127.0.0.1:8645`. Quotes + invoices + on-chain payment verification via `eth_getTransactionReceipt` against `rpc.goat.network`. Confirms USDC Transfer event recipient + amount before marking an invoice PAID. Receive-only wallet at `procurement/.pharmasupply-wallet.json` (gitignored). |
| `procurement/procurement_client.py` | ClawClinic side of the A2A loop. Reads `inventory.json`, fetches PharmaSupply quote, **broadcasts a real USDC transfer on GOAT mainnet via the `evm-wallet-skill`**, settles with PharmaSupply, updates inventory + spend ledger. |
| `procurement/clinic_config.py` | Single source of truth for runtime-editable spending limits + inventory thresholds + clinic facts. Atomic JSON writes, propose-then-apply pattern, rolling 24h spend ledger, audit log. |
| `procurement/clinic_config.default.json` | Seed config (autonomous limit $5, daily cap $50, one SKU). Used the first time `/onboard` runs. |
| `procurement/clinic_config.json` | Runtime config. Gitignored. |
| `procurement/inventory.json` | Current on-hand quantities per SKU. |
| `procurement/.pending_restocks.json` / `.onboard_proposals.json` / `.spend_ledger.json` / `.onboard_audit.log` | Runtime state — all gitignored |
| `hermes/` | Snapshot of the Hermes brain checked into the repo for the submission. Reference-only; live versions still in `~/.hermes/`. |

### Things you must NOT commit
`~/.evm-wallet.json` (private key) · `~/.hermes/.env` (API secrets) · `procurement/.pharmasupply-wallet.json` (gitignored) · anything from `~/.hermes/` that contains secrets.

---

## Hermes gotchas learned the hard way

1. **Prompt-injection scanner blocks "pretend" in SOUL.md / HERMES.md** — the regex hits `pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)`. Wrote "Never pretend to be human" → blocked. Use "Never claim to be human" instead.
2. **Gateway intercepts `/command` before the LLM sees it.** Slash commands need either built-in resolution, a skill, or a `register_command`-style plugin. Free text goes straight to the LLM.
3. **Plugin handler return values are sent DIRECTLY to the user** (gateway/run.py:7666). NOT injected into LLM history. So either your handler does the whole job (static commands), or you give the user enough info to make their *next* message self-sufficient (dynamic commands).
4. **`tool_progress` for Telegram defaults to `"new"`** which leaks `terminal: "python3 ..."` lines. Set to `"off"` under `display.platforms.telegram`. Also note: there's a duplicate `platforms: {}` line later in `display:` that overrides ours — delete it.
5. **User plugins require opt-in.** Add `plugins: { enabled: [clawclinic] }` at the top level of `config.yaml`. Default state is "discovered but skipped."
6. **HERMES.md is loaded from cwd up to git root.** The gateway runs from `~/.hermes/hermes-agent`. That's where the file lives.
7. **`hermes gateway restart`** picks up plugin code + display config changes. SOUL.md and HERMES.md hot-reload per turn, plugin code does NOT.
8. **Strict command allowlist lives in the gateway**, not just the plugin. `_CLAWCLINIC_COMMANDS` in `~/.hermes/hermes-agent/gateway/run.py:1064` is the set the gateway accepts; anything else returns "/X is not available in ClawClinic mode" *before* the plugin handler runs. Add new commands to BOTH the plugin AND that set.
9. **`evm-wallet-skill/src/transfer.js` viem-2.x bug.** `walletClient.encodeFunctionData(...)` does not exist in viem ≥ 2.0. Patched to import `encodeFunctionData` directly from `viem` and call it as a free function. Without this patch, ERC20 transfers (including USDC restock payments) fail at gas-estimation with "encodeFunctionData is not a function". Same skill, separate patch from the gas.js fix.
10. **ElevenLabs Custom-LLM cascade-timeout gotcha.** Default `cascade_timeout_seconds` is 8 s; if Hermes takes longer than that to emit the first content token, ElevenLabs disconnects with `custom_llm_error: LLM Cascade Error`. The voice proxy emits an immediate SSE `role` chunk to keep the connection alive, and we bumped the cascade timeout to 15 s in the ElevenLabs agent. Also: ElevenLabs requests `stream: true`; you MUST set `streaming.enabled: true` in `~/.hermes/config.yaml` or the api-server returns a non-streamed JSON body and ElevenLabs rejects it.

---

## Voice integration (ElevenLabs + Twilio)

**Topology** — phone call → Twilio number → ElevenLabs agent → ngrok https → `voice_proxy.py:8643` → Hermes API server on `127.0.0.1:8642` → Anthropic.

**What the proxy does:**
- Strips ElevenLabs' generic "You are an AI agent..." system message and substitutes the ClawClinic voice persona (`CLAWCLINIC_SYSTEM` constant in `voice_proxy.py`).
- Sends an immediate `assistant`-role SSE chunk so ElevenLabs' cascade timer doesn't kill the connection during Hermes' 3-5 s TTF.
- Optional jittered filler phrase ("one moment while I check that") with `FILLER_PROBABILITY` knob — currently `0.45` to break up long silences without sounding scripted.
- Exposes `/book` and `/appointment_status` as **server tools** the ElevenLabs agent calls directly. Bookings persist to `voice/bookings.csv` with overlap detection and service-aware duration.
- The caller's phone number is plumbed in as a **Dynamic Variable** (`system__caller_id`) on the booking tool — the agent does NOT ask the caller to read out their number; ElevenLabs fills it in from the call metadata.

**ElevenLabs agent config (key fields):**
- LLM: Custom LLM, server URL `https://<ngrok>.ngrok-free.dev/v1/chat/completions`, model `hermes-agent`, API key = `API_SERVER_KEY` from `~/.hermes/.env`.
- `cascade_timeout_seconds: 15` (max — gives margin over Hermes' TTF).
- Tools: `claw_clinic_book_appointment` (POST `/book`), `claw_clinic_appointment_status` (POST `/appointment_status`).

**Shared booking history** between voice + Telegram: SOUL.md + SKILL.md reference `bookings.py lookup BK-XXXXXXXX` which reads `voice/bookings.csv`. So a caller can book by voice, then check status via Telegram with `/lookup BK-...`, or vice versa.

---

## A2A procurement (PharmaSupply) — `/restock`

**Story for judges:** ClawClinic monitors clinic supply inventory; when an item drops below threshold, it delegates procurement to PharmaSupply (a second local agent we built), receives an invoice, **broadcasts a real USDC transfer on GOAT mainnet**, and PharmaSupply confirms the payment by checking the transaction receipt against the GOAT RPC for the USDC Transfer event recipient + amount. No human in the loop for spend at or below the autonomous limit.

**Verified live:** real $0.50 USDC tx `0xf86370a8674c13c3dd61e2897943a5052fa857f02746dca151d656adf6362691` on chain 2345.

**Topology:**
```
Telegram /restock
   │
   ▼
clawclinic plugin _restock → shells out to procurement_client.py
   │  reads inventory.json → finds low SKU
   │  reads clinic_config.json → gets per-restock + 24h limits
   │  GET http://127.0.0.1:8645/quote?sku=FLU-TRAY-100
   ▼
PharmaSupply returns invoice {pay_to, total_usd, ships_by}
   │  if total ≤ autonomous_limit AND 24h_spent + total ≤ daily_cap:
   │     auto-broadcast USDC.e transfer via evm-wallet-skill
   │  else:
   │     emit literal CONFIRM-RESTOCK-XXXXXX token; halt until operator echoes it
   ▼
Real tx broadcast on GOAT mainnet
   ▼
POST /settle {invoice_id, tx_hash}
   │  PharmaSupply: eth_getTransactionReceipt → verify USDC Transfer log
   │     - matches USDC contract address
   │     - recipient == pay_to
   │     - amount ≥ total_usd
   ▼
Settled. ClawClinic appends to inventory + 24h spend ledger.
```

**Wallets:**
- ClawClinic (signer): `0x9deEC91428b2637c9Bdb8B74aa8c0C0baFC88592` (key at `~/.evm-wallet.json`)
- PharmaSupply (receiver): `0x75459d120f4F924a71231807db11080C5bC25EE8` (key at `procurement/.pharmasupply-wallet.json`, gitignored, receive-only at runtime)

**Failure handling** — six known failure modes all surface a clean human-readable message rather than a stack trace: PharmaSupply unreachable, RPC unreachable, tx not yet mined, tx to wrong recipient, tx for less than the invoice amount, missing/bad fields.

---

## `/onboard` — clinic configuration with strict guardrails

A single front-door command for runtime configuration. Reads are free; every write is staged behind a literal `CONFIRM-ONBOARD-XXXXXX` token that the operator must echo back verbatim. Approximate confirmations ("yes", "do it") are rejected.

**Sub-actions:**
- `/onboard` — show current config + 24h spend + pending proposal count + sub-action menu
- `/onboard pending` — list pending proposals
- `/onboard help` — full reference
- `/onboard set-limit <usd>` — per-restock autonomous limit (hard ceiling $1000)
- `/onboard set-daily <usd>` — rolling 24h cumulative cap (hard ceiling $10,000)
- `/onboard set-clinic name|hours|address <value>` — clinic facts (only these 3 fields are writable)
- `/onboard add-sku <SKU> <unit_price> <threshold> <name…>` — add inventory item
- `/onboard remove-sku <SKU>` — delete inventory item
- `/onboard reset` — restore defaults
- `/onboard confirm <CONFIRM-ONBOARD-XXXXXX>` — apply a pending proposal
- `/onboard abort <CONFIRM-ONBOARD-XXXXXX>` — discard a pending proposal

**Audit trail** — every propose / apply / abort event is appended to `procurement/.onboard_audit.log` as one JSON object per line with timestamp + action + payload. Survives gateway restarts.

**Hard ceilings** — even with operator confirmation, autonomous_limit_usd is capped at $1000 and daily_cap_usd at $10,000. SKU thresholds are capped at 100,000 units and unit prices at $10,000.

**Demo loop hitting both Cat 3 and Cat 4:**
1. `/onboard` → show $5 limit, $50 daily cap
2. `/restock` → real $0.50 tx broadcast + verified (Cat 3 GREEN)
3. `/onboard set-limit 0.10` → returns CONFIRM-ONBOARD token
4. `/onboard confirm yes do it` → REJECTED (paraphrase rejected — Cat 4 GREEN)
5. `/onboard confirm <token>` → applied
6. `/restock` → halts, returns CONFIRM-RESTOCK token because $0.50 > $0.10 limit (Cat 4 GREEN)
7. `/onboard set-limit 5.00` → confirm → back to baseline

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

## ElevenLabs + Twilio voice integration — DONE (steps preserved for reference / re-deploy)

**Status:** Live. See "Voice integration" section above for the runtime topology. The steps below are kept verbatim because they document the setup decisions someone would need to redo this on another machine.

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

## Demo plan — five-act, ~3 min, hits every judging category

1. **Problem (15 sec)** — "Clinics lose $50–150 per missed call. 30% unanswered. 30+ Toronto clinics validated."
2. **What it does (15 sec)** — "AI receptionist on Telegram AND voice. Books, answers FAQs, autonomously restocks supplies, charges clinics per outcome via x402 on GOAT."
3. **Patient booking + x402 (40 sec)** — `/book` → `1 0xPAYER` → bot creates order → send $1 USDC from MetaMask → `status` → BK- confirmation → show tx on explorer.goat.network. **(Category: x402 integrity, customer side)**
4. **Voice (30 sec)** — call the Twilio number, say "I'd like to book an appointment tomorrow morning, my name is X" → ElevenLabs agent confirms and reads back a `BK-` confirmation → check `/lookup BK-...` on Telegram to show the voice booking is visible to staff.
5. **A2A machine payment + onboard guardrail (45 sec)** — `/onboard` (show $5 limit) → `/restock` (real $0.50 USDC tx broadcast + on-chain verified, ships) → `/onboard set-limit 0.10` → confirm with the literal token → `/restock` halts, demands `CONFIRM-RESTOCK` token → try paraphrase "yes do it" → REJECTED → `/onboard set-limit 5` confirm → back to baseline. **(Categories: x402 integrity GREEN, guardrails GREEN.)**
6. **Identity (10 sec)** — show https://8004scan.io/agents/goat/29 — agent registered on mainnet.

---

## Critical-path checklist

- [x] Voice integration live
- [x] A2A procurement live with real on-chain settlement
- [x] `/onboard` configuration with literal-token guardrails + audit log
- [x] GitHub repo pushed (`Aarya2004/ClawClinic`)
- [ ] Submit https://bit.ly/openclaw-hackathon-submission
- [ ] Rehearse the 3-min demo end-to-end at least twice
- [ ] Confirm Telegram, voice, ngrok, PharmaSupply server, and Hermes gateway are all up before the demo

The submission is the only hard wall. Everything else is upside.
