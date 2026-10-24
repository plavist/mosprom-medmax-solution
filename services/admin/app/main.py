"""HR-админка: Basic-auth, UI и прокси к ingest/pipeline."""
import hashlib
import os
import secrets
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

STATIC = Path(__file__).parent / "static"
security = HTTPBasic()


def auth(cred: HTTPBasicCredentials = Depends(security)) -> None:
    user = os.getenv("ADMIN_USER", "hr")
    pwd = os.getenv("ADMIN_PASSWORD", "admin")
    ok = secrets.compare_digest(cred.username.encode(), user.encode()) & secrets.compare_digest(cred.password.encode(), pwd.encode())
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="admin", dependencies=[Depends(auth)])


def upstream(name: str) -> str:
    return {"ingest": os.getenv("INGEST_URL", "http://ingest:8000"),
            "pipeline": os.getenv("PIPELINE_URL", "http://pipeline:8000")}[name]


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(service: str, path: str, request: Request):
    if service not in ("ingest", "pipeline"):
        raise HTTPException(404, "unknown service")
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type",)}
    if service == "pipeline":  # токен внутренних маршрутов pipeline (производный от ADMIN_PASSWORD)
        headers["x-internal-token"] = hashlib.sha256(("internal:" + os.getenv("ADMIN_PASSWORD", "admin")).encode()).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.request(request.method, f"{upstream(service)}/{path}", params=request.query_params,
                                content=await request.body(), headers=headers)
    except httpx.TransportError as e:
        raise HTTPException(502, f"{service} недоступен: {e}")
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
