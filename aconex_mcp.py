# aconex_mcp.py
from __future__ import annotations
from typing import Optional, Dict, Any
from time import time

import os
import requests
import xmltodict
import httpx

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# ==========================
# Config por variables de entorno (Render → Environment)
# ==========================
ACONEX_BASE = os.getenv("ACONEX_BASE", "https://us1.aconex.com/api").rstrip("/")
ACONEX_OAUTH_BASE = os.getenv(
    "ACONEX_OAUTH_BASE",
    "https://constructionandengineering.oraclecloud.com/auth"
).rstrip("/")

# Credenciales para client_credentials (poner en Render)
ACONEX_CLIENT_ID = os.getenv("ACONEX_CLIENT_ID", "")
ACONEX_CLIENT_SECRET = os.getenv("ACONEX_CLIENT_SECRET", "")
# Scope opcional; si tu IDCS lo exige, ponelo (si no, dejalo vacío)
ACONEX_SCOPE = os.getenv("ACONEX_SCOPE", "").strip()

# Proyecto por defecto (para no tener que mandar projectId siempre)
DEFAULT_PROJECT_ID = os.getenv("ACONEX_DEFAULT_PROJECT_ID")  # ej: "1207982555"

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "60"))  # segundos

# ==========================
# App FastAPI
# ==========================
app = FastAPI(title="Aconex MCP", version="2.0.0")

# CORS abierto para que el builder de ChatGPT/otros clientes llamen sin trabas
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ==========================
# Helpers
# ==========================
def _effective_project_id(pid: Optional[str]) -> str:
    if pid:
        return pid
    if DEFAULT_PROJECT_ID:
        return DEFAULT_PROJECT_ID
    raise HTTPException(400, "Falta projectId y no hay ACONEX_DEFAULT_PROJECT_ID configurado")

def _as_json(resp: requests.Response):
    """Devuelve JSON si viene JSON; si viene XML, lo convierte a JSON; si no, texto plano."""
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

# Cache simple del access token en memoria
_TOKEN: Dict[str, Any] = {"value": None, "exp": 0}

async def _get_access_token() -> str:
    """Obtiene (y cachea) un access_token por client_credentials."""
    now = time()
    if _TOKEN["value"] and (_TOKEN["exp"] - now) > 60:
        return _TOKEN["value"]

    if not ACONEX_CLIENT_ID or not ACONEX_CLIENT_SECRET:
        raise HTTPException(500, "Falta configurar ACONEX_CLIENT_ID / ACONEX_CLIENT_SECRET en Render")

    data = {"grant_type": "client_credentials"}
    if ACONEX_SCOPE:
        data["scope"] = ACONEX_SCOPE

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{ACONEX_OAUTH_BASE}/token",
            data=data,
            auth=(ACONEX_CLIENT_ID, ACONEX_CLIENT_SECRET),
        )

    if r.status_code != 200:
        # Exponemos texto para diagnóstico (invalid_client / unsupported_grant_type / invalid_scope)
        raise HTTPException(502, f"Token error {r.status_code}: {r.text}")

    tok = r.json()
    access = tok.get("access_token")
    if not access:
        raise HTTPException(502, "Token sin access_token en respuesta de IDCS")
    expires_in = int(tok.get("expires_in", 1800))
    _TOKEN["value"] = access
    _TOKEN["exp"] = now + expires_in
    return access

# ==========================
# Rutas base / health / debug
# ==========================
@app.get("/")
@app.head("/")
def root():
    return {"ok": True, "service": "aconex-mcp"}

@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)

@app.get("/healthz")
def healthz_get():
    return {
        "ok": True,
        "aconex_base": ACONEX_BASE,
        "default_project": DEFAULT_PROJECT_ID
    }

@app.head("/healthz")
def healthz_head():
    return Response(status_code=200)

# Endpoints de diagnóstico (no exponen secretos)
@app.get("/debug_env")
def debug_env():
    return {
        "ok": True,
        "aconex_base": ACONEX_BASE,
        "has_client_id": bool(ACONEX_CLIENT_ID),
        "has_client_secret": bool(ACONEX_CLIENT_SECRET),
        "has_scope": bool(ACONEX_SCOPE),
        "default_project": DEFAULT_PROJECT_ID
    }

@app.get("/debug_token")
async def debug_token():
    try:
        t = await _get_access_token()
        return {
            "ok": True,
            "token_prefix": t[:16],
            "expires_in_sec": int(_TOKEN["exp"] - time())
        }
    except HTTPException as e:
        return JSONResponse({"ok": False, "status": e.status_code, "detail": e.detail}, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "status": 500, "detail": str(e)}, status_code=200)

# ==========================
# Endpoints Aconex (server-side Bearer)
# ==========================
@app.get("/search_register")
@app.get("/search_register/")   # alias con barra final
@app.get("/searchRegister")     # alias camelCase por si algún cliente lo usa así
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
        params=params,
        timeout=HTTP_TIMEOUT,
    )
    return _as_json(resp)

@app.get("/register_schema")
@app.get("/registerSchema")
async def register_schema(projectId: Optional[str] = Query(default=None)):
    token = await _get_access_token()
    pid = _effective_project_id(projectId)
    url = f"{ACONEX_BASE}/projects/{pid}/register/schema"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    return _as_json(resp)

@app.get("/document_metadata")
@app.get("/documentMetadata")
async def document_metadata(projectId: Optional[str] = Query(default=None), documentId: str = ""):
    if not documentId:
        raise HTTPException(400, "Falta documentId")
    token = await _get_access_token()
    pid = _effective_project_id(projectId)
    url = f"{ACONEX_BASE}/projects/{pid}/register/{documentId}/metadata"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    return _as_json(resp)

@app.get("/download_file")
@app.get("/downloadFile")
async def download_file(projectId: Optional[str] = Query(default=None), documentId: str = ""):
    if not documentId:
        raise HTTPException(400, "Falta documentId")
    token = await _get_access_token()
    pid = _effective_project_id(projectId)
    url = f"{ACONEX_BASE}/projects/{pid}/register/{documentId}/file"
    upstream = requests.get(
        url, headers={"Authorization": f"Bearer {token}"},
        stream=True, timeout=max(HTTP_TIMEOUT, 300),
    )
    # Propaga nombre de archivo si viene en Content-Disposition
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
        headers={"Content-Disposition": f'attachment; filename="{fname or (documentId + ".bin")}"'},
    )
