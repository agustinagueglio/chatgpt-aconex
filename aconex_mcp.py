from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import os, requests, xmltodict, httpx

# ---- CONFIG ----
ACONEX_BASE = os.getenv("ACONEX_BASE", "https://us1.aconex.com/api")
ACONEX_OAUTH_BASE = "https://constructionandengineering.oraclecloud.com/auth"

app = FastAPI(title="Aconex MCP Minimal", version="1.0.0")

@app.get("/")
@app.head("/")
def root():
    return {"ok": True, "service": "aconex-mcp"}

@app.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    return RedirectResponse(
        f"{ACONEX_OAUTH_BASE}/authorize?{request.query_params}",
        status_code=302
    )

@app.post("/oauth/token")
async def oauth_token(request: Request):
    body = await request.body()
    fwd_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if "authorization" in request.headers:
        fwd_headers["Authorization"] = request.headers["authorization"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{ACONEX_OAUTH_BASE}/token",
                              headers=fwd_headers, content=body)

    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))

@app.on_event("startup")
async def on_startup():
    # Para ver en el log QUÉ archivo cargó Uvicorn
    print("### LOADED FILE:", __file__)

# ---- ENDPOINTS DE DEBUG ----
@app.get("/__whoami")
def __whoami():
    return {"file": __file__}

@app.get("/__routes")
def __routes():
    return [r.path for r in app.router.routes]

# ---- PROXY OAUTH HACIA ACONEX (mismo root domain que la API) ----
@app.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    # Redirige a Aconex manteniendo todos los parámetros
    return RedirectResponse(f"{ACONEX_OAUTH_BASE}/authorize?{request.query_params}", status_code=302)

@app.post("/oauth/token")
async def oauth_token(request: Request):
    body = await request.body()
    fwd_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if "authorization" in request.headers:
        fwd_headers["Authorization"] = request.headers["authorization"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{ACONEX_OAUTH_BASE}/token", headers=fwd_headers, content=body)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))

# ---- CORS ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---- HELPERS ----
def _bearer_or_401(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <token>")
    return authorization

def _as_json(resp: requests.Response):
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return JSONResponse(data, status_code=resp.status_code)
    else:
        try:
            data = xmltodict.parse(resp.text)
        except Exception:
            return PlainTextResponse(resp.text, status_code=resp.status_code)
        return JSONResponse(data, status_code=resp.status_code)

# ---- ENDPOINTS DE NEGOCIO ----
@app.get("/healthz")
def healthz():
    return {"ok": True, "aconex_base": ACONEX_BASE}

@app.get("/search_register")
def search_register(
    projectId: str,
    page_number: int = 1,
    page_size: int = 50,
    search_query: str | None = None,
    return_fields: str = "docno,title,statusid,revision,registered",
    authorization: str | None = Header(default=None),
):
    auth = _bearer_or_401(authorization)
    params = {
        "search_type": "PAGED",
        "page_number": page_number,
        "page_size": page_size,
        "return_fields": return_fields
    }
    if search_query:
        params["search_query"] = search_query
    url = f"{ACONEX_BASE}/projects/{projectId}/register"
    resp = requests.get(url, headers={"Authorization": auth, "Accept": "application/json"}, params=params, timeout=60)
    return _as_json(resp)

@app.get("/register_schema")
def register_schema(projectId: str, authorization: str | None = Header(default=None)):
    auth = _bearer_or_401(authorization)
    url = f"{ACONEX_BASE}/projects/{projectId}/register/schema"
    resp = requests.get(url, headers={"Authorization": auth, "Accept": "application/json"}, timeout=60)
    return _as_json(resp)

@app.get("/document_metadata")
def document_metadata(projectId: str, documentId: str, authorization: str | None = Header(default=None)):
    auth = _bearer_or_401(authorization)
    url = f"{ACONEX_BASE}/projects/{projectId}/register/{documentId}/metadata"
    resp = requests.get(url, headers={"Authorization": auth, "Accept": "application/json"}, timeout=60)
    return _as_json(resp)

@app.get("/download_file")
def download_file(projectId: str, documentId: str, authorization: str | None = Header(default=None)):
    auth = _bearer_or_401(authorization)
    url = f"{ACONEX_BASE}/projects/{projectId}/register/{documentId}/file"
    upstream = requests.get(url, headers={"Authorization": auth}, stream=True, timeout=300)
    fname = None
    disp = upstream.headers.get("Content-Disposition") or ""
    if "filename=" in disp:
        try:
            fname = disp.split("filename=",1)[1].strip("\"'; ")
        except Exception:
            fname = None
    media = upstream.headers.get("Content-Type") or "application/octet-stream"
    return StreamingResponse(
        upstream.iter_content(chunk_size=64*1024),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{fname or (documentId + ".bin")}"'}
    )

