# aconex_mcp.py
from __future__ import annotations
from typing import Optional, Dict, Any
from time import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import os, requests, xmltodict
import httpx

# ==========================
# Config por variables de entorno
# ==========================
ACONEX_BASE = os.getenv("ACONEX_BASE", "https://us1.aconex.com/api").rstrip("/")
# Token endpoint de Oracle/IDCS (no cambiar dominio)
ACONEX_OAUTH_BASE = os.getenv(
    "ACONEX_OAUTH_BASE",
    "https://constructionandengineering.oraclecloud.com/auth"
).rstrip("/")

# Credenciales para client_credentials (ponerlas en Render)
ACONEX_CLIENT_ID = os.getenv("ACONEX_CLIENT_ID", "")
ACONEX_CLIENT_SECRET = os.getenv("ACONEX_CLIENT_SECRET", "")
# Scope opcional; muchas veces no hace falta. Dejar vacío si tu IDCS no lo requiere.
ACONEX_SCOPE = os.getenv("ACONEX_SCOPE", "").strip()

# Proyecto por defecto
DEFAULT_PROJECT_ID = os.getenv("ACONEX_DEFAULT_PROJECT_ID")

# ==========================
# Token cache simple en memoria
# ==========================
_TOKEN: Dict[str, Any] = {"value": None, "exp": 0}

async def _get_access_token() -> str:
    """
    Pide/renueva un token por client_credentials y lo cachea.
    """
    now = time()
    if _TOKEN["value"] and _TOKEN["exp"] - now > 60:
        return _TOKEN["value"]

    if not ACONEX_CLIENT_ID or not ACONEX_CLIENT_SECRET:
        raise HTTPException(500, "Falta configurar ACONEX_CLIENT_ID / ACONEX_CLIENT_SECRET en Render")

    data = {"grant_type": "client_credentials"}
    if ACONEX_SCOPE:
        data["scope"] = ACONEX_SCOPE

    # Basic auth con client_id:client_secret
    auth = (ACONEX_CLIENT_ID, ACONEX_CLIENT_SECRET)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{ACONEX_OAUTH_BASE}/token", data=data, auth=auth)
    if r.status_code != 200:
        raise HTTPException(502, f"Token error {r.status_code}: {r.text}")

    tok = r.json()
    access = tok.get("access_token")
    if not access:
        raise HTTPException(502, "Token sin access_token")
    expires_in = int(tok.get("expires_in", 1800))
    _TOKEN["value"] = access
    _TOKEN["exp"] = now + expires_in
    return access

def _effective_project_id(pid: Optional[str]) -> str:
    if pid:
        return pid
    if DEFAULT_PROJECT_ID:
        return DEFAULT_PROJECT_ID
    raise HTTPException(400, "Falta projectId y no hay ACONEX_DEFAULT_PROJECT_ID configurado")

def _as_json(resp: requests.Response):
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return JSONResponse(data, status_code=resp.status_code)
    try:
        data = xmltodict.parse(resp.text)
    except Exception:
        return PlainTextResponse(resp.text, status_code=resp.status_code)
    return JSONResponse(data, status_code=resp.status_code)

# ==========================
# App
# ==========================
app = FastAPI(title="Aconex MCP", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ==========================
# Rutas utilitarias
# ==========================
@app.get("/")
@app.head("/")
def root():
    return {"ok": True, "service": "aconex-mcp"}

@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)

@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "aconex_base": ACONEX_BASE,
        "default_project": DEFAULT_PROJECT_ID
    }

# ==========================
# Endpoints que llaman a Aconex (sin Bearer del cliente)
# ==========================
@app.get("/search_register")
async def search_register(
    projectId: Optional[str] = Query(default=None),
    page_number: int = 1,
    page_size: int = 50,
    search_query: Optional[str] = None,
    return_fields: str = "docno,title,statusid,revision,registered",
):
    token = await _get_access_token()
    pid = _effective_project_id(projectId)

    params = {
        "search_type": "PAGED",
        "page_number": page_number,
        "page_size": page_size,
        "return_fields": return_fields,
    }
    if search_query:
        params["search_query"] = search_query

    url = f"{ACONEX_BASE}/projects/{pid}/register"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params, timeout=60
    )
    return _as_json(resp)

@app.get("/register_schema")
async def register_schema(projectId: Optional[str] = None):
    token = await _get_access_token()
    pid = _effective_project_id(projectId)
    url = f"{ACONEX_BASE}/projects/{pid}/register/schema"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=60
    )
    return _as_json(resp)

@app.get("/document_metadata")
async def document_metadata(projectId: Optional[str] = None, documentId: str = ""):
    token = await _get_access_token()
    pid = _effective_project_id(projectId)
    url = f"{ACONEX_BASE}/projects/{pid}/register/{documentId}/metadata"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=60
    )
    return _as_json(resp)

@app.get("/download_file")
async def download_file(projectId: Optional[str] = None, documentId: str = ""):
    token = await _get_access_token()
    pid = _effective_project_id(projectId)
    url = f"{ACONEX_BASE}/projects/{pid}/register/{documentId}/file"
    upstream = requests.get(
        url, headers={"Authorization": f"Bearer {token}"},
        stream=True, timeout=300
    )
    fname = None
    disp = upstream.headers.get("Content-Disposition") or ""
    if "filename=" in disp:
        try:
            fname = disp.split("filename=", 1)[1].strip("\"'; ")
        except Exception:
            fname = None
    media = upstream.headers.get("Content-Type") or "application/octet-stream"
    return StreamingResponse(
        upstream.iter_content(64 * 1024),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{fname or (documentId + ".bin")}"'
        },
    )
