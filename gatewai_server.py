"""Cloud entrypoint: sync REST API + reverse-proxy to Streamlit on one PORT.

Uses Starlette (already required by Streamlit) instead of FastAPI to avoid
starlette version conflicts at install time.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

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

_streamlit_proc: Optional[subprocess.Popen] = None
_http_client: Optional[httpx.AsyncClient] = None
_streamlit_ready = False

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


def _auth_or_error(request: Request):
    if not GATEWAI_PASSWORD:
        return JSONResponse(
            {"detail": "Cloud sync unavailable (GATEWAI_PASSWORD not set)."},
            status_code=503,
        )
    authorization = request.headers.get("authorization") or ""
    x_pw = request.headers.get("x-gatewai-password") or ""
    token = None
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_pw:
        token = x_pw.strip()
    if not token or token != GATEWAI_PASSWORD:
        return JSONResponse({"detail": "Unauthorized."}, status_code=401)
    return None


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


async def healthz(request: Request):
    if not _streamlit_ready or _http_client is None:
        return JSONResponse({"ok": False, "streamlit": False}, status_code=503)
    return JSONResponse({"ok": True, "streamlit": True})


async def api_health(request: Request):
    err = _auth_or_error(request)
    if err:
        return err
    projects, _system = ensure_data_dirs()
    return JSONResponse(
        {
            "ok": True,
            "projects_dir": projects,
            "project_count": len(list_project_names(projects)),
        }
    )


async def api_list_projects(request: Request):
    err = _auth_or_error(request)
    if err:
        return err
    projects, _system = ensure_data_dirs()
    return JSONResponse({"projects": list_project_names(projects)})


async def api_get_project(request: Request):
    err = _auth_or_error(request)
    if err:
        return err
    name = request.path_params["name"]
    qs = parse_qs(request.url.query)
    include_globals = (qs.get("include_globals", ["0"])[0] in ("1", "true", "True"))
    projects, system = ensure_data_dirs()
    safe_name = safe_project_folder_name(name)
    try:
        zip_bytes = build_project_zip_bytes(
            safe_name,
            include_system_globals=include_globals,
            base_project_dir=projects,
            base_system_dir=system,
        )
    except FileNotFoundError:
        return JSONResponse(
            {"detail": f"Project '{safe_name}' not found."}, status_code=404
        )
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    filename = f"{safe_name}.gatewai.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Gatewai-Bytes": str(len(zip_bytes)),
        },
    )


async def api_put_project(request: Request):
    err = _auth_or_error(request)
    if err:
        return err
    name = request.path_params["name"]
    qs = parse_qs(request.url.query)
    overwrite = qs.get("overwrite", ["0"])[0] in ("1", "true", "True")
    apply_globals = qs.get("apply_globals", ["0"])[0] in ("1", "true", "True")
    projects, system = ensure_data_dirs()
    safe_name = safe_project_folder_name(name)
    body = await request.body()
    if not body:
        return JSONResponse({"detail": "Empty zip body."}, status_code=400)
    if len(body) > PROJECT_ZIP_MAX_BYTES:
        return JSONResponse(
            {
                "detail": (
                    f"Zip larger than {PROJECT_ZIP_MAX_BYTES // (1024 * 1024)} MB."
                )
            },
            status_code=413,
        )
    try:
        result = import_project_zip_bytes(
            body,
            safe_name,
            overwrite=overwrite,
            apply_system_globals=apply_globals,
            base_project_dir=projects,
            base_system_dir=system,
        )
    except FileExistsError as e:
        return JSONResponse({"detail": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    return JSONResponse(
        {
            "ok": True,
            "project_name": result["project_name"],
            "globals_applied": result.get("globals_applied", 0),
            "bytes": len(body),
        }
    )


async def _proxy_http(request: Request):
    if _http_client is None:
        return JSONResponse({"detail": "Streamlit not ready."}, status_code=503)

    path = request.url.path
    target_path = path if path else "/"
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


async def proxy_stcore_stream(websocket: WebSocket):
    await websocket.accept()
    qs = websocket.url.query
    ws_url = f"ws://{STREAMLIT_HOST}:{STREAMLIT_PORT}/_stcore/stream"
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


async def catch_all(request: Request):
    path = request.url.path.lstrip("/")
    if path == "healthz" or path.startswith("api/"):
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return await _proxy_http(request)


routes = [
    Route("/healthz", healthz, methods=["GET"]),
    Route("/api/v1/health", api_health, methods=["GET"]),
    Route("/api/v1/projects", api_list_projects, methods=["GET"]),
    Route("/api/v1/projects/{name}", api_get_project, methods=["GET"]),
    Route("/api/v1/projects/{name}", api_put_project, methods=["PUT"]),
    WebSocketRoute("/_stcore/stream", proxy_stcore_stream),
    Route("/", catch_all, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    Route(
        "/{path:path}",
        catch_all,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    ),
]

app = Starlette(
    debug=False,
    routes=routes,
    on_startup=[on_startup],
    on_shutdown=[on_shutdown],
)
