from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response, Query
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import os, requests, xmltodict, httpx

# ==========================
# Config por variables de entorno
# ==========================
ACONEX_BASE = os.getenv("ACONEX_BASE", "https://us1.aconex.com/api")
# Endpoint OAuth real de Aconex/Oracle (NO lo cambies)
ACONEX_OAUTH_BASE = os.getenv(
    "ACONEX_OAUTH_BASE",
    "https://constructionandengineering.oraclecloud.com/auth"
)

# Proyecto por defecto (para no tener que pasar projectId en cada llamada)
DEFAULT_PROJECT_ID = os.getenv("ACONEX_DEFAULT_PROJECT_ID")   # ej: 1207982555

def _default_project_id() -> Optional[str]:
    """Helper para mostrar en /healthz qué valor está leyendo el server."""
    return os.getenv("ACONEX_DEFAULT_PROJECT_ID")

# ==========================
# App
# ==========================
app = FastAPI(title="Aconex MCP", version="1.1.0")

# CORS abierto (el agente de ChatGPT llama desde otra URL)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ==========================
# Utilidades
# ==========================
def _bearer_or_401(authorization: Optional[str]):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <token>")
    return authorization

def _effective_project_id(projectId: Optional[str]):
    if projectId:
        return projectId
    if DEFAULT_PROJECT_ID:
        return DEFAULT_PROJECT_ID
    raise HTTPException(
        status_code=400,
        detail="Falta projectId y no hay ACONEX_DEFAULT_PROJECT_ID configurado en el servidor."
    )

def _as_json(resp: requests.Response):
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return JSONResponse(data, status_code=resp.status_code)
    # XML → JSON
    try:
        data = xmltodict.parse(resp.text)
    except Exception:
        return PlainTextResponse(resp.text, status_code=resp.status_code)
    return JSONResponse(data, status_code=resp.status_code)

# ==========================
# Raíz / healthz (para probes y evitar URL_UNKNOWN)
# ==========================
@app.get("/")
@app.head("/")
def root():
    return {"ok": True, "service": "aconex-mcp"}

@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "aconex_base": ACONEX_BASE,
        "default_project": _default_project_id(),  # <-- ahora sí existe
    }

# ==========================
# OAuth proxy (mismo dominio que tu API)
# ==========================
@app.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    # Redirige a Oracle/OAuth con los mismos query params (client_id, redirect_uri, scope, etc.)
    return RedirectResponse(
        f"{ACONEX_OAUTH_BASE}/authorize?{request.query_params}",
        status_code=302
    )

@app.post("/oauth/token")
async def oauth_token(request: Request):
    # Reenvía el body tal cual (x-www-form-urlencoded) y el header Authorization (Basic client_id:secret)
    body = await request.body()
    fwd_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if "authorization" in request.headers:
        fwd_headers["Authorization"] = request.headers["authorization"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{ACONEX_OAUTH_BASE}/token",
            headers=fwd_headers,
            content=body,
        )
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json")
    )

# ==========================
# Endpoints del conector
# ==========================
@app.get("/search_register")
def search_register(
    projectId: str | None = Query(default=None),
    page_number: int = 1,
    page_size: int = 50,
    search_query: str | None = None,
    return_fields: str = "docno,title,statusid,revision,registered",
    authorization: str | None = Header(default=None),
):
    auth = _bearer_or_401(authorization)
    projectId = _effective_project_id(projectId)

    params = {
        "search_type": "PAGED",
        "page_number": page_number,
        "page_size": page_size,
        "return_fields": return_fields,
    }
    if search_query:
        params["search_query"] = search_query

    url = f"{ACONEX_BASE}/projects/{projectId}/register"
    resp = requests.get(
        url, headers={"Authorization": auth, "Accept": "application/json"},
        params=params, timeout=60
    )
    return _as_json(resp)

@app.get("/register_schema")
def register_schema(
    projectId: Optional[str] = None,
    authorization: Optional[str] = Header(default=None),
):
    auth = _bearer_or_401(authorization)
    projectId = _effective_project_id(projectId)

    url = f"{ACONEX_BASE}/projects/{projectId}/register/schema"
    resp = requests.get(
        url, headers={"Authorization": auth, "Accept": "application/json"},
        timeout=60
    )
    return _as_json(resp)

@app.get("/document_metadata")
def document_metadata(
    projectId: Optional[str] = None,
    documentId: str = "",
    authorization: Optional[str] = Header(default=None),
):
    auth = _bearer_or_401(authorization)
    projectId = _effective_project_id(projectId)

    url = f"{ACONEX_BASE}/projects/{projectId}/register/{documentId}/metadata"
    resp = requests.get(
        url, headers={"Authorization": auth, "Accept": "application/json"},
        timeout=60
    )
    return _as_json(resp)

@app.get("/download_file")
def download_file(
    projectId: Optional[str] = None,
    documentId: str = "",
    authorization: Optional[str] = Header(default=None),
):
    auth = _bearer_or_401(authorization)
    projectId = _effective_project_id(projectId)

    url = f"{ACONEX_BASE}/projects/{projectId}/register/{documentId}/file"
    upstream = requests.get(
        url, headers={"Authorization": auth}, stream=True, timeout=300
    )

    # Propaga nombre de archivo si viene
    fname = None
    disp = upstream.headers.get("Content-Disposition") or ""
    if "filename=" in disp:
        try:
            fname = disp.split("filename=", 1)[1].strip("\"'; ")
        except Exception:
            fname = None

    media = upstream.headers.get("Content-Type") or "application/octet-stream"
    return StreamingResponse(
        upstream.iter_content(chunk_size=64 * 1024),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{fname or (documentId + ".bin")}"'
        },
    )
