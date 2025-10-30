# eight-md-mcp — Solscan Pro v2 MCP (HTTP)

A minimal **MCP** server (HTTP transport) that proxies **Solscan Pro API v2.0** behind a single tool.
Optimized for ChatGPT/Claude: all `tools/call` responses return **`type: "text"`** with the **upstream JSON embedded as a string**, which helps avoid `424` errors observed in some clients.

---

## Features

* MCP lifecycle support: **`2025-03-26`** and **`2025-06-18`**
* Single tool: **`solscan_v2_call`**
* **Full Solscan endpoint catalog embedded** in the tool `description` (via `tools/list`) → no extra discovery call needed
* Endpoint alias normalization (e.g., `account/tokens` → `account/token-accounts`)
* Local field filtering via `select` or `query.fields` (never sent to upstream)
* Detailed logging of MCP requests/responses and upstream calls

---

## Quickstart

```bash
git clone https://github.com/lumlabs/eight-md-mcp
cd eight-md-mcp

python3 -m venv .venv
source .venv/bin/activate

pip install -U pip
pip install fastapi uvicorn httpx python-dotenv
```

Create a `.env` file next to `server.py`:

```env
SOLSCAN_API_KEY=your_solscan_pro_token_here
SOLSCAN_BASE=https://pro-api.solscan.io/v2.0
MCP_HOST=0.0.0.0
MCP_PORT=8000
MCP_PATH=/mcp
# Optional tuning:
MCP_LOG_PREVIEW=2000
MCP_TEXT_MAX=200000
```

Run:

```bash
python server.py
# → Uvicorn running on http://0.0.0.0:8000
```

**MCP URL:** `http://<host>:8000/mcp` (or your `MCP_PATH`).

---

## MCP Flow (what’s implemented)

* `initialize` → JSON with `protocolVersion`, `capabilities.tools.listChanged=false`, `serverInfo`
* `tools/list` → returns **one** tool, `solscan_v2_call`, with the **full endpoint catalog** embedded in `description` (as text containing a JSON string)
* `tools/call` → proxies to Solscan; response is always `type: "text"` with embedded JSON
* `notifications/*` → `204 No Content`

This “text-only for tools/call” strategy avoids client-side `424` issues some MCP HTTP clients encounter with `type:"json"`.

---

## Tool: `solscan_v2_call`

Universal GET proxy for `/<group>/<endpoint>` within Solscan Pro v2.

**Arguments:**

```json
{
  "endpoint": "account/detail",
  "query": { "address": "<pubkey>" },
  "timeout_ms": 30000,
  "select": ["lamports", "solBalance"]
}
```

* `endpoint` (required): e.g., `account/detail`, `token/meta`, `transaction/detail`, …
* `query`: forwarded as URL query string (except `fields`, which is handled locally)
* `timeout_ms`: upstream HTTP timeout
* `select`: local post-filter of `result.data` keys (see below)

**Response (MCP):**
`result.content[0].type = "text"`; the **first line** is a short header, followed by the **upstream JSON** serialized as a string.

---

## Examples (curl)

**Initialize:**

```bash
curl -s http://localhost:8000/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":0,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

**List tools (description includes the full catalog):**

```bash
curl -s http://localhost:8000/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq
```

**Call `account/detail`:**

```bash
curl -s http://localhost:8000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc":"2.0","id":2,"method":"tools/call",
    "params":{"name":"solscan_v2_call","arguments":{
      "endpoint":"account/detail",
      "query":{"address":"<PUBKEY>"}
    }}
  }' | jq
```

**Local field filtering:**

```bash
# via select
"arguments": {
  "endpoint": "account/detail",
  "query": {"address":"<PUBKEY>"},
  "select": ["lamports","solBalance"]
}

# or via query.fields (handled locally; not sent upstream)
"arguments": {
  "endpoint": "account/detail",
  "query": {"address":"<PUBKEY>", "fields":"lamports,solBalance"}
}
```

---

## Endpoint Aliases (Normalization)

Common non-canonical paths are normalized automatically, with a note included in the response:

| Alias            | Canonical                |
| ---------------- | ------------------------ |
| `account/data`   | `account/detail`         |
| `account/tokens` | `account/token-accounts` |
| `token/metadata` | `token/meta`             |
| `nft/metadata`   | `nft/detail`             |

---

## Notes & Tips

* Ensure your **Solscan Pro token** is valid and placed in `.env` as `SOLSCAN_API_KEY`.
* For production, place the server behind HTTPS (reverse proxy) and lock down access (IP allowlist or MCP auth).
* The tool `description` contains the full, grouped catalog; models typically read it from `tools/list` and can call the proxy directly without extra discovery.

---

## License

MIT © LUM Labs
