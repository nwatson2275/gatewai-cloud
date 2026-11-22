"""Cloud entrypoint: sync REST API + reverse-proxy to Streamlit on one PORT."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from project_transfer import (
    PROJECT_ZIP_MAX_BYTES,
    build_project_zip_bytes,
    ensure_data_dirs,
    import_project_zip_bytes,
    list_project_names,
    safe_project_folder_name,
)

STREAMLIT_HOST = "127.0.0.1"
STREAMLIT_PORT = 8501
STREAMLIT_BASE = f"http://{STREAMLIT_HOST}:{STREAMLIT_PORT}"
GATEWAI_PASSWORD = os.environ.get("GATEWAI_PASSWORD", "").strip()

app = FastAPI(title="Gatewai Cloud", docs_url=None, redoc_url=None)
_streamlit_proc: Optional[subprocess.Popen] = None
_http_client: Optional[httpx.AsyncClient] = None
_streamlit_ready = False


def _require_password(
    authorization: Optional[str] = None,
    x_gatewai_password: Optional[str] = None,
):
    if not GATEWAI_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="Cloud sync unavailable (GATEWAI_PASSWORD not set).",
        )
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_gatewai_password:
        token = x_gatewai_password.strip()
    if not token or token != GATEWAI_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized.")


def _start_streamlit():
    global _streamlit_proc
    if _streamlit_proc and _streamlit_proc.poll() is None:
        return
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.address",
        STREAMLIT_HOST,
        "--server.port",
        str(STREAMLIT_PORT),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--server.enableCORS",
        "false",
        "--server.enableXsrfProtection",
        "false",
    ]
    _streamlit_proc = subprocess.Popen(
        cmd,
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
        env=os.environ.copy(),
    )


async def _wait_for_streamlit(timeout_s: float = 120.0):
    deadline = time.time() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.time() < deadline:
            if _streamlit_proc and _streamlit_proc.poll() is not None:
                raise RuntimeError(
                    f"Streamlit exited early with code {_streamlit_proc.returncode}"
                )
            try:
                resp = await client.get(STREAMLIT_BASE + "/_stcore/health", timeout=2.0)
                if resp.status_code < 500:
                    return
            except Exception:
                try:
                    resp = await client.get(STREAMLIT_BASE + "/", timeout=2.0)
                    if resp.status_code < 500:
                        return
                except Exception:
                    pass
            await asyncio.sleep(0.5)
    raise RuntimeError("Streamlit did not become ready in time.")


@app.on_event("startup")
async def on_startup():
    global _http_client, _streamlit_ready
    ensure_data_dirs()
    _start_streamlit()
    await _wait_for_streamlit()
    _http_client = httpx.AsyncClient(
        base_url=STREAMLIT_BASE,
        timeout=None,
        follow_redirects=False,
    )
    _streamlit_ready = True


@app.on_event("shutdown")
async def on_shutdown():
    global _http_client, _streamlit_proc, _streamlit_ready
    _streamlit_ready = False
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
    if _streamlit_proc and _streamlit_proc.poll() is None:
        _streamlit_proc.terminate()
        try:
            _streamlit_proc.wait(timeout=10)
        except Exception:
            _streamlit_proc.kill()
    _streamlit_proc = None


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness for Render health checks."""
    if not _streamlit_ready or _http_client is None:
        return JSONResponse({"ok": False, "streamlit": False}, status_code=503)
    return {"ok": True, "streamlit": True}


# --- Sync API ---


@app.get("/api/v1/health")
async def api_health(
    authorization: Optional[str] = Header(default=None),
    x_gatewai_password: Optional[str] = Header(default=None, alias="X-Gatewai-Password"),
):
    _require_password(authorization, x_gatewai_password)
    projects, _system = ensure_data_dirs()
    return {
        "ok": True,
        "projects_dir": projects,
        "project_count": len(list_project_names(projects)),
    }


@app.get("/api/v1/projects")
async def api_list_projects(
    authorization: Optional[str] = Header(default=None),
    x_gatewai_password: Optional[str] = Header(default=None, alias="X-Gatewai-Password"),
):
    _require_password(authorization, x_gatewai_password)
    projects, _system = ensure_data_dirs()
    return {"projects": list_project_names(projects)}


@app.get("/api/v1/projects/{name}")
async def api_get_project(
    name: str,
    include_globals: int = Query(0),
    authorization: Optional[str] = Header(default=None),
    x_gatewai_password: Optional[str] = Header(default=None, alias="X-Gatewai-Password"),
):
    _require_password(authorization, x_gatewai_password)
    projects, system = ensure_data_dirs()
    safe_name = safe_project_folder_name(name)
    try:
        zip_bytes = build_project_zip_bytes(
            safe_name,
            include_system_globals=bool(include_globals),
            base_project_dir=projects,
            base_system_dir=system,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Project '{safe_name}' not found.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    filename = f"{safe_name}.gatewai.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Gatewai-Bytes": str(len(zip_bytes)),
        },
    )


@app.put("/api/v1/projects/{name}")
async def api_put_project(
    name: str,
    request: Request,
    overwrite: int = Query(0),
    apply_globals: int = Query(0),
    authorization: Optional[str] = Header(default=None),
    x_gatewai_password: Optional[str] = Header(default=None, alias="X-Gatewai-Password"),
):
    _require_password(authorization, x_gatewai_password)
    projects, system = ensure_data_dirs()
    safe_name = safe_project_folder_name(name)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty zip body.")
    if len(body) > PROJECT_ZIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Zip larger than {PROJECT_ZIP_MAX_BYTES // (1024 * 1024)} MB.",
        )
    try:
        result = import_project_zip_bytes(
            body,
            safe_name,
            overwrite=bool(overwrite),
            apply_system_globals=bool(apply_globals),
            base_project_dir=projects,
            base_system_dir=system,
        )
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "ok": True,
        "project_name": result["project_name"],
        "globals_applied": result.get("globals_applied", 0),
        "bytes": len(body),
    }


# --- Streamlit reverse proxy ---

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


async def _proxy_http(request: Request, path: str):
    if _http_client is None:
        return JSONResponse({"detail": "Streamlit not ready."}, status_code=503)

    target_path = "/" + path if path else "/"
    if request.url.query:
        target_path = f"{target_path}?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    body = await request.body()

    try:
        upstream = await _http_client.request(
            request.method,
            target_path,
            headers=headers,
            content=body,
        )
    except httpx.RequestError as e:
        return JSONResponse(
            {"detail": f"Upstream Streamlit error: {e}"},
            status_code=502,
        )

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def _proxy_websocket(websocket: WebSocket, upstream_path: str):
    await websocket.accept()
    qs = websocket.url.query
    ws_url = f"ws://{STREAMLIT_HOST}:{STREAMLIT_PORT}{upstream_path}"
    if qs:
        ws_url = f"{ws_url}?{qs}"

    try:
        import websockets
    except ImportError:
        await websocket.close(code=1011)
        return

    header_items = [
        (k, v)
        for k, v in websocket.headers.items()
        if k.lower()
        not in (
            "host",
            "connection",
            "upgrade",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
            "content-length",
        )
    ]

    try:
        async with websockets.connect(ws_url, additional_headers=header_items) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


@app.websocket("/_stcore/stream")
async def proxy_stcore_stream(websocket: WebSocket):
    await _proxy_websocket(websocket, "/_stcore/stream")


@app.api_route("/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_all(request: Request, path: str = ""):
    if path == "healthz" or path.startswith("api/"):
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return await _proxy_http(request, path)
