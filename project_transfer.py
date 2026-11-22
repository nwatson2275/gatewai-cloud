"""Import-safe project zip transfer + desktop↔cloud sync client.

No Streamlit side effects — safe for gatewai_server and local app imports.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime
from urllib.parse import quote, urljoin

import requests

import database as db

PROJECT_ZIP_WARN_BYTES = 500 * 1024 * 1024
PROJECT_ZIP_MAX_BYTES = 1024 * 1024 * 1024
SYSTEM_GLOBALS_EXPORT = "system_globals.json"
GLOBAL_CORE = ["SI_Identity", "SI_User_Profile", "SI_Observed_Profile"]
DEFAULT_CLOUD_URL = "https://gatewai-u1ww.onrender.com"


def data_roots(data_dir=None):
    root = (data_dir if data_dir is not None else os.environ.get("GATEWAI_DATA_DIR", "")).strip() or "."
    projects = os.path.join(root, "Projects")
    system = os.path.join(root, "System")
    return root, projects, system


def ensure_data_dirs(data_dir=None):
    _, projects, system = data_roots(data_dir)
    os.makedirs(projects, exist_ok=True)
    os.makedirs(system, exist_ok=True)
    return projects, system


def list_project_names(base_project_dir=None):
    if base_project_dir is None:
        _, base_project_dir, _ = data_roots()
    if not os.path.isdir(base_project_dir):
        return []
    return sorted(
        d
        for d in os.listdir(base_project_dir)
        if os.path.isdir(os.path.join(base_project_dir, d))
    )


def safe_project_folder_name(name):
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", str(name or "").strip())
    cleaned = cleaned.strip(". ")
    return cleaned or "Imported_Project"


# Back-compat alias used by app.py
_safe_project_folder_name = safe_project_folder_name


def export_system_globals_payload(base_system_dir=None):
    """Slim export of global SI slots for optional backup/migrate zips."""
    if base_system_dir is None:
        _, _, base_system_dir = data_roots()
    sys_db = db.get_system_db_path(base_system_dir)
    if not os.path.exists(sys_db):
        return {}
    payload = {}
    try:
        with sqlite3.connect(sys_db, timeout=30) as conn:
            c = conn.cursor()
            for slot_name in GLOBAL_CORE:
                c.execute(
                    "SELECT content, description, ai_managed FROM memory_slots WHERE slot_name = ?",
                    (slot_name,),
                )
                row = c.fetchone()
                if row:
                    payload[slot_name] = {
                        "content": row[0] if row[0] is not None else "",
                        "description": row[1] or "",
                        "ai_managed": bool(row[2]) if row[2] is not None else False,
                    }
    except Exception:
        return {}
    return payload


def apply_system_globals_payload(payload, base_system_dir=None):
    if not isinstance(payload, dict) or not payload:
        return 0
    if base_system_dir is None:
        _, _, base_system_dir = data_roots()
    sys_db = db.get_system_db_path(base_system_dir)
    db.init_db(sys_db)
    applied = 0
    for slot_name in GLOBAL_CORE:
        entry = payload.get(slot_name)
        if not isinstance(entry, dict):
            continue
        content = entry.get("content", "")
        description = entry.get("description", "System Core File")
        ai_managed = bool(entry.get("ai_managed", False))
        db.save_slot(
            "",
            sys_db,
            slot_name,
            content if content is not None else "",
            "core",
            description,
            "global",
            "system",
            False,
            False,
            ai_managed,
        )
        applied += 1
    return applied


def build_project_zip_bytes(
    project_name,
    include_system_globals=False,
    base_project_dir=None,
    base_system_dir=None,
):
    """Zip one project folder (and optional system globals) into memory."""
    if base_project_dir is None or base_system_dir is None:
        _, default_projects, default_system = data_roots()
        if base_project_dir is None:
            base_project_dir = default_projects
        if base_system_dir is None:
            base_system_dir = default_system
    proj_dir = os.path.join(base_project_dir, project_name)
    if not os.path.isdir(proj_dir):
        raise FileNotFoundError(f"Project folder not found: {project_name}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "gatewai-project-v1",
                    "project_name": project_name,
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "includes_system_globals": bool(include_system_globals),
                },
                indent=2,
            ),
        )
        for root, _dirs, files in os.walk(proj_dir):
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, proj_dir).replace("\\", "/")
                zf.write(full, f"project/{rel}")
        if include_system_globals:
            globals_payload = export_system_globals_payload(base_system_dir)
            zf.writestr(
                SYSTEM_GLOBALS_EXPORT,
                json.dumps(globals_payload, indent=2),
            )
    return buffer.getvalue()


def import_project_zip_bytes(
    raw_bytes,
    target_name,
    overwrite=False,
    apply_system_globals=True,
    base_project_dir=None,
    base_system_dir=None,
):
    """Unpack a Gatewai project zip into Projects/<target_name>/."""
    if base_project_dir is None or base_system_dir is None:
        _, default_projects, default_system = data_roots()
        if base_project_dir is None:
            base_project_dir = default_projects
        if base_system_dir is None:
            base_system_dir = default_system
    if raw_bytes is None:
        raise ValueError("Empty upload.")
    if len(raw_bytes) > PROJECT_ZIP_MAX_BYTES:
        raise ValueError(
            f"Zip is larger than {PROJECT_ZIP_MAX_BYTES // (1024 * 1024)} MB. "
            "Trim Assets or history before transferring."
        )
    target_name = safe_project_folder_name(target_name)
    dest = os.path.join(base_project_dir, target_name)
    if os.path.exists(dest) and not overwrite:
        raise FileExistsError(
            f"Project '{target_name}' already exists. Choose overwrite or a new name."
        )

    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        names = zf.namelist()
        project_prefix = None
        for candidate in ("project/",):
            if any(n.startswith(candidate) for n in names):
                project_prefix = candidate
                break
        if project_prefix is None:
            has_history = any(n.endswith("history.json") or n == "history.json" for n in names)
            has_db = any(n.endswith("project.db") or n == "project.db" for n in names)
            if not (has_history or has_db):
                raise ValueError(
                    "Not a Gatewai project zip (missing project/ folder or history.json / project.db)."
                )
            project_prefix = ""

        if os.path.exists(dest) and overwrite:
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        assets_dir = os.path.join(dest, "Assets")
        os.makedirs(assets_dir, exist_ok=True)

        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if name in ("manifest.json", SYSTEM_GLOBALS_EXPORT):
                continue
            if project_prefix and not name.startswith(project_prefix):
                continue
            rel = name[len(project_prefix) :] if project_prefix else name
            if not rel or rel.endswith("/"):
                continue
            if ".." in rel.split("/"):
                continue
            out_path = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with zf.open(info, "r") as src, open(out_path, "wb") as out:
                shutil.copyfileobj(src, out)

        globals_applied = 0
        if apply_system_globals and SYSTEM_GLOBALS_EXPORT in names:
            try:
                payload = json.loads(zf.read(SYSTEM_GLOBALS_EXPORT).decode("utf-8"))
                globals_applied = apply_system_globals_payload(payload, base_system_dir)
            except Exception:
                globals_applied = 0

    proj_db = db.get_project_db_path(base_project_dir, target_name)
    db.init_db(proj_db)
    db.migrate_db(proj_db)
    hist_path = os.path.join(dest, "history.json")
    if not os.path.exists(hist_path):
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=4)
    return {"project_name": target_name, "globals_applied": globals_applied}


# --- Desktop cloud sync client ---


class CloudSyncError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _normalize_base_url(base_url):
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise CloudSyncError("Cloud URL is required.")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url


def _auth_headers(password):
    token = (password or "").strip()
    if not token:
        raise CloudSyncError("Vault password is required.")
    return {
        "Authorization": f"Bearer {token}",
        "X-Gatewai-Password": token,
    }


def _raise_for_sync_response(resp):
    if resp.status_code == 401:
        raise CloudSyncError("Wrong vault password (401).", 401)
    if resp.status_code == 404:
        raise CloudSyncError("Project not found on cloud (404).", 404)
    if resp.status_code == 409:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text[:200]
        raise CloudSyncError(detail or "Project already exists on cloud (409).", 409)
    if resp.status_code == 503:
        raise CloudSyncError("Cloud sync API unavailable (GATEWAI_PASSWORD not set).", 503)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = (resp.text or "")[:300]
        raise CloudSyncError(
            detail or f"Cloud request failed ({resp.status_code}).",
            resp.status_code,
        )


def cloud_health(base_url, password, timeout=60):
    url = urljoin(_normalize_base_url(base_url) + "/", "api/v1/health")
    resp = requests.get(url, headers=_auth_headers(password), timeout=timeout)
    _raise_for_sync_response(resp)
    return resp.json()


def cloud_list_projects(base_url, password, timeout=60):
    url = urljoin(_normalize_base_url(base_url) + "/", "api/v1/projects")
    resp = requests.get(url, headers=_auth_headers(password), timeout=timeout)
    _raise_for_sync_response(resp)
    data = resp.json()
    return list(data.get("projects") or [])


def cloud_push_project(
    base_url,
    password,
    project_name,
    overwrite=False,
    include_system_globals=False,
    base_project_dir=None,
    base_system_dir=None,
    timeout=600,
):
    zip_bytes = build_project_zip_bytes(
        project_name,
        include_system_globals=include_system_globals,
        base_project_dir=base_project_dir,
        base_system_dir=base_system_dir,
    )
    safe_name = safe_project_folder_name(project_name)
    base = _normalize_base_url(base_url)
    url = (
        f"{base}/api/v1/projects/{quote(safe_name, safe='')}"
        f"?overwrite={'1' if overwrite else '0'}"
        f"&apply_globals={'1' if include_system_globals else '0'}"
    )
    headers = _auth_headers(password)
    headers["Content-Type"] = "application/zip"
    resp = requests.put(url, data=zip_bytes, headers=headers, timeout=timeout)
    _raise_for_sync_response(resp)
    try:
        return resp.json()
    except Exception:
        return {"project_name": safe_name, "bytes": len(zip_bytes)}


def cloud_pull_project(
    base_url,
    password,
    remote_name,
    local_name=None,
    overwrite=False,
    apply_system_globals=True,
    include_system_globals=True,
    base_project_dir=None,
    base_system_dir=None,
    timeout=600,
):
    safe_remote = safe_project_folder_name(remote_name)
    target = safe_project_folder_name(local_name or remote_name)
    base = _normalize_base_url(base_url)
    url = (
        f"{base}/api/v1/projects/{quote(safe_remote, safe='')}"
        f"?include_globals={'1' if include_system_globals else '0'}"
    )
    resp = requests.get(url, headers=_auth_headers(password), timeout=timeout)
    _raise_for_sync_response(resp)
    return import_project_zip_bytes(
        resp.content,
        target,
        overwrite=overwrite,
        apply_system_globals=apply_system_globals,
        base_project_dir=base_project_dir,
        base_system_dir=base_system_dir,
    )
