# ClawClinic — Hermes Agent Brain

The Hermes-side code that powers the Telegram-facing ClawClinic bot. These files run inside a [Hermes Agent](https://hermes-agent.nousresearch.com/) gateway — they are loaded from `~/.hermes/` on the host machine, not executed from this repo. This directory exists so the submission has a self-contained snapshot of how the bot is configured.

## Layout

| File | Lives at (runtime) | Purpose |
|---|---|---|
| `SOUL.md` | `~/.hermes/SOUL.md` | The agent persona — who ClawClinic is, what it will and will not do, the guardrails it enforces in every reply. |
| `HERMES.md` | `~/.hermes/hermes-agent/HERMES.md` | Project context loaded into every turn — market problem, on-chain identity, x402 merchant config, command surface, booking and refill flows, structured-reply templates. |
| `plugin/__init__.py` | `~/.hermes/plugins/clawclinic/__init__.py` | Hermes plugin registering six slash commands (`/book`, `/cancel`, `/hours`, `/insurance`, `/refill`, `/identity`). The handlers reply directly to the user; the LLM only sees the user's follow-up free-text turn. |
| `plugin/plugin.yaml` | `~/.hermes/plugins/clawclinic/plugin.yaml` | Plugin manifest. |
| `skill/SKILL.md` | `~/.hermes/skills/clawclinic/SKILL.md` | Skill instructions — how the LLM should drive the booking, cancellation, and refill flows when it sees a free-text follow-up to one of the slash commands above. |
| `skill/x402.py` | `~/.hermes/skills/clawclinic/x402.py` | HMAC-signed x402 helper (create + status). Used by the booking and refill flows to charge the clinic $1.00 USDC per confirmed outcome via the GOAT mainnet x402 endpoint. Reads `X402_API_KEY`, `X402_API_SECRET`, and `X402_API_URL` from the environment — never from this file. |
| `skill/guardrails.py` | `~/.hermes/skills/clawclinic/guardrails.py` | Literal-confirmation-token gate. Used for destructive actions (e.g. bulk cancellation) — the LLM must echo back the exact token the script prints, and any approximate paraphrase is rejected. |
| `skill/slots.json` | `~/.hermes/skills/clawclinic/slots.json` | Fake clinic backend — six bookable slots and the supported insurance providers. |

## How these fit together at runtime

```
Telegram message
   │
   ▼
Hermes gateway
   │  if it starts with /, the plugin (plugin/__init__.py) handles it directly
   │     and answers the user. The LLM does not see slash-command messages.
   │  otherwise, free text is passed through to the LLM with SOUL.md + HERMES.md
   │     + skill/SKILL.md loaded as context.
   ▼
Claude (anthropic provider)
   │  decides whether to invoke the booking, refill, identity, or insurance
   │  flow described in SKILL.md
   ▼
shells out to skill/x402.py + skill/guardrails.py as needed; persists to slots.json
   │
   ▼
Telegram reply
```

## What is intentionally NOT in this directory

- **No private keys.** `~/.evm-wallet.json` (ClawClinic's signer for ERC-8004 registration and the agent-to-agent restock flow) lives only on the host and never enters the repo.
- **No API credentials.** `X402_API_KEY` and `X402_API_SECRET` live in `~/.hermes/.env` and are read by `x402.py` at runtime — they are never written into the source file.
- **No Anthropic / Telegram tokens.** Same story.
- **No conversation logs or session state.** `~/.hermes/sessions/`, `state.db*`, `auth.json`, and other runtime artefacts are excluded by design.

## Reproducing this on another machine

This snapshot is reference-only. A full reproduction would:

1. Install Hermes Agent and run `hermes init`.
2. Copy `SOUL.md` to `~/.hermes/SOUL.md`.
3. Copy `HERMES.md` to `~/.hermes/hermes-agent/HERMES.md`.
4. Copy `plugin/` to `~/.hermes/plugins/clawclinic/`.
5. Copy `skill/` to `~/.hermes/skills/clawclinic/`.
6. Add `plugins: { enabled: [clawclinic] }` to `~/.hermes/config.yaml`.
7. Write `X402_API_KEY` / `X402_API_SECRET` / `TELEGRAM_BOT_TOKEN` / `ANTHROPIC_API_KEY` into `~/.hermes/.env`.
8. Run `hermes gateway restart`.

The voice integration ([`../voice/`](../voice/)) and the A2A procurement flow ([`../procurement/`](../procurement/)) sit alongside this — they reach Hermes through its OpenAI-compatible API server, not through the Telegram path.
