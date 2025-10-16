from time import time
from fastapi.responses import JSONResponse

@app.get("/debug_env")
def debug_env():
    return {
        "ok": True,
        "aconex_base": ACONEX_BASE,
        "has_client_id": bool(ACONEX_CLIENT_ID),
        "has_client_secret": bool(ACONEX_CLIENT_SECRET),
        "default_project": DEFAULT_PROJECT_ID
    }

@app.get("/debug_token")
async def debug_token():
    try:
        t = await _get_access_token()
        return {
            "ok": True,
            "token_prefix": t[:16],        # no mostramos el token entero
            "expires_in_sec": int(_TOKEN["exp"] - time())
        }
    except HTTPException as e:
        # devolvemos 200 para poder leer el detalle fácil
        return JSONResponse({"ok": False, "status": e.status_code, "detail": e.detail}, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "status": 500, "detail": str(e)}, status_code=200)
