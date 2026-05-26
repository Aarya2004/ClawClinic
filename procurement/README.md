# ClawClinic — A2A Procurement

Autonomous agent-to-agent restocking flow. ClawClinic monitors its supply inventory and, when an item drops below threshold, delegates procurement to **PharmaSupply** — a second local agent that issues a quote and an invoice. ClawClinic broadcasts a real USDC transfer on GOAT mainnet to settle the invoice, PharmaSupply verifies the transfer on-chain, and the order ships.

Purpose: hit the **x402 Protocol Integrity** judging category with a real, autonomous machine payment between two agents — no human in the loop for spend under $5.

## Architecture

```
┌────────────────┐                          ┌──────────────────┐
│   ClawClinic   │                          │   PharmaSupply   │
│  (this agent)  │                          │   (port 8645)    │
└────────┬───────┘                          └─────────┬────────┘
         │                                            │
         │  1. read inventory.json (12 < threshold 20) │
         │                                            │
         │  2. GET /quote?sku=FLU-TRAY-100            │
         │ ─────────────────────────────────────────▶ │
         │                                            │
         │     ◀───────────  invoice_id, pay_to,      │
         │                   total_usd, ships_by      │
         │                                            │
         │  3. evm-wallet-skill → USDC transfer       │
         │     on GOAT mainnet (chain 2345)           │
         │     ─────────────▶  on-chain               │
         │     ◀─────────────  tx_hash                │
         │                                            │
         │  4. POST /settle {invoice_id, tx_hash}     │
         │ ─────────────────────────────────────────▶ │
         │     ┌──────────────────────────────────────┘
         │     │  RPC: eth_getTransactionReceipt(tx_hash)
         │     │  verify USDC Transfer log to pay_to ≥ total_usd
         │     ▼
         │     ◀───────────  status: PAID, ships_by    │
         │                                            │
         │  5. update inventory.json (12 → 112)       │
```

## Guardrails

- **Autonomous limit: $5 USD per restock run.** Spend at or below this proceeds without human approval.
- **Over-limit confirmation token.** Above $5, the client returns a single-use token like `CONFIRM-RESTOCK-92DA87` that the operator must literally repeat to proceed. Tokens are stored in `.pending_restocks.json` and consumed on use.
- **On-chain verification.** PharmaSupply does not trust the tx hash alone — it calls `eth_getTransactionReceipt` against `rpc.goat.network`, finds the USDC Transfer event in the logs, and confirms both the recipient address and the amount before marking the invoice paid.
- **Failure paths fail loudly, not silently.** PharmaSupply unreachable, RPC unreachable, tx not yet on chain, tx to wrong recipient, tx for less than the invoice amount — each surfaces a clear human-readable error. No raw stack traces, no silent retries.

## Demo flow

```
$ python3 procurement_client.py restock

Inventory check:
  Fluoride trays (box of 100): 12 remaining (threshold 20) — ⚠️  LOW

Total spend $0.50 <= $5.00. Proceeding autonomously.

PharmaSupply quote INV-D472E307: 1× Fluoride trays (box of 100) @ $0.50 = $0.50 (ships by 2026-05-28)

Sending 0.50 USDC to 0x75459d12…5EE8 on GOAT mainnet…

Tx broadcast: 0xf86370a8674c13c3dd61e2897943a5052fa857f02746dca151d656adf6362691
Explorer: https://explorer.goat.network/tx/0xf86370a8674c13c3dd61e2897943a5052fa857f02746dca151d656adf6362691

PharmaSupply confirmed payment: $0.50 settled in block 0xc0767d. Ships by 2026-05-28.

Inventory updated: Fluoride trays (box of 100) now 112 remaining.

✅ Restock complete.
```

## Files

| Path | Purpose |
|---|---|
| `pharmasupply_server.py` | Standalone HTTP service representing the supplier agent. Quotes, invoices, and **real on-chain settlement verification** against GOAT mainnet RPC. Listens on `127.0.0.1:8645`. |
| `procurement_client.py` | The ClawClinic side of the flow. CLI entry point used by the `/restock` slash command. Reads inventory, fetches quotes, broadcasts USDC transfers via the `evm-wallet-skill`, settles with PharmaSupply, updates inventory. |
| `inventory.json` | Current stock levels and reorder thresholds. Demo-seeded with one SKU (fluoride trays). |
| `pharmasupply_catalog.json` | (Optional override) Supplier's catalog. Falls back to defaults baked into `pharmasupply_server.py` if absent. |
| `pharmasupply_invoices.json` | Persisted invoice ledger. Auto-created. |
| `.pending_restocks.json` | Persisted confirmation tokens for over-limit restocks. Auto-created. |
| `.pharmasupply-wallet.json` | PharmaSupply's key pair. **Gitignored.** Receive-only in normal use — kept around purely so demo funds can be recovered later. |

## Prereqs

- Python 3.11+ with stdlib only (no extra packages).
- Node.js (for the `evm-wallet-skill` that lives at `../.claude/skills/evm-wallet-skill/`).
- Funded ClawClinic wallet at `~/.evm-wallet.json` with enough USDC.e on GOAT mainnet to pay the invoice plus a tiny amount of native BTC for gas.

The wallet skill needs one patch applied (already done in this repo): in `transfer.js`, replace `walletClient.encodeFunctionData(...)` with `encodeFunctionData(...)` imported directly from `viem`. Without it, `eth_estimateGas` fails for ERC20 transfers on viem ≥ 2.x.

## Running

In two terminals:

```bash
# terminal 1 — supplier agent
python3 pharmasupply_server.py

# terminal 2 — issue a restock
python3 procurement_client.py inventory
python3 procurement_client.py restock
```

Or, when wired into the Hermes plugin, the operator types `/restock` in Telegram and the bot prints the same output.

## Honest framing for judges and the submission

PharmaSupply is **not** an approved x402 merchant on GOAT mainnet. It is a second local agent that ClawClinic delegates procurement to. The invoice is settled with a real on-chain USDC transfer that PharmaSupply verifies against the GOAT mainnet RPC. In production PharmaSupply would expose **x402 as its payment interface**; for this hackathon we model the same machine-payment integrity (quote → pay → on-chain verify → fulfil → guardrails on autonomous spend) with a direct settlement step.

## Known limitations

- **One SKU.** Demo focuses on the workflow, not catalog size. Adding more SKUs is a config edit, not a code change.
- **No retry on broadcast failure.** If `node src/transfer.js` returns non-zero before submitting the tx, the run fails. Once a tx is broadcast, PharmaSupply will retry verification up to ~15 s for the receipt to land on the RPC.
- **Self-payment risk window.** ClawClinic's wallet sends; PharmaSupply's wallet receives; the privkey for PharmaSupply lives in this repo's gitignored `.pharmasupply-wallet.json`. For production each agent would have an independently provisioned signer, ideally in an HSM or a custodial wallet.
- **No refund path.** Settlement is one-way; if shipping fails, there is no on-chain refund flow yet.
