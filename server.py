# server.py
# MCP HTTP-only server for Solscan Pro API v2.0

import os
import sys
import json
import logging
from typing import Any, Dict, List, Tuple, Optional, Iterable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn

# ----------------------------- Config -----------------------------
SOLSCAN_BASE = os.getenv("SOLSCAN_BASE", "https://pro-api.solscan.io/v2.0").rstrip("/")
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")
HTTP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("MCP_PORT", "8000"))
HTTP_PATH = os.getenv("MCP_PATH", "/mcp")
MAX_LOG_LEN = int(os.getenv("MCP_LOG_PREVIEW", "2000"))
TEXT_MAX = int(os.getenv("MCP_TEXT_MAX", "200000"))
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18"}

# ----------------------------- Logging ----------------------------
root = logging.getLogger()
root.handlers.clear()
h = logging.StreamHandler(sys.stderr)
h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
root.addHandler(h)
root.setLevel(logging.INFO)

for n in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp-http", "httpx"):
    lg = logging.getLogger(n)
    lg.handlers.clear()
    lg.addHandler(h)
    lg.propagate = False
    lg.setLevel(logging.INFO)

log = logging.getLogger("mcp-http")

def _safe_preview(obj: Any) -> str:
    try:
        if isinstance(obj, (dict, list)):
            s = json.dumps(obj, ensure_ascii=False)
        elif isinstance(obj, (bytes, bytearray)):
            s = obj.decode("utf-8", "replace")
        else:
            s = str(obj)
    except Exception as e:
        s = f"<unserializable: {e}>"
    return s[:MAX_LOG_LEN] + ("…<truncated>" if len(s) > MAX_LOG_LEN else "")

def _ok(_id: Any, result: Any, tag: str = "") -> JSONResponse:
    payload = {"jsonrpc": "2.0", "id": _id, "result": result}
    log.info("JSON-RPC OUT%s: %s", f" [{tag}]" if tag else "", _safe_preview(payload))
    return JSONResponse(payload, media_type="application/json")

def _err(_id: Any, code: int, message: str, tag: str = "") -> JSONResponse:
    payload = {"jsonrpc": "2.0", "id": _id, "error": {"code": code, "message": message}}
    log.info("JSON-RPC OUT%s: %s", f" [{tag}]" if tag else "", _safe_preview(payload))
    return JSONResponse(payload, media_type="application/json")

# ----------------------------- Catalog -----------------------------
CATALOG: Dict[str, Any] = {
    "version": "v2.0",
    "note": "All calls go through this MCP via solscan_v2_call. Required params in 'required'. Use select=['field'] or query.fields='a,b' to locally filter result.data keys.",
    "groups": {
        "account": [
            {"endpoint": "account/detail", "required": ["address"], "optional": [], "desc": "Basic account info (lamports, owner, etc)."},
            {"endpoint": "account/portfolio", "required": ["address"], "optional": ["offset","limit"], "desc": "Tokens/NFTs/positions overview."},
            {"endpoint": "account/token-accounts", "required": ["address"], "optional": ["mint","program","offset","limit"], "desc": "Token accounts of owner."},
            {"endpoint": "account/transactions", "required": ["address"], "optional": ["before","until","limit"], "desc": "Tx signatures involving account."},
        ],
        "token": [
            {"endpoint": "token/meta", "required": ["address"], "optional": [], "desc": "Token metadata (symbol, decimals)."},
            {"endpoint": "token/holders", "required": ["address"], "optional": ["offset","limit"], "desc": "Top holders of a token."},
            {"endpoint": "token/price", "required": ["address"], "optional": [], "desc": "Price/market data if available."},
        ],
        "nft": [
            {"endpoint": "nft/detail", "required": ["address"], "optional": [], "desc": "NFT metadata and ownership."},
            {"endpoint": "nft/list", "required": ["owner"], "optional": ["collection","offset","limit"], "desc": "NFTs owned by a wallet."},
        ],
        "transaction": [
            {"endpoint": "transaction/detail", "required": ["signature"], "optional": [], "desc": "Parsed tx details by signature."},
            {"endpoint": "transaction/logs", "required": ["signature"], "optional": [], "desc": "Transaction logs if available."},
        ],
        "block": [
            {"endpoint": "block/detail", "required": ["slot"], "optional": [], "desc": "Block info by slot."},
            {"endpoint": "block/transactions", "required": ["slot"], "optional": ["limit","offset"], "desc": "Transactions in block."},
        ],
        "market": [
            {"endpoint": "market/price", "required": ["address"], "optional": [], "desc": "Asset spot price snapshot."},
        ],
        "program": [
            {"endpoint": "program/accounts", "required": ["programId"], "optional": ["filters","limit","offset"], "desc": "Program-derived accounts."},
        ],
        "chain": [
            {"endpoint": "chain/epoch", "required": [], "optional": [], "desc": "Current epoch data."},
            {"endpoint": "chain/health", "required": [], "optional": [], "desc": "Service/cluster health."},
        ],
    },
    "tips": [
        "Do NOT send 'fields' upstream; MCP strips it and filters locally.",
        "For transactions pagination, prefer 'before' with last signature.",
    ],
}

# Поддерживаемые и алиасные эндпоинты
KNOWN_ENDPOINTS: List[str] = sorted({item["endpoint"] for g in CATALOG["groups"].values() for item in g})
ENDPOINT_ALIASES: Dict[str, str] = {
    "account/data": "account/detail",
    "account/tokens": "account/token-accounts",
    "token/metadata": "token/meta",
    "nft/metadata": "nft/detail",
}

ALLOWED_PREFIXES = {"account","token","nft","transaction","block","market","program","monitor","chain"}

# ----------------------------- Tools -----------------------------
def _catalog_text() -> str:
    # Превращаем каталог в строку JSON и оборачиваем короткой инструкцией
    head = (
        "Solscan Pro v2 — full method catalog (grouped). "
        "Call: solscan_v2_call(endpoint='<group/endpoint>', query={...}).\n\n"
    )
    return head + json.dumps(CATALOG, ensure_ascii=False)

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "solscan_v2_call",
        # ВАЖНО: длинное описание сразу содержит весь каталог, чтобы инструктировать модель без доп. вызовов
        "description": _catalog_text(),
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "e.g. account/detail"},
                "query": {"type": "object", "additionalProperties": True},
                "timeout_ms": {"type": "number"},
                "select": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keep only these keys in top-level result.data (e.g. ['lamports','solBalance'])."
                }
            },
            "required": ["endpoint"],
        },
    },
]

# ----------------------------- Helpers -----------------------------
def _pairs_from_query(query: Optional[Dict[str, Any]]) -> List[Tuple[str, str]]:
    if not query:
        return []
    pairs: List[Tuple[str, str]] = []
    for k, v in query.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            for item in v:
                pairs.append((k, str(item)))
        else:
            pairs.append((k, str(v)))
    return pairs

def _filter_data_keep_keys(obj: Any, keys: Iterable[str]) -> Any:
    if not isinstance(obj, dict):
        return obj
    ks = list(keys)
    return {k: obj[k] for k in ks if k in obj}

def _json_to_text_block(obj: Any) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception as e:
        s = f'{{"_mcp_error":"unserializable","detail":"{e}"}}'
    if len(s) > TEXT_MAX:
        return s[:TEXT_MAX] + f'\n...<truncated {len(s)-TEXT_MAX} chars>'
    return s

def _text_result(message: str, json_payload: Optional[Any] = None) -> Dict[str, Any]:
    parts = [message]
    if json_payload is not None:
        parts.append(_json_to_text_block(json_payload))
    text = "\n".join(parts)
    return {"isError": False, "content": [{"type": "text", "text": text}]}

def _text_error(message: str, extra: Optional[Any] = None) -> Dict[str, Any]:
    parts = [f"error: {message}"]
    if extra is not None:
        parts.append(_json_to_text_block(extra))
    text = "\n".join(parts)
    return {"isError": True, "content": [{"type": "text", "text": text}]}

def normalize_endpoint(ep: str) -> Tuple[str, Optional[str]]:
    raw = ep.strip().lstrip("/")
    if raw in ENDPOINT_ALIASES:
        return ENDPOINT_ALIASES[raw], f"endpoint alias '{raw}' normalized to '{ENDPOINT_ALIASES[raw]}'"
    if raw in KNOWN_ENDPOINTS:
        return raw, None
    variants = {
        raw.replace("tokens", "token-accounts"): "maybe you meant 'token-accounts'",
        raw.replace("data", "detail"): "maybe you meant 'detail'",
        raw.replace("metadata", "meta"): "maybe you meant 'meta'",
    }
    for v, note in variants.items():
        if v in KNOWN_ENDPOINTS:
            return v, f"{note} (normalized '{raw}' -> '{v}')"
    return raw, None

async def solscan_get(endpoint: str, query: Optional[Dict[str, Any]], timeout_ms: Optional[int]) -> Dict[str, Any]:
    ep = endpoint.lstrip("/")
    head = (ep.split("/", 1)[0] or "").strip()
    if head not in ALLOWED_PREFIXES:
        return {"ok": False, "status": 400, "data": {"error": f"forbidden endpoint prefix '{head}'"}}
    timeout = httpx.Timeout(timeout_ms / 1000.0) if timeout_ms else httpx.Timeout(30.0)
    url = f"{SOLSCAN_BASE}/{ep}"
    params = _pairs_from_query(query or {})
    headers = {"accept": "application/json"}
    if SOLSCAN_API_KEY:
        headers["token"] = SOLSCAN_API_KEY
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params, headers=headers)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    log.info("SOLSCAN GET %s -> %s; body: %s", url, resp.status_code, _safe_preview(data))
    if resp.is_success:
        return {"ok": True, "data": data}
    return {"ok": False, "status": resp.status_code, "data": data}

# ----------------------------- FastAPI app ----------------------------------
app = FastAPI(title="Solscan MCP HTTP (single tool, catalog embedded in description)")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    client = getattr(request.client, "host", "?")
    log.info("HTTP %s %s from %s", request.method, request.url.path, client)
    if request.method.upper() == "POST":
        try:
            body = await request.body()
            try:
                log.info("HTTP IN  %s %s body: %s", request.method, request.url.path, _safe_preview(json.loads(body)))
            except Exception:
                log.info("HTTP IN  %s %s body(raw): %s", request.method, request.url.path, _safe_preview(body))
            async def receive():
                return {"type": "http.request", "body": body}
            request._receive = receive  # type: ignore[attr-defined]
        except Exception as e:
            log.info("HTTP IN  %s %s <body read error: %s>", request.method, request.url.path, e)
    resp = await call_next(request)
    log.info("HTTP %s %s -> %s", request.method, request.url.path, resp.status_code)
    return resp

# Health
@app.get("/")
@app.get(HTTP_PATH)
async def health():
    return JSONResponse({"ok": True, "path": HTTP_PATH})

# JSON-RPC handlers
@app.post("/")
async def root_alias(req: Request):
    return await mcp_rpc(req)

@app.post(HTTP_PATH)
async def mcp_rpc(req: Request):
    try:
        payload = await req.json()
    except Exception:
        return _err(None, -32600, "Invalid Request")

    log.info("JSON-RPC IN : %s", _safe_preview(payload))

    method = payload.get("method")
    _id = payload.get("id")

    # notifications/* — 204 No Content
    if _id is None or ("id" not in payload):
        return PlainTextResponse("", status_code=204)

    # initialize
    if method == "initialize":
        params = payload.get("params") or {}
        client_ver = params.get("protocolVersion")
        if not client_ver or client_ver not in SUPPORTED_PROTOCOL_VERSIONS:
            sup = ", ".join(sorted(SUPPORTED_PROTOCOL_VERSIONS))
            return _err(_id, -32602, f"Unsupported protocolVersion. Supported: {sup}", tag="initialize:error")
        result = {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "solscan-pro-v2-mcp", "version": "1.5.0"},
        }
        return _ok(_id, result, tag="initialize")

    # tools/list
    if method == "tools/list":
        # Возвращаем ОДИН инструмент с БОЛЬШИМ description, содержащим весь каталог.
        return _ok(_id, {"tools": TOOLS, "nextCursor": None}, tag="tools/list")

    # tools/call
    if method == "tools/call":
        params = payload.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        log.info("CALL name=%r args=%s", name, _safe_preview(args))

        if name != "solscan_v2_call":
            return _err(_id, -32601, f"Unknown tool: {name}", tag="tools/call")

        endpoint = args.get("endpoint")
        query = dict(args.get("query") or {})
        timeout_ms = args.get("timeout_ms")
        select: Optional[List[str]] = args.get("select")

        # перехват query.fields — локальный пост-фильтр
        fields = query.pop("fields", None)
        if select is None and fields:
            if isinstance(fields, str):
                select = [p.strip() for p in fields.split(",") if p.strip()]
            elif isinstance(fields, (list, tuple)):
                select = [str(p).strip() for p in fields if str(p).strip()]

        if not endpoint or not isinstance(endpoint, str):
            msg = (
                "endpoint must be a non-empty string.\n"
                "See catalog in the tool description (tools/list) for available endpoints."
            )
            return _ok(_id, _text_error(msg), tag="solscan_v2_call:error")

        endpoint_str = endpoint.strip()
        endpoint_norm, note = normalize_endpoint(endpoint_str)

        upstream = await solscan_get(endpoint_norm, query, timeout_ms)
        if not upstream.get("ok"):
            head = (endpoint_norm.split("/", 1)[0] or "").strip()
            suggestions = [e for e in KNOWN_ENDPOINTS if e.startswith(head + "/")]
            extra = {
                "requested": endpoint_str,
                "normalized": endpoint_norm,
                "known": suggestions[:10],
                "upstream_status": upstream.get("status"),
                "upstream_data": upstream.get("data"),
            }
            msg = "upstream error"
            if upstream.get("status") == 404:
                msg = "endpoint not found upstream (did you mean one of the known endpoints?)"
            return _ok(_id, _text_error(msg, extra=extra), tag="solscan_v2_call:error")

        result_json = upstream["data"]

        # локальный фильтр по select внутри data
        if select and isinstance(result_json, dict):
            data_obj = result_json.get("data")
            if isinstance(data_obj, dict):
                result_json = {
                    **result_json,
                    "data": _filter_data_keep_keys(data_obj, select),
                    "_mcp_note": {"filtered_by": select}
                }

        header = f"Upstream OK for {endpoint_norm}. JSON follows below:"
        if note:
            header = f"{header}\n(note: {note})"
        return _ok(_id, _text_result(header, result_json), tag="solscan_v2_call")

    return _err(_id, -32601, f"Unsupported method: {method}", tag="unsupported")

# ----------------------------- Run -----------------------------
if __name__ == "__main__":
    if not SOLSCAN_API_KEY:
        log.warning("SOLSCAN_API_KEY is empty. Requests may fail with 401.")
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_config={
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"()": "uvicorn.logging.DefaultFormatter", "fmt": "%(levelprefix)s %(message)s", "use_colors": False}},
        "handlers": {"stderr": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "default"}},
        "loggers": {"uvicorn": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                    "uvicorn.error": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                    "uvicorn.access": {"handlers": ["stderr"], "level": "WARNING", "propagate": False}}
    }, access_log=False)

