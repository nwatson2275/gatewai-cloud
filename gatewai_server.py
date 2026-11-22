"""Cloud ASGI entry: Streamlit UI + sync REST API in one process (no reverse proxy).

Uses st.App so /_stcore/stream WebSockets work natively. Sync routes live under /api/v1/.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import parse_qs

import streamlit as st
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from project_transfer import (
    PROJECT_ZIP_MAX_BYTES,
    build_project_zip_bytes,
    ensure_data_dirs,
    import_project_zip_bytes,
    list_project_names,
    safe_project_folder_name,
)

GATEWAI_PASSWORD = os.environ.get("GATEWAI_PASSWORD", "").strip()


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


async def healthz(request: Request):
    """Unauthenticated liveness for Render health checks."""
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
    include_globals = qs.get("include_globals", ["0"])[0] in ("1", "true", "True")
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


@asynccontextmanager
async def lifespan(_app):
    ensure_data_dirs()
    yield


app = st.App(
    "app.py",
    routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/v1/health", api_health, methods=["GET"]),
        Route("/api/v1/projects", api_list_projects, methods=["GET"]),
        Route("/api/v1/projects/{name}", api_get_project, methods=["GET"]),
        Route("/api/v1/projects/{name}", api_put_project, methods=["PUT"]),
    ],
    lifespan=lifespan,
)
