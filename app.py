import streamlit as st
#import google.generativeai as genai
from google import genai
from google.genai import types
import json
import os
import time
import hmac
import hashlib
import markdown
import portalocker
import re
import requests
import ast
import shutil
import database as db
import sqlite3
import zipfile
import io
import tempfile
from datetime import datetime
from project_transfer import (
    PROJECT_ZIP_WARN_BYTES,
    PROJECT_ZIP_MAX_BYTES,
    SYSTEM_GLOBALS_EXPORT,
    GLOBAL_CORE as TRANSFER_GLOBAL_CORE,
    safe_project_folder_name as _safe_project_folder_name,
    export_system_globals_payload,
    apply_system_globals_payload,
    build_project_zip_bytes,
    import_project_zip_bytes,
    list_project_names,
    cloud_list_projects,
    cloud_push_project,
    cloud_pull_project,
    CloudSyncError,
    DEFAULT_CLOUD_URL,
)
from bs4 import BeautifulSoup
from pypdf import PdfReader
from fpdf import FPDF
import streamlit.components.v1 as components
from streamlit_ace import st_ace
import base64 # <-- NEW: Import base64 for printing functionality
import html # <-- NEW: Import html for escaping content before printing
#from google.ai.generativelanguage_v1beta.types import content
#from google.ai.generativelanguage_v1beta import types


# --- NEW: Initialize help chat session state ---
if 'help_messages' not in st.session_state:
    st.session_state.help_messages = []
if 'help_chat_input_key' not in st.session_state:
    st.session_state.help_chat_input_key = 0

# --- 1. CORE CONFIGURATION ---
#print("DEBUG: ---- SCRIPT START ----") # <-- ADD THIS LINE
# --- EARLY STATE MODIFICATION HANDLING ---
# This block runs *before* any UI elements are rendered,
# allowing us to safely modify session state variables bound to widgets.

if st.session_state.get('asset_selector_state_needs_update', False):
    # Ensure asset_selector_state exists and is a list
    current_selection = st.session_state.get('asset_selector_state', [])
    if not isinstance(current_selection, list):
        current_selection = [] # Default to empty list if it's somehow corrupted

    # Perform the filtering logic
    updated_selection = [s for s in current_selection if s not in st.session_state.auto_deselect_list]

    # Update the state variable
    st.session_state.asset_selector_state = updated_selection

    # Reset the flag and the auto_deselect_list
    st.session_state.asset_selector_state_needs_update = False
    st.session_state.auto_deselect_list = []

    # No rerun needed here; the update happens before the widget re-renders based on this new state.
#    print("DEBUG: Applied asset_selector_state update from flag.")
# --- END EARLY STATE MODIFICATION HANDLING ---


st.set_page_config(
    page_title="Gatewai VI - DEV",
    page_icon="🤖",
    layout="wide",  # <--- This is the magic key
    initial_sidebar_state="expanded"
)

# Data root must exist before the password gate so one-shot seeds can run on cold start.
GATEWAI_DATA_DIR = os.environ.get("GATEWAI_DATA_DIR", "").strip() or "."
BASE_PROJECT_DIR = os.path.join(GATEWAI_DATA_DIR, "Projects")
BASE_SYSTEM_DIR = os.path.join(GATEWAI_DATA_DIR, "System")
DOMAINS_DIR = os.path.join(BASE_SYSTEM_DIR, "Domains")
if not os.path.exists(BASE_PROJECT_DIR):
    os.makedirs(BASE_PROJECT_DIR)
if not os.path.exists(BASE_SYSTEM_DIR):
    os.makedirs(BASE_SYSTEM_DIR)
if not os.path.exists(DOMAINS_DIR):
    os.makedirs(DOMAINS_DIR)

def apply_gatewai_seed_si_identity():
    """One-shot: copy SI_Identity from GATEWAI_SEED_SI_IDENTITY into System/system.db."""
    seed = os.environ.get("GATEWAI_SEED_SI_IDENTITY", "")
    if seed is None:
        return
    seed = str(seed)
    if not seed.strip():
        return
    try:
        sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
        db.init_db(sys_db)
        db.migrate_db(sys_db)
        db.save_slot(
            "",
            sys_db,
            "SI_Identity",
            seed,
            "core",
            "System Core File",
            "global",
            "system",
            False,
            False,
            False,
        )
        marker = os.path.join(BASE_SYSTEM_DIR, ".si_identity_seeded")
        with open(marker, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds") + "\n")
        print(f"DEBUG: Seeded SI_Identity ({len(seed)} chars) from GATEWAI_SEED_SI_IDENTITY")
    except Exception as e:
        print(f"ERROR seeding SI_Identity: {e}")

apply_gatewai_seed_si_identity()

# Cloud gate: when GATEWAI_PASSWORD is set (Render), require unlock.
# Desktop without the env var stays open.
# Auth is session-scoped AND cookie-backed so mobile reconnects don't bounce to login.
_GATEWAI_PASSWORD = os.environ.get("GATEWAI_PASSWORD", "").strip()
_GATEWAI_AUTH_COOKIE = "gatewai_vault"
_GATEWAI_AUTH_TTL_SEC = 14 * 24 * 60 * 60


def _gatewai_auth_token(password, ttl_sec=_GATEWAI_AUTH_TTL_SEC):
    exp = int(time.time()) + int(ttl_sec)
    payload = f"1|{exp}"
    sig = hmac.new(password.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode("utf-8")).decode("ascii")


def _gatewai_auth_token_valid(password, token):
    if not password or not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        ver, exp_s, sig = raw.split("|", 2)
        exp = int(exp_s)
        if ver != "1" or exp < int(time.time()):
            return False
        payload = f"{ver}|{exp}"
        expect = hmac.new(
            password.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expect, sig)
    except Exception:
        return False


def _read_gatewai_auth_cookie():
    try:
        cookies = getattr(st.context, "cookies", None)
        if cookies is None:
            return ""
        if hasattr(cookies, "get"):
            return str(cookies.get(_GATEWAI_AUTH_COOKIE) or "")
        return str(cookies.get(_GATEWAI_AUTH_COOKIE, "") if isinstance(cookies, dict) else "")
    except Exception:
        return ""


def _emit_gatewai_auth_cookie(token):
    """Set durable unlock cookie in the browser (survives Streamlit session recycle)."""
    if not token:
        return
    # Escape for JS string context
    safe = token.replace("\\", "\\\\").replace('"', '\\"')
    components.html(
        f"""<script>
(() => {{
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = "{_GATEWAI_AUTH_COOKIE}={safe}; path=/; max-age={_GATEWAI_AUTH_TTL_SEC}; SameSite=Lax" + secure;
}})();
</script>""",
        height=0,
    )


def pending_turn_path(project_name=None):
    project_name = project_name or st.session_state.get("active_project") or ""
    if not project_name:
        return None
    return os.path.join(BASE_PROJECT_DIR, project_name, "pending_turn.json")


def write_pending_turn(prompt_text, project_name=None):
    """Durable in-flight submit marker so session recycle can still call the API."""
    path = pending_turn_path(project_name)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "prompt": str(prompt_text or ""),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"ERROR: write_pending_turn failed: {e}")


def read_pending_turn(project_name=None):
    path = pending_turn_path(project_name)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and str(data.get("prompt") or "").strip():
            return data
    except Exception as e:
        print(f"ERROR: read_pending_turn failed: {e}")
    return None


def clear_pending_turn(project_name=None):
    path = pending_turn_path(project_name)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"ERROR: clear_pending_turn failed: {e}")


def _prompt_already_answered_in_history(prompt_text, messages):
    """True if this user prompt already has a non-empty model reply after it."""
    prompt_text = str(prompt_text or "").strip()
    if not prompt_text or not isinstance(messages, list):
        return False
    for i in range(len(messages) - 1):
        cur = messages[i]
        nxt = messages[i + 1]
        if not isinstance(cur, dict) or not isinstance(nxt, dict):
            continue
        if (
            cur.get("role") == "user"
            and str(cur.get("content") or "").strip() == prompt_text
            and nxt.get("role") == "model"
            and str(nxt.get("content") or "").strip()
        ):
            return True
    return False


def _pending_turn_age_seconds(pending):
    if not isinstance(pending, dict):
        return None
    raw = str(pending.get("saved_at") or "").strip()
    if not raw:
        return None
    try:
        saved = datetime.fromisoformat(raw)
        return max(0.0, (datetime.now() - saved).total_seconds())
    except Exception:
        return None


def _write_messages_history_file(messages, project_name=None):
    """Write history.json directly (avoid full save_project_state side effects)."""
    project_name = project_name or st.session_state.get("active_project") or ""
    if not project_name:
        return False
    hist_path = os.path.join(BASE_PROJECT_DIR, project_name, "history.json")
    try:
        os.makedirs(os.path.dirname(hist_path), exist_ok=True)
        payload = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in (messages or [])
            if isinstance(m, dict)
        ]
        with open(hist_path, "w", encoding="utf-8") as hist_file:
            json.dump(payload, hist_file, indent=4)
            hist_file.flush()
            os.fsync(hist_file.fileno())
        return True
    except Exception as e:
        print(f"ERROR: history.json write failed: {e}")
        return False


def scrub_orphan_user_turns(messages, keep_trailing_in_flight=False):
    """Drop every user bubble that has no following model reply.

    Early history writes left permanent partial user turns in the middle of
    chat; new prompts after that do not remove them. Returns (msgs, changed).
    """
    if not isinstance(messages, list):
        return [], False
    out = []
    changed = False
    i = 0
    n = len(messages)
    while i < n:
        cur = messages[i]
        if not isinstance(cur, dict):
            i += 1
            continue
        role = cur.get("role")
        if role == "user":
            nxt = messages[i + 1] if i + 1 < n else None
            answered = (
                isinstance(nxt, dict)
                and nxt.get("role") == "model"
                and str(nxt.get("content") or "").strip()
            )
            if answered:
                out.append(cur)
                out.append(nxt)
                i += 2
                continue
            # Unanswered user turn
            is_last = i == n - 1
            if is_last and keep_trailing_in_flight:
                out.append(cur)
                i += 1
                continue
            changed = True
            i += 1
            continue
        # Keep model/other rows (orphan model is rare but harmless to keep)
        out.append(cur)
        i += 1
    return out, changed


def reconcile_stale_chat_turn():
    """Drop leftover pending_turn / hold flags / unanswered user bubbles.

    Scrubs ALL unanswered user turns from history (not only the last one), so a
    stale partial prompt cannot linger after later successful sends.
    """
    cleaned = False
    # Include remount-before-model: Submit sets _run_model_after_remount then
    # st.rerun(); that next pass must keep the trailing user bubble.
    in_flight = bool(st.session_state.get("_chat_submit_this_run")) or bool(
        st.session_state.get("_run_model_after_remount")
    ) or bool(st.session_state.get("prompt_submit_in_flight"))

    pending = read_pending_turn()
    if pending and not in_flight:
        clear_pending_turn()
        cleaned = True

    if not in_flight:
        if st.session_state.get("held_chat_prompt") or st.session_state.get("prompt_submit_in_flight"):
            st.session_state.pop("held_chat_prompt", None)
            st.session_state.pop("prompt_submit_in_flight", None)
            cleaned = True

    messages = st.session_state.get("messages") or []
    scrubbed, changed = scrub_orphan_user_turns(
        messages,
        keep_trailing_in_flight=in_flight,
    )
    if changed:
        st.session_state.messages = scrubbed
        _write_messages_history_file(scrubbed)
        cleaned = True

    return cleaned


def pending_shadow_path(project_name=None):
    project_name = project_name or st.session_state.get("active_project") or ""
    if not project_name:
        return None
    return os.path.join(BASE_PROJECT_DIR, project_name, "pending_shadow.json")


def write_pending_shadow(after_msg_count, project_name=None):
    """Durable marker: chat reply saved, auto-shadow not finished yet."""
    path = pending_shadow_path(project_name)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "after_msg_count": int(after_msg_count),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"ERROR: write_pending_shadow failed: {e}")


def read_pending_shadow(project_name=None):
    path = pending_shadow_path(project_name)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"ERROR: read_pending_shadow failed: {e}")
        return None


def clear_pending_shadow(project_name=None):
    path = pending_shadow_path(project_name)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"ERROR: clear_pending_shadow failed: {e}")


def set_shadow_debug_logs(logs):
    """Keep Shadow Debugger durable across mobile reconnects/reruns."""
    if isinstance(logs, str):
        logs = [logs]
    if not isinstance(logs, list):
        logs = [str(logs)]
    st.session_state.shadow_logs = list(logs)
    # Auto-run logs should win over a stale manual-run buffer.
    st.session_state.pop("shadow_logs_manual_run", None)
    return st.session_state.shadow_logs


def scrub_chat_hold():
    """Clear in-flight prompt recovery so deletes/wipes don't resurrect text."""
    st.session_state.pop("held_chat_prompt", None)
    st.session_state.pop("unanswered_attempt", None)
    st.session_state.pop("prompt_submit_in_flight", None)
    st.session_state.pop("_chat_submit_this_run", None)
    clear_pending_turn()


def wipe_all_pending_turns():
    """Delete every project's pending_turn.json (stale in-flight markers)."""
    try:
        root = BASE_PROJECT_DIR
        if not root or not os.path.isdir(root):
            return
        for name in os.listdir(root):
            path = os.path.join(root, name, "pending_turn.json")
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f"ERROR: wipe pending_turn failed for {name}: {e}")
    except Exception as e:
        print(f"ERROR: wipe_all_pending_turns failed: {e}")


if _GATEWAI_PASSWORD:
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if not st.session_state.authenticated:
        cookie_token = _read_gatewai_auth_cookie()
        if _gatewai_auth_token_valid(_GATEWAI_PASSWORD, cookie_token):
            st.session_state.authenticated = True
            st.session_state._emit_auth_cookie = cookie_token
            st.rerun()
        st.title("Gatewai")
        st.caption("Enter the vault password to continue.")
        with st.form("gatewai_login"):
            entered = st.text_input("Password", type="password")
            submitted_login = st.form_submit_button("Unlock")
        if submitted_login:
            if entered == _GATEWAI_PASSWORD:
                st.session_state.authenticated = True
                st.session_state._emit_auth_cookie = _gatewai_auth_token(_GATEWAI_PASSWORD)
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.stop()
    # Refresh/set cookie after unlock so mobile reconnects skip the login form.
    emit_token = st.session_state.pop("_emit_auth_cookie", None)
    if emit_token:
        _emit_gatewai_auth_cookie(emit_token)
    elif not _gatewai_auth_token_valid(_GATEWAI_PASSWORD, _read_gatewai_auth_cookie()):
        st.session_state._emit_auth_cookie = _gatewai_auth_token(_GATEWAI_PASSWORD)
        _emit_gatewai_auth_cookie(st.session_state.pop("_emit_auth_cookie"))

# API Key Management - Initialize st.session_state.gemini_api_key if not present
if 'gemini_api_key' not in st.session_state:
    env_key = os.environ.get("GEMINI_API_KEY")
    secrets_key = ""
    try:
        secrets_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        secrets_key = ""

    if env_key:
        st.session_state.gemini_api_key = env_key
    elif secrets_key:
        st.session_state.gemini_api_key = secrets_key
    else:
        st.session_state.gemini_api_key = "" # Default empty if no key found

if 'deepseek_api_key' not in st.session_state:
    env_ds_key = os.environ.get("DEEPSEEK_API_KEY")
    secrets_ds_key = ""
    try:
        secrets_ds_key = st.secrets.get("DEEPSEEK_API_KEY", "")
    except Exception:
        secrets_ds_key = ""

    if env_ds_key:
        st.session_state.deepseek_api_key = env_ds_key
    elif secrets_ds_key:
        st.session_state.deepseek_api_key = secrets_ds_key
    else:
        st.session_state.deepseek_api_key = ""

# This warning is now displayed at the top level if no key is present after all checks
if not st.session_state.gemini_api_key and not st.session_state.deepseek_api_key:
    st.warning("⚠️ No API Key provided. Some AI functionalities will be disabled.")

# --- 1.1 UI & CSS (light theme: black text on white) ---
st.markdown(
    """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&display=swap');
        [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
        [data-testid="stSidebar"] textarea {
            color: #212121 !important;
            background-color: #ffffff !important;
        }
        [data-testid="stSidebar"] code {
            white-space: pre-wrap !important; word-break: break-word !important;
            display: block; width: 100%; background-color: #f0f2f6; padding: 10px; border-radius: 5px;
            font-size: 0.85rem; color: #212121;
        }
        code, pre {
            white-space: pre-wrap !important;
            word-break: break-word;
            overflow-wrap: break-word;
        }
        .section-header {
            margin-top: 15px; margin-bottom: 5px; font-weight: bold;
            color: #424242; border-bottom: 1px solid #e0e0e0;
        }
        /* Right Memory + Live Preview — app-level panes that push the whole shell left. */
        :root {
            --gatewai-rsb-width: 0px;
            --gatewai-lp-width: 0px;
        }
        /* Push left sidebar + main (incl. project manager) by both panes combined. */
        [data-testid="stAppViewContainer"] {
            margin-right: calc(var(--gatewai-lp-width) + var(--gatewai-rsb-width)) !important;
            transition: margin-right 0.12s ease;
        }
        .gatewai-rsb-panel {
            position: fixed !important;
            top: 0 !important;
            right: 0 !important;
            width: var(--gatewai-rsb-width) !important;
            height: 100vh !important;
            max-height: 100vh !important;
            background-color: #f0f2f6 !important;
            border-left: 1px solid rgba(49, 51, 63, 0.2) !important;
            overflow-x: hidden !important;
            overflow-y: auto !important;
            z-index: 1000100 !important;
            /* Extra top padding mirrors stSidebarHeader room for the chevron. */
            padding: 2.4rem 0.85rem 1.25rem 1rem !important;
            box-sizing: border-box !important;
        }
        /* Live Preview sits left of Memory (or at the right edge when Memory is closed). */
        .gatewai-lp-panel {
            position: fixed !important;
            top: 0 !important;
            right: var(--gatewai-rsb-width) !important;
            width: var(--gatewai-lp-width) !important;
            height: 100vh !important;
            max-height: 100vh !important;
            background-color: #ffffff !important;
            border-left: 1px solid rgba(49, 51, 63, 0.2) !important;
            overflow-x: hidden !important;
            overflow-y: auto !important;
            z-index: 1000090 !important;
            padding: 0.75rem 0.85rem 1.25rem 1rem !important;
            box-sizing: border-box !important;
        }
        .gatewai-rsb-drag,
        .gatewai-lp-drag {
            position: absolute !important;
            top: 0 !important;
            left: 0 !important;
            width: 6px !important;
            height: 100% !important;
            cursor: col-resize !important;
            z-index: 1000102 !important;
            background: transparent !important;
        }
        .gatewai-rsb-drag:hover,
        .gatewai-rsb-drag.dragging,
        .gatewai-lp-drag:hover,
        .gatewai-lp-drag.dragging {
            background: rgba(49, 51, 63, 0.18) !important;
        }
        /* Mirror Streamlit's stSidebarCollapseButton / stExpandSidebarButton. */
        .gatewai-rsb-chevron-btn {
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            background: transparent !important;
            border: none !important;
            border-radius: 0.5rem !important;
            padding: 0.2rem !important;
            margin: 0 !important;
            cursor: pointer !important;
            color: rgba(49, 51, 63, 0.6) !important; /* fadedText60 */
            line-height: 1 !important;
            box-shadow: none !important;
            min-width: 2rem !important;
            min-height: 2rem !important;
        }
        .gatewai-rsb-chevron-btn:hover {
            background: rgba(49, 51, 63, 0.1) !important;
            color: rgba(49, 51, 63, 0.85) !important;
        }
        .gatewai-rsb-chevron-btn .material-symbols-outlined {
            font-family: 'Material Symbols Outlined' !important;
            font-size: 1.5rem !important;
            font-weight: 400 !important;
            line-height: 1 !important;
            font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
        }
        /* Collapse: top-left of right pane (content edge) — mirror of left sidebar's top-right. */
        #gatewai-rsb-collapse-btn {
            position: absolute !important;
            top: 0.45rem !important;
            left: 0.35rem !important;
            z-index: 1000105 !important;
        }
        /* Expand: fixed top-right, same header band as stExpandSidebarButton. */
        #gatewai-rsb-expand-btn {
            position: fixed !important;
            top: 0.35rem !important;
            right: 0.55rem !important;
            z-index: 1000200 !important;
        }
        /* Decoy Streamlit << / >> targets — markers only; JS also hides the real buttons. */
        div.gatewai-rsb-expand,
        div.gatewai-rsb-collapse-target {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            opacity: 0 !important;
            overflow: hidden !important;
            pointer-events: none !important;
        }
        div.stButton > button:first-child { width: 100%; border-radius: 5px; }
        .purge-btn { border: 1px solid #ff4b4b; color: #ff4b4b; }
        .st-emotion-cache-1r6c0hp, .st-emotion-cache-1oe5f0g, .st-emotion-cache-1v0bbx, .st-emotion-cache-1w0pmv7 {
            overflow-x: hidden !important;
        }
        div[data-testid="stChatInput"] textarea {
            background-color: #ffffff !important;
            color: #212121 !important;
        }
        div[data-testid="stChatInput"] textarea::placeholder {
            color: #757575 !important;
        }
        @media (max-width: 768px) {
            section.main [data-testid="stExpander"] pre,
            section.main [data-testid="stExpander"] code {
                max-height: 140px !important;
                overflow: auto !important;
            }
            /* Keep the chat history pane scrollable on iOS after a model reply. */
            section.main div[data-testid="stVerticalBlockBorderWrapper"][style*="height"] {
                overflow-y: auto !important;
                -webkit-overflow-scrolling: touch !important;
                touch-action: pan-y !important;
            }
        }
        /* Keep snap iframes out of flex flow — never display:none (Safari skips those scripts). */
        section.main iframe[height="0"],
        section.main iframe[height="1"] {
            position: absolute !important;
            left: 0 !important;
            top: 0 !important;
            width: 0 !important;
            height: 0 !important;
            border: 0 !important;
            opacity: 0 !important;
            pointer-events: none !important;
            overflow: hidden !important;
        }
    </style>
    """,
    unsafe_allow_html=True
)


_CHAT_DRAFT_COOKIE = "gatewai_chat_draft"
_CHAT_DRAFT_LS = "gatewai_chat_draft"


def read_chat_draft_cookie():
    """Draft saved before a mobile reload (cookie is available on the next request)."""
    try:
        cookies = getattr(st.context, "cookies", None)
        if cookies is None:
            return ""
        raw = ""
        if hasattr(cookies, "get"):
            raw = str(cookies.get(_CHAT_DRAFT_COOKIE) or "")
        if not raw:
            return ""
        from urllib.parse import unquote

        return unquote(raw).strip()
    except Exception:
        return ""


def install_mobile_resume_guard():
    """Mobile draft save + ghost-shell prune. Do NOT auto-restore drafts into the box.

    Auto-restore was mobile-only pain: after Send, localStorage still held the text
    (send-button selectors miss on iOS), then timeouts/pageshow put a 'partial
    prompt never submitted' back into the chat box. Desktop rarely hit that path.
    """
    components.html(
        f"""
<script>
(function () {{
  const w = window.parent;
  const doc = w.document;
  const FLAG = '__gatewaiMobileResumeGuard';
  const LS_KEY = '{_CHAT_DRAFT_LS}';
  const COOKIE = '{_CHAT_DRAFT_COOKIE}';
  const SENT_KEY = 'gatewai_draft_cleared_after_send';

  function clearDraft() {{
    try {{ w.localStorage.removeItem(LS_KEY); }} catch (e) {{}}
    try {{
      const secure = w.location.protocol === 'https:' ? '; Secure' : '';
      doc.cookie = COOKIE + '=; path=/; max-age=0; SameSite=Lax' + secure;
    }} catch (e) {{}}
  }}

  function chatTextarea() {{
    return doc.querySelector('[data-testid="stChatInput"] textarea');
  }}

  function emptyChatBoxIfDraft() {{
    try {{
      const ta = chatTextarea();
      if (!ta) return;
      const draft = (function () {{
        try {{ return w.localStorage.getItem(LS_KEY) || ''; }} catch (e) {{ return ''; }}
      }})();
      // After a send we always wipe; also wipe if box still holds stale draft text.
      if (w.sessionStorage.getItem(SENT_KEY) || draft) {{
        clearDraft();
        try {{
          const proto = w.HTMLTextAreaElement && w.HTMLTextAreaElement.prototype;
          const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
          if (desc && desc.set) desc.set.call(ta, '');
          else ta.value = '';
          ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }} catch (e) {{}}
        try {{ w.sessionStorage.removeItem(SENT_KEY); }} catch (e) {{}}
      }}
    }} catch (e) {{}}
  }}

  function markSentAndClear() {{
    try {{ w.sessionStorage.setItem(SENT_KEY, '1'); }} catch (e) {{}}
    clearDraft();
    emptyChatBoxIfDraft();
  }}

  // Remove leftover Latest FAB / prune ghost shells on every inject.
  try {{
    const oldFab = doc.getElementById('gatewai-jump-latest-fab');
    if (oldFab) oldFab.remove();
  }} catch (e) {{}}
  try {{
    const roots = doc.querySelectorAll('[data-testid="stAppViewContainer"]');
    for (let i = 0; i < roots.length - 1; i++) {{ try {{ roots[i].remove(); }} catch (e) {{}} }}
    const apps = doc.querySelectorAll('[data-testid="stApp"]');
    for (let i = 0; i < apps.length - 1; i++) {{ try {{ apps[i].remove(); }} catch (e) {{}} }}
  }} catch (e) {{}}

  // Always clear stale draft after sends / on each Streamlit remount inject.
  emptyChatBoxIfDraft();

  if (doc.documentElement[FLAG]) return;
  doc.documentElement[FLAG] = true;

  function saveDraft(text) {{
    try {{
      if (text && String(text).trim()) {{
        w.localStorage.setItem(LS_KEY, String(text));
      }} else {{
        w.localStorage.removeItem(LS_KEY);
      }}
    }} catch (e) {{}}
  }}

  function readDraft() {{
    try {{ return w.localStorage.getItem(LS_KEY) || ''; }} catch (e) {{ return ''; }}
  }}

  function saveDraftFromDom() {{
    const ta = chatTextarea();
    if (!ta) return;
    saveDraft(ta.value || '');
  }}

  function writeDraftCookie(text) {{
    try {{
      const max = 3000;
      const clipped = (text || '').length > max ? (text || '').slice(0, max) : (text || '');
      const secure = w.location.protocol === 'https:' ? '; Secure' : '';
      doc.cookie = COOKIE + '=' + encodeURIComponent(clipped) +
        '; path=/; max-age=3600; SameSite=Lax' + secure;
    }} catch (e) {{}}
  }}

  function hasStackedAppShell() {{
    return doc.querySelectorAll('[data-testid="stAppViewContainer"]').length > 1
      || doc.querySelectorAll('[data-testid="stApp"]').length > 1;
  }}

  function pruneStackedAppShells() {{
    try {{
      const roots = doc.querySelectorAll('[data-testid="stAppViewContainer"]');
      if (roots.length > 1) {{
        for (let i = 0; i < roots.length - 1; i++) {{
          try {{ roots[i].remove(); }} catch (e) {{}}
        }}
      }}
      const apps = doc.querySelectorAll('[data-testid="stApp"]');
      if (apps.length > 1) {{
        for (let i = 0; i < apps.length - 1; i++) {{
          try {{ apps[i].remove(); }} catch (e) {{}}
        }}
      }}
    }} catch (e) {{}}
  }}

  // Prune only — never location.reload() here. Auto-reload during Submit /
  // Scrub / Thinking was stacking the pale double shell on mobile.
  function maybeAutoReloadGhost() {{
    pruneStackedAppShells();
  }}

  function snapChatPaneOnly() {{
    try {{
      const root = doc.querySelector('section.main') || doc;
      const msgs = root.querySelectorAll('[data-testid="stChatMessage"]');
      if (!msgs.length) return;
      const last = msgs[msgs.length - 1];
      let el = last.parentElement;
      while (el && el !== doc.body) {{
        if (el === doc.documentElement || el === doc.body) break;
        if (el.matches && (
          el.matches('section.main') ||
          el.matches('[data-testid="stAppViewContainer"]') ||
          el.matches('[data-testid="stAppScrollToBottomContainer"]')
        )) {{
          el = el.parentElement;
          continue;
        }}
        if (el.clientHeight > w.innerHeight * 0.85) {{
          el = el.parentElement;
          continue;
        }}
        if (el.scrollHeight > el.clientHeight + 8 && el.clientHeight > 40) {{
          el.scrollTop = el.scrollHeight;
          return;
        }}
        el = el.parentElement;
      }}
    }} catch (e) {{}}
  }}

  function isChatSendControl(el) {{
    if (!el || !el.closest) return false;
    if (el.closest('[data-testid="stChatInputSubmitButton"]')) return true;
    if (el.closest('[data-testid="stChatInput"] button')) return true;
    const wrap = el.closest('[data-testid="stChatInput"]');
    if (!wrap) return false;
    const btn = el.closest('button');
    return !!(btn && wrap.contains(btn));
  }}

  doc.addEventListener('input', function (e) {{
    const t = e.target;
    if (!t || !t.closest || !t.closest('[data-testid="stChatInput"]')) return;
    // Do not keep saving after we've marked a send.
    if (w.sessionStorage.getItem(SENT_KEY)) return;
    saveDraft(t.value || '');
  }}, true);

  doc.addEventListener('keydown', function (e) {{
    const t = e.target;
    if (!t || !t.closest || !t.closest('[data-testid="stChatInput"] textarea')) return;
    if (e.key !== 'Enter' || e.shiftKey) return;
    markSentAndClear();
  }}, true);
  doc.addEventListener('click', function (e) {{
    if (!isChatSendControl(e.target)) return;
    markSentAndClear();
  }}, true);
  doc.addEventListener('touchend', function (e) {{
    if (!isChatSendControl(e.target)) return;
    markSentAndClear();
  }}, true);

  // Save on lock only — do NOT restore into the box (that caused the mobile ghost prompt).
  doc.addEventListener('visibilitychange', function () {{
    if (doc.visibilityState === 'hidden') {{
      if (!w.sessionStorage.getItem(SENT_KEY)) {{
        saveDraftFromDom();
        writeDraftCookie(readDraft());
      }}
      return;
    }}
    setTimeout(emptyChatBoxIfDraft, 100);
    setTimeout(maybeAutoReloadGhost, 400);
    setTimeout(snapChatPaneOnly, 500);
  }});

  w.addEventListener('pageshow', function () {{
    setTimeout(emptyChatBoxIfDraft, 50);
    setTimeout(maybeAutoReloadGhost, 400);
    setTimeout(snapChatPaneOnly, 600);
  }});

  setInterval(function () {{
    if (doc.visibilityState !== 'visible') return;
    pruneStackedAppShells();
    maybeAutoReloadGhost();
  }}, 5000);

  setTimeout(emptyChatBoxIfDraft, 50);
  setTimeout(emptyChatBoxIfDraft, 400);
  setTimeout(snapChatPaneOnly, 900);
  setTimeout(pruneStackedAppShells, 300);
  setTimeout(maybeAutoReloadGhost, 800);
}})();
</script>
        """,
        height=0,
        width=0,
    )


install_mobile_resume_guard()


# --- 1.2 TOOLS ---

#google_search_tool = types.Tool(
#    google_search=types.GoogleSearch()
#)

# --- 2. FILE SYSTEM ARCHITECTURE (PHASE 2) ---
# Local default: ./Projects and ./System. On Render set GATEWAI_DATA_DIR=/var/data
# so a persistent disk keeps projects across deploys.
# GATEWAI_DATA_DIR / BASE_* / DOMAINS_DIR are initialized before the password gate.

# Initialize Global Profile
#global_profile_path = os.path.join(BASE_SYSTEM_DIR, "global_profile.txt")
#if not os.path.exists(global_profile_path):
#    with open(global_profile_path, "w") as f:
#        f.write("User: PhD in Operations Management. Location: Norton, MA. Interests: Strategic efficiency. Preference: Direct, high-logic #responses.")

# Initialize Domains
default_domains = {
    "Agile Strategy": "Insightful Thought Partner. Blend high-level operations logic with creative problem-solving.",
    "PhD Operations": "Rigid, logical, precise. Focus on constraints, bottlenecks, and ROI.",
    "Creative Story": "Whimsical Author. Focus on sensory details and character growth.",
    "Technical Writing": "Precise Editor. Stick to provided assets strictly."
}
for name, prompt in default_domains.items():
    p_path = os.path.join(DOMAINS_DIR, f"{name}.txt")
    if not os.path.exists(p_path):
        with open(p_path, "w") as f: f.write(prompt)

# for showing chat in Tab 2
if 'show_editor_chat' not in st.session_state:
    st.session_state.show_editor_chat = False

# Create the tool definition
google_search_tool = types.Tool(
    google_search=types.GoogleSearch()
)
# --- 2.1 CORE SCHEMA DEFINITION (THE 6 PILLARS) ---
# --- 2.1 CORE SCHEMA DEFINITION (THE 7 PILLARS) ---
CORE_SCHEMA = {
    # MI (Modular Intelligence)
    "summary.txt": "MI_Master_Record",
    "master_record_overflow.txt": "MI_Master_Record_Overflow", # <-- ADD THIS NEW LINE
#    "master_record_summary.txt": "MI_Master_Record_Summary", # <--- ADD THIS NEW LINE
    "notes.txt": "MI_Sticky_Notes",
    "status.txt": "MI_Project_Status",
    # MC (Metacognition)
    "frontier.txt": "MC_Frontier",
    "assumptions.txt": "MC_Assumptions",
    "blindspots.txt": "MC_Blindspots",
    "focus.txt": "MC_Focus", # <--- ADD THIS NEW LINE
    # SI (System Identity)
    "identity.txt": "SI_Identity",              # <-- SYSTEM BRAIN (Always Active)
    "profile.txt": "SI_User_Profile",          # <-- CONSCIOUS (User Edits this)
    "profile_shadow.txt": "SI_Observed_Profile", # <-- SUBCONSCIOUS (AI Edits this)
    "reference_frontier.txt": "SI_Reference_Frontier" # <--- ADD THIS NEW LINE
}

# --- NEW GLOBAL DEFINITION ---
GLOBAL_CORE = list(TRANSFER_GLOBAL_CORE)
# SI_Identity seed runs before the password gate (see apply_gatewai_seed_si_identity above).
# --- END NEW GLOBAL DEFINITION ---

def normalize_folder_name(folder):
    """Optional single-level folder label for user slots. Empty string = Ungrouped/root."""
    f = str(folder or "").strip().replace("\\", "/").strip("/")
    if not f:
        return ""
    # First segment only — keep folder names simple for v1
    f = f.split("/")[0].strip()
    f = re.sub(r"[^\w\s\-]", "", f).strip()
    return f

def get_existing_user_folders(metadata):
    folders = set()
    for meta in (metadata or {}).values():
        if meta.get("slot_type") == "user":
            folder = normalize_folder_name(meta.get("folder", ""))
            if folder:
                folders.add(folder)
    return sorted(folders, key=str.lower)

CHAT_VISIBLE_STEP = 30

def render_project_chat(height, earlier_key):
    """Draw the latest stretch of chat. The full history stays in memory."""
    messages = st.session_state.get("messages") or []
    visible = int(st.session_state.get("chat_visible_count", CHAT_VISIBLE_STEP))
    visible = max(CHAT_VISIBLE_STEP, visible)
    hidden = max(0, len(messages) - visible)
    shown = messages if hidden == 0 else messages[-visible:]
    box = chat_history_container(height)
    with box:
        if hidden:
            st.caption(f"Showing the latest {len(shown)} of {len(messages)} messages.")
            if st.button(
                f"Show {min(CHAT_VISIBLE_STEP, hidden)} earlier",
                key=earlier_key,
            ):
                st.session_state.chat_visible_count = visible + CHAT_VISIBLE_STEP
                st.rerun()
        for message in shown:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
    # Prefer Streamlit container autoscroll only — extra snap iframes after reply
    # were contributing to mobile ghost UI. Flag still used as a hint for CSS/clients.
    st.session_state.pop("chat_autoscroll", False)
    return box

def is_mobile_client():
    """Best-effort User-Agent check (Streamlit mobile browser / PWA)."""
    try:
        headers = getattr(st.context, "headers", None)
        if headers is None:
            return False
        ua = ""
        if hasattr(headers, "get"):
            ua = str(headers.get("User-Agent", "") or headers.get("user-agent", "") or "")
        return any(
            token in ua.lower()
            for token in ("mobile", "android", "iphone", "ipod", "ipad", "webos", "iemobile")
        )
    except Exception:
        return False


def chat_pane_height(desktop=720, mobile=520):
    """Taller mobile chat pane so a model reply is reachable without fighting page scroll."""
    if st.session_state.get("pending_updates"):
        mobile = min(mobile, 420)
        desktop = min(desktop, 560)
    return mobile if is_mobile_client() else desktop


def chat_history_container(height):
    """Fixed-height chat pane pinned to the latest message."""
    kwargs = {"height": height}
    # Right-sidebar expand/collapse remounts chat; don't yank to the latest message.
    preserve = bool(st.session_state.pop("_preserve_gatewai_chat_scroll", False))
    try:
        import inspect
        if "autoscroll" in inspect.signature(st.container).parameters:
            if preserve:
                st.session_state._restore_gatewai_chat_scroll = True
            else:
                kwargs["autoscroll"] = True
    except Exception:
        if preserve:
            st.session_state._restore_gatewai_chat_scroll = True
    return st.container(**kwargs)


def request_chat_autoscroll():
    """Ask the next chat render to JS-snap the chat pane to the latest message."""
    st.session_state.chat_autoscroll = True


def snap_chat_panes_to_bottom(force=False):
    """Scroll only the fixed-height chat pane (not the page / Editor)."""
    if not force and not st.session_state.get("chat_autoscroll"):
        return
    components.html(
        """
<script>
(function () {
  const doc = window.parent.document;
  const win = window.parent;
  function snap() {
    const root = doc.querySelector('section.main') || doc;
    const msgs = root.querySelectorAll('[data-testid="stChatMessage"]');
    if (!msgs.length) return;
    const last = msgs[msgs.length - 1];
    let el = last.parentElement;
    while (el && el !== doc.body) {
      if (el === doc.documentElement || el === doc.body) break;
      if (el.matches && (
        el.matches('section.main') ||
        el.matches('[data-testid="stAppViewContainer"]') ||
        el.matches('[data-testid="stAppScrollToBottomContainer"]') ||
        el.matches('[data-testid="stMain"]')
      )) {
        el = el.parentElement;
        continue;
      }
      if (el.clientHeight > win.innerHeight * 0.85) {
        el = el.parentElement;
        continue;
      }
      if (el.scrollHeight > el.clientHeight + 8 && el.clientHeight > 40) {
        try { el.style.scrollBehavior = 'auto'; } catch (e) {}
        el.scrollTop = el.scrollHeight;
        return;
      }
      el = el.parentElement;
    }
  }
  snap();
  requestAnimationFrame(snap);
  [40, 120, 300, 700, 1400].forEach(function (ms) { setTimeout(snap, ms); });
})();
</script>
        """,
        height=0,
        width=0,
    )


def pin_editor_tab_to_top():
    """Force the main pane to the top whenever the Editor view mounts.

    Main view is a radio (not st.tabs). ACE focus and Streamlit's scroll-to-bottom
    container often yank the page down on switch — keep pinning to top for a bit.
    """
    components.html(
        """
<script>
(function () {
  const doc = window.parent.document;
  const win = window.parent;
  const FLAG = '__gatewaiEditorPinTopHookV3';
  function toTop() {
    const targets = [
      doc.scrollingElement || doc.documentElement,
      doc.body,
      doc.querySelector('[data-testid="stAppScrollToBottomContainer"]'),
      doc.querySelector('section.main'),
      doc.querySelector('[data-testid="stMain"]'),
      doc.querySelector('[data-testid="stAppViewContainer"]'),
      doc.querySelector('.main .block-container')
    ];
    targets.forEach(function (el) {
      if (!el) return;
      try {
        el.scrollTop = 0;
        el.scrollLeft = 0;
      } catch (e) {}
    });
    try { win.scrollTo(0, 0); } catch (e) {}
  }
  function scheduleTop() {
    toTop();
    requestAnimationFrame(toTop);
    [0, 30, 80, 160, 320, 600, 1000, 1800, 2800].forEach(function (ms) {
      setTimeout(toTop, ms);
    });
  }
  // This inject only mounts while Editor is showing — always pin on paint.
  scheduleTop();
  if (!doc.documentElement[FLAG]) {
    doc.documentElement[FLAG] = true;
    doc.addEventListener('click', function (e) {
      const t = e.target && e.target.closest && e.target.closest(
        'label, button, [role="radio"], [data-baseweb="radio"]'
      );
      if (!t) return;
      const label = (t.textContent || '').toLowerCase();
      if (label.indexOf('editor') === -1) return;
      scheduleTop();
    }, true);
    // ACE grabbing focus used to scroll the page to the editor mid-page.
    doc.addEventListener('focusin', function (e) {
      const t = e.target;
      if (!t) return;
      if (!(t.closest && (t.closest('.ace_editor') || t.closest('.ace_text-input')))) return;
      setTimeout(toTop, 0);
      setTimeout(toTop, 80);
    }, true);
  }
})();
</script>
        """,
        height=0,
        width=0,
    )

def list_project_slot_names():
    """Core slots first, then user slots (non-core) sorted by name."""
    all_files = list(CORE_SCHEMA.values())
    if "loaded_slots_metadata" in st.session_state:
        core_names = set(CORE_SCHEMA.values())
        all_files.extend(
            sorted([
                k for k, v in st.session_state.loaded_slots_metadata.items()
                if v.get("slot_type") == "user" and k not in core_names
            ])
        )
    return all_files


def render_memory_slot_sidebar(key_prefix, height=480, show_meta=True):
    """Collapsible memory-slot browser: names + Active-in-chat toggles.

    Shared by Editor and GateWai so users can include/exclude slots without
    leaving the chat view. key_prefix keeps widget keys unique per surface.
    """
    all_files = list_project_slot_names()
    if "selected_file" not in st.session_state:
        st.session_state.selected_file = CORE_SCHEMA.get("summary.txt", "MI_Master_Record")
    if all_files and st.session_state.selected_file not in all_files:
        st.session_state.selected_file = all_files[0]

    hdr_file, hdr_active = st.columns([3, 1])
    with hdr_file:
        st.caption("File")
    with hdr_active:
        st.caption("Active")

    core_files = set(CORE_SCHEMA.values())
    # height=None → let the parent sidebar scroll (app-level right rail).
    if height is None:
        list_box = st.container()
    elif len(all_files) > 8:
        list_box = st.container(height=height)
    else:
        list_box = st.container()

    def render_slot_row(fname):
        name_col, active_col = st.columns([3, 1])
        is_selected = (fname == st.session_state.selected_file)
        with name_col:
            if st.button(
                fname,
                key=f"{key_prefix}_select_{fname}",
                type="primary" if is_selected else "secondary",
                use_container_width=True,
            ):
                st.session_state.selected_file = fname
                st.session_state["expanded_slot"] = fname
                # Live Preview off → open the slot in Editor. Preview on → stay on GateWai.
                if not st.session_state.get("show_preview"):
                    st.session_state.main_view_selector = "📝 Editor"
                st.rerun()
        with active_col:
            if fname in core_files:
                st.button(
                    "🟢",
                    key=f"{key_prefix}_core_active_{fname}",
                    disabled=True,
                    use_container_width=True,
                    help="System core — always active in chat",
                )
            else:
                row_meta = st.session_state.get("loaded_slots_metadata", {}).get(fname, {})
                is_active = row_meta.get("active_in_context", True)
                if st.button(
                    "🟢" if is_active else "⚪",
                    key=f"{key_prefix}_active_{fname}",
                    type="secondary",
                    use_container_width=True,
                    help=(
                        "Active in Chat — click to hide from the AI"
                        if is_active
                        else "Hidden from Chat — click to include in the AI context"
                    ),
                ):
                    meta_map = st.session_state.setdefault("loaded_slots_metadata", {})
                    if fname not in meta_map:
                        meta_map[fname] = {}
                    meta_map[fname]["active_in_context"] = not is_active
                    # Persist Active toggles so they survive refresh.
                    try:
                        save_state_dict = {k: st.session_state.get(k, "") for k in CORE_SCHEMA.values()}
                        save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                        save_state_dict["messages"] = st.session_state.get("messages") or []
                        for user_key in st.session_state.loaded_slots_metadata.keys():
                            if user_key not in CORE_SCHEMA.values():
                                save_state_dict[user_key] = st.session_state.get(user_key, "")
                        save_project_state(save_state_dict, st.session_state.active_project, silent=True)
                    except Exception as e:
                        print(f"ERROR: persist active_in_context failed: {e}")
                    st.rerun()

    def count_active_in_chat(fnames):
        meta_map = st.session_state.get("loaded_slots_metadata", {})
        active_n = 0
        for f in fnames:
            if f in core_files:
                active_n += 1
            elif meta_map.get(f, {}).get("active_in_context", True):
                active_n += 1
        return active_n

    def folder_expander_label(icon, title, fnames):
        total = len(fnames)
        active_n = count_active_in_chat(fnames)
        return f"{icon} {title} ({active_n}/{total} active)"

    with list_box:
        core_list = list(CORE_SCHEMA.values())
        with st.expander(
            folder_expander_label("🔒", "System Core", core_list),
            expanded=False,
            key=f"{key_prefix}_folder__system_core",
        ):
            for fname in core_list:
                render_slot_row(fname)

        user_groups = {}
        for fname in all_files:
            if fname in core_files:
                continue
            folder = normalize_folder_name(
                st.session_state.get("loaded_slots_metadata", {}).get(fname, {}).get("folder", "")
            )
            user_groups.setdefault(folder, []).append(fname)

        named_folders = sorted([f for f in user_groups if f], key=str.lower)
        for folder in named_folders:
            files_in_folder = user_groups[folder]
            with st.expander(
                folder_expander_label("📁", folder, files_in_folder),
                expanded=False,
                key=f"{key_prefix}_folder__{folder}",
            ):
                for fname in files_in_folder:
                    render_slot_row(fname)

        ungrouped = user_groups.get("", [])
        if ungrouped:
            with st.expander(
                folder_expander_label("📄", "Ungrouped", ungrouped),
                expanded=False,
                key=f"{key_prefix}_folder__ungrouped",
            ):
                for fname in ungrouped:
                    render_slot_row(fname)

    if not show_meta:
        return

    selected_file = st.session_state.selected_file
    st.markdown("---")
    meta = st.session_state.get("loaded_slots_metadata", {}).get(selected_file, {})
    if meta.get("is_distributed", False):
        st.success("🌐 **DISTRIBUTED TRUTH**\nThis is a summary record.")
    elif meta.get("part_of_dtn", False):
        st.info("🔗 **Part of DTN**\nLinked to a summary record.")

    if selected_file in CORE_SCHEMA.values():
        st.info("🔒 **SYSTEM CORE**\nAlways Active")
    else:
        scope = meta.get("scope", "project").upper()
        desc = meta.get("description", "User Draft")
        is_active = meta.get("active_in_context", True)
        folder = normalize_folder_name(meta.get("folder", ""))
        folder_line = f"\nFolder: {folder}" if folder else "\nFolder: Ungrouped"
        if is_active:
            st.success(f"🔓 **USER ({scope})**\n{desc}{folder_line}")
        else:
            st.caption(f"🙈 **HIDDEN ({scope})**\nFile is safe, but AI ignores it.{folder_line}")
        if meta.get("ai_managed", False):
            st.caption("AI-managed draft")


def render_staging_area():
    """Review pending Shadow/Harvest proposals inside the GateWai tab (not above it).

    Sitting above the tabs made mobile look like a second dialog and, with the old
    85vh column CSS + side-by-side diffs, unglued the whole page after proposals.
    """
    if "pending_updates" not in st.session_state or not st.session_state.pending_updates:
        return

    st.divider()
    with st.container(border=True):
        st.warning("🔔 **APPROVAL REQUIRED:** The AI has proposed changes to your Core Memory.")

        slots_to_review = list(st.session_state.pending_updates.items())
        if not slots_to_review:
            st.success("All pending changes processed.")
            if "pending_updates" in st.session_state:
                del st.session_state.pending_updates
            st.rerun()

        narrow = is_mobile_client()
        for slot_name, update_data in slots_to_review:
            if isinstance(update_data, dict) and "content" in update_data:
                new_content_str = update_data["content"]
                is_suspect_staged = update_data.get("suspect", False)
            else:
                new_content_str = update_data
                is_suspect_staged = False

            if str(slot_name).startswith("__metadata_"):
                continue

            warning_icon = "🚩 " if is_suspect_staged else ""
            if st.session_state.get("open_staged_proposal") != slot_name:
                if st.button(
                    f"{warning_icon}{slot_name}",
                    key=f"open_proposal_{slot_name}",
                    use_container_width=True,
                ):
                    st.session_state.open_staged_proposal = slot_name
                    st.rerun()
                continue

            with st.container(border=True):
                st.markdown(f"**{warning_icon}{slot_name}**")
                if st.button("Close", key=f"close_proposal_{slot_name}"):
                    st.session_state.open_staged_proposal = None
                    st.rerun()

                current_val = st.session_state.get(slot_name, "")
                if not isinstance(current_val, str):
                    current_val = str(current_val)

                # One scrolling diff pane (same on mobile + desktop). Side-by-side
                # CURRENT/PROPOSED boxes scrolled independently and hid what changed.
                st.caption("📝 **What changed** (unified diff)")
                diff_height = 220 if narrow else 320
                with st.container(height=diff_height):
                    st.code(
                        proposal_text_diff(current_val, new_content_str),
                        language="diff",
                        wrap_lines=True,
                    )
                with st.expander("Full CURRENT text", expanded=False):
                    st.code(
                        current_val if current_val else "(empty)",
                        language=get_code_language(str(current_val)),
                        wrap_lines=True,
                    )
                with st.expander("Full PROPOSED text", expanded=False):
                    st.code(
                        new_content_str if new_content_str else "(empty)",
                        language=get_code_language(new_content_str),
                        wrap_lines=True,
                    )

                b_col1, b_col2, b_col3 = st.columns([1, 1, 2] if narrow else [1, 1, 3])
                if b_col1.button("✅ APPROVE", key=f"accept_{slot_name}", type="primary", use_container_width=True):
                    commit_slot_text(slot_name, new_content_str, replaced_text=st.session_state.get(slot_name, ""))
                    del st.session_state.pending_updates[slot_name]
                    metadata_key = f"__metadata_{slot_name}"
                    if metadata_key in st.session_state.pending_updates:
                        meta_updates = st.session_state.pending_updates[metadata_key]
                        if slot_name in st.session_state.loaded_slots_metadata and isinstance(meta_updates, dict):
                            st.session_state.loaded_slots_metadata[slot_name].update(meta_updates)
                        del st.session_state.pending_updates[metadata_key]
                    save_state_dict = {}
                    for core_key in CORE_SCHEMA.values():
                        save_state_dict[core_key] = st.session_state.get(core_key, "")
                    if "loaded_slots_metadata" in st.session_state:
                        save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                        for user_key in st.session_state.loaded_slots_metadata.keys():
                            if user_key not in CORE_SCHEMA.values():
                                save_state_dict[user_key] = st.session_state.get(user_key, "")
                    save_state_dict["messages"] = st.session_state.messages
                    config_keys = [
                        "chat_engine_set", "ingest_engine_set", "harvest_engine_set",
                        "shadow_engine_set", "transcribe_engine_set", "selected_lens_name",
                        "temp_val", "top_p_val", "memory_depth_val", "compaction_threshold",
                        "last_harvest_message_index", "review_mode_active", "auto_shadow_active",
                    ]
                    for key in config_keys:
                        save_state_dict[key] = st.session_state.get(key)
                    save_project_state(save_state_dict, st.session_state.active_project, silent=True)
                    if st.session_state.get("open_staged_proposal") == slot_name:
                        st.session_state.open_staged_proposal = None
                    st.toast(f"✅ Committed '{slot_name}' to database.")
                    st.rerun()

                if b_col2.button("❌ REJECT", key=f"reject_{slot_name}", use_container_width=True):
                    del st.session_state.pending_updates[slot_name]
                    metadata_key = f"__metadata_{slot_name}"
                    if metadata_key in st.session_state.pending_updates:
                        del st.session_state.pending_updates[metadata_key]
                    if st.session_state.get("open_staged_proposal") == slot_name:
                        st.session_state.open_staged_proposal = None
                    st.toast(f"❌ Rejected update to {slot_name}")
                    st.rerun()

        primary_count = sum(
            1 for k in st.session_state.get("pending_updates", {})
            if not str(k).startswith("__metadata_")
        )
        st.caption(f"Pending proposals: {primary_count}")
        st.markdown("---")
        col_reject_all, col_spacer, col_accept_all = st.columns([1, 1, 1] if narrow else [1, 2, 1])
        has_primary_pending_updates = primary_count > 0
        buttons_disabled = not has_primary_pending_updates

        with col_reject_all:
            if st.button(
                "❌ Reject All",
                use_container_width=True,
                key="reject_all_proposals_btn",
                disabled=buttons_disabled,
                help="Reject all pending proposals in the Staging Area.",
            ):
                if "pending_updates" in st.session_state:
                    primary_slot_names_to_delete = [
                        key for key in list(st.session_state.pending_updates.keys())
                        if not key.startswith("__metadata_")
                    ]
                    for slot_name in primary_slot_names_to_delete:
                        st.session_state.pending_updates.pop(slot_name, None)
                        st.session_state.pending_updates.pop(f"__metadata_{slot_name}", None)
                st.session_state.open_staged_proposal = None
                persist_pending_updates_now()
                st.toast("All pending proposals rejected.")
                st.rerun()

        with col_accept_all:
            if st.button(
                "✅ Accept All",
                use_container_width=True,
                key="accept_all_proposals_btn",
                type="primary",
                disabled=buttons_disabled,
                help="Approve and commit all pending proposals to the database.",
            ):
                primary_slot_names_to_accept = [
                    key for key in list(st.session_state.pending_updates.keys())
                    if not key.startswith("__metadata_")
                ]
                for slot_name in primary_slot_names_to_accept:
                    update_data = st.session_state.pending_updates.get(slot_name)
                    if update_data is None:
                        continue
                    if isinstance(update_data, dict) and "content" in update_data:
                        new_content_str = update_data["content"]
                    else:
                        new_content_str = update_data
                    commit_slot_text(slot_name, new_content_str, replaced_text=st.session_state.get(slot_name, ""))
                    metadata_key = f"__metadata_{slot_name}"
                    if metadata_key in st.session_state.pending_updates:
                        meta_updates = st.session_state.pending_updates[metadata_key]
                        if slot_name in st.session_state.loaded_slots_metadata and isinstance(meta_updates, dict):
                            st.session_state.loaded_slots_metadata[slot_name].update(meta_updates)
                        del st.session_state.pending_updates[metadata_key]
                    del st.session_state.pending_updates[slot_name]

                save_state_dict = {}
                for core_key in CORE_SCHEMA.values():
                    save_state_dict[core_key] = st.session_state.get(core_key, "")
                if "loaded_slots_metadata" in st.session_state:
                    save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                    for user_key in st.session_state.loaded_slots_metadata.keys():
                        if user_key not in CORE_SCHEMA.values():
                            save_state_dict[user_key] = st.session_state.get(user_key, "")
                save_state_dict["messages"] = st.session_state.messages
                config_keys = [
                    "chat_engine_set", "ingest_engine_set", "harvest_engine_set",
                    "shadow_engine_set", "transcribe_engine_set", "selected_lens_name",
                    "temp_val", "top_p_val", "memory_depth_val", "compaction_threshold",
                    "last_harvest_message_index", "review_mode_active", "auto_shadow_active",
                ]
                for key in config_keys:
                    save_state_dict[key] = st.session_state.get(key)
                save_project_state(save_state_dict, st.session_state.active_project, silent=True)
                if "pending_updates" in st.session_state and not st.session_state.pending_updates:
                    del st.session_state.pending_updates
                st.session_state.open_staged_proposal = None
                st.toast("✅ All pending proposals accepted and committed to database.")
                st.rerun()


def persist_slot_folder(slot_name, folder):
    """Write a folder label onto a user slot in RAM + SQLite. Returns True if the value changed."""
    folder = normalize_folder_name(folder)
    if "loaded_slots_metadata" not in st.session_state:
        return False
    meta = st.session_state.loaded_slots_metadata.get(slot_name)
    if not meta or meta.get("slot_type") == "core":
        return False
    if normalize_folder_name(meta.get("folder", "")) == folder:
        return False
    meta["folder"] = folder
    st.session_state.loaded_slots_metadata[slot_name] = meta
    proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
    sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
    content = st.session_state.get(slot_name, meta.get("content", ""))
    db.save_slot(
        proj_db, sys_db, slot_name, content,
        meta.get("slot_type", "user"),
        meta.get("description", ""),
        meta.get("scope", "project"),
        meta.get("owner_id", "system"),
        meta.get("is_distributed", False),
        meta.get("part_of_dtn", False),
        meta.get("ai_managed", False),
        folder=folder,
    )
    return True

def render_folder_picker(key_prefix, current="", metadata=None, label="Folder"):
    """Select an existing folder, Ungrouped, or type a new name. Returns ('set'|'new', folder)."""
    existing = get_existing_user_folders(metadata if metadata is not None else st.session_state.get("loaded_slots_metadata", {}))
    current = normalize_folder_name(current)
    ungrouped = "(Ungrouped)"
    new_opt = "+ New folder..."
    options = [ungrouped]
    for f in existing:
        if f not in options:
            options.append(f)
    if current and current not in options:
        options.insert(1, current)
    options.append(new_opt)
    idx = options.index(current) if current in options else 0
    choice = st.selectbox(label, options, index=idx, key=f"{key_prefix}_folder_choice", help="Optional. Empty/Ungrouped keeps the file at the root of the list.")
    if choice == new_opt:
        typed = st.text_input("New folder name", key=f"{key_prefix}_folder_new", placeholder="e.g. Research")
        return "new", normalize_folder_name(typed)
    if choice == ungrouped:
        return "set", ""
    return "set", choice

# --- LLM PROVIDER ROUTING (Gemini + DeepSeek) ---
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

def is_deepseek_engine(engine_name):
    return str(engine_name or "").lower().startswith("deepseek")

def _parts_to_text(parts):
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        texts = []
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                texts.append(part.get("text", ""))
            else:
                texts.append(getattr(part, "text", str(part)))
        return "\n".join([t for t in texts if t])
    return str(parts)

def _contents_to_openai_messages(contents):
    if isinstance(contents, str):
        return [{"role": "user", "content": contents}]
    if isinstance(contents, dict):
        contents = [contents]
    if not isinstance(contents, list):
        return [{"role": "user", "content": str(contents)}]

    messages = []
    for item in contents:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue
        role = item.get("role", "user")
        if role == "model":
            role = "assistant"
        text = _parts_to_text(item.get("parts", item.get("content", "")))
        messages.append({"role": role, "content": text})
    return messages if messages else [{"role": "user", "content": str(contents)}]

def _extract_gen_config(config):
    if config is None:
        return None, None, None, None
    if isinstance(config, dict):
        return (
            config.get("temperature"),
            config.get("top_p"),
            config.get("system_instruction"),
            config.get("max_output_tokens") or config.get("max_tokens"),
        )
    return (
        getattr(config, "temperature", None),
        getattr(config, "top_p", None),
        getattr(config, "system_instruction", None),
        getattr(config, "max_output_tokens", None) or getattr(config, "max_tokens", None),
    )

class _SimpleLLMResponse:
    def __init__(self, text):
        self.text = text if text is not None else ""

class DeepSeekModels:
    def __init__(self, api_key):
        self.api_key = api_key

    def generate_content(self, model, contents, config=None):
        if not self.api_key:
            raise ValueError("DeepSeek API key is missing. Add it in the sidebar.")

        messages = _contents_to_openai_messages(contents)
        temperature, top_p, system_instruction, max_output_tokens = _extract_gen_config(config)
        if system_instruction:
            messages = [{"role": "system", "content": str(system_instruction)}] + messages

        payload = {"model": model, "messages": messages, "stream": False}
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens

        response = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            json=payload,
            timeout=300,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"DeepSeek API error {response.status_code}: {response.text}")

        data = response.json()
        try:
            message = data["choices"][0]["message"]
            text = (message.get("content") or "").strip()
            if not text:
                text = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"Unexpected DeepSeek API response: {data}")
        return _SimpleLLMResponse(text)

class DeepSeekCompatClient:
    def __init__(self, api_key):
        self.models = DeepSeekModels(api_key)

def get_llm_client(engine_name, api_key=None):
    """Return a client with .models.generate_content(...) for Gemini or DeepSeek."""
    if is_deepseek_engine(engine_name):
        return DeepSeekCompatClient(st.session_state.get("deepseek_api_key", "") or "")
    key = api_key if api_key is not None else st.session_state.get("gemini_api_key", "")
    return genai.Client(api_key=key)
# --- END LLM PROVIDER ROUTING ---


if 'asset_selector_state_needs_update' not in st.session_state:
    st.session_state.asset_selector_state_needs_update = False
if 'auto_deselect_list' not in st.session_state:
    st.session_state.auto_deselect_list = []



# --- 3. HELPER FUNCTIONS (WITH VIEW STATE PERSISTENCE) ---
def get_project_paths(name):
    proj_dir = os.path.join(BASE_PROJECT_DIR, name)
    asset_dir = os.path.join(proj_dir, "Assets")
    if not os.path.exists(asset_dir): os.makedirs(asset_dir)

    paths = {k: os.path.join(proj_dir, k) for k in CORE_SCHEMA.keys()}
    paths["history"] = os.path.join(proj_dir, "history.json")
    paths["view_state"] = os.path.join(proj_dir, "view_state.json") # <--- NEW
    paths["config"] = os.path.join(proj_dir, "config.json") # <--- NEW
    paths["assets"] = asset_dir
    return paths

def load_project_state(name):
    proj_dir = os.path.join(BASE_PROJECT_DIR, name)
    proj_db = db.get_project_db_path(BASE_PROJECT_DIR, name)
    sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)

    if not os.path.exists(proj_dir): os.makedirs(proj_dir)
    db.init_db(proj_db)
    db.init_db(sys_db)

    # --- RUN DATABASE MIGRATION ---
    db.migrate_db(proj_db)  # Ensure the database schema is up-to-date
    db.migrate_db(sys_db)  # Ensure the database schema is up-to-date
    # --- END DATABASE MIGRATION ---

    db.migrate_flat_files(proj_dir, proj_db)

    state = {}

    # 1. Load Chat History
    hist_path = os.path.join(proj_dir, "history.json")
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                # Read content and check if it's empty before attempting JSON load
                file_content = f.read().strip()
                if file_content:
                    state["messages"] = json.loads(file_content)
                else:
                    state["messages"] = [] # File exists but is empty, treat as empty history
        except json.JSONDecodeError:
            st.error(f"Corruption detected in chat history for '{name}'. Resetting history.")
            state["messages"] = [] # Corrupt file, initialize as empty list
            # Optional: You could add a line here to back up the corrupted file
            # shutil.copy(hist_path, hist_path + ".corrupted_bak")
            # Then, overwrite the corrupted file with an empty list to fix it
            with open(hist_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=4)
        except Exception as e:
            st.error(f"Unexpected error loading chat history for '{name}': {e}. Resetting history.")
            state["messages"] = []
            with open(hist_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=4)
    else:
        state["messages"] = [] # File does not exist, initialize as empty list

    # 2. Load View State (Hidden/Active Toggles)
    view_state = {}
    view_path = os.path.join(proj_dir, "view_state.json")
    if os.path.exists(view_path):
        try:
            with open(view_path, "r", encoding="utf-8") as f:
                view_state = json.load(f)
        except json.JSONDecodeError:
            st.error(f"Corruption detected in view_state.json for '{name}'. Resetting view state.")
            view_state = {}
        except Exception as e:
            st.error(f"Unexpected error loading view_state.json for '{name}': {e}. Resetting view state.")
            view_state = {}

    # [NEW] 3. Load Config State (Engines & Params)
    config_path = os.path.join(proj_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_content = f.read().strip()
                if file_content:
                    config_data = json.loads(file_content)
                    state["config"] = config_data # Store to load into session later
                    # --- DEBUG: Print config_data after loading ---
#                    print(f"DEBUG LOAD_PROJECT_STATE: Loaded config_data from file: {config_data}")
                else:
                    state["config"] = {} # File exists but is empty
        except Exception as e:
            print(f"ERROR loading config.json: {e}")
            state["config"] = {} # Corrupt file or other error

    # 4. Load ALL Slots
    active_lens = st.session_state.get('selected_lens_name', None)

    # Load Project Slots
    project_slots = db.load_all_slots(proj_db, None, active_lens)

    # Load System Slots (Global)
    system_slots = db.load_all_slots(None, sys_db, active_lens)

    # Merge: Project overrides System (standard behavior),
    # BUT for our specific global profiles, we want System to define the truth.
    full_slots = project_slots.copy()

    # Enforce Global Authority for specific slots
    for k, v in system_slots.items():
        if k in GLOBAL_CORE:
            full_slots[k] = v # Force Global version
        elif k not in full_slots:
            full_slots[k] = v # Default inheritance

    # Apply View State
    core_names = set(CORE_SCHEMA.values())
    for k, meta in full_slots.items():
        if k in view_state:
            meta['active_in_context'] = view_state[k]
        # Core / global SI slots are always active in chat (not toggleable).
        if k in core_names or k in GLOBAL_CORE:
            meta['slot_type'] = 'core'
            meta['active_in_context'] = True
            if k in GLOBAL_CORE:
                meta['scope'] = 'global'
        state[k] = meta['content']

    state["loaded_slots_metadata"] = full_slots

    # Enforce Defaults for Core
    for core_key in CORE_SCHEMA.values():
        if core_key not in state: state[core_key] = ""
        if core_key not in full_slots:
            full_slots[core_key] = {
                "slot_name": core_key,
                "content": state.get(core_key, ""),
                "slot_type": "core",
                "description": "System Core File",
                "scope": "global" if core_key in GLOBAL_CORE else "project",
                "owner_id": "system",
                "is_distributed": False,
                "part_of_dtn": False,
                "ai_managed": False,
                "active_in_context": True,
                "folder": "",
            }
        else:
            # Normalize folder for existing slots (missing => root/ungrouped)
            full_slots[core_key]["folder"] = ""

    # Normalize folder labels on all loaded slots
    for k, meta in full_slots.items():
        if meta.get("slot_type") == "user":
            meta["folder"] = normalize_folder_name(meta.get("folder", ""))
        else:
            meta["folder"] = ""

    return state

# Locate the `save_project_state` function in your Updated_app_code.py.
# Replace the entire "4. Save All Slots (DTN-Aware)" block with the following:

def pending_updates_path(name):
    return os.path.join(BASE_PROJECT_DIR, name, "pending_updates.json")

def read_pending_updates(name):
    path = pending_updates_path(name)
    if not name or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def write_pending_updates(name, updates):
    """Keep staged proposals on disk. They used to live only in the browser session."""
    if not name:
        return
    path = pending_updates_path(name)
    updates = updates if isinstance(updates, dict) else {}
    try:
        if not updates:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(updates, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"ERROR: write_pending_updates failed for {name}: {e}")


def persist_pending_updates_now():
    """Flush Staging Area to disk immediately (before rerun / mobile disconnect)."""
    project = st.session_state.get("active_project")
    pending = st.session_state.get("pending_updates") or {}
    if not isinstance(pending, dict):
        pending = {}
    write_pending_updates(project, pending)
    try:
        st.session_state._pending_updates_snapshot = json.dumps(
            pending, sort_keys=True, default=str
        )
    except Exception:
        st.session_state._pending_updates_snapshot = ""

ENGINE_ROOM_FIELDS = (
    ("chat_engine_set", "chat_engine"),
    ("ingest_engine_set", "ingest_engine"),
    ("harvest_engine_set", "harvest_engine"),
    ("shadow_engine_set", "shadow_engine"),
    ("transcribe_engine_set", "transcribe_engine"),
    ("selected_lens_name", "domain_lens"),
    ("temp_val", "temperature"),
    ("top_p_val", "top_p"),
    ("memory_depth_val", "memory_depth"),
    ("compaction_threshold", "compaction_threshold"),
    ("review_mode_active", "review_mode_active"),
    ("auto_shadow_active", "auto_shadow_active"),
    ("last_harvest_message_index", "last_harvest_message_index"),
)


_RSB_WIDTH_COOKIE = "gatewai_rsb_width"
_RSB_WIDTH_LS = "gatewai_rsb_width"
_RSB_OPEN_COOKIE = "gatewai_rsb_open"
_RSB_OPEN_LS = "gatewai_rsb_open"
_RSB_DEFAULT_WIDTH_PX = 320
_RSB_MIN_WIDTH_PX = 220
_RSB_MAX_WIDTH_PX = 560


def _clamp_rsb_width_px(raw):
    """Normalize width: legacy percent (≤48) → px; otherwise clamp pixel width."""
    try:
        width = int(float(raw))
    except (TypeError, ValueError):
        return _RSB_DEFAULT_WIDTH_PX
    # Older builds stored 18–48 as a column percent.
    if 1 <= width <= 48:
        width = int(1100 * (width / 100.0))
    return max(_RSB_MIN_WIDTH_PX, min(_RSB_MAX_WIDTH_PX, width))


def read_rsb_width_cookie():
    """Width last set by dragging the right-sidebar border (available next request)."""
    try:
        cookies = getattr(st.context, "cookies", None)
        if cookies is None or not hasattr(cookies, "get"):
            return None
        raw = cookies.get(_RSB_WIDTH_COOKIE)
        if raw is None or str(raw).strip() == "":
            return None
        return _clamp_rsb_width_px(raw)
    except Exception:
        return None


def read_rsb_open_cookie():
    """Open/closed last set by the client-side chevron (no Streamlit remount)."""
    try:
        cookies = getattr(st.context, "cookies", None)
        if cookies is None or not hasattr(cookies, "get"):
            return None
        raw = cookies.get(_RSB_OPEN_COOKIE)
        if raw is None:
            return None
        raw = str(raw).strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        return None
    except Exception:
        return None


def persist_ui_layout_prefs(project_name=None):
    """Write Memory-slots open/width into config.json (survive refresh)."""
    project_name = project_name or st.session_state.get("active_project") or ""
    if not project_name:
        return
    path = os.path.join(BASE_PROJECT_DIR, project_name, "config.json")
    existing, unreadable = read_project_config(project_name)
    if unreadable:
        return
    open_slots = bool(
        st.session_state.get(
            "show_memory_slots_pref",
            st.session_state.get("show_memory_slots", False),
        )
    )
    width = _clamp_rsb_width_px(
        st.session_state.get(
            "memory_slots_width_pref",
            st.session_state.get("memory_slots_width", _RSB_DEFAULT_WIDTH_PX),
        )
    )
    existing["show_memory_slots"] = open_slots
    existing["memory_slots_width"] = width
    existing["gatewai_show_slots"] = open_slots
    existing["show_file_list"] = open_slots
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"ERROR: persist_ui_layout_prefs failed: {e}")


def set_memory_slots_open(open_slots):
    """Expand/collapse the right Memory-slots sidebar (mirrors left sidebar chevrons)."""
    open_slots = bool(open_slots)
    st.session_state.show_memory_slots = open_slots
    st.session_state.show_memory_slots_pref = open_slots
    # Left sidebar toggle is client-side and keeps chat scroll; our remount would
    # otherwise autoscroll GateWai to the latest message — preserve instead.
    st.session_state._preserve_gatewai_chat_scroll = True
    persist_ui_layout_prefs()


def restore_gatewai_chat_scroll_if_needed():
    """After a right-sidebar remount, put the chat pane back where the user left it."""
    if not st.session_state.pop("_restore_gatewai_chat_scroll", False):
        return
    components.html(
        """
<script>
(function () {
  const w = window.parent;
  const doc = w.document;
  const KEY = 'gatewai_chat_scroll_top';
  let raw = null;
  try { raw = w.sessionStorage.getItem(KEY); } catch (e) {}
  if (raw === null || raw === '') return;
  const y = parseInt(raw, 10);
  if (isNaN(y)) return;

  function findChatPane() {
    const root = doc.querySelector('section.main') || doc;
    const msgs = root.querySelectorAll('[data-testid="stChatMessage"]');
    if (!msgs.length) return null;
    const last = msgs[msgs.length - 1];
    let el = last.parentElement;
    while (el && el !== doc.body) {
      if (el === doc.documentElement || el === doc.body) break;
      if (el.matches && (
        el.matches('section.main') ||
        el.matches('[data-testid="stAppViewContainer"]') ||
        el.matches('[data-testid="stAppScrollToBottomContainer"]') ||
        el.matches('[data-testid="stMain"]')
      )) {
        el = el.parentElement;
        continue;
      }
      if (el.scrollHeight > el.clientHeight + 8 && el.clientHeight > 40) return el;
      el = el.parentElement;
    }
    return null;
  }

  function restore() {
    const pane = findChatPane();
    if (!pane) return;
    try { pane.style.scrollBehavior = 'auto'; } catch (e) {}
    pane.scrollTop = y;
  }
  restore();
  requestAnimationFrame(restore);
  [40, 120, 280, 600, 1200].forEach(function (ms) { setTimeout(restore, ms); });
})();
</script>
        """,
        height=0,
        width=0,
    )


def memory_slots_width_px():
    """Persisted pixel width of the app-level right Memory sidebar."""
    cookie_w = read_rsb_width_cookie()
    if cookie_w is not None:
        st.session_state.memory_slots_width_pref = cookie_w
        st.session_state.memory_slots_width = cookie_w
    if "memory_slots_width_pref" not in st.session_state:
        st.session_state.memory_slots_width_pref = _RSB_DEFAULT_WIDTH_PX
    width = _clamp_rsb_width_px(st.session_state.memory_slots_width_pref)
    st.session_state.memory_slots_width_pref = width
    return width


def install_right_sidebar_chrome(open_slots, width_px):
    """Fix Memory panel to the viewport right — collapse/expand like Streamlit's left sidebar.

    Open/close is CSS + localStorage only (no Streamlit remount), so GateWai chat
    keeps its scroll position the same way the left sidebar does.
    """
    open_js = "true" if open_slots else "false"
    width_px = int(_clamp_rsb_width_px(width_px))
    components.html(
        f"""
<script>
(function () {{
  const w = window.parent;
  const doc = w.document;
  const WIDTH = {width_px};
  const MIN_W = {_RSB_MIN_WIDTH_PX};
  const MAX_W = {_RSB_MAX_WIDTH_PX};
  const LS_KEY = '{_RSB_WIDTH_LS}';
  const COOKIE = '{_RSB_WIDTH_COOKIE}';
  const OPEN_LS = '{_RSB_OPEN_LS}';
  const OPEN_COOKIE = '{_RSB_OPEN_COOKIE}';
  const FLAG = '__gatewaiRightSidebarChromeV3';

  // Client-owned open state (mirrors Streamlit left sidebar — no Python rerun).
  let open = {open_js};
  try {{
    const saved = w.localStorage.getItem(OPEN_LS);
    if (saved === '0' || saved === '1') open = (saved === '1');
  }} catch (e) {{}}

  let widthPx = WIDTH;
  try {{
    const ls = parseInt(w.localStorage.getItem(LS_KEY) || '', 10);
    if (ls) widthPx = Math.max(MIN_W, Math.min(MAX_W, ls));
  }} catch (e) {{}}

  function setWidthCookie(px) {{
    try {{
      const secure = w.location.protocol === 'https:' ? '; Secure' : '';
      doc.cookie = COOKIE + '=' + encodeURIComponent(String(px))
        + '; path=/; max-age=31536000; SameSite=Lax' + secure;
    }} catch (e) {{}}
    try {{ w.localStorage.setItem(LS_KEY, String(px)); }} catch (e) {{}}
  }}

  function setOpenCookie(isOpen) {{
    try {{
      const secure = w.location.protocol === 'https:' ? '; Secure' : '';
      doc.cookie = OPEN_COOKIE + '=' + (isOpen ? '1' : '0')
        + '; path=/; max-age=31536000; SameSite=Lax' + secure;
    }} catch (e) {{}}
    try {{ w.localStorage.setItem(OPEN_LS, isOpen ? '1' : '0'); }} catch (e) {{}}
  }}

  function isMainChrome(node) {{
    if (!node || !node.querySelector) return true;
    if (node.querySelector('[data-testid="stChatMessage"]')) return true;
    if (node.querySelector('[data-testid="stChatInput"]')) return true;
    if (node.querySelector('[data-testid="stRadio"]')) return true;
    if (node.querySelector('[role="radiogroup"]')) return true;
    if (node.querySelector('.ace_editor')) return true;
    if (node.querySelector('.gatewai-lp-shell')) return true;
    return false;
  }}

  function findPanel() {{
    const shell = doc.querySelector('.gatewai-rsb-shell');
    if (!shell) return null;
    let el = shell.closest('[data-testid="stVerticalBlock"]');
    if (!el) return shell.closest('[data-testid="element-container"]') || shell.parentElement;
    while (el) {{
      const parent = el.parentElement
        && el.parentElement.closest('[data-testid="stVerticalBlock"]');
      if (!parent || parent === el) break;
      if (isMainChrome(parent)) break;
      if (!parent.querySelector('.gatewai-rsb-shell')) break;
      el = parent;
    }}
    if (isMainChrome(el)) {{
      return shell.closest('[data-testid="element-container"]') || shell.parentElement;
    }}
    return el;
  }}

  function scrubMisappliedPanels() {{
    Array.from(doc.querySelectorAll('.gatewai-rsb-panel')).forEach(function (node) {{
      if (isMainChrome(node)) node.classList.remove('gatewai-rsb-panel');
    }});
  }}

  function makeChevronBtn(id, iconName, title, onClick) {{
    let btn = doc.getElementById(id);
    if (!btn) {{
      btn = doc.createElement('button');
      btn.id = id;
      btn.type = 'button';
      btn.className = 'gatewai-rsb-chevron-btn';
      btn.innerHTML = '<span class="material-symbols-outlined" aria-hidden="true">'
        + iconName + '</span>';
      btn.addEventListener('click', onClick);
    }} else {{
      btn.onclick = onClick;
    }}
    btn.title = title;
    btn.setAttribute('aria-label', title);
    return btn;
  }}

  function alignExpandWithLeftHeader(btn) {{
    const leftBtn = doc.querySelector('[data-testid="stExpandSidebarButton"]')
      || doc.querySelector('[data-testid="stSidebarCollapseButton"] button')
      || doc.querySelector('[data-testid="stSidebarCollapseButton"]')
      || doc.querySelector('[data-testid="stHeader"]');
    let topPx = 6;
    if (leftBtn) {{
      const r = leftBtn.getBoundingClientRect();
      const h = btn.getBoundingClientRect().height || 32;
      topPx = Math.max(2, Math.round(r.top + (r.height - h) / 2));
    }}
    btn.style.top = topPx + 'px';
    btn.style.right = '0.55rem';
  }}

  let _dragging = false;
  function ensureDrag(panel) {{
    let handle = panel.querySelector(':scope > .gatewai-rsb-drag');
    if (!handle) {{
      handle = doc.createElement('div');
      handle.className = 'gatewai-rsb-drag';
      handle.title = 'Drag to resize';
      panel.appendChild(handle);
    }}
    if (handle.dataset.bound === '1') return;
    handle.dataset.bound = '1';
    handle.addEventListener('mousedown', function (ev) {{
      if (!open) return;
      ev.preventDefault();
      _dragging = true;
      handle.classList.add('dragging');
      doc.body.style.cursor = 'col-resize';
      doc.body.style.userSelect = 'none';
    }});
    doc.addEventListener('mousemove', function (ev) {{
      if (!_dragging) return;
      widthPx = Math.max(MIN_W, Math.min(MAX_W, Math.round(w.innerWidth - ev.clientX)));
      doc.documentElement.style.setProperty('--gatewai-rsb-width', widthPx + 'px');
      panel.style.width = widthPx + 'px';
    }});
    doc.addEventListener('mouseup', function () {{
      if (!_dragging) return;
      _dragging = false;
      handle.classList.remove('dragging');
      doc.body.style.cursor = '';
      doc.body.style.userSelect = '';
      setWidthCookie(widthPx);
    }});
  }}

  function setOpen(next) {{
    open = !!next;
    setOpenCookie(open);
    applyLayout();
  }}

  function ensureCollapseBtn(panel) {{
    const btn = makeChevronBtn(
      'gatewai-rsb-collapse-btn',
      'keyboard_double_arrow_right',
      'Collapse Memory slots sidebar',
      function (ev) {{
        ev.preventDefault();
        ev.stopPropagation();
        setOpen(false);
      }}
    );
    if (btn.parentElement !== panel) panel.appendChild(btn);
    btn.style.display = open ? '' : 'none';
  }}

  function ensureExpandBtn() {{
    const legacy = doc.getElementById('gatewai-rsb-edge-tab');
    if (legacy) legacy.remove();
    let btn = doc.getElementById('gatewai-rsb-expand-btn');
    if (open) {{
      if (btn) btn.remove();
      return;
    }}
    btn = makeChevronBtn(
      'gatewai-rsb-expand-btn',
      'keyboard_double_arrow_left',
      'Expand Memory slots sidebar',
      function (ev) {{
        ev.preventDefault();
        ev.stopPropagation();
        setOpen(true);
      }}
    );
    if (btn.parentElement !== doc.body) doc.body.appendChild(btn);
    alignExpandWithLeftHeader(btn);
  }}

  function applyLayout() {{
    scrubMisappliedPanels();
    const px = open ? widthPx : 0;
    doc.documentElement.style.setProperty('--gatewai-rsb-width', px + 'px');
    const panel = findPanel();
    if (!panel || isMainChrome(panel)) {{
      doc.documentElement.style.setProperty('--gatewai-rsb-width', '0px');
      const c = doc.getElementById('gatewai-rsb-collapse-btn');
      if (c) c.remove();
      ensureExpandBtn();
      return;
    }}
    // Keep the pane mounted (like st.sidebar) — only the width collapses.
    panel.classList.add('gatewai-rsb-panel');
    panel.style.width = px + 'px';
    panel.style.visibility = open ? 'visible' : 'hidden';
    panel.style.pointerEvents = open ? 'auto' : 'none';
    ensureDrag(panel);
    ensureCollapseBtn(panel);
    ensureExpandBtn();
  }}

  function mount() {{
    if (_dragging) return;
    applyLayout();
  }}

  w.__gatewaiRsbMount = mount;
  w.__gatewaiRsbSetOpen = setOpen;
  function mountSoon() {{
    if (w.__gatewaiRsbTimer) clearTimeout(w.__gatewaiRsbTimer);
    w.__gatewaiRsbTimer = setTimeout(function () {{
      if (typeof w.__gatewaiRsbMount === 'function') w.__gatewaiRsbMount();
    }}, 50);
  }}
  mount();
  setTimeout(mount, 120);
  setTimeout(mount, 500);
  if (!w[FLAG]) {{
    w[FLAG] = true;
    try {{
      const obs = new MutationObserver(mountSoon);
      obs.observe(doc.body, {{ childList: true, subtree: true }});
    }} catch (e) {{}}
    w.addEventListener('resize', function () {{
      const btn = doc.getElementById('gatewai-rsb-expand-btn');
      if (btn) alignExpandWithLeftHeader(btn);
    }});
  }}
}})();
</script>
        """,
        height=0,
    )


def render_app_memory_sidebar():
    """App-level Memory sidebar — always mounted on desktop; JS collapses width only."""
    with st.container():
        st.markdown('<div class="gatewai-rsb-shell" aria-hidden="true"></div>', unsafe_allow_html=True)
        st.markdown("### 📂 Memory")
        st.caption("Tap 🟢/⚪ to include or hide a slot in chat context. Drag the left border to resize.")
        render_memory_slot_sidebar("memory", height=None, show_meta=True)


_LP_WIDTH_COOKIE = "gatewai_lp_width"
_LP_WIDTH_LS = "gatewai_lp_width"
_LP_DEFAULT_WIDTH_PX = 400
_LP_MIN_WIDTH_PX = 280
_LP_MAX_WIDTH_PX = 720


def _clamp_lp_width_px(raw):
    try:
        width = int(float(raw))
    except (TypeError, ValueError):
        return _LP_DEFAULT_WIDTH_PX
    return max(_LP_MIN_WIDTH_PX, min(_LP_MAX_WIDTH_PX, width))


def read_lp_width_cookie():
    try:
        cookies = getattr(st.context, "cookies", None)
        if cookies is None or not hasattr(cookies, "get"):
            return None
        raw = cookies.get(_LP_WIDTH_COOKIE)
        if raw is None or str(raw).strip() == "":
            return None
        return _clamp_lp_width_px(raw)
    except Exception:
        return None


def live_preview_width_px():
    cookie_w = read_lp_width_cookie()
    if cookie_w is not None:
        st.session_state.live_preview_width_pref = cookie_w
    if "live_preview_width_pref" not in st.session_state:
        st.session_state.live_preview_width_pref = _LP_DEFAULT_WIDTH_PX
    width = _clamp_lp_width_px(st.session_state.live_preview_width_pref)
    st.session_state.live_preview_width_pref = width
    return width


def install_live_preview_chrome(open_preview, width_px):
    """Fix Live Preview to the top-right (left of Memory), push the whole app left."""
    open_js = "true" if open_preview else "false"
    width_px = int(_clamp_lp_width_px(width_px))
    components.html(
        f"""
<script>
(function () {{
  const w = window.parent;
  const doc = w.document;
  const OPEN = {open_js};
  const WIDTH = {width_px};
  const MIN_W = {_LP_MIN_WIDTH_PX};
  const MAX_W = {_LP_MAX_WIDTH_PX};
  const LS_KEY = '{_LP_WIDTH_LS}';
  const COOKIE = '{_LP_WIDTH_COOKIE}';
  const FLAG = '__gatewaiLivePreviewChrome';

  function setCookie(px) {{
    try {{
      const secure = w.location.protocol === 'https:' ? '; Secure' : '';
      doc.cookie = COOKIE + '=' + encodeURIComponent(String(px))
        + '; path=/; max-age=31536000; SameSite=Lax' + secure;
    }} catch (e) {{}}
    try {{ w.localStorage.setItem(LS_KEY, String(px)); }} catch (e) {{}}
  }}

  function rsbPx() {{
    const raw = getComputedStyle(doc.documentElement).getPropertyValue('--gatewai-rsb-width');
    const n = parseInt(raw, 10);
    return isNaN(n) ? 0 : n;
  }}

  function applyWidth(px) {{
    px = Math.max(MIN_W, Math.min(MAX_W, Math.round(px)));
    doc.documentElement.style.setProperty('--gatewai-lp-width', OPEN ? (px + 'px') : '0px');
    return px;
  }}

  function isMainChrome(node) {{
    if (!node || !node.querySelector) return true;
    if (node.querySelector('[data-testid="stChatMessage"]')) return true;
    if (node.querySelector('[data-testid="stChatInput"]')) return true;
    if (node.querySelector('[data-testid="stRadio"]')) return true;
    if (node.querySelector('[role="radiogroup"]')) return true;
    if (node.querySelector('.ace_editor')) return true;
    if (node.querySelector('.gatewai-rsb-shell')) return true;
    return false;
  }}

  function findPanel() {{
    const shell = doc.querySelector('.gatewai-lp-shell');
    if (!shell) return null;
    let el = shell.closest('[data-testid="stVerticalBlock"]');
    if (!el) return shell.closest('[data-testid="element-container"]') || shell.parentElement;
    while (el) {{
      const parent = el.parentElement
        && el.parentElement.closest('[data-testid="stVerticalBlock"]');
      if (!parent || parent === el) break;
      if (isMainChrome(parent)) break;
      if (!parent.querySelector('.gatewai-lp-shell')) break;
      el = parent;
    }}
    if (isMainChrome(el)) {{
      return shell.closest('[data-testid="element-container"]') || shell.parentElement;
    }}
    return el;
  }}

  function scrubMisappliedPanels() {{
    Array.from(doc.querySelectorAll('.gatewai-lp-panel')).forEach(function (node) {{
      if (isMainChrome(node)) node.classList.remove('gatewai-lp-panel');
    }});
  }}

  let _dragging = false;
  function ensureDrag(panel) {{
    let handle = panel.querySelector(':scope > .gatewai-lp-drag');
    if (!handle) {{
      handle = doc.createElement('div');
      handle.className = 'gatewai-lp-drag';
      handle.title = 'Drag to resize Live Preview';
      panel.appendChild(handle);
    }}
    if (handle.dataset.bound === '1') return;
    handle.dataset.bound = '1';
    handle.addEventListener('mousedown', function (ev) {{
      ev.preventDefault();
      _dragging = true;
      handle.classList.add('dragging');
      doc.body.style.cursor = 'col-resize';
      doc.body.style.userSelect = 'none';
    }});
    doc.addEventListener('mousemove', function (ev) {{
      if (!_dragging) return;
      const px = applyWidth(w.innerWidth - ev.clientX - rsbPx());
      panel.style.width = px + 'px';
    }});
    doc.addEventListener('mouseup', function () {{
      if (!_dragging) return;
      _dragging = false;
      handle.classList.remove('dragging');
      doc.body.style.cursor = '';
      doc.body.style.userSelect = '';
      const raw = getComputedStyle(doc.documentElement).getPropertyValue('--gatewai-lp-width');
      const px = parseInt(raw, 10) || WIDTH;
      setCookie(px);
    }});
  }}

  function mount() {{
    if (_dragging) return;
    scrubMisappliedPanels();
    let startW = WIDTH;
    try {{
      const ls = parseInt(w.localStorage.getItem(LS_KEY) || '', 10);
      if (ls) startW = Math.max(MIN_W, Math.min(MAX_W, ls));
    }} catch (e) {{}}
    applyWidth(startW);
    const panel = findPanel();
    if (OPEN && panel && !isMainChrome(panel)) {{
      panel.classList.add('gatewai-lp-panel');
      try {{ panel.scrollTop = 0; }} catch (e) {{}}
      ensureDrag(panel);
    }} else {{
      if (panel) panel.classList.remove('gatewai-lp-panel');
      if (!OPEN || !panel || isMainChrome(panel)) {{
        doc.documentElement.style.setProperty('--gatewai-lp-width', '0px');
      }}
    }}
  }}

  w.__gatewaiLpMount = mount;
  function mountSoon() {{
    if (w.__gatewaiLpTimer) clearTimeout(w.__gatewaiLpTimer);
    w.__gatewaiLpTimer = setTimeout(function () {{
      if (typeof w.__gatewaiLpMount === 'function') w.__gatewaiLpMount();
    }}, 50);
  }}
  mount();
  setTimeout(function () {{ if (w.__gatewaiLpMount) w.__gatewaiLpMount(); }}, 120);
  setTimeout(function () {{ if (w.__gatewaiLpMount) w.__gatewaiLpMount(); }}, 500);
  if (!w[FLAG]) {{
    w[FLAG] = true;
    try {{
      const obs = new MutationObserver(mountSoon);
      obs.observe(doc.body, {{ childList: true, subtree: true }});
    }} catch (e) {{}}
  }}
}})();
</script>
        """,
        height=0,
    )


def render_app_live_preview():
    """App-level Live Preview panel (GateWai only; fixed from the top by chrome JS)."""
    with st.container():
        st.markdown('<div class="gatewai-lp-shell" aria-hidden="true"></div>', unsafe_allow_html=True)
        render_live_preview_pane()


def render_live_preview_pane():
    """GateWai Live Preview — artifacts + the currently selected Memory slot.

    File picking / Active toggles live in the right Memory sidebar; this pane
    only renders the selected slot's contents (plus HTML artifacts / web ctx).
    Content-box height slider is last.
    """
    st.subheader("📋 Live Preview")
    selected = st.session_state.get("selected_file") or CORE_SCHEMA.get(
        "summary.txt", "MI_Master_Record"
    )
    st.caption(f"Showing **{selected}** — pick another file in the Memory sidebar.")

    if "live_preview_box_height" not in st.session_state:
        st.session_state.live_preview_box_height = 550
    box_height = int(st.session_state.live_preview_box_height or 550)

    # A. HTML ARTIFACTS
    if st.session_state.messages:
        last_msg = st.session_state.messages[-1]
        if last_msg["role"] == "model":
            html_content = extract_html(last_msg["content"])
            if html_content:
                st.success("🖼️ Live Artifact Detected")
                with st.expander("Show Rendered View", expanded=True):
                    components.html(html_content, height=600, scrolling=True)

    # B. WEB CONTEXT
    if st.session_state.temp_web_ctx:
        st.info("🌐 **WEB CONTENT ACTIVE**")
        with st.expander("View Scraped Data"):
            st.write(st.session_state.temp_web_ctx[:500] + "...")

    # C. Selected slot contents
    st.markdown(f"### {selected}")
    meta = st.session_state.get("loaded_slots_metadata", {}).get(selected, {})
    if selected in CORE_SCHEMA.values():
        st.caption("System core")
    elif meta:
        scope = str(meta.get("scope", "project")).upper()
        st.caption(f"User slot · {scope}")
    val = st.session_state.get(selected, "")
    with st.container(height=box_height, border=True):
        st.markdown(val if val else "*Empty*", unsafe_allow_html=True)

    # D. Height control last
    st.slider(
        "Content Box Height",
        min_value=100,
        max_value=800,
        step=50,
        key="live_preview_box_height",
        help="Adjust the height of the content box.",
    )


def engine_room_widget_key(canonical, project=None):
    project = project if project is not None else st.session_state.get("active_project", "")
    return f"ui::{canonical}::{project}"

def engine_room_snapshot_from_session():
    snap = {}
    for canonical, file_key in ENGINE_ROOM_FIELDS:
        value = st.session_state.get(canonical)
        if file_key == "last_harvest_message_index":
            try:
                value = int(value or 0)
            except (TypeError, ValueError):
                value = 0
        if value is not None:
            snap[file_key] = value
    return snap

def read_project_config(name):
    path = os.path.join(BASE_PROJECT_DIR, name, "config.json")
    if not name or not os.path.exists(path):
        return {}, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded, False
    except Exception:
        return {}, True
    return {}, True

def remember_engine_room_snapshot(name):
    """Call after a project is loaded from disk. Do not write that load back over the file."""
    if not name:
        return
    st.session_state[f"_engine_room_snapshot::{name}"] = engine_room_snapshot_from_session()
    st.session_state._engine_room_reseed = True

def adopt_saved_engine_room_if_needed(name):
    """A session that still holds the built-in engines must not overwrite the saved ones."""
    if not name:
        return
    snap_key = f"_engine_room_snapshot::{name}"
    if isinstance(st.session_state.get(snap_key), dict):
        return
    existing, unreadable = read_project_config(name)
    if unreadable:
        return
    if existing:
        for canonical, file_key in ENGINE_ROOM_FIELDS:
            if file_key in existing and existing[file_key] is not None:
                st.session_state[canonical] = existing[file_key]
        st.session_state._engine_room_reseed = True
    st.session_state[snap_key] = engine_room_snapshot_from_session()

def persist_engine_room_settings(name):
    """Write an engine-room value only after it changes during this visit to the project."""
    if not name:
        return
    st.session_state._engine_room_reseed = False
    snap_key = f"_engine_room_snapshot::{name}"
    snap = st.session_state.get(snap_key)
    current = engine_room_snapshot_from_session()
    if not isinstance(snap, dict):
        st.session_state[snap_key] = current
        return
    changed_keys = [key for key, value in current.items() if snap.get(key) != value]
    if not changed_keys:
        return
    path = os.path.join(BASE_PROJECT_DIR, name, "config.json")
    existing, unreadable = read_project_config(name)
    if unreadable:
        return
    for key in changed_keys:
        existing[key] = current[key]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        st.session_state[snap_key] = current
    except Exception:
        pass

def save_project_state(state, name, silent=False):
    proj_dir = os.path.join(BASE_PROJECT_DIR, name)
    proj_db = db.get_project_db_path(BASE_PROJECT_DIR, name)
    sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
    hist_path = os.path.join(proj_dir, "history.json")
    view_path = os.path.join(proj_dir, "view_state.json")
    config_path = os.path.join(proj_dir, "config.json")
    try:
        # 1. Save Chat History
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump([{"role": m["role"], "content": m["content"]} for m in state["messages"]], f, indent=4)

        # 2. Save View State (The Persistence Fix)
        if "loaded_slots_metadata" in state:
            view_map = {}
            for k, meta in state["loaded_slots_metadata"].items():
                if 'active_in_context' in meta:
                    view_map[k] = meta['active_in_context']

            with open(view_path, "w", encoding="utf-8") as f:
                json.dump(view_map, f, indent=4)

        # [NEW] 3. Save Config State
        # A follow-up save after Harvest does not carry every setting. Missing
        # keys keep the file's current values. If the file is unreadable, leave
        # it alone — replacing it with the built-in engines was the default pattern.
        existing_config = {}
        config_unreadable = False
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as existing_file:
                    loaded_config = json.load(existing_file)
                    if isinstance(loaded_config, dict):
                        existing_config = loaded_config
                    else:
                        config_unreadable = True
            except Exception:
                config_unreadable = True

        def saved_config_value(state_key, file_key, default):
            if state_key in state and state.get(state_key) is not None:
                return state.get(state_key)
            if file_key in existing_config:
                return existing_config[file_key]
            return default

        if not config_unreadable:
            config_data = {
                "chat_engine": saved_config_value("chat_engine_set", "chat_engine", "gemini-2.5-flash-lite"),
                "ingest_engine": saved_config_value("ingest_engine_set", "ingest_engine", "gemini-2.5-pro"),
                "harvest_engine": saved_config_value("harvest_engine_set", "harvest_engine", "gemini-2.5-pro"),
                "shadow_engine": saved_config_value("shadow_engine_set", "shadow_engine", "gemini-2.5-flash-lite"),
                "transcribe_engine": saved_config_value("transcribe_engine_set", "transcribe_engine", "gemini-2.5-flash-lite"),
                "domain_lens": saved_config_value("selected_lens_name", "domain_lens", "General"),
                "temperature": saved_config_value("temp_val", "temperature", 0.7),
                "top_p": saved_config_value("top_p_val", "top_p", 0.9),
                "memory_depth": saved_config_value("memory_depth_val", "memory_depth", 3),
                "compaction_threshold": saved_config_value("compaction_threshold", "compaction_threshold", 50000),
                "last_harvest_message_index": saved_config_value("last_harvest_message_index", "last_harvest_message_index", 0),
                "review_mode_active": saved_config_value("review_mode_active", "review_mode_active", True),
                "auto_shadow_active": saved_config_value("auto_shadow_active", "auto_shadow_active", True),
                "show_memory_slots": saved_config_value(
                    "show_memory_slots_pref", "show_memory_slots", False
                ),
                "memory_slots_width": saved_config_value(
                    "memory_slots_width_pref", "memory_slots_width", 28
                ),
                # Legacy aliases — same shared rail.
                "gatewai_show_slots": saved_config_value(
                    "show_memory_slots_pref", "show_memory_slots", False
                ),
                "show_file_list": saved_config_value(
                    "show_memory_slots_pref", "show_memory_slots", False
                ),
            }
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())

        # 4. Save All Slots (DTN-Aware)
        # --- CRITICAL FIX: Ensure CORE slots are always saved first ---
        for core_slot_key in CORE_SCHEMA.values():
            if core_slot_key in state: # Check if the content exists in the state dict
                meta = state.get("loaded_slots_metadata", {}).get(core_slot_key, {})

                # Determine the correct scope for saving
                # For SI_User_Profile and SI_Observed_Profile, always force 'global' scope
                # Otherwise, use what's in metadata or default to 'project'
                determined_scope = meta.get('scope', 'project')
                if core_slot_key in GLOBAL_CORE: # GLOBAL_CORE: SI_Identity, SI_User_Profile, SI_Observed_Profile
                    determined_scope = 'global'
                # For other core files like MI_Master_Record, MI_Project_Status, etc.,
                # their scope is typically 'project' unless explicitly changed by DTN.
                # The existing meta.get('scope', 'project') handles this correctly.

                db.save_slot(
                    proj_db,
                    sys_db,
                    core_slot_key,
                    state[core_slot_key],
                    meta.get('slot_type', 'core'),
                    meta.get('description', 'System Core File'),
                    determined_scope, # Use the explicitly determined scope
                    meta.get('owner_id', 'system'),
                    meta.get('is_distributed', False),
                    meta.get('part_of_dtn', False),
                    meta.get('ai_managed', False),
                    edit_summary="Project save",
                    folder=""
                )
        # Now iterate over user slots (and any other non-core slots whose metadata was loaded)
        # This ensures user-defined slots are also saved correctly.
        if "loaded_slots_metadata" in state:
            for slot_key, meta in state["loaded_slots_metadata"].items():
                # Skip core slots as they were handled above
                if slot_key in CORE_SCHEMA.values():
                    continue

                if slot_key in state: # Check if the content exists in the state dict
                    db.save_slot(
                        proj_db,
                        sys_db,
                        slot_key,
                        state[slot_key],
                        meta.get('slot_type', 'user'), # Explicitly 'user' for safety or rely on meta
                        meta.get('description', ''),
                        meta.get('scope', 'project'),
                        meta.get('owner_id', 'system'),
                        meta.get('is_distributed', False),
                        meta.get('part_of_dtn', False),
                        meta.get('ai_managed', False), # <-- ENSURE THIS IS PASSED
                        edit_summary="Project save",
                        folder=normalize_folder_name(meta.get('folder', ''))
                    )
    except Exception as e:
        if not silent: st.error(f"Save Error: {e}")

def database_for_slot(slot_name):
    """Return (project db, system db, scope, db that stores this slot)."""
    proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
    sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
    if slot_name in GLOBAL_CORE:
        scope = "global"
    else:
        scope = st.session_state.get("loaded_slots_metadata", {}).get(slot_name, {}).get("scope", "project")
    target = sys_db if scope == "global" else proj_db
    return proj_db, sys_db, scope, target

def read_stored_slot(db_path, slot_name):
    """Text currently stored for this slot, or None if the slot has never been saved."""
    reader = getattr(db, "get_slot_content", None)
    if reader is not None:
        return reader(db_path, slot_name)
    if not db_path or not os.path.exists(db_path):
        return None
    with sqlite3.connect(db_path, timeout=30) as conn:
        row = conn.execute(
            "SELECT content FROM memory_slots WHERE slot_name = ?",
            (slot_name,),
        ).fetchone()
    if not row:
        return None
    return row[0] if row[0] is not None else ""

def save_open_slot(slot_name, content, summary="Manual save"):
    """Write one slot and keep the previous text in edit history."""
    proj_db, sys_db, scope, _target = database_for_slot(slot_name)
    meta = st.session_state.get("loaded_slots_metadata", {}).get(slot_name, {})
    s_type = "core" if slot_name in CORE_SCHEMA.values() else meta.get("slot_type", "user")
    s_folder = "" if s_type == "core" else normalize_folder_name(meta.get("folder", ""))
    description = meta.get("description") or ("System Core File" if s_type == "core" else "User Draft")
    db.save_slot(
        proj_db,
        sys_db,
        slot_name,
        content,
        s_type,
        description,
        scope,
        meta.get("owner_id", "system"),
        meta.get("is_distributed", False),
        meta.get("part_of_dtn", False),
        meta.get("ai_managed", False),
        edit_summary=summary,
        folder=s_folder,
    )
    # The editor already shows this text. Remounting it reruns the whole app and
    # Streamlit then opens the chat tab.
    commit_slot_text(slot_name, content, remount=False)


#def save_global_profile(text):
#    with open(global_profile_path, "w", encoding="utf-8") as f: f.write(text)

def load_domain_content(domain_name):
    p = os.path.join(DOMAINS_DIR, f"{domain_name}.txt")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f: return f.read()
        except UnicodeDecodeError:
            try:
                with open(p, "r", encoding="cp1252") as f: return f.read()
            except:
                with open(p, "r", encoding="utf-8", errors="ignore") as f: return f.read()
    return ""

def export_to_pdf(text, title):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", 'B', 16)
    pdf.cell(0, 10, text=title, align='C', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("helvetica", size=11)
    safe_text = text.encode('latin-1', 'replace').decode('latin-1')
    pdf.multi_cell(0, 10, text=safe_text)
    return bytes(pdf.output())

def extract_html(text):
    if not isinstance(text, str): return None
    html_match = re.search(r"```html\n(.*?)\n```", text, re.DOTALL)
    if not html_match: html_match = re.search(r"(<html.*?>.*?</html>)", text, re.DOTALL)
    return html_match.group(1) if html_match else None

def estimate_tokens(text): return len(text) // 4

def get_asset_content(selected_assets, asset_dir):
    content = ""
    for a in selected_assets:
        p = os.path.join(asset_dir, a)
        if a.endswith('.pdf'):
            try:
                reader = PdfReader(p)
                text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
                content += f"\n--- PDF: {a} ---\n{text}\n"
            except: content += f"\n[Error reading {a}]\n"
        else:
            try:
                with open(p, 'r', encoding="utf-8", errors='ignore') as f:
                    content += f"\n--- FILE: {a} ---\n{f.read()}\n"
            except: content += f"\n[Error reading {a}]\n"
    return content

def chunk_text(text, chunk_size=15000):
    encoded = text.encode('utf-8')
    chunks = [encoded[i:i + chunk_size].decode('utf-8', errors='ignore')
              for i in range(0, len(encoded), chunk_size)]
    return chunks

def get_code_language(content_string):
    #content_string = content_string.strip()
    # CRITICAL FIX: Ensure content_string is a string from the very beginning
    # If its not a string, try to convert it to a string.
    # This handles cases where it might be a dict, list, int, etc.
    if not isinstance(content_string, str):
        try:
            content_string = str(content_string)
        except Exception:
            return "text" # If even str() conversion fails, fall back to "text"
    if not isinstance(content_string, str):
        return "text" # Default to text if not a string
    if (content_string.startswith('{') and content_string.endswith('}')) or \
        (content_string.startswith('[') and content_string.endswith(']')):
        return "json"
    # New check for markdown content
    if any(keyword in content_string for keyword in ["# ", "## ", "* ", "- ", "```", "---"]):
        return "markdown"
    return "text" # Fallback to plain text for anything else

def extract_balanced_json_object(text):
    """Return the first top-level {...} object, respecting quoted strings."""
    s = str(text or "")
    start = s.find("{")
    if start < 0:
        return s.strip()
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(s[start:], start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
    return s[start:].strip()

def escape_control_chars_in_json_strings(s):
    """Escape raw newlines/tabs inside JSON string values."""
    out = []
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                out.append(ch)
                escape = False
            elif ch == "\\":
                out.append(ch)
                escape = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)

def escape_bare_quotes_in_json_strings(s):
    """Treat inner quotes that don't close a key/value as escaped quotes."""
    out = []
    in_str = False
    escape = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if escape:
                out.append(ch)
                escape = False
            elif ch == "\\":
                out.append(ch)
                escape = True
            elif ch == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                nxt = s[j] if j < n else ""
                if nxt in ",}]:":
                    out.append('"')
                    in_str = False
                else:
                    out.append('\\"')
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
        i += 1
    return "".join(out)

def json_error_context(text, err, radius=280):
    pos = getattr(err, "pos", None)
    if not text:
        return "(empty)"
    if pos is None:
        return text[:1200]
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    pointer = " " * (pos - start) + "^"
    return f"Around character {pos}:\n{text[start:end]}\n{pointer}"

def recover_truncated_json_object(text):
    """Keep fully closed key/value pairs from a cut-off JSON object."""
    s = str(text or "")
    start = s.find("{")
    if start < 0:
        return None
    n = len(s)

    def skip_ws(j):
        while j < n and s[j] in " \t\r\n":
            j += 1
        return j

    def read_string(j):
        if j >= n or s[j] != '"':
            return None, j
        begin = j
        j += 1
        escape = False
        while j < n:
            ch = s[j]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                return s[begin:j + 1], j + 1
            j += 1
        return None, begin

    def read_balanced(j):
        if j >= n or s[j] not in "{[":
            return None, j
        begin = j
        depth = 0
        in_str = False
        escape = False
        while j < n:
            ch = s[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        return s[begin:j + 1], j + 1
            j += 1
        return None, begin

    def read_literal(j):
        begin = j
        while j < n and s[j] not in ",}] \t\r\n":
            j += 1
        if j == begin:
            return None, j
        token = s[begin:j]
        if token in ("true", "false", "null") or re.fullmatch(
            r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", token
        ):
            return token, j
        return None, begin

    def read_value(j):
        j = skip_ws(j)
        if j >= n:
            return None, j
        if s[j] == '"':
            return read_string(j)
        if s[j] in "{[":
            return read_balanced(j)
        return read_literal(j)

    i = skip_ws(start + 1)
    pairs = []
    while i < n:
        i = skip_ws(i)
        if i >= n or s[i] == "}":
            break
        if s[i] != '"':
            break
        key, nxt = read_string(i)
        if key is None:
            break
        i = skip_ws(nxt)
        if i >= n or s[i] != ":":
            break
        val, nxt = read_value(i + 1)
        if val is None:
            # Drop the incomplete last key — never stage a half-written file.
            break
        pairs.append(f"{key}:{val}")
        i = skip_ws(nxt)
        if i < n and s[i] == ",":
            i += 1
            continue
        break

    if not pairs:
        return None
    try:
        obj = json.loads("{" + ",".join(pairs) + "}", strict=False)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None

def parse_harvest_json(raw_text):
    """Extract and repair LLM JSON. Returns (obj_or_none, error_or_none, context)."""
    text = str(raw_text or "").strip()
    if text.lower().startswith("```json"):
        text = text[7:].lstrip()
    elif text.lower().startswith("```"):
        text = text[3:].lstrip()
    if text.lower().endswith("```"):
        text = text[:-3].rstrip()
    extracted = extract_balanced_json_object(text)
    candidates = [extracted]
    repaired = escape_control_chars_in_json_strings(extracted)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    candidates.append(repaired)
    quoted_commas = re.sub(r'("(?:\\.|[^"\\])*")\s+(")', r'\1, \2', repaired)
    quoted_commas = re.sub(r'([}\]])\s+(")', r'\1, \2', quoted_commas)
    candidates.append(quoted_commas)
    candidates.append(escape_bare_quotes_in_json_strings(repaired))
    candidates.append(escape_bare_quotes_in_json_strings(quoted_commas))
    last_err = None
    last_tried = extracted
    for cand in candidates:
        last_tried = cand
        try:
            obj = json.loads(cand, strict=False)
            if isinstance(obj, dict):
                return obj, None, None
            last_err = ValueError("JSON root was not an object")
        except json.JSONDecodeError as e:
            last_err = e
    for cand in candidates:
        recovered = recover_truncated_json_object(cand)
        if recovered:
            note = (
                f"Recovered {len(recovered)} complete key(s) from truncated JSON "
                f"({', '.join(recovered.keys())}); dropped the incomplete tail."
            )
            return recovered, None, note
    ctx = json_error_context(last_tried, last_err) if isinstance(last_err, json.JSONDecodeError) else str(last_err)
    return None, last_err, ctx

def extract_llm_text(resp):
    if resp is None:
        return ""
    try:
        text = getattr(resp, "text", None)
        if text and str(text).strip():
            return str(text).strip()
    except Exception:
        pass
    chunks = []
    for cand in getattr(resp, "candidates", None) or []:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if not parts:
            continue
        for part in parts:
            part_text = getattr(part, "text", None)
            if part_text:
                chunks.append(str(part_text))
    return "\n".join(chunks).strip()

def transcribe_chat_audio(audio_file, engine_name="gemini-2.5-flash-lite"):
    """Turn a chat-input recording into editable text. Gemini only — DeepSeek has no STT."""
    if audio_file is None:
        return ""
    data = audio_file.getvalue() if hasattr(audio_file, "getvalue") else audio_file.read()
    if not data:
        return ""
    mime = getattr(audio_file, "type", None) or "audio/wav"
    if is_deepseek_engine(engine_name):
        engine_name = "gemini-2.5-flash"
    client = get_llm_client(engine_name)
    prompt = [
        types.Part.from_bytes(data=data, mime_type=mime),
        "Transcribe the spoken words exactly. Return only the transcript, with no quotes or commentary.",
    ]
    configs = [
        {"temperature": 0.1, "thinking_config": {"thinking_budget": 0}},
        {"temperature": 0.1},
    ]
    last_err = None
    for config in configs:
        try:
            resp = client.models.generate_content(model=engine_name, contents=prompt, config=config)
            text = extract_llm_text(resp)
            if text:
                return text
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return ""

def request_harvest_json(client, engine_name, prompt, temperature=0.7, top_p=0.9):
    def _generate(config):
        resp = client.models.generate_content(model=engine_name, contents=prompt, config=config)
        return extract_llm_text(resp)

    base_config = {
        "temperature": temperature,
        "top_p": top_p,
        "max_output_tokens": 65536,
    }
    text_resp = ""
    last_err = None
    attempts = [base_config]
    if not is_deepseek_engine(engine_name):
        attempts = [{**base_config, "thinking_config": {"thinking_budget": 0}}, base_config]
    for config in attempts:
        try:
            text_resp = _generate(config)
        except Exception as e:
            last_err = e
            text_resp = ""
        if text_resp:
            break
    if not text_resp:
        return None, last_err or ValueError("Model returned empty output"), "(empty)"
    return parse_harvest_json(text_resp)

def clip_for_harvest(text, limit=6000):
    s = str(text or "")
    if len(s) <= limit:
        return s
    omitted = len(s) - limit
    head = (limit * 2) // 3
    tail = limit - head
    return f"{s[:head]}\n\n[... {omitted} characters omitted ...]\n\n{s[-tail:]}"

def clip_recent_text(text, limit=12000):
    """Keep the newest text. Shadow updates from the latest turns, not the oldest."""
    s = str(text or "")
    if len(s) <= limit:
        return s
    omitted = len(s) - limit
    return f"[... {omitted} earlier characters omitted ...]\n\n{s[-limit:]}"

def fetch_harvest_object(client, engine_name, prompt, temperature=0.7, top_p=0.9):
    obj, err, ctx = request_harvest_json(client, engine_name, prompt, temperature, top_p)
    empty = (not isinstance(obj, dict)) and (
        not ctx or ctx == "(empty)" or (err is not None and "empty" in str(err).lower())
    )
    if empty:
        time.sleep(1.5)
        obj, err, ctx = request_harvest_json(client, engine_name, prompt, temperature, top_p)
    return obj, err, ctx

def harvest_file_text(value):
    """A harvest value must be a complete file string, not a list of fragments."""
    if value is None:
        return ""
    if isinstance(value, dict):
        if "content" in value:
            return str(value.get("content") or "").strip()
        return ""
    if isinstance(value, (list, tuple)):
        return ""
    return str(value).strip()

def harvest_too_small(current, proposed):
    cur, new = str(current or ""), str(proposed or "")
    if len(cur) < 400:
        return False
    return len(new) < int(len(cur) * 0.5)

def shadow_preserves_existing(current, proposed):
    """False when a rewrite dropped most of the current file (typical 'revert' from Shadow)."""
    cur = str(current or "").strip()
    new = str(proposed or "").strip()
    if len(cur) < 200:
        return True
    if harvest_too_small(cur, new):
        return False
    head_len = min(300, len(cur))
    if cur[:head_len] in new:
        return True
    units = [u.strip() for u in re.split(r"\n\s*\n", cur) if len(u.strip()) >= 40]
    if not units:
        return True
    hay = normalize_harvest_text(new)
    hits = 0
    for unit in units:
        needle = normalize_harvest_text(unit)[:80]
        if needle and needle in hay:
            hits += 1
    return (hits / len(units)) >= 0.6

def save_replaces_most(current, proposed):
    """True when a save would throw away most of the text already stored for this slot."""
    cur = str(current or "")
    new = str(proposed or "")
    if len(cur.strip()) < 400:
        return False
    if normalize_harvest_text(cur) == normalize_harvest_text(new):
        return False
    return not shadow_preserves_existing(cur, new)

def short_text_diff(old, new, max_lines=40, head=50, tail=15, fromfile="saved", tofile="editor"):
    """A short unified diff. Long files are clipped before the comparison."""
    import difflib

    def clip_lines(text, head_n=head, tail_n=tail):
        lines = str(text or "").splitlines()
        if len(lines) <= head_n + tail_n + 1:
            return lines
        skipped = len(lines) - head_n - tail_n
        return lines[:head_n] + [f"... {skipped} lines omitted ..."] + lines[-tail_n:]

    diff = difflib.unified_diff(
        clip_lines(old),
        clip_lines(new),
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
        n=2,
    )
    out = []
    for line in diff:
        out.append(line)
        if len(out) >= max_lines:
            out.append("... diff truncated ...")
            break
    return "\n".join(out) if out else "(no line diff)"


def proposal_text_diff(old, new):
    """Staging-area diff: enough context to review Shadow/Harvest proposals."""
    return short_text_diff(
        old,
        new,
        max_lines=120,
        head=80,
        tail=40,
        fromfile="current",
        tofile="proposed",
    )

def report_harvest_error(title, message, context=None):
    if "harvest_errors" not in st.session_state:
        st.session_state.harvest_errors = []
    st.session_state.harvest_errors.append({
        "title": title,
        "message": message,
        "context": context or "",
    })
    st.toast(title)

HARVEST_CORE_SLOTS = [
    "MI_Master_Record", "MC_Frontier", "MI_Project_Status",
    "MC_Blindspots", "MC_Assumptions", "SI_Observed_Profile", "MC_Focus",
]
HARVEST_SMALL_SLOTS = [
    "MC_Frontier", "MI_Project_Status", "MC_Blindspots",
    "MC_Assumptions", "SI_Observed_Profile", "MC_Focus",
]
HARVEST_SLOT_CHARTERS = {
    "MC_Focus": (
        "Immediate direction, pressing questions, and next steps for the current discussion. "
        "Transient. NOT long-term risks, NOT overall project phase, NOT user habits."
    ),
    "MI_Project_Status": (
        "Current operational phase and objective — a concise workflow state. "
        "NOT immediate discussion questions, NOT long-term strategic unknowns."
    ),
    "MC_Frontier": (
        "Active risks, strategic unknowns, and key open questions for the project. "
        "Follow SI_Reference_Frontier. NOT immediate next steps, NOT project phase, NOT user preferences."
    ),
    "MC_Blindspots": (
        "What the discussion is ignoring or failing to see. "
        "NOT known Frontier risks, NOT next steps, NOT project status."
    ),
    "MC_Assumptions": (
        "Unstated premises and unproven beliefs being relied upon. "
        "NOT open questions (Frontier), NOT next steps (Focus)."
    ),
    "SI_Observed_Profile": (
        "Observed user behavioral patterns and preferences. "
        "NOT project facts, NOT task lists, NOT Frontier risks."
    ),
    "MI_Master_Record": (
        "Durable project facts, decisions, evidence, and resolved conclusions. "
        "Preserve existing structure. NOT a dump of Focus, Profile, or Frontier. NOT a chat recap."
    ),
}
HARVEST_WRAPPER_KEYS = (
    "updates", "result", "data", "slots", "files", "output",
    "memory", "payload", "changes",
)

def unwrap_harvest_payload(updates, known_user_files=None):
    known_user_files = set(known_user_files or [])
    if not isinstance(updates, dict):
        return {}
    known = set(HARVEST_CORE_SLOTS) | known_user_files | {"user_file_updates"}

    def has_known(d):
        return isinstance(d, dict) and any(k in known for k in d)

    if has_known(updates):
        return updates
    for wrap_key in HARVEST_WRAPPER_KEYS:
        inner = updates.get(wrap_key)
        if has_known(inner):
            return inner
    if len(updates) == 1:
        only = next(iter(updates.values()))
        if has_known(only):
            return only
    return updates

def collect_harvest_updates(updates, known_user_files=None):
    """Return (recognized_items, unrecognized_keys). items are (name, value, kind)."""
    known_user_files = set(known_user_files or [])
    payload = unwrap_harvest_payload(updates, known_user_files)
    items = []
    recognized = set()
    if not isinstance(payload, dict):
        return items, []
    for slot in HARVEST_CORE_SLOTS:
        if slot in payload:
            items.append((slot, payload[slot], "core"))
            recognized.add(slot)
    ufu = payload.get("user_file_updates")
    if isinstance(ufu, dict):
        recognized.add("user_file_updates")
        for name, value in ufu.items():
            items.append((name, value, "user"))
            recognized.add(name)
    for name, value in payload.items():
        if name in recognized:
            continue
        if name in known_user_files:
            items.append((name, value, "user"))
            recognized.add(name)
    unrecognized = [k for k in payload.keys() if k not in recognized]
    return items, unrecognized

def normalize_harvest_text(text):
    text = str(text or "").strip()
    text = re.sub(r"```(?:json|markdown|python)?\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n```", "", text)
    text = re.sub(r"^-+\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*$\n", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text, flags=re.DOTALL)
    return text.strip()

def harvest_other_charters(target):
    lines = []
    for name, charter in HARVEST_SLOT_CHARTERS.items():
        if name == target:
            continue
        lines.append(f"- {name}: {charter}")
    return "\n".join(lines)

def harvest_copies_other_slot(target, proposed, get_text):
    """Reject a proposal that is clearly another slot's file, not a rewrite of this one."""
    prop = normalize_harvest_text(proposed)
    if len(prop) < 80:
        return None
    self_n = normalize_harvest_text(get_text(target))
    for name in HARVEST_CORE_SLOTS:
        if name == target:
            continue
        other = normalize_harvest_text(get_text(name))
        if len(other) < 80:
            continue
        if prop == other:
            return name
        prefix_len = min(400, len(other))
        if prefix_len < 200:
            continue
        prefix = other[:prefix_len]
        if prefix in prop and (not self_n or prefix not in self_n):
            return name
    return None

SHADOW_SLOT_JOBS = {
    "MC_Focus": (
        "Write ONLY the current next steps, pressing questions, and immediate direction from the latest turn. "
        "Short. No chat recap, no invented items, no project history, no risks, no phase/status, no user habits."
    ),
    "MI_Project_Status": (
        "Infer the current operational phase and objective. "
        "Update if the slot is empty or the phase/objective changed. "
        "Concise workflow state only — not next steps, not long-term risks."
    ),
    "MC_Frontier": (
        "Add or revise ONLY active risks, strategic unknowns, and key open questions this interaction actually raised. "
        "Do not invent. Do not add next steps, status, habits, or a recap. Do not copy MC_Focus. "
        "Keep still-active items. Mark resolved items as Resolved."
    ),
    "MC_Blindspots": (
        "Look for what is MISSING from the discussion and update this file. Do not invent gaps."
    ),
    "MC_Assumptions": (
        "Identify unproven premises actually being relied upon and update this file. Do not invent."
    ),
    "SI_Observed_Profile": (
        "Identify new behavioral patterns and preferences. "
        "Add new bullets or modify existing ones. Log habits, not project facts."
    ),
}

def shadow_slot_charter(target, loaded_metadata):
    charter = HARVEST_SLOT_CHARTERS.get(target)
    if charter:
        return charter
    meta = loaded_metadata.get(target) or {}
    return (meta.get("description") or "").strip() or (
        "Keep this file true to its existing purpose and structure."
    )

def shadow_copies_other_slot(target, proposed, get_text):
    """Reject a wholesale swap of another slot's file, not shared project wording."""
    prop = normalize_harvest_text(proposed)
    if len(prop) < 80:
        return None
    self_n = normalize_harvest_text(get_text(target))
    for name in HARVEST_CORE_SLOTS:
        if name == target:
            continue
        other = normalize_harvest_text(get_text(name))
        if len(other) < 80:
            continue
        if prop == other:
            return name
        n = min(300, len(other), len(prop))
        if n >= 200 and prop[:n] == other[:n] and (not self_n or prop[:n] != self_n[:n]):
            return name
    return None

def shadow_update_instructions(target, loaded_metadata, current_state, is_distributed):
    """One slot per call. Write the update — do not decline it."""
    extra = ""
    if target == "MC_Frontier":
        extra = (
            "FRONTIER FORMAT REFERENCE:\n"
            f"{clip_for_harvest(current_state.get('SI_Reference_Frontier', ''), 2500)}\n"
        )
        if is_distributed:
            extra += (
                "Append new findings under ## Compact-Context Analysis. "
                "Do not modify ## Full-Context Analysis.\n"
            )
    elif target in loaded_metadata and loaded_metadata[target].get("slot_type") == "user":
        extra = (
            f"Integrate new details from THIS interaction into '{target}'. "
            "Return the COMPLETE current file with those additions.\n"
        )
    job = SHADOW_SLOT_JOBS.get(
        target,
        "Integrate new relevant details from the interaction.",
    )
    stay_out = ""
    if target in ("MC_Focus", "MC_Frontier"):
        stay_out = (
            f"PURPOSE: {shadow_slot_charter(target, loaded_metadata)}\n"
            "Only include material that belongs in this slot. "
            "If the interaction has no new material for this slot, return the current file unchanged.\n"
        )
    return (
        f"Update {target}. Return the complete file for this slot only.\n"
        f"{stay_out}"
        f"JOB: {job}\n"
        f"{extra}"
        "Use concise Markdown. Remove items that are no longer true. "
        "Do not copy another memory slot into this file. "
        "Do not pad with extra commentary or unused ideas.\n"
        "No conversational filler. No job headers.\n"
        "Your first output line must be exactly: OUTPUT CONTENT BELOW THIS LINE:\n"
    )

def extract_content_from_ai_output(ai_output_text):
#    print(f"DEBUG: extract_content_from_ai_output received (first 200 chars): {str(ai_output_text)[:200]}...") # Print first 200 chars
    clean_text = str(ai_output_text).strip()

    # Aggressively remove markdown code fences AND "json" prefix if present
    # This handles "```json\n{...}```", "```\n{...}```", and "json\n{...}"
    if clean_text.lower().startswith("```json"):
        clean_text = clean_text[len("```json"):].lstrip()
    elif clean_text.lower().startswith("```"):
        clean_text = clean_text[3:].lstrip()
    elif clean_text.lower().startswith("json"): # New: handle "json\n{...}" without backticks
        clean_text = clean_text[len("json"):].lstrip()

    if clean_text.lower().endswith("```"):
        clean_text = clean_text[:-3].rstrip()

    # 1. Try to parse as JSON and extract 'content'
    try:
        parsed_json = json.loads(clean_text, strict=False)
        if isinstance(parsed_json, dict) and "content" in parsed_json:
            # If content is found, return it, stripping any extra whitespace
            return parsed_json["content"].strip()
        # If its valid JSON but doesnt have a 'content' key, or if its a list/other JSON type,
        # we return the *original clean_text* that was passed to json.loads,
        # assuming the AI intended that to be the content.
        return clean_text.strip()
    except json.JSONDecodeError:
        # If its not valid JSON at all, treat it as raw text
        return clean_text.strip()

def apply_smart_staging_override(target, current_content, new_content, shadow_logs):
    force_staging = False
    reason = "" # Reason for staging

    # A. Destructive Update Detection (Size Check)
    if current_content and new_content:
        size_ratio = len(new_content) / len(current_content) if len(current_content) > 0 else 0
        if size_ratio < 0.5:
            force_staging = True
            reason = f"Significant size reduction ({size_ratio:.2f})."

    # B. Content Removal Detection (Preference Check - Adapt for other slot types)
    if not force_staging and target == "SI_Observed_Profile": # Specific check for SI_Observed_Profile
        preferences_removed = 0
        current_prefs = re.findall(r"^\s*[-*]\s.*$", current_content, re.MULTILINE)
        new_prefs = re.findall(r"^\s*[-*]\s.*$", new_content, re.MULTILINE)
        if current_prefs:
            preferences_removed = len(current_prefs) - len(new_prefs)
            if preferences_removed > 3:
                force_staging = True
                reason = f"Removed more than 3 preferences."

    # C. JSON Validation (if applicable)
    if not force_staging and (target in ["MI_Master_Record", "MI_Project_Status", "MC_Frontier", "MC_Assumptions", "MC_Blindspots", "SI_Observed_Profile", "MC_Focus"]): # Adjust for JSON slots
        try:
            json.loads(new_content)
        except json.JSONDecodeError:
            force_staging = True
            reason = "Invalid JSON format."

    if force_staging:
        shadow_logs.append(f"⚠️ SMART STAGING: {target} - {reason}")

    return force_staging, reason
# Insert this new function immediately after `prepare_printable_html`
# (e.g., around line 530)

# Locate your trigger_print_and_reload_js function (around line 530 in your `updated_app_py`).
# Replace the entire function with this corrected version:
# --- NEW: Helper function to prepare HTML content for printing (DIV ONLY) ---
# This function generates ONLY the HTML string for the printable div.
def prepare_printable_html(content_text, title="Gatewai Document", is_markdown=False):
    # If content is markdown, convert it to HTML first
    if is_markdown:
        # Use markdown library to convert to HTML
        # 'content_text' is now markdown, markdown.markdown() converts it to HTML
        content_html_body = markdown.markdown(
            content_text,
            extensions=["extra", "sane_lists", "nl2br"],
        )
    else:
        # If not markdown, assume content_text is already HTML or plain text
        # If its plain text, we might want to wrap it in <pre> for display
        # For now, well assume its either HTML or meant to be rendered as-is (like chat messages)
        content_html_body = content_text

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>{title}</title>
        <style>
            /* Basic print styles for the iframes document */
            body {{
                font-family: sans-serif;
                margin: 20px;
                color: black;
                background-color: white;
                line-height: 1.6;
            }}
            h1, h2, h3, h4, h5, h6 {{
                page-break-after: avoid;
                color: #000;
                margin-top: 1em;
                margin-bottom: 0.5em;
            }}
            pre, code {{
                white-space: pre-wrap !important;
                word-break: break-word !important;
                overflow-wrap: break-word !important;
                page-break-inside: avoid !important;
                background-color: #f4f4f4;
                border: 1px solid #eee;
                padding: 10px;
                color: black;
                display: block; /* Ensure it behaves like a block */
            }}
            .chat-container {{
                border: 1px solid #ccc;
                padding: 15px;
                border-radius: 8px;
                background-color: #f9f9f9;
            }}
            .chat-message {{
                margin-bottom: 10px;
                padding: 8px 12px;
                border-radius: 8px;
                max-width: 100%;
                page-break-inside: avoid;
                color: black;
            }}
            .user-message {{
                text-align: right;
                background-color: #e0f7fa; /* Light blue */
                border: 1px solid #b2ebf2;
            }}
            .model-message {{
                text-align: left;
                background-color: #f3e5f5; /* Light purple */
                border: 1px solid #e1bee7;
            }}
            /* General element resets */
            div, p, span, ul, ol, li {{
                page-break-inside: auto;
            }}
            /* Ensure images scale down */
            img {{
                max-width: 100%;
                height: auto;
            }}
        </style>
    </head>
    <body>
        {content_html_body}
    </body>
    </html>
    """

# --- NEW: Function to generate JS for printing content within a temporary iframe ---
def print_in_iframe_js(html_content_to_print):
    # escaped_html_literal will be a Python string that is a valid JavaScript string literal
    # e.g., '"<p>Hello</p>"' (json.dumps adds the outer quotes and escapes inner content)
    escaped_html_literal = json.dumps(html_content_to_print)

    js_code_parts = []

    js_code_parts.append(f"""
    <script>
        (function() {{
            console.log("JS (iframe creator): Initiating iframe print sequence. Timestamp: " + new Date().getTime());

            var parentDoc = window.parent.document;

            var iframeCreationAttempts = 0;
            var maxIframeCreationAttempts = 50;
            var iframeCreationInterval = setInterval(function() {{
                iframeCreationAttempts++;
                if (parentDoc && parentDoc.body) {{
                    clearInterval(iframeCreationInterval);
                    console.log("JS (iframe creator): Parent document body found after " + iframeCreationAttempts + " attempts.");

                    var iframe = parentDoc.createElement('iframe');
                    iframe.style.display = 'none';
                    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups allow-modals');

                    parentDoc.body.appendChild(iframe);
                    console.log("JS (iframe creator): Iframe appended to parent body. Timestamp: " + new Date().getTime());

                    iframe.onload = function() {{
                        console.log("JS (iframe creator): Iframe onload event fired. Timestamp: " + new Date().getTime());
                        var iframeWindow = iframe.contentWindow;

                        setTimeout(function() {{
                            iframeWindow.focus();
                            try {{
                                iframeWindow.print();
                                console.log("JS (iframe creator): iframe.contentWindow.print() called.");
                            }} catch (e) {{
                                console.error("JS (iframe creator): Error calling iframe.contentWindow.print():", e);
                            }}

                            // --- MODIFIED CLEANUP BLOCK ---
                            setTimeout(function() {{
                                parentDoc.body.removeChild(iframe);
                                console.log("JS (iframe creator): Iframe removed from parent body.");
                                // NO RELOAD HERE. The iframe is removed, and the UI should remain as is.
                            }}, 500); // Reduced delay for cleanup, as no reload is happening

                        }}, 200);
                    }};
    """)

    # CRITICAL FIX: Inject the iframe.srcdoc assignment using string concatenation
    # This ensures the Python variable's VALUE (which is already a JS string literal)
    # is inserted directly, bypassing f-string parsing for its content.
    js_code_parts.append(f"                    iframe.srcdoc = {escaped_html_literal};")

    # Also fix the console.log line for content length
    js_code_parts.append(f"""
                    console.log("JS (iframe creator): iframe.srcdoc set. Content length: " + {len(html_content_to_print)} + ". Timestamp: " + new Date().getTime());

                }} else if (iframeCreationAttempts >= maxIframeCreationAttempts) {{
                    clearInterval(iframeCreationInterval);
                    console.error("JS (iframe creator): Parent document body not found after max attempts. Cannot create iframe.");
                    // window.parent.location.reload(true); // Removed reload on failure as well
                }} else {{
                    console.log("JS (iframe creator): Waiting for parent document body... Attempt " + iframeCreationAttempts);
                }}
            }}, 100);

        }})();
    </script>
    """)

    return "".join(js_code_parts)

def _slot_committed_key(slot_name):
    return f"_committed_text_{slot_name}"

def _slot_replaced_key(slot_name):
    return f"_replaced_text_{slot_name}"

def refresh_editor_widget(slot_name=None):
    """Remount the ACE editor so it picks up approved/saved text instead of a stale buffer."""
    if slot_name is None or slot_name == st.session_state.get("selected_file"):
        st.session_state.editor_key_version = st.session_state.get("editor_key_version", 0) + 1

def commit_slot_text(slot_name, new_text, replaced_text=None, remount=True):
    """Write a slot and remember the committed text so a stale editor cannot roll it back."""
    prev = replaced_text if replaced_text is not None else st.session_state.get(slot_name, "")
    new_text = "" if new_text is None else str(new_text)
    st.session_state[slot_name] = new_text
    st.session_state[_slot_committed_key(slot_name)] = new_text
    if prev is not None and normalize_harvest_text(prev) != normalize_harvest_text(new_text):
        st.session_state[_slot_replaced_key(slot_name)] = str(prev)
    if remount:
        refresh_editor_widget(slot_name)

def emit_browser_print(html_document):
    """Inject print JS in a new payload each time so Streamlit cannot reuse an old iframe."""
    n = int(st.session_state.get("print_job_nonce", 0)) + 1
    st.session_state.print_job_nonce = n
    # components.v1.html has no `key` on this Streamlit build; a unique marker
    # changes the payload so the previous print iframe is not reused.
    st.components.v1.html(
        f"<!-- gatewai-print-{n} -->{print_in_iframe_js(html_document)}",
        height=0,
        width=0,
    )

def resolve_editor_content(selected_file, ace_value):
    """Keep approved/saved text if ACE is still holding the pre-approve buffer."""
    stored = str(st.session_state.get(selected_file, "") or "")
    committed = str(st.session_state.get(_slot_committed_key(selected_file), stored) or stored)
    replaced = str(st.session_state.get(_slot_replaced_key(selected_file), "") or "")
    ace = "" if ace_value is None else str(ace_value)
    canonical = committed or stored
    if not ace:
        return canonical
    ace_n = normalize_harvest_text(ace)
    canonical_n = normalize_harvest_text(canonical)
    replaced_n = normalize_harvest_text(replaced)
    if ace_n == canonical_n:
        return ace
    if replaced_n and ace_n == replaced_n:
        refresh_editor_widget(selected_file)
        return canonical
    if stored and canonical and normalize_harvest_text(stored) == replaced_n:
        refresh_editor_widget(selected_file)
        return canonical
    if canonical and not shadow_preserves_existing(canonical, ace):
        refresh_editor_widget(selected_file)
        return canonical
    return ace

def _ace_editor_key(slot_name):
    """Widget identity includes the project so one slot name cannot reuse another's buffer."""
    version = int(st.session_state.get("editor_key_version", 0))
    project = str(st.session_state.get("active_project", ""))
    return f"ace_editor_{project}::{slot_name}::{version}"

def release_editor_for_project_switch(previous_slots=None):
    """Forget editor seeds and slot snapshots from the project we are leaving."""
    st.session_state.editor_key_version = int(st.session_state.get("editor_key_version", 0)) + 1
    for key in list(st.session_state.keys()):
        ks = str(key)
        if ks.startswith(("_ace_seed_", "_ace_fresh_", "_committed_text_", "_replaced_text_", "ace_editor_")):
            del st.session_state[key]
    for slot_name in previous_slots or []:
        if slot_name in GLOBAL_CORE:
            continue
        st.session_state.pop(slot_name, None)
        st.session_state.pop(_slot_committed_key(slot_name), None)
        st.session_state.pop(_slot_replaced_key(slot_name), None)

@st.fragment
def render_live_editor(selected_file):
    """Write the open slot from ACE without rerunning the rest of the app.

    The seed stays fixed for this widget key so a typing update cannot push a new
    value back into ACE and restart the editor. A None return is ignored so a
    remount cannot blank the slot.
    """
    if "editor_key_version" not in st.session_state:
        st.session_state.editor_key_version = 0
    editor_key = _ace_editor_key(selected_file)
    seed_key = f"_ace_seed_{editor_key}"
    fresh_key = f"_ace_fresh_{editor_key}"
    if seed_key not in st.session_state:
        # Drop prior seeds for this project+slot so file switches do not keep a
        # full copy of every earlier version.
        project = str(st.session_state.get("active_project", ""))
        stale_seed = re.compile(
            rf"^_ace_seed_ace_editor_{re.escape(project)}::{re.escape(str(selected_file))}::\d+$"
        )
        for old_key in list(st.session_state.keys()):
            if stale_seed.match(str(old_key)):
                del st.session_state[old_key]
        st.session_state[seed_key] = str(st.session_state.get(selected_file, "") or "")
        st.session_state[fresh_key] = True
    typed = st_ace(
        value=st.session_state[seed_key],
        language="markdown",
        theme="chrome",
        keybinding="vscode",
        font_size=14,
        height=500,
        show_gutter=True,
        show_print_margin=False,
        wrap=True,
        auto_update=True,
        key=editor_key,
    )
    if typed is None:
        return
    typed = str(typed)
    # A just-mounted editor must report its seed. Another project's buffer on
    # that first return is ignored so it cannot replace the loaded slot.
    if st.session_state.pop(fresh_key, False) and typed != st.session_state.get(seed_key, ""):
        return
    replaced = str(st.session_state.get(_slot_replaced_key(selected_file), "") or "")
    committed = st.session_state.get(_slot_committed_key(selected_file))
    if (
        replaced
        and committed is not None
        and normalize_harvest_text(typed) == normalize_harvest_text(replaced)
        and normalize_harvest_text(typed) != normalize_harvest_text(committed)
    ):
        st.session_state[selected_file] = str(committed)
        refresh_editor_widget(selected_file)
        # A full-app rerun here used to land before the chat prompt was sent,
        # so the box cleared and the message never reached the API.
        # While a prompt is waiting, leave the script running so that send happens.
        if st.session_state.get("held_chat_prompt"):
            return
        try:
            st.rerun(scope="fragment")
        except Exception:
            return
        return
    st.session_state[selected_file] = typed

@st.fragment
def render_editor_save(selected_file):
    """Save without rerunning the app, so the Editor tab stays open."""
    project_name = st.session_state.active_project
    if st.button("💾 Save", key="editor_save_slot"):
        content_to_save = st.session_state.get(selected_file, "")
        _proj_db, _sys_db, _scope, slot_db = database_for_slot(selected_file)
        stored = read_stored_slot(slot_db, selected_file)
        if save_replaces_most(stored or "", content_to_save):
            st.session_state.confirm_slot_overwrite = {
                "project": project_name,
                "slot": selected_file,
                "new": content_to_save,
                "old": stored or "",
            }
        else:
            save_open_slot(selected_file, content_to_save)
            st.session_state.pop("confirm_slot_overwrite", None)
            st.toast(f"Saved {selected_file}!")

    confirm = st.session_state.get("confirm_slot_overwrite")
    if (
        isinstance(confirm, dict)
        and confirm.get("project") == project_name
        and confirm.get("slot") == selected_file
    ):
        old_text = confirm.get("old") or ""
        new_text = confirm.get("new") or ""
        st.warning(
            f"This save would replace most of **{project_name} / {selected_file}**. "
            f"The stored file is {len(old_text):,} characters; the editor has {len(new_text):,}. "
            "Nothing has been written yet."
        )
        st.code(short_text_diff(old_text, new_text), language="diff")
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button("Save anyway", type="primary", use_container_width=True):
            save_open_slot(selected_file, new_text)
            st.session_state.pop("confirm_slot_overwrite", None)
            st.toast(f"Saved {selected_file}.")
        if cancel_col.button("Cancel save", use_container_width=True):
            st.session_state.pop("confirm_slot_overwrite", None)
    elif confirm and confirm.get("project") == project_name and confirm.get("slot") != selected_file:
        st.session_state.pop("confirm_slot_overwrite", None)

# --- NEW: Function to generate JS for print and reload (for iframe injection) ---
def trigger_print_and_reload_js(printable_div_id):
    js_code = f"""
    <script>
        (function() {{
            var intervalId;
            var attempts = 0;
            var maxAttempts = 20; // Try for up to 2 seconds

            function findAndPrint() {{
                attempts++;
                // CRITICAL: Explicitly target the parent window's document
                var parentDocument = window.parent.document;
                var printableDiv = parentDocument.getElementById('{printable_div_id}');

                if (printableDiv) {{
                    clearInterval(intervalId);
                    console.log("JS (iframe, parent context): Printable div found. ID: {printable_div_id}");
                    console.log("JS (iframe, parent context): Content of printableDiv.innerHTML (first 500 chars):", printableDiv.innerHTML.substring(0, 500));

                    // Ensure the printable div is explicitly visible for printing
                    // The CSS @media print block will handle hiding everything else.
                    printableDiv.style.display = 'block';
                    printableDiv.style.visibility = 'visible';
                    printableDiv.style.position = 'fixed'; // Float it above everything
                    printableDiv.style.top = '0';
                    printableDiv.style.left = '0';
                    printableDiv.style.width = '100%';
                    printableDiv.style.height = '100vh'; // Take full viewport height for print
                    printableDiv.style.zIndex = '99999';
                    printableDiv.style.backgroundColor = 'white'; // Ensure white background
                    printableDiv.style.color = 'black'; // Ensure black text

                    // CRITICAL FIX: Introduce a small delay to allow the browser to apply CSS and render the div
                    // before triggering the print dialog. This is often crucial for print engines.
                    setTimeout(function() {{
                        // Trigger print dialog on the parent window
                        try {{
                            window.parent.print();
                            console.log("JS (iframe, parent context): window.parent.print() called after 200ms delay.");
                        }} catch (e) {{
                            console.error("JS (iframe, parent context): Error calling window.parent.print():", e);
                        }}

                        // --- CLEANUP ---
                        // Restore original styles (or simply let the reload handle it)
                        // It's safer to let the reload handle full restoration.
                        // We do need to hide the div immediately after print is triggered
                        printableDiv.style.display = 'none';
                        printableDiv.style.visibility = 'hidden';
                        printableDiv.style.position = 'static'; // Reset position
                        printableDiv.style.width = 'auto';
                        printableDiv.style.height = 'auto';
                        printableDiv.style.zIndex = 'auto';

                        // Trigger a hard reload of the parent page after print dialog is dismissed
                        setTimeout(function() {{
                            try {{
                                window.parent.location.reload(true); // Force reload, bypassing cache
                                console.log("JS (iframe, parent context): window.parent.location.reload(true) called.");
                            }} catch (e) {{
                                console.error("JS (iframe, parent context): Error calling window.parent.location.reload(true):", e);
                            }}
                            // Clean up this iframe itself after triggering reload
                            var selfIframe = window.frameElement;
                            if (selfIframe && selfIframe.parentNode) {{
                                selfIframe.parentNode.removeChild(selfIframe);
                            }}
                            console.log("JS (iframe, parent context): Cleanup attempted.");
                        }}, 10000); // Give some time for print dialog interaction

                    }}, 10000); // 200ms delay before printing to allow CSS to settle

                }} else if (attempts >= maxAttempts) {{
                    clearInterval(intervalId);
                    console.error("JS (iframe, parent context): Printable div with ID '{printable_div_id}' NOT FOUND in parent DOM after max attempts.");
                    // Remove this iframe itself if it failed to find the element
                    var selfIframe = window.frameElement;
                    if (selfIframe && selfIframe.parentNode) {{
                        selfIframe.parentNode.removeChild(selfIframe);
                    }}
                }} else {{
                    console.log("JS (iframe, parent context): Printable div NOT FOUND (attempt " + attempts + "/" + maxAttempts + "). Retrying...");
                }}
            }}

            // Start polling
            intervalId = setInterval(findAndPrint, 100); // Check every 100ms
            console.log("JS (iframe, parent context): Print sequence initiated from iframe. Printable Div ID: {printable_div_id}");
        }})();
    </script>
    """
    return js_code
# --- END NEW HELPER FUNCTION ---

# --- DEBUGGING SHADOW DISPATCHER (TIME-CERTAINTY MATRIX) ---
# --- SECTION 9: THE SCOPE-AWARE SHADOW DISPATCHER (OMNI-ROUTER & DUAL PROFILE) ---
def resolve_shadow_target(name, valid_targets):
    """Match a router name to a real slot, including a missing or extra .md."""
    text = str(name or "").strip()
    if text in valid_targets:
        return text
    folded = {slot.casefold(): slot for slot in valid_targets}
    key = text.casefold()
    if key in folded:
        return folded[key]
    if not key.endswith(".md") and (key + ".md") in folded:
        return folded[key + ".md"]
    if key.endswith(".md") and key[:-3] in folded:
        return folded[key[:-3]]
    return None

def mark_auto_shadow_complete():
    """Record that auto-shadow has caught up through the current chat length."""
    st.session_state.pending_auto_shadow = False
    st.session_state.last_auto_shadow_at_msg_count = len(st.session_state.get("messages") or [])
    clear_pending_shadow()


def needs_auto_shadow_catchup():
    """True when chat already saved a model reply but auto-shadow never finished.

    Common on mobile: the request dies after save_project_state and before/during
    the shadow pass; answered_already then blocks a chat retry.
    """
    if not st.session_state.get("auto_shadow_active", True):
        return False
    if st.session_state.get("harvest_in_progress", False):
        return False
    messages = st.session_state.get("messages") or []
    if not messages:
        return False
    last = messages[-1]
    if not (isinstance(last, dict) and last.get("role") == "model"):
        return False
    if st.session_state.get("pending_auto_shadow") or read_pending_shadow():
        return True
    last_n = int(st.session_state.get("last_auto_shadow_at_msg_count") or 0)
    return len(messages) > last_n


def run_auto_shadow_turn(temp_val=None, top_p_val=None):
    """Run one gated auto-shadow pass. Returns (logs, any_staged, completed).

    completed=True means we should not retry for this message count (ran or
    intentionally skipped because Auto Shadow is off). Deferrals keep completed=False.
    """
    shadow_logs = []
    any_staged = False
    if not st.session_state.get("auto_shadow_active", True):
        shadow_logs.append("⏸️ Skipped: Auto Shadow is off. Use Run Shadow Now when you want a pass.")
        return shadow_logs, False, True
    if st.session_state.get("pending_updates") and len(st.session_state.pending_updates) > 0:
        shadow_logs.append("⏳ Skipped: Pending updates exist. Shadow deferring until Staging Area is clear.")
        return shadow_logs, False, False
    if st.session_state.get("harvest_in_progress", False):
        shadow_logs.append("⏳ Skipped: Harvest In Progress.")
        return shadow_logs, False, False

    try:
        shadow_logs = ["🕵️ Shadow: Starting Loop..."]
        current_state_dict = {k: st.session_state.get(k, "") for k in CORE_SCHEMA.values()}
        if "loaded_slots_metadata" in st.session_state:
            for k, meta in st.session_state.loaded_slots_metadata.items():
                if meta.get("slot_type") == "user":
                    current_state_dict[k] = st.session_state.get(k, "")

        last_harvest_idx = st.session_state.get("last_harvest_message_index", 0)
        history_delta = st.session_state.messages[last_harvest_idx:]
        recent_history = [m for m in history_delta if "**🕵️ SHADOW LOGS**" not in m["content"]]
        s_eng = st.session_state.get("shadow_engine_set", "gemini-2.5-flash-lite")
        if temp_val is None:
            temp_val = st.session_state.get("temp_val", 0.7)
        if top_p_val is None:
            top_p_val = st.session_state.get("top_p_val", 0.9)

        st.session_state.role_stats["shadow"] += 1
        updates, logs_str = run_shadow_dispatcher(
            recent_history,
            current_state_dict,
            st.session_state.loaded_slots_metadata,
            st.session_state.domain_content,
            st.session_state.gemini_api_key,
            temp_val,
            top_p_val,
            s_eng,
        )
        shadow_logs.append(logs_str)

        if updates:
            is_review_active = st.session_state.get("review_mode_active", True)
            for k, update_data in updates.items():
                v_str = update_data["content"]
                is_suspect_from_shadow = update_data.get("suspect", False)
                current_val = st.session_state.get(k, "")
                is_different = False

                if isinstance(v_str, (dict, list)):
                    v_str_norm = json.dumps(v_str, indent=2)
                else:
                    v_str_norm = str(v_str)

                try:
                    obj_new = json.loads(v_str_norm)
                    obj_curr = json.loads(str(current_val))
                    if obj_new != obj_curr:
                        is_different = True
                except Exception:
                    clean_v = v_str_norm.strip()
                    if clean_v.startswith("---"):
                        clean_v = clean_v[3:].lstrip()
                    if clean_v.endswith("---"):
                        clean_v = clean_v[:-3].rstrip()
                    clean_current = str(current_val).strip()
                    if clean_current.startswith("---"):
                        clean_current = clean_current[3:].lstrip()
                    if clean_current.endswith("---"):
                        clean_current = clean_current[:-3].rstrip()
                    if clean_v != clean_current:
                        is_different = True

                if not is_different:
                    shadow_logs.append(f"💤 {k}: Content identical, skipping update.")
                    continue

                ai_managed = st.session_state.loaded_slots_metadata.get(k, {}).get("ai_managed", False)
                current_val_for_audit = st.session_state.get(k, "")
                if ai_managed and not shadow_preserves_existing(current_val_for_audit, v_str):
                    shadow_logs.append(
                        f"🛑 {k}: Discarded Shadow rewrite that would revert the current file."
                    )
                    continue

                if is_review_active or is_suspect_from_shadow:
                    if "pending_updates" not in st.session_state:
                        st.session_state.pending_updates = {}
                    st.session_state.pending_updates[k] = {
                        "content": v_str,
                        "suspect": is_suspect_from_shadow,
                    }
                    shadow_logs.append(
                        f"📝 STAGED: {k} queued for review (Review Mode ON or Suspect)."
                    )
                    # Skip per-slot toasts on mobile — they shove the viewport after submit.
                    if not is_mobile_client():
                        st.toast(f"Shadow proposed update to {k}")
                    any_staged = True
                elif not is_review_active and ai_managed and not is_suspect_from_shadow:
                    proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
                    sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
                    meta = st.session_state.loaded_slots_metadata.get(k, {})
                    edit_summary = "AI-generated edit (Shadow auto-commit)"
                    db.save_slot(
                        proj_db,
                        sys_db,
                        k,
                        v_str,
                        meta.get("slot_type", "user"),
                        meta.get("description", ""),
                        meta.get("scope", "project"),
                        meta.get("owner_id", "system"),
                        meta.get("is_distributed", False),
                        meta.get("part_of_dtn", False),
                        ai_managed=True,
                        old_content=current_val_for_audit,
                        edit_summary=edit_summary,
                    )
                    commit_slot_text(k, v_str, replaced_text=current_val_for_audit)
                    shadow_logs.append(
                        f"🤖 AI-Managed AUTO-COMMIT: {k} updated directly (Review Mode OFF)."
                    )
                else:
                    shadow_logs.append(
                        f"💤 {k}: Not AI-managed, Review Mode OFF, and not suspect. Shadow took no action."
                    )
        else:
            shadow_logs.append("💤 No updates required.")
        if any_staged:
            # Must hit disk before any st.rerun(); mobile reconnects often drop session.
            persist_pending_updates_now()
            shadow_logs.append(
                f"💾 Persisted {len(st.session_state.get('pending_updates') or {})} staged proposal(s) to disk."
            )
        return shadow_logs, any_staged, True
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "ResourceExhausted" in err_msg:
            shadow_logs.append("🚦 429 Quota Hit - Skipping Shadow.")
            st.warning("AI rate limit reached for Shadow. Please try again in a moment.")
        else:
            shadow_logs.append(f"💀 CRASH (Auto Shadow): {err_msg}")
            print(f"ERROR: Auto Shadow: {err_msg}")
            st.warning("Shadow hit an unexpected error. It will retry on the next refresh.")
        # Leave incomplete so catch-up can retry (except soft 429 — still retry later).
        return shadow_logs, False, False


def run_shadow_dispatcher(history_list, current_state, loaded_metadata, domain_context, api_key, temp, top_p, engine_name="gemini-2.5-flash-lite"):
#    print(f"start run_shadow_dispatcher (first 200 chars): {str(history_list)[:200]}...") # Print first 200 chars
    logs = []
    logs.append(f"🕵️ Shadow: Started using {engine_name}")

    def safe_read(response):
        try: return response.text
        except: return ""
    # 1. FETCH DYNAMIC INVENTORY
    available_slots = list(current_state.keys())

    # PROTECTION:
    # 1. Exclude SI_Identity (System Brain).
    # 2. Exclude SI_User_Profile (User's Manual Bio - READ ONLY for Shadow).
    # 3. Allow SI_Observed_Profile (AI's Observations - WRITE ONLY for Shadow).
    # --- CRITICAL FIX: Shadow's targets are strictly limited to metacognitive slots ---
    METAPROPOSAL_TARGETS = [
        "MI_Project_Status",
        "MC_Focus",
        "SI_Observed_Profile",
        "MC_Blindspots",
        "MC_Assumptions",
        "MC_Frontier"
    ]
    # System slots, plus existing AI-managed user files. Never invent a user file.
    # Uploaded sources stay out unless they are marked AI-managed.
    valid_targets = [k for k in available_slots if k in METAPROPOSAL_TARGETS]
    user_target_lines = []
    for slot_name, meta in loaded_metadata.items():
        if meta.get('slot_type') == 'user' and meta.get('ai_managed', False):
            if slot_name not in valid_targets:
                valid_targets.append(slot_name)
            desc = " ".join(str(meta.get("description") or "").split())[:140]
            user_target_lines.append(f"- {slot_name}: {desc}" if desc else f"- {slot_name}")
    user_target_block = "\n".join(user_target_lines) if user_target_lines else "(none)"
    if user_target_lines:
        logs.append(f"User files eligible: {len(user_target_lines)}")

    # --- END CRITICAL FIX ---

    clean_history = ""
    if isinstance(history_list, list):
        for msg in history_list:
            if "**🕵️ SHADOW LOGS**" not in msg['content']:
                clean_history += f"[{msg['role'].upper()}]: {msg['content']}\n\n"
    else: clean_history = str(history_list)

    try:
        shadow_client = get_llm_client(engine_name, api_key=api_key)

        # 3. Triage (THE OMNI-ROUTER)
        triage_prompt = (
            f"You are the Data Router. Your domain lens is: {domain_context}\n\n"
            "Map the interaction to the correct Memory Slots.\n\n"
            f"--- SYSTEM SLOTS ---\n{json.dumps([k for k in valid_targets if k in METAPROPOSAL_TARGETS], indent=2)}\n\n"
            f"--- AI-MANAGED USER FILES ---\n{user_target_block}\n\n"
            f"--- INTERACTION ---\n{clean_history}\n"
            "-----------------------\n"
            "ROUTING LOGIC (Scan for IMPLICIT data):\n"
            "1. AI-MANAGED USER FILES: Include one when this interaction adds or revises material that belongs in that file, using its name and description. The user does not have to say the filename. Do NOT include a file just because it exists or the topic is only loosely related. NEVER invent a filename. Only names from the list above are allowed.\n"
            "2. IF WORKFLOW CHANGE -> [MI_Project_Status]\n"
            "3. IF IMMEDIATE FOCUS/NEXT STEPS/QUESTIONS -> [MC_Focus]\n"
            "4. IF USER PREFERENCE/HABIT -> [SI_Observed_Profile] (e.g. 'User hates prose summaries')\n"
            "5. IF LOGIC GAP -> [MC_Blindspots] (What are we ignoring?)\n"
            "6. IF UNSTATED PREMISE -> [MC_Assumptions] (What are we taking for granted?)\n"
            "7. IF ACTIVE RISKS, STRATEGIC UNKNOWNS, OR KEY OPEN QUESTIONS -> [MC_Frontier]\n\n"
            "CRITICAL CONSTRAINTS:\n"
            "- MULTI-SLOT OK: If an event touches several files, list them.\n"
            "- Next steps and immediate questions go to MC_Focus, not MC_Frontier.\n"
            "- Strategic risks and long-open unknowns go to MC_Frontier, not MC_Focus.\n"
            "- FORMAT: OUTPUT ONLY A JSON LIST.\n"
            "DECISION (Example: [\"MI_Project_Status\", \"MC_Focus\", \"MC_Blindspots\"]):"
        )
        triage_resp = shadow_client.models.generate_content(
            model=engine_name,
            contents=triage_prompt,
            config={
                "temperature": temp,
                "top_p": top_p
                }
        )

        triage_text = safe_read(triage_resp).strip()

        logs.append(f"🧠 Shadow Thought: {triage_text}")

        match = re.search(r"\[.*\]", triage_text, re.DOTALL)
        if not match: return {}, "\n".join(logs)

        try:
            raw_targets = json.loads(match.group(0))
            # Only proceed with targets that are explicitly allowed.
            targets = []
            for raw_name in raw_targets:
                resolved = resolve_shadow_target(raw_name, valid_targets)
                if resolved and resolved not in targets:
                    targets.append(resolved)
            if len(targets) < len(raw_targets):
                logs.append(f"⚠️ Shadow: Filtered out invalid target(s) from triage output. Original: {raw_targets}, Filtered: {targets}")
            if not targets:
                targets = [t for t in ("MC_Focus",) if t in valid_targets]
                if targets:
                    logs.append("⚠️ Shadow: Triage listed nothing; defaulting to MC_Focus")
        except json.JSONDecodeError:
            logs.append(f"💀 Shadow: Failed to parse JSON from triage output: {triage_text}")
            return {}, "\n".join(logs)
        except Exception as e:
            logs.append(f"💀 Shadow: Unexpected error during triage target processing: {e}")
            return {}, "\n".join(logs)

        mr_meta = loaded_metadata.get('MI_Master_Record', {})
        is_master_record_distributed = mr_meta.get('is_distributed', False)
        logs.append(f"🌐 DTN State Check: MI_Master_Record is_distributed = {is_master_record_distributed}")

        updates = {}
        for target in targets:
            # --- DEBUG: Check loaded_slots_metadata for the current target ---
            target_meta = loaded_metadata.get(target, {})
            logs.append(f"DEBUG SHADOW: Metadata for '{target}': {target_meta}")
            # --- END DEBUG ---
            logs.append(f"✏️ Updating {target}...")
            current_content = current_state.get(target, '')

            if st.session_state.get('review_mode_active', True) and \
               target in st.session_state.get('pending_updates', {}):
                logs.append(f"⚠️ Shadow: Skipping {target} as it already has a pending update for review.")
                continue

            instr = shadow_update_instructions(
                target, loaded_metadata, current_state, is_master_record_distributed
            )
            if target == "MC_Focus":
                interaction = clip_recent_text(clean_history, 3500)
            elif target == "MC_Frontier":
                interaction = clip_recent_text(clean_history, 6000)
            else:
                interaction = clean_history
            p = (
            f"--- JOB: UPDATE {target} ---\n"
            f"{instr}\n\n"
            f"INTERACTION:\n{interaction}\n\n"
            f"CURRENT CONTENT ({target}):\n{current_content}\n"
            "-----------------------\n"
            "OUTPUT CONTENT BELOW THIS LINE:\n"
            )
            #print(f"\nDEBUG SHADOW AI_MANAGED: Full prompt being sent to model for '{target}':\n{p[:1000]}...\n") # Print first 1000 chars of the prompt

            upd_resp = shadow_client.models.generate_content(
                model=engine_name,
                contents=p,
                config={
                    "temperature": temp,
                    "top_p": top_p
                    }
            )
            raw_text = safe_read(upd_resp).strip()
            #print(f"DEBUG SHADOW AI_MANAGED: Raw AI response for '{target}' (first 500 chars):\n{raw_text[:500]}...\n")
            # --- DEDICATED SHADOW OUTPUT CLEANER ---
            # This function removes prompt leaks and formatting artifacts specifically from the Shadow Engine's output
            # before it ever reaches the Staging Area or Smart Diff logic.
            def clean_shadow_output(s, target_slot_name): # target_slot_name is kept for logging/future flexibility
                clean_s = str(s).strip()

                # Define the universal delimiter we expect from the prompt
                output_delimiter = "OUTPUT CONTENT BELOW THIS LINE:"

                # Look for the delimiter in the AI's raw output
                start_idx = clean_s.find(output_delimiter)

                if start_idx != -1:
                    # If found, return everything *after* the delimiter, stripped.
                    content_after_delimiter = clean_s[start_idx + len(output_delimiter):].strip()
#                    print(f"DEBUG CLEAN_SHADOW: Delimiter FOUND for {target_slot_name}. Content after: {content_after_delimiter[:200]}...") # NEW DEBUG

                    # Remove any leading/trailing markdown code fences that might still appear
                    # This is a final safeguard, as the delimiter should ideally prevent this.
                    if content_after_delimiter.lower().startswith("```json"):
                        content_after_delimiter = content_after_delimiter[len("```json"):].lstrip()
                    elif content_after_delimiter.lower().startswith("```"):
                        content_after_delimiter = content_after_delimiter[3:].lstrip()

                    if content_after_delimiter.lower().endswith("```"):
                        content_after_delimiter = content_after_delimiter[:-3].rstrip()

                    return content_after_delimiter.strip()
                else:
                    # --- NEW: FALLBACK MECHANISM IF DELIMITER IS NOT FOUND ---
                    print(f"WARNING: Delimiter NOT FOUND for {target_slot_name}. Attempting fallback cleaning. Raw output:\n{clean_s[:500]}...")
                    
                    # Assume the entire content is the intended output, then apply general cleaning
                    fallback_content = clean_s

                    # Remove common leading/trailing artifacts that models sometimes include
                    fallback_content = re.sub(r"```(?:json|markdown|python)?\n", "", fallback_content, flags=re.IGNORECASE).strip()
                    fallback_content = re.sub(r"\n```", "", fallback_content).strip()
                    fallback_content = re.sub(r"OUTPUT \(RAW TEXT ONLY\):", "", fallback_content, flags=re.IGNORECASE).strip()
                    fallback_content = re.sub(r"--- JOB: UPDATE .*? ---", "", fallback_content, flags=re.IGNORECASE | re.DOTALL).strip()
                    fallback_content = re.sub(r"INSTRUCTIONS:.*?(?=INTERACTION:)", "", fallback_content, flags=re.IGNORECASE | re.DOTALL).strip()
                    fallback_content = re.sub(r"INTERACTION:.*?(?=CURRENT CONTENT:)", "", fallback_content, flags=re.IGNORECASE | re.DOTALL).strip()
                    fallback_content = re.sub(r"CURRENT CONTENT \(.*\):", "", fallback_content, flags=re.IGNORECASE | re.DOTALL).strip()
                    fallback_content = re.sub(r"-----------------------", "", fallback_content, flags=re.IGNORECASE | re.DOTALL).strip()

                    # CRITICAL: If the delimiter is NOT found, it means the AI completely failed
                    # to follow the most basic output instruction. This is a severe error.
                    # We log this for debugging and return an empty string to prevent pollution.
                    print(f"ERROR: Shadow output for {target_slot_name} did not contain the expected delimiter: '{output_delimiter}'. Raw output:\n{clean_s[:500]}...") # Log first 500 chars
                    #print(f"DEBUG CLEAN_SHADOW: Delimiter NOT FOUND for {target_slot_name}. Raw output:\n{clean_s[:500]}...") # NEW DEBUG
                    return fallback_content # Return the cleaned fallback content instead of empty string

            cleaned_text = clean_shadow_output(raw_text, target)
            print(f"DEBUG SHADOW AI_MANAGED: Cleaned text for '{target}' (first 500 chars):\n{cleaned_text[:500]}...\n")

            # --- NEW LOGIC: DISREGARD PROPOSAL IF DELIMITER IS MISSING ---
            if not cleaned_text.strip():
                logs.append(f"❌ Shadow: Disregarding update for '{target}'. Delimiter 'OUTPUT CONTENT BELOW THIS LINE:' was missing in AI response, or content was empty after cleaning.")
                continue # Skip to the next target in the loop, effectively disregarding this proposal
            # --- END NEW LOGIC ---

            is_ai_managed_target = (
                target in loaded_metadata
                and loaded_metadata[target].get("slot_type") == "user"
                and loaded_metadata[target].get("ai_managed", False)
            )
            if is_ai_managed_target and not shadow_preserves_existing(current_content, cleaned_text):
                logs.append(
                    f"🛑 Shadow: Discarded '{target}' rewrite — it dropped too much of the current file "
                    f"(would revert to an earlier/partial version)."
                )
                continue

            def shadow_slot_now(name):
                pending = updates.get(name)
                if isinstance(pending, dict) and pending.get("content"):
                    return pending["content"]
                return current_state.get(name, "")

            copied_from = shadow_copies_other_slot(target, cleaned_text, shadow_slot_now)
            if copied_from and normalize_harvest_text(cleaned_text) == normalize_harvest_text(shadow_slot_now(copied_from)):
                logs.append(
                    f"🛑 Shadow: Discarded '{target}' — proposal was an exact copy of {copied_from}."
                )
                continue

            # --- NEW: SUSPECT UPDATE ANALYSIS ---
            is_suspect = False
            suspect_reason = ""

            # 1. Significant Truncation/Loss (Example: if new content is drastically shorter)
            if len(cleaned_text) < len(current_content) * 0.5 and len(current_content) > 100: # If new is less than half of old (and old was substantial)
                is_suspect = True
                suspect_reason = "Significant truncation detected."
                logs.append(f"🚩 SUSPECT ({target}): {suspect_reason}")

            # 2. Major Structural Changes (Requires more advanced analysis - for now, rely on explicit prompts)
            # This is harder to detect generically without diffing, but we can look for deletions of core metadata keys.
            # For now, we'll rely on other checks.

            # 3. Contradictory Inferences (Requires more advanced analysis - for now, rely on explicit prompts)
            # We'll assume the AI's instructions are sufficient for now.

            # 4. Unexplained Content Additions (If new content is long and not clearly linked to interaction)
            if len(cleaned_text) > 500 and not any(keyword in clean_history.lower() for keyword in ["suggest", "propose", "add", "update", target.lower()]):
                is_suspect = True
                suspect_reason = "Large, unexplained content addition."
                logs.append(f"🚩 SUSPECT ({target}): {suspect_reason}")

            # --- STORE UPDATE AND SUSPECT FLAG ---
            updates[target] = {"content": cleaned_text, "suspect": is_suspect} # MODIFIED: Store as dict with content and suspect flag
            if is_suspect:
                logs.append(f"✅ Flagged '{target}' as suspect: {suspect_reason}")
            else:
                logs.append(f"✅ Shadow: Processed output for {target} using universal delimiter extraction.")
        return updates, "\n".join(logs) # MODIFIED: Return updates dict which now includes 'suspect' flag


            # --- NEW: Robust content extraction for Shadow output ---
#            updates[target] = extract_content_from_ai_output(cleaned_text)
            #updates[target] = cleaned_text # Shadow output should now be strictly raw text
            #logs.append(f"✅ Shadow: Processed output for {target} using universal delimiter extraction.")       #     logs.append(f"✅ Shadow: Processed output for {target} using content extractor.")
        return updates, "\n".join(logs)
    except Exception as e:
        return {}, f"💀 Crash: {e}"

# --- 4. SESSION INITIALIZATION (With Global Persistence) ---
#print("DEBUG: About to initialize session state. NPA") # <-- ADD THIS LINE
# --- NEW: Global Domain Lens & Content Initialization ---
if 'selected_lens_name' not in st.session_state:
    # Attempt to load default or first available domain
    domain_files_init = [f.replace(".txt", "") for f in os.listdir(DOMAINS_DIR) if f.endswith(".txt")]
    st.session_state.selected_lens_name = "General" if "General" in domain_files_init else (domain_files_init if domain_files_init else "")
if 'domain_content' not in st.session_state:
    st.session_state.domain_content = load_domain_content(st.session_state.selected_lens_name)
# --- END NEW ---

SESSION_FILE = os.path.join(BASE_SYSTEM_DIR, "last_session.json")

# 1. Attempt to Load Last Active Project from Disk
if "active_project" not in st.session_state:
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                data = json.load(f)
                # Verify the folder still exists before loading
                last_proj = data.get("last_project", "General")
                if os.path.isdir(os.path.join(BASE_PROJECT_DIR, last_proj)):
                    st.session_state.active_project = last_proj
                else:
                    st.session_state.active_project = "General"
        except:
            st.session_state.active_project = "General"
    else:
        st.session_state.active_project = "General"

if "req_count" not in st.session_state: st.session_state.req_count = 0

# Track usage by Role
if "role_stats" not in st.session_state:
    st.session_state.role_stats = {"chat": 0, "ingest": 0, "shadow": 0}

if "show_preview" not in st.session_state: st.session_state.show_preview = False
if "show_html_preview" not in st.session_state: st.session_state.show_html_preview = False # <-- ADD THIS LINE
# --- NEW: Session state for Markdown Preview ---
#if 'ai_response_generated_for_current_turn' not in st.session_state:
st.session_state.ai_response_generated_for_current_turn = False
# Harvest runs inside its button and clears this before it returns.
# A True value on a later run is leftover. It was rerunning the page
# instead of sending the chat message.
st.session_state["harvest_in_progress"] = False
#else 
if 'show_markdown_preview' not in st.session_state: st.session_state.show_markdown_preview = False
# --- END NEW ---
if "temp_web_ctx" not in st.session_state: st.session_state.temp_web_ctx = ""
if "auto_deselect_list" not in st.session_state: st.session_state.auto_deselect_list = []
# --- SMART LOADER (Configuration Hydration) ---
# We track which project is currently loaded in RAM
#print("DEBUG: About to perform Smart Loader for project state.") # <-- ADD THIS LINE

if "loaded_project" not in st.session_state: st.session_state.loaded_project = None

# _chat_submit_this_run must be same-run only. Streamlit persists session_state
# across reruns; a stuck True skipped reconcile and left "Sending…" forever.
st.session_state.pop("_chat_submit_this_run", None)

# One-time per browser session: delete all stale pending_turn.json files.
if not st.session_state.get("_wiped_pending_turns"):
    wipe_all_pending_turns()
    st.session_state._wiped_pending_turns = True

# Only load from disk if we switched projects or just started
if st.session_state.loaded_project != st.session_state.active_project:
    # Drop the previous project's slots and the editor buffer before disk load.
    # The ACE widget key used to ignore the project, so a switch could paint the
    # previous Master Record and then save it over the new one.
    previous_slots = list(st.session_state.get("loaded_slots_metadata", {}).keys())
    release_editor_for_project_switch(previous_slots)

    print(f"DEBUG LOAD STATE: Active Project: {st.session_state.active_project}")
    proj_db_path_debug = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
    sys_db_path_debug = db.get_system_db_path(BASE_SYSTEM_DIR)
    print(f"DEBUG LOAD STATE: Project DB Path: {proj_db_path_debug}")
    print(f"DEBUG LOAD STATE: System DB Path: {sys_db_path_debug}")
    state = load_project_state(st.session_state.active_project)
#    print("DEBUG: load_project_state completed.") # <-- ADD THIS LINE


    # 1. Restore Standard State
    for k, v in state.items():
        if k != "config":
            st.session_state[k] = v

    # 2. Restore Config State
    if "config" in state:
        cfg = state["config"]
        st.session_state.chat_engine_set = cfg.get("chat_engine", "gemini-2.5-flash")
        st.session_state.ingest_engine_set = cfg.get("ingest_engine", "gemini-2.5-pro")
        st.session_state.harvest_engine_set = cfg.get("harvest_engine", "gemini-2.5-pro")
        st.session_state.shadow_engine_set = cfg.get("shadow_engine", "gemini-2.5-flash-lite")
        st.session_state.transcribe_engine_set = cfg.get("transcribe_engine", "gemini-2.5-flash-lite")

        # --- CRITICAL FIX: Ensure selected_lens_name is always a string ---
        loaded_lens = cfg.get("domain_lens", "General")
        if isinstance(loaded_lens, list):
            # If it's a list (corrupted state), try to take the first element or default to "General"
            if loaded_lens:
                st.session_state.selected_lens_name = str(loaded_lens[0])
            else:
                st.session_state.selected_lens_name = "General"
        else:
            # Otherwise, ensure it's a string (handles str or None gracefully)
            st.session_state.selected_lens_name = str(loaded_lens)

        # Also ensure domain_content is reloaded correctly based on the (now fixed) selected_lens_name
        st.session_state.domain_content = load_domain_content(st.session_state.selected_lens_name)
        # --- END CRITICAL FIX ---

        st.session_state.temp_val = cfg.get("temperature", 0.7)
        st.session_state.top_p_val = cfg.get("top_p", 0.9)
        st.session_state.memory_depth_val = cfg.get("memory_depth", 3)
        st.session_state.compaction_threshold = cfg.get("compaction_threshold", 50000)
        st.session_state.last_harvest_message_index = int(cfg.get("last_harvest_message_index", 0) or 0)
        st.session_state.review_mode_active = cfg.get("review_mode_active", True)
        st.session_state.auto_shadow_active = cfg.get("auto_shadow_active", True)
        st.session_state[f"auto_shadow_toggle::{st.session_state.active_project}"] = bool(
            st.session_state.auto_shadow_active
        )
        # Shared right Memory-slots rail (migrate older per-tab prefs).
        _slots_open = bool(
            cfg.get(
                "show_memory_slots",
                cfg.get("gatewai_show_slots", cfg.get("show_file_list", False)),
            )
        )
        st.session_state.show_memory_slots_pref = _slots_open
        st.session_state.show_memory_slots = _slots_open
        st.session_state.memory_slots_width_pref = _clamp_rsb_width_px(
            cfg.get("memory_slots_width", _RSB_DEFAULT_WIDTH_PX)
        )
        st.session_state.memory_slots_width = st.session_state.memory_slots_width_pref
         # --- DEBUG: Print session state after loading ---
#        print(f"DEBUG LOAD_PROJECT_STATE: st.session_state.last_harvest_message_index set to {st.session_state.last_harvest_message_index}")
    else:
        st.session_state.last_harvest_message_index = 0
    # The rewind box used one widget for every project, so it kept showing
    # the previous project's pointer after a switch.
    st.session_state.pop("harvest_pointer_input", None)
    st.session_state.pending_updates = read_pending_updates(st.session_state.active_project)
    try:
        st.session_state._pending_updates_snapshot = json.dumps(
            st.session_state.pending_updates, sort_keys=True, default=str
        )
    except Exception:
        st.session_state._pending_updates_snapshot = ""
    # Baseline so opening a project does not re-shadow the whole history —
    # unless a prior request died after the chat reply and before shadow finished.
    pending_shadow = read_pending_shadow(st.session_state.active_project)
    if pending_shadow:
        after = int(pending_shadow.get("after_msg_count") or 0)
        st.session_state.pending_auto_shadow = True
        st.session_state.last_auto_shadow_at_msg_count = max(0, after - 1) if after else 0
        set_shadow_debug_logs([
            "⏳ Reconnect: chat reply was saved but auto-shadow never finished.",
            "Catch-up will run on this page load.",
            f"(pending after_msg_count={after})",
        ])
    else:
        st.session_state.last_auto_shadow_at_msg_count = len(st.session_state.get("messages") or [])
        st.session_state.pending_auto_shadow = False
    st.session_state.loaded_project = st.session_state.active_project
    remember_engine_room_snapshot(st.session_state.active_project)
    loaded_names = set(st.session_state.get("loaded_slots_metadata", {}).keys())
    if st.session_state.get("selected_file") not in loaded_names:
        st.session_state.selected_file = CORE_SCHEMA.get("summary.txt", "MI_Master_Record")
        st.session_state.last_selected_file_for_editor = st.session_state.selected_file
    # Drop stale pending_turn left after successful replies (common on mobile restart).
    if reconcile_stale_chat_turn():
        st.rerun()

# Staged proposals survive a browser restart. Harvest stores them in the
# session and reruns; the next run copies that session onto disk.
if st.session_state.loaded_project == st.session_state.active_project:
    current_pending = st.session_state.get("pending_updates") or {}
    if not isinstance(current_pending, dict):
        current_pending = {}
    try:
        pending_snapshot = json.dumps(current_pending, sort_keys=True, default=str)
    except Exception:
        pending_snapshot = None
    if pending_snapshot is not None and pending_snapshot != st.session_state.get("_pending_updates_snapshot"):
        write_pending_updates(st.session_state.active_project, current_pending)
        st.session_state._pending_updates_snapshot = pending_snapshot

adopt_saved_engine_room_if_needed(st.session_state.active_project)

# Every run: scrub orphan pending_turn / dangling user bubbles in-place.
# Do NOT st.rerun() here — a rerun before st.chat_input drops the user's Send.
reconcile_stale_chat_turn()

# Read the chat box before the sidebar. A sidebar rerun used to clear the
# box before this widget ran, so the message never reached the API.
MAIN_CHAT_INPUT_KEY = "main_chat_input"
dictation_notice = st.session_state.pop("dictation_notice", None)
if dictation_notice:
    level, message = dictation_notice
    if level == "error":
        st.error(message)
    else:
        st.caption(message)

if "main_chat_input_nonce" not in st.session_state:
    st.session_state.main_chat_input_nonce = 0
# Cookie draft restore disabled: on mobile it re-filled the chat box with a
# stale "partial prompt" after successful sends (desktop rarely hit this).
# Draft is still saved to localStorage on phone-lock for crash recovery, but
# we no longer push it back into st.chat_input automatically.
st.session_state._chat_draft_cookie_applied = True
st.session_state.pop("pending_chat_draft", None)
# The key stays put on a normal send. Changing it at the start of a run
# drops the submission the user just made, and the box comes back empty.
if "pending_chat_dictation" in st.session_state:
    st.session_state.main_chat_input_nonce += 1
    chat_input_key = f"{MAIN_CHAT_INPUT_KEY}_{st.session_state.main_chat_input_nonce}"
    st.session_state[chat_input_key] = st.session_state.pop("pending_chat_dictation")
elif "pending_chat_draft" in st.session_state:
    st.session_state.main_chat_input_nonce += 1
    chat_input_key = f"{MAIN_CHAT_INPUT_KEY}_{st.session_state.main_chat_input_nonce}"
    st.session_state[chat_input_key] = st.session_state.pop("pending_chat_draft")
else:
    chat_input_key = f"{MAIN_CHAT_INPUT_KEY}_{st.session_state.main_chat_input_nonce}"

submitted = st.chat_input(
    "Enter or dictate a message...",
    accept_audio=True,
    key=chat_input_key,
)
if submitted is not None:
    if isinstance(submitted, str):
        typed = submitted.strip()
    else:
        typed = str(getattr(submitted, "text", None) or "").strip()
        if not typed and hasattr(submitted, "get"):
            typed = str(submitted.get("text") or "").strip()
        audio = getattr(submitted, "audio", None)
        if audio is None and hasattr(submitted, "get"):
            audio = submitted.get("audio")
        if audio and not typed:
            try:
                with st.spinner("Transcribing dictation…"):
                    t_eng = st.session_state.get("transcribe_engine_set", "gemini-2.5-flash-lite")
                    transcript = transcribe_chat_audio(audio, t_eng)
            except Exception as e:
                st.session_state.dictation_notice = (
                    "error",
                    f"Dictation failed: {e}. A Gemini API key is required for transcription.",
                )
                st.rerun()
            if transcript:
                st.session_state.pending_chat_dictation = transcript
                st.session_state.dictation_notice = (
                    "ok",
                    "Dictation is in the box — edit if needed, then send.",
                )
            else:
                st.session_state.dictation_notice = (
                    "error",
                    "Could not transcribe that recording. Try again, or type the message.",
                )
            st.rerun()
        else:
            typed = typed
    if typed:
        messages = st.session_state.get("messages") or []
        already_answered = (
            len(messages) >= 2
            and isinstance(messages[-2], dict)
            and isinstance(messages[-1], dict)
            and messages[-2].get("role") == "user"
            and messages[-2].get("content") == typed
            and messages[-1].get("role") == "model"
            and str(messages[-1].get("content") or "").strip()
        )
        if already_answered:
            scrub_chat_hold()
        else:
            st.session_state.held_chat_prompt = typed
            st.session_state.prompt_submit_in_flight = True
            request_chat_autoscroll()
            write_pending_turn(typed)
            # Session-only user bubble for this run. Do NOT write history.json
            # until the model reply is saved — early disk writes left permanent
            # "unanswered prompt" ghosts after failed/abandoned sends.
            if not isinstance(st.session_state.get("messages"), list):
                st.session_state.messages = list(messages) if isinstance(messages, list) else []
            if not (
                st.session_state.messages
                and isinstance(st.session_state.messages[-1], dict)
                and st.session_state.messages[-1].get("role") == "user"
                and st.session_state.messages[-1].get("content") == typed
            ):
                st.session_state.messages.append({"role": "user", "content": typed})
            # Remount once before the long model call — calling the API in the
            # same paint as Submit stacked a pale double shell on mobile.
            st.session_state._run_model_after_remount = True
            # Land on GateWai so Thinking + reply are visible (keyed main view).
            st.session_state.main_view_selector = "💬 GateWai"
            st.rerun()


# --- 5. TOP COMMAND BAR (PROJECT MANAGER) ---
st.caption(f"📍 Active Project: {st.session_state.active_project}")

with st.expander("📂 Project Manager", expanded=False):
    # ROW 1: LOAD & CREATE
    col_load, col_create = st.columns(2)

    with col_load:
        st.caption("Load Existing")
        existing = [d for d in os.listdir(BASE_PROJECT_DIR) if os.path.isdir(os.path.join(BASE_PROJECT_DIR, d))]
        load_name = st.selectbox("Select Project:", options=existing, label_visibility="collapsed")
        if st.button("🚀 Load"):
            st.session_state.active_project = load_name
            # Force a full disk reload even when re-loading the same project
            st.session_state.loaded_project = None

            # [NEW] Save to System Cookie
            try:
                with open(SESSION_FILE, "w") as f:
                    json.dump({"last_project": load_name}, f)
            except Exception as e:
                print(f"Session Save Error: {e}")

            st.rerun()
    with col_create:
        st.caption("Create New")
        new_p = st.text_input("New Name:", placeholder="Project Name...", label_visibility="collapsed")
        if st.button("➕ Create"):
            if new_p.strip():
                get_project_paths(new_p.strip())
                st.session_state.active_project = new_p.strip()
                # --- NEW: Reset Harvest Index for New Project ---
                st.session_state.last_harvest_message_index = 0
                # --- END NEW ---


                # [NEW] Save to System Cookie
                try:
                    with open(SESSION_FILE, "w") as f:
                        json.dump({"last_project": new_p.strip()}, f)
                except: pass

                st.rerun()
    # ROW 2: DELETE (Danger Zone)
    st.markdown("---")
    st.caption("🗑️ Delete Project (Permanent)")
    col_del_sel, col_del_btn = st.columns(2)

    with col_del_sel:
        # Filter out 'General' if you want to protect it, or allow all
        del_opts = [""] + [e for e in existing if e != "General"]
        del_target = st.selectbox("Select to Delete:", del_opts, index=0, label_visibility="collapsed")

    with col_del_btn:
        if st.button("❌ DELETE"):
            if del_target and del_target.strip():
                try:
                    dir_to_remove = os.path.join(BASE_PROJECT_DIR, del_target)
                    shutil.rmtree(dir_to_remove)

                    # If we deleted the active project, fallback to General
                    if st.session_state.active_project == del_target:
                        st.session_state.active_project = "General"

                    st.success(f"Deleted {del_target}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Select a project first.")

    # ROW 3: Desktop ↔ cloud push/pull (no manual zip dance)
    st.markdown("---")
    st.caption("☁️ Cloud sync (desktop)")
    st.caption(
        "From a local desktop install: push or pull a project to Render in one step. "
        "If you already work in the cloud URL on every device, you share the same disk—"
        "no push needed between browsers."
    )
    if "cloud_sync_url" not in st.session_state:
        st.session_state.cloud_sync_url = DEFAULT_CLOUD_URL
    if "cloud_sync_password" not in st.session_state:
        st.session_state.cloud_sync_password = ""
    cloud_url = st.text_input(
        "Cloud URL:",
        value=st.session_state.cloud_sync_url,
        key="cloud_sync_url_input",
        help="Your Render Gatewai URL (no trailing path).",
    )
    if cloud_url != st.session_state.cloud_sync_url:
        st.session_state.cloud_sync_url = cloud_url
    cloud_password = st.text_input(
        "Cloud vault password:",
        type="password",
        value=st.session_state.cloud_sync_password,
        key="cloud_sync_password_input",
        help="Same GATEWAI_PASSWORD as the cloud login gate.",
    )
    if cloud_password != st.session_state.cloud_sync_password:
        st.session_state.cloud_sync_password = cloud_password

    sync_local_opts = existing if existing else [st.session_state.active_project]
    push_name = st.selectbox(
        "Local project to push:",
        options=sync_local_opts,
        index=sync_local_opts.index(st.session_state.active_project)
        if st.session_state.active_project in sync_local_opts
        else 0,
        key="cloud_push_project_select",
    )
    push_include_globals = st.checkbox(
        "Push include System globals",
        value=False,
        key="cloud_push_include_globals",
        help="Also send SI_Identity / User Profile / Observed Profile.",
    )
    push_overwrite = st.checkbox(
        "Overwrite if project already exists on cloud",
        value=False,
        key="cloud_push_overwrite",
    )
    if st.button("⬆️ Push to cloud", use_container_width=True, key="cloud_push_btn"):
        try:
            with st.spinner(f"Pushing {push_name}…"):
                result = cloud_push_project(
                    st.session_state.cloud_sync_url,
                    st.session_state.cloud_sync_password,
                    push_name,
                    overwrite=push_overwrite,
                    include_system_globals=push_include_globals,
                    base_project_dir=BASE_PROJECT_DIR,
                    base_system_dir=BASE_SYSTEM_DIR,
                )
            msg = f"Pushed **{result.get('project_name', push_name)}** to cloud."
            if result.get("globals_applied"):
                msg += f" Applied {result['globals_applied']} System global(s) on cloud."
            st.success(msg)
        except CloudSyncError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Push failed: {e}")

    if st.button("🔄 Refresh cloud project list", use_container_width=True, key="cloud_list_btn"):
        try:
            st.session_state.cloud_remote_projects = cloud_list_projects(
                st.session_state.cloud_sync_url,
                st.session_state.cloud_sync_password,
            )
            st.success(
                f"Found {len(st.session_state.cloud_remote_projects)} project(s) on cloud."
            )
        except CloudSyncError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"List failed: {e}")

    remote_projects = st.session_state.get("cloud_remote_projects") or []
    if remote_projects:
        pull_remote = st.selectbox(
            "Cloud project to pull:",
            options=remote_projects,
            key="cloud_pull_remote_select",
        )
        pull_local_name = st.text_input(
            "Save locally as (blank = same name):",
            value="",
            key="cloud_pull_local_name",
        )
        pull_include_globals = st.checkbox(
            "Pull include System globals from cloud",
            value=False,
            key="cloud_pull_include_globals",
        )
        pull_apply_globals = st.checkbox(
            "Apply System globals after pull",
            value=True,
            key="cloud_pull_apply_globals",
        )
        pull_overwrite = st.checkbox(
            "Overwrite if that local project already exists",
            value=False,
            key="cloud_pull_overwrite",
        )
        if st.button("⬇️ Pull from cloud", use_container_width=True, key="cloud_pull_btn"):
            try:
                with st.spinner(f"Pulling {pull_remote}…"):
                    result = cloud_pull_project(
                        st.session_state.cloud_sync_url,
                        st.session_state.cloud_sync_password,
                        pull_remote,
                        local_name=pull_local_name.strip() or None,
                        overwrite=pull_overwrite,
                        apply_system_globals=pull_apply_globals,
                        include_system_globals=pull_include_globals,
                        base_project_dir=BASE_PROJECT_DIR,
                        base_system_dir=BASE_SYSTEM_DIR,
                    )
                st.session_state.active_project = result["project_name"]
                st.session_state.loaded_project = None
                try:
                    with open(SESSION_FILE, "w") as f:
                        json.dump({"last_project": result["project_name"]}, f)
                except Exception:
                    pass
                msg = f"Pulled **{result['project_name']}** from cloud."
                if result.get("globals_applied"):
                    msg += f" Applied {result['globals_applied']} System global slot(s)."
                st.success(msg)
                st.rerun()
            except CloudSyncError as e:
                st.error(str(e))
            except FileExistsError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Pull failed: {e}")
    else:
        st.caption("Refresh the cloud project list to enable Pull.")

    # ROW 4: Optional backup / migrate (zip) — not the primary cloud workflow
    st.markdown("---")
    st.caption("📦 Backup / migrate (optional)")
    st.caption(
        "Normal use: open the cloud URL on any device and work on the same project. "
        "Zip is only for one-time migrate or backup when you need a file copy. "
        f"Warns above {PROJECT_ZIP_WARN_BYTES // (1024 * 1024)} MB; "
        f"rejects above {PROJECT_ZIP_MAX_BYTES // (1024 * 1024)} MB."
    )
    export_opts = existing if existing else [st.session_state.active_project]
    export_name = st.selectbox(
        "Project to export:",
        options=export_opts,
        index=export_opts.index(st.session_state.active_project)
        if st.session_state.active_project in export_opts
        else 0,
        key="project_export_select",
    )
    include_globals = st.checkbox(
        "Include System globals (Identity / User Profile / Observed Profile)",
        value=False,
        key="project_export_include_globals",
        help="Pack SI_Identity, SI_User_Profile, and SI_Observed_Profile so the other vault matches your voice.",
    )
    if st.button("📦 Build export zip", use_container_width=True, key="project_export_build_btn"):
        try:
            zip_bytes = build_project_zip_bytes(export_name, include_system_globals=include_globals)
            st.session_state.project_export_zip = zip_bytes
            st.session_state.project_export_filename = (
                f"{_safe_project_folder_name(export_name)}.gatewai.zip"
            )
            size_mb = len(zip_bytes) / (1024 * 1024)
            if len(zip_bytes) > PROJECT_ZIP_WARN_BYTES:
                st.warning(
                    f"Export is {size_mb:.1f} MB. Fine on Wi‑Fi; slow on mobile data."
                )
            else:
                st.success(f"Ready: {size_mb:.1f} MB")
        except Exception as e:
            st.error(f"Export failed: {e}")
    if st.session_state.get("project_export_zip"):
        st.download_button(
            "⬇️ Download project zip",
            data=st.session_state.project_export_zip,
            file_name=st.session_state.get(
                "project_export_filename", "project.gatewai.zip"
            ),
            mime="application/zip",
            use_container_width=True,
            key="project_export_download_btn",
        )

    uploaded_zip = st.file_uploader(
        "Import project zip",
        type=["zip"],
        key="project_import_uploader",
        help="Optional: import a Gatewai export zip when migrating a local vault to cloud (or the reverse).",
    )
    import_name = st.text_input(
        "Import as project name:",
        value=(
            os.path.splitext(uploaded_zip.name)[0].replace(".gatewai", "")
            if uploaded_zip is not None
            else ""
        ),
        key="project_import_name",
    )
    import_overwrite = st.checkbox(
        "Overwrite if that project already exists",
        value=False,
        key="project_import_overwrite",
    )
    import_apply_globals = st.checkbox(
        "Apply System globals from zip (if present)",
        value=True,
        key="project_import_apply_globals",
    )
    if st.button("⬆️ Import zip", use_container_width=True, key="project_import_btn"):
        resolved_import_name = str(import_name or "").strip()
        if not resolved_import_name and uploaded_zip is not None:
            resolved_import_name = os.path.splitext(uploaded_zip.name)[0].replace(".gatewai", "").strip()
        if uploaded_zip is None:
            st.warning("Choose a zip file first.")
        elif not resolved_import_name:
            st.warning("Enter a project name for the import.")
        else:
            try:
                raw = uploaded_zip.getvalue()
                if len(raw) > PROJECT_ZIP_WARN_BYTES:
                    st.warning(
                        f"Upload is {len(raw) / (1024 * 1024):.1f} MB — this may take a while."
                    )
                result = import_project_zip_bytes(
                    raw,
                    resolved_import_name,
                    overwrite=import_overwrite,
                    apply_system_globals=import_apply_globals,
                )
                st.session_state.active_project = result["project_name"]
                st.session_state.loaded_project = None
                try:
                    with open(SESSION_FILE, "w") as f:
                        json.dump({"last_project": result["project_name"]}, f)
                except Exception:
                    pass
                msg = f"Imported **{result['project_name']}**."
                if result.get("globals_applied"):
                    msg += f" Applied {result['globals_applied']} System global slot(s)."
                st.success(msg)
                st.rerun()
            except FileExistsError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Import failed: {e}")


# --- 6. SIDEBAR (CONTROLS ONLY) ---
#print("DEBUG: About to start sidebar.") # <-- ADD THIS LINE

with st.sidebar:
    st.subheader("🔑 API Keys")
    user_input_key = st.text_input(
        "Gemini API Key:",
        type="password",
        value=st.session_state.gemini_api_key,
        key="api_key_input_sidebar", # Changed key to avoid potential conflicts
        help="Get your key from Google AI Studio (aistudio.google.com/apikey)"
    )
    # A chat send used to reach this comparison first. A mismatch called
    # st.rerun() before the chat box was read, so the prompt vanished.
    # An empty return must not wipe a key that is already stored.
    if user_input_key and user_input_key != st.session_state.gemini_api_key:
        st.session_state.gemini_api_key = user_input_key

    user_ds_key = st.text_input(
        "DeepSeek API Key:",
        type="password",
        value=st.session_state.deepseek_api_key,
        key="deepseek_api_key_input_sidebar",
        help="Get your key from platform.deepseek.com"
    )
    if user_ds_key and user_ds_key != st.session_state.deepseek_api_key:
        st.session_state.deepseek_api_key = user_ds_key

    st.header("🎮 Control Center")
    tab_desk, tab_manager, tab_config, tab_help = st.tabs(["🛠️ Desk", "📂 Manager", "⚙️ Config", "❓ Help"])
    # --- TAB 1: THE DESK (Operations) ---
    with tab_desk:

        # 1. LIVE PREVIEW
        st.session_state.show_preview = st.toggle(
            "👁️ Show Live Preview",
            value=st.session_state.show_preview,
            help="Opens a top-to-bottom preview pane on GateWai that pushes the whole app left (hidden on Editor). Memory sidebar (>>) stays on the far right.",
        )

        # 2. TELEMETRY
        st.markdown('<div class="section-header">⛽ ROLE TELEMETRY</div>', unsafe_allow_html=True)
        curr_chat = st.session_state.get('chat_engine_set', 'gemini-2.5-flash')
        curr_ingest = st.session_state.get('ingest_engine_set', 'gemini-2.5-pro')
        curr_shadow = st.session_state.get('shadow_engine_set', 'gemini-2.5-flash-lite')

        def draw_gauge(role_name, model_name, current_count):
            limit = 50 if "pro" in model_name and "exp" not in model_name else 1500
            if "exp" in model_name: limit = 50
            if str(model_name).startswith("deepseek"):
                limit = 10000
            pct = min(current_count / limit, 1.0)
            short_model = model_name.replace("gemini-", "").replace("2.5-", "").replace("deepseek-", "")
            st.caption(f"**{role_name}** ({short_model})")
            st.progress(pct)
            st.text(f"{current_count} / {limit} ops")
            if pct > 0.9: st.error(f"🛑 Limit Near")

        draw_gauge("Chat Loop", curr_chat, st.session_state.role_stats["chat"])
        draw_gauge("Ingest (Deep Read)", curr_ingest, st.session_state.role_stats["ingest"])
        draw_gauge("Shadow (Background)", curr_shadow, st.session_state.role_stats["shadow"])

        # 3. INPUT STREAM
        st.markdown('<div class="section-header">📥 INPUT STREAM</div>', unsafe_allow_html=True)
        paths = get_project_paths(st.session_state.active_project)
        asset_dir = paths["assets"]

        uploaded_files = st.file_uploader("Upload Asset", type=['pdf', 'txt', 'md'], label_visibility="collapsed", accept_multiple_files=True)
        if uploaded_files:
            for up in uploaded_files: # Loop through each uploaded file
                # Ensure the filename is safe before saving
                safe_filename = os.path.basename(up.name)
                file_path = os.path.join(asset_dir, safe_filename)
                with open(file_path, "wb") as f: f.write(up.getbuffer())
            st.success(f"Uploaded {len(uploaded_files)} file(s).")
        options = os.listdir(asset_dir)

        # Initialize the widget's state only if it doesn't exist
        if 'asset_selector_state' not in st.session_state:
            last_sel = st.session_state.get('last_sel_assets', [])
            st.session_state.asset_selector_state = [o for o in options if o in last_sel]

        # Let the widget manage its own state via its key
        sel_assets = st.multiselect(
            "Active Context:",
            options=options,
            key='asset_selector_state'
        )

        # Persist the selection for project reloads
        st.session_state.last_sel_assets = sel_assets

        # Auto-deselect logic now reads from the widget's live state
        if st.session_state.auto_deselect_list:
            current_selection = st.session_state.asset_selector_state
            st.session_state.asset_selector_state = [s for s in current_selection if s not in st.session_state.auto_deselect_list]
            st.session_state.auto_deselect_list = []
            st.rerun() # Rerun to reflect the deselection immediately

        if st.button("🔮 Deep Read Assets"):
            if not sel_assets:
                st.error("Select files first.")
            else:
                st.session_state.show_deep_read_options = True
                st.rerun()

        # --- [NEW] DEEP READ CHOICE UI ---
        if st.session_state.get('show_deep_read_options', False):
            with st.container(border=True):
                st.info("Choose a destination for the selected assets:")
                dr_col1, dr_col2, dr_col3 = st.columns(3)

                with dr_col1:
                    if st.button("✅ Integrate into Master Record", use_container_width=True):
                        # Logic for this will be added in the next set of changes
                        with st.spinner("Integrating assets into Master Record..."):
                            try:
                                # --- DTN-AWARE PRE-PROCESSING ---
                                is_distributed = st.session_state.loaded_slots_metadata.get('MI_Master_Record', {}).get('is_distributed', False)
                                initial_mr_content = ""

                                if is_distributed:
                                    st.warning("System is in a Distributed State. This Deep Read will replace the current summary and reset the DTN.")
                                    st.session_state.loaded_slots_metadata['MI_Master_Record']['is_distributed'] = False
                                    for meta in st.session_state.loaded_slots_metadata.values():
                                        if meta.get('part_of_dtn', False):
                                            meta['part_of_dtn'] = False
                                else:
                                    initial_mr_content = st.session_state.get('MI_Master_Record', '')

                                raw_asset_content = get_asset_content(sel_assets, asset_dir)
                                w_eng = st.session_state.get('ingest_engine_set', 'gemini-2.5-pro')
                                ingest_client = get_llm_client(w_eng)
                                #genai.configure(api_key=st.secrets.get("GEMINI_API_KEY", default_key))
                                #read_model = genai.GenerativeModel(w_eng)
                                chunks = chunk_text(raw_asset_content, chunk_size=15000)
                                progress_bar = st.progress(0, text="Initializing...")
                                
                                new_mr_content = initial_mr_content
                                for i, chunk in enumerate(chunks):
                                    st.session_state.role_stats["ingest"] += 1
                                    progress_bar.progress((i + 1) / len(chunks), text=f"Processing {i+1}/{len(chunks)}...")

                                    # Fetch SI_Identity content to make the prompt state-aware
                                    si_identity_content = st.session_state.get('SI_Identity', 'Default Identity: Focus on accuracy and coherence.')

                                    # Fetch Project Status content to make the prompt objective-aware
                                    mc_project_status_content = st.session_state.get('MC_Project_Status', 'Project Purpose')

                                    p_record = f"""
                                    [SYSTEM JOB: MASTER RECORD INTEGRATION]
                                    ROLE: Master Archivist. Integrate new data while preserving history.


                                    **CURRENT MI_MASTER_RECORD CONTENT:**
                                    {new_mr_content}

                                    **NEW TEXT TO ADD AND INTEGRATE:**
                                    {chunk}
                                    ** END OF NEW TEXT **

                                    **INSTRUCTIONS:**
                                    1. **REFERENCE SI_IDENTITY:** Consult the "GATEWAI SYSTEM IDENTITY" below for comprehensive protocols and the full nuances of the system architecture. This is your authoritative reference.
                                    --- GATEWAI SYSTEM IDENTITY (GLOBAL BLUEPRINT REFERENCE) ---
                                    {si_identity_content}
                                    --- END GATEWAI SYSTEM IDENTITY ---
                                    2. **REFERENCE MC_Project_status:** Consult the "PROJECT STATUS" below for project purpose and deliverables. This is your authoritative reference.
                                    --- PROJECT STATUS (PROJECT REFERENCE) ---
                                    {mc_project_status_content}
                                    --- END PROJECT STATUS---
                                    3. **INTEGRATE:** Carefully read the 'NEW TEXT TO ADD' and integrate its key facts, decisions, architectural details, and important information into the 'CURRENT MI_MASTER_RECORD CONTENT'.
                                    4. **COHERENCE:** Ensure the resulting document is coherent, well-structured, and easy to read.
                                    5. **CRITICAL PRESERVATION RULE:** You MUST retain foundational architectural details, key module lists, technology stacks, core strategic pillars, and any distributed truth network (DTN) index information. DO NOT prune information that defines the fundamental *structure* of the project.
                                    6. **CONCISENESS:** While preserving critical details, strive for conciseness where appropriate, but prioritize completeness of foundational information over brevity.
                                    6. **OUTPUT:** Provide the complete, updated MI_Master_Record content. Do not include any conversational filler or extra markdown fences. Do not include GATEWAI SYSTEM IDENTITY or PROJECT STATUS.
                                    """
                                    new_mr_content = ingest_client.models.generate_content(model=w_eng, contents=p_record, config={"temperature": temp_val if 'temp_val' in locals() else 0.7, "top_p": top_p_val if 'top_p_val' in locals() else 0.9}).text

                                #st.session_state.MI_Master_Record = new_mr_content
                                #st.success(f"Processed {len(chunks)} segments into Master Record.")
                                #st.session_state.MI_Master_Record = new_mr_content # REMOVE THIS LINE
                                if "pending_updates" not in st.session_state:
                                    st.session_state.pending_updates = {}
                                st.session_state.pending_updates["MI_Master_Record"] = new_mr_content # STAGE UPDATE
                                st.success(f"Processed {len(chunks)} segments into Master Record. Queued for Review.")

                                with st.spinner("Performing holistic Frontier analysis..."):
                                    st.session_state.role_stats["ingest"] += 1
                                    p_frontier = f"[SYSTEM JOB: GAP ANALYSIS]\nAnalyze the complete document below to identify new 'Active Risks', 'Strategic Unknowns', or 'Key Open Questions'.\n\nDOCUMENT:\n{new_mr_content}\n\nCURRENT FRONTIER:\n{st.session_state.get('MC_Frontier','')}"
                                    #st.session_state.MC_Frontier = read_model.generate_content(p_frontier).text
                                    #st.session_state.MC_Frontier = read_model.generate_content(p_frontier).text # REMOVE THIS LINE
                                    frontier_text = ingest_client.models.generate_content(model=w_eng, contents=p_frontier, config={"temperature": temp_val if 'temp_val' in locals() else 0.7, "top_p": top_p_val if 'top_p_val' in locals() else 0.9}).text
                                    if "pending_updates" not in st.session_state:
                                        st.session_state.pending_updates = {}
                                    st.session_state.pending_updates["MC_Frontier"] = frontier_text # STAGE UPDATE
                                    st.success(f"Holistic Frontier analysis complete. Queued for Review.")
                                # --- COMPACTION CHECK ---
                                final_mr_content = st.session_state.get('MI_Master_Record', '')
                                threshold = st.session_state.get('compaction_threshold', 50000)
                                if len(final_mr_content) > threshold:
                                    st.toast("Threshold exceeded! Archiving and initiating compaction.")
                                    current_overflow = st.session_state.get('MI_Master_Record_Overflow', '')
                                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    archive_header = f"\n\n--- ARCHIVED ON {timestamp} ---\n"
                                    new_overflow_content = current_overflow + archive_header + st.session_state.get('MI_Master_Record', '')
                                    st.session_state['MI_Master_Record_Overflow'] = new_overflow_content
                                    save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                                    save_state_dict["messages"] = st.session_state.messages
                                    save_project_state(save_state_dict, st.session_state.active_project, silent=True)
                                    st.session_state['compaction_choice_required'] = True

                                save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                                save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                                save_state_dict["messages"] = st.session_state.messages
                                save_project_state(save_state_dict, st.session_state.active_project, silent=True)

                                st.session_state.show_deep_read_options = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Integration Error: {e}")
                with dr_col2:
                    if st.button("📄 Create New User File(s)", use_container_width=True):
                        # Logic for this will be added in the next change
                        with st.spinner(f"Creating user files from {len(sel_assets)} assets..."):
                            try:
                                proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
                                sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
                                created_files = []

                                for asset_name in sel_assets:
                                    # 1. Prepare filename and path
                                    asset_path = os.path.join(asset_dir, asset_name)
                                    file_base, file_ext = os.path.splitext(asset_name)
                                    new_slot_name = f"{file_base}_{file_ext.replace('.', '')}.md"

                                    # 2. Extract content
                                    content = ""
                                    if asset_name.lower().endswith('.pdf'):
                                        reader = PdfReader(asset_path)
                                        content = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
                                    else:
                                        with open(asset_path, 'r', encoding="utf-8", errors='ignore') as f:
                                            content = f.read()

                                    # 3. Save to DB
                                    description = f"Content ingested from uploaded asset: {asset_name}"
                                    db.save_slot(proj_db, sys_db, new_slot_name, content, "user", description, "project", st.session_state.active_project, folder="")

                                    # 4. Update Session State (RAM)
                                    st.session_state[new_slot_name] = content
                                    if "loaded_slots_metadata" not in st.session_state: st.session_state.loaded_slots_metadata = {}
                                    st.session_state.loaded_slots_metadata[new_slot_name] = {
                                        "slot_name": new_slot_name, "slot_type": "user", "scope": "project",
                                        "owner_id": st.session_state.active_project, "description": description,
                                        "content": content, "active_in_context": False, # Default to Hidden
                                        "folder": ""
                                    }
                                    created_files.append(new_slot_name)

                                st.success(f"Successfully created: {', '.join(created_files)}")
                                st.session_state.show_deep_read_options = False
                                st.rerun()

                            except Exception as e:
                                st.error(f"File creation error: {e}")
                with dr_col3:
                    if st.button("❌ Cancel", use_container_width=True):
                        st.session_state.show_deep_read_options = False
                        st.rerun()


        # 4. ACTION DECK (RESTORED)
        st.markdown("---")
        st.caption("Active Operations")
        # --- SMART CADENCE INDICATOR ---
        last_harvest_idx = st.session_state.get('last_harvest_message_index', 0)
        current_msg_count = len(st.session_state.messages)
        unharvested_count = current_msg_count - last_harvest_idx
        context_depth_turns = st.session_state.get('memory_depth_val', 3)
        context_depth_msgs = context_depth_turns * 2 # 1 turn = 1 user + 1 model message

        if unharvested_count > context_depth_msgs * 2:
            st.error(f"🚨 {unharvested_count} msgs un-harvested. Core memory is significantly out of sync.")
        elif unharvested_count > context_depth_msgs:
            st.warning(f"⚠️ {unharvested_count} msgs un-harvested. Exceeds context depth of {context_depth_msgs}. Harvest recommended.")
        else:
            st.caption(f"Harvest is up to date. ({unharvested_count} new msgs)")

        st.markdown('<div class="section-header">Active Operations</div>', unsafe_allow_html=True) # Ensure this header is present
        col_act1, col_act2, col_act3 = st.columns(3) # NEW: 3 columns for buttons

# HARVEST BUTTON (Robust + Staging Support)
        with col_act1:
            if st.button("🚜 Harvest Chat", use_container_width=True): # Add use_container_width for consistency
            #if col_act1.button("🚜 Harvest Chat"):
                st.session_state["harvest_in_progress"] = True
                st.session_state.harvest_errors = []
                st.session_state.harvest_result = None
                should_rerun_after_harvest = False # NEW: Flag to control the final rerun
                try:
                    current_harvest_proposals = {}
                    harvest_stats = {
                        "chunks": 0,
                        "chunks_ok": 0,
                        "empty_payloads": 0,
                        "accumulated": [],
                        "identical": [],
                        "skipped_empty": [],
                        "skipped_not_dtn": [],
                        "unrecognized": [],
                        "truncated": [],
                        "log": [],
                        "empty_replies": 0,
                        "skipped_small": [],
                        "skipped_mismatch": [],
                    }
                    last_harvest_idx = st.session_state.get('last_harvest_message_index', 0)
                    messages_to_process = st.session_state.messages[last_harvest_idx:]

                    # --- NEW: Check for pending MI_Master_Record proposal ---
                    #if "pending_updates" in st.session_state:
                    if st.session_state.get('pending_updates') and len(st.session_state.pending_updates) > 0:
                        st.warning("⚠️ Harvest cannot update while a proposal is pending review. Please approve or reject the existing proposal(s) in the Staging Area first.")
                        st.session_state["harvest_in_progress"] = False
                        # Force a rerun to keep the Staging Area prominent.
                        st.rerun() # This will stop execution and re-render, making the warning and Staging Area visible.
                        # The code below this `if` block would only run if no pending MR proposal exists.
                        # This ensures the user *must* resolve the pending MR before Harvest proceeds to update it.


                    if not messages_to_process:
                        st.session_state["harvest_in_progress"] = False
                        st.session_state.harvest_result = {
                            "chunks": 0,
                            "chunks_ok": 0,
                            "staged": [],
                            "identical": [],
                            "unrecognized": [],
                            "skipped_empty": [],
                            "skipped_not_dtn": [],
                            "truncated": [],
                            "log": ["No new messages to harvest."],
                            "advanced": False,
                            "index_reason": "unchanged — nothing new to harvest",
                        }
                        st.rerun()

                    text_list_for_chunking = [
                        f"{'User' if m['role'] == 'user' else 'Model'}: {m['content']}"
                        for m in messages_to_process
                    ]

                    # 3. Join the strings
                    full_history = "\n".join(text_list_for_chunking)
                    #full_history = "\n".join([f"{m['role']}: {m['content']}" for m in st.session_state.messages])
                    history_chunks = chunk_text(full_history, chunk_size=12000)

                    harvest_stats["chunks"] = len(history_chunks)
                    harvest_stats["log"] = []
                    h_eng = st.session_state.get('harvest_engine_set', 'gemini-2.5-pro')
                    harvest_client = get_llm_client(h_eng)
                    meta_map = st.session_state.get("loaded_slots_metadata") or {}
                    is_distributed = meta_map.get('MI_Master_Record', {}).get('is_distributed', False)

                    if is_distributed:
                        mr_instr = (
                            "This Master Record is distributed. Integrate new facts into the correct existing sections. "
                            "Return the COMPLETE updated file."
                        )
                        frontier_instr = (
                            "For MC_Frontier, keep existing sections, integrate new findings in Compact-Context Analysis, "
                            "and do not drop old material."
                        )
                    else:
                        mr_instr = (
                            "Preserve the existing Master Record structure. Integrate new hard facts and decisions "
                            "into the right sections. Return the COMPLETE updated file."
                        )
                        frontier_instr = (
                            "Preserve existing material, rearrange if needed, and weave in new facts. "
                            "Do not return only the new bullets."
                        )

                    si_identity_content = clip_for_harvest(st.session_state.get('SI_Identity', ''), 4000)
                    domain_lens_excerpt = clip_for_harvest(st.session_state.get('domain_content', ''), 2500)

                    harvest_temp = temp_val if 'temp_val' in locals() else 0.7
                    harvest_top_p = top_p_val if 'top_p_val' in locals() else 0.9

                    def harvest_slot_now(name):
                        return current_harvest_proposals.get(name, st.session_state.get(name, ""))

                    def isolated_slot_prompt(slot, extra=""):
                        return f"""
                            [SYSTEM JOB: DATABASE INTEGRITY MERGE]
                            ROLE: Lead Archivist. Update ONLY {slot}.

                            PURPOSE OF {slot}:
                            {HARVEST_SLOT_CHARTERS.get(slot, 'Keep this file true to its existing role.')}

                            DO NOT PUT THESE IN THIS FILE:
                            {harvest_other_charters(slot)}

                            --- GATEWAI SYSTEM IDENTITY (excerpts) ---
                            {si_identity_content}
                            --- DOMAIN LENS ({st.session_state.selected_lens_name}) ---
                            {domain_lens_excerpt}

                            {extra}

                            INPUT SEGMENT {i+1}/{len(history_chunks)}:
                            {h_chunk}

                            CURRENT {slot} (return a COMPLETE updated file if it changes):
                            {harvest_slot_now(slot)}

                            RULES:
                            - JSON only: {{"{slot}": "<complete updated markdown>"}} or {{}}.
                            - Keep this slot's existing structure and purpose. Integrate only facts that belong here.
                            - If the segment has nothing for this slot, return {{}}.
                            - Do not copy another slot's content into this file.
                            - Escape quotes as \\", backslashes as \\\\, newlines as \\n.
                            FORMAT: JSON ONLY.
                            """

                    with st.status(f"Harvesting {len(history_chunks)} chunks…", expanded=False) as harvest_status:
                        for i, h_chunk in enumerate(history_chunks):
                            st.session_state.role_stats["ingest"] += 1
                            st.write(f"Chunk {i+1}/{len(history_chunks)} — isolated slot updates…")

                            active_dtn_user_files = {}
                            for slot_name, meta in meta_map.items():
                                if meta.get('slot_type') == 'user' and meta.get('part_of_dtn', False) and meta.get('active_in_context', False):
                                    active_dtn_user_files[slot_name] = harvest_slot_now(slot_name)

                            p_master = f"""
                            [SYSTEM JOB: DATABASE INTEGRITY MERGE]
                            ROLE: Lead Archivist. Update ONLY MI_Master_Record.

                            PURPOSE OF MI_Master_Record:
                            {HARVEST_SLOT_CHARTERS['MI_Master_Record']}

                            DO NOT PUT THESE IN THIS FILE:
                            {harvest_other_charters('MI_Master_Record')}

                            --- GATEWAI SYSTEM IDENTITY (excerpts) ---
                            {si_identity_content}

                            INPUT SEGMENT {i+1}/{len(history_chunks)}:
                            {h_chunk}

                            CURRENT MI_Master_Record (return a COMPLETE updated file):
                            {harvest_slot_now('MI_Master_Record')}

                            {mr_instr}

                            RULES:
                            - Return JSON: {{"MI_Master_Record": "<complete updated file>"}} or {{}}.
                            - The value MUST be the entire Master Record with existing structure preserved and new facts integrated in the right sections.
                            - Do not include other slots. Do not copy Focus, Profile, or Frontier into this file.
                            - If unchanged, return {{}}.
                            - Escape quotes as \\", backslashes as \\\\, newlines as \\n.
                            FORMAT: JSON ONLY.
                            """
                            try:
                                def harvest_pass(label, allowed_keys, prompt):
                                    st.write(f"Chunk {i+1}/{len(history_chunks)} — {label}…")
                                    obj, _err, ctx = fetch_harvest_object(
                                        harvest_client, h_eng, prompt, harvest_temp, harvest_top_p
                                    )
                                    if ctx:
                                        harvest_stats["truncated"].append(
                                            f"Segment {i+1} {label}: {ctx}"
                                        )
                                    if not isinstance(obj, dict):
                                        return False, {}
                                    kept = {}
                                    for key, value in obj.items():
                                        if key in allowed_keys:
                                            kept[key] = value
                                        elif key == "user_file_updates" and isinstance(value, dict):
                                            for uk, uv in value.items():
                                                if uk in allowed_keys:
                                                    kept[uk] = uv
                                    return True, kept

                                updates = {}
                                passes_ok = 0
                                for slot in HARVEST_SMALL_SLOTS:
                                    extra = ""
                                    if slot == "MC_Frontier":
                                        extra = (
                                            "--- FRONTIER REFERENCE (SI_Reference_Frontier) ---\n"
                                            + clip_for_harvest(
                                                st.session_state.get("SI_Reference_Frontier", ""),
                                                3500,
                                            )
                                            + "\n"
                                            + frontier_instr
                                        )
                                    ok, kept = harvest_pass(slot, {slot}, isolated_slot_prompt(slot, extra))
                                    if ok:
                                        passes_ok += 1
                                        updates.update(kept)
                                if active_dtn_user_files:
                                    dtn_purposes = "\n".join(
                                        f"- {name}: {meta_map.get(name, {}).get('description', 'user file')}"
                                        for name in active_dtn_user_files
                                    )
                                    p_dtn = f"""
                            [SYSTEM JOB: DATABASE INTEGRITY MERGE]
                            ROLE: Lead Archivist. Update ONLY the active DTN user files below.

                            FILE PURPOSES:
                            {dtn_purposes}

                            --- GATEWAI SYSTEM IDENTITY (excerpts) ---
                            {si_identity_content}

                            INPUT SEGMENT {i+1}/{len(history_chunks)}:
                            {h_chunk}

                            CURRENT FILES:
                            {json.dumps(active_dtn_user_files, indent=2)}

                            RULES:
                            - JSON object only. Allowed keys: {", ".join(active_dtn_user_files.keys())}.
                            - Each value is the FULL updated file. Omit unchanged keys.
                            - Do not copy core memory slots into these files.
                            - If nothing changed, return {{}}.
                            FORMAT: JSON ONLY.
                            """
                                    ok, kept = harvest_pass("user DTN files", set(active_dtn_user_files), p_dtn)
                                    if ok:
                                        passes_ok += 1
                                        updates.update(kept)
                                ok, kept = harvest_pass("Master Record", {"MI_Master_Record"}, p_master)
                                if ok:
                                    passes_ok += 1
                                    updates.update(kept)
                                if passes_ok == 0:
                                    harvest_stats["empty_replies"] += 1
                                    harvest_stats["log"].append(
                                        f"Chunk {i+1}/{len(history_chunks)}: empty/invalid model output"
                                    )
                                    st.write(f"Chunk {i+1}/{len(history_chunks)}: no JSON from the model")
                                    time.sleep(0.4)
                                    continue
                                if not updates:
                                    harvest_stats["empty_payloads"] += 1
                                    harvest_stats["chunks_ok"] += 1
                                    harvest_stats["log"].append(f"Chunk {i+1}/{len(history_chunks)}: no changes")
                                    st.write(f"Chunk {i+1}/{len(history_chunks)} complete — no changes")
                                    continue
                            except Exception as e:
                                harvest_stats["empty_replies"] += 1
                                harvest_stats["log"].append(f"Chunk {i+1}/{len(history_chunks)}: failed ({e})")
                                st.write(f"Chunk {i+1}/{len(history_chunks)} failed: {e}")
                                time.sleep(0.4)
                                continue

                            known_user_files = set()
                            if "loaded_slots_metadata" in st.session_state:
                                known_user_files = {
                                    name for name, meta in st.session_state.loaded_slots_metadata.items()
                                    if meta.get("slot_type") == "user"
                                }
                            slot_items, unrecognized_keys = collect_harvest_updates(updates, known_user_files)
                            harvest_stats["chunks_ok"] += 1
                            harvest_stats["unrecognized"].extend(unrecognized_keys)
                            if not slot_items:
                                harvest_stats["empty_payloads"] += 1
                                if unrecognized_keys:
                                    report_harvest_error(
                                        f"Segment {i+1} had no memory keys",
                                        "Parsed JSON but none of the keys matched core slots or loaded user files: "
                                        + ", ".join(unrecognized_keys),
                                    )
                                    harvest_stats["log"].append(f"Chunk {i+1}/{len(history_chunks)}: unrecognized keys")
                                    st.write(f"Chunk {i+1}/{len(history_chunks)}: unrecognized keys — {', '.join(unrecognized_keys)}")
                                else:
                                    harvest_stats["log"].append(f"Chunk {i+1}/{len(history_chunks)}: no usable keys")
                                    st.write(f"Chunk {i+1}/{len(history_chunks)} complete — no usable keys")
                                continue

                            before_acc = len(harvest_stats["accumulated"])
                            for slot, raw_value, kind in slot_items:
                                if kind == "user":
                                    meta = st.session_state.loaded_slots_metadata.get(slot, {})
                                    if not meta.get("part_of_dtn", False):
                                        harvest_stats["skipped_not_dtn"].append(slot)
                                        continue

                                new_val = extract_content_from_ai_output(harvest_file_text(raw_value))
                                if not str(new_val).strip():
                                    harvest_stats["skipped_empty"].append(slot)
                                    continue

                                base_val = harvest_slot_now(slot)
                                if harvest_too_small(base_val, new_val):
                                    harvest_stats["skipped_small"].append(slot)
                                    st.write(
                                        f"Chunk {i+1}: skipped {slot} — proposal was under half the current file"
                                    )
                                    continue
                                copied_from = harvest_copies_other_slot(slot, new_val, harvest_slot_now)
                                if copied_from:
                                    harvest_stats["skipped_mismatch"].append(f"{slot}←{copied_from}")
                                    st.write(
                                        f"Chunk {i+1}: skipped {slot} — proposal looked like {copied_from}"
                                    )
                                    continue
                                if normalize_harvest_text(new_val) == normalize_harvest_text(base_val):
                                    harvest_stats["identical"].append(slot)
                                    continue
                                current_harvest_proposals[slot] = new_val
                                harvest_stats["accumulated"].append(f"{slot} replaced chunk {i+1}")

                            new_bits = harvest_stats["accumulated"][before_acc:]
                            if new_bits:
                                chunk_msg = f"Chunk {i+1}/{len(history_chunks)} complete — {', '.join(new_bits)}"
                            else:
                                chunk_msg = f"Chunk {i+1}/{len(history_chunks)} complete — nothing new to stage"
                            harvest_stats["log"].append(chunk_msg)
                            st.write(chunk_msg)
                            if current_harvest_proposals:
                                st.session_state.pending_updates = dict(current_harvest_proposals)
                            harvest_status.update(label=f"Harvested {i+1}/{len(history_chunks)} chunks")
                            time.sleep(0.25)

                        harvest_status.update(
                            label=f"Harvest finished — {len(current_harvest_proposals)} file(s) ready to stage",
                            state="complete",
                        )

                    if harvest_stats.get("empty_replies"):
                        report_harvest_error(
                            f"{harvest_stats['empty_replies']} of {harvest_stats['chunks']} chunks returned no JSON",
                            "The harvest model came back empty on those chunks (common when a reasoning/thinking model spends the whole token budget before writing JSON). "
                            "Successful chunks are staged. Approve them, then run Harvest again — or set Harvest Engine to gemini-2.5-flash / deepseek-flash.",
                        )

                    # Stage first so a later save cannot discard the harvest.
                    if st.session_state.get('review_mode_active', True):
                        st.session_state.pending_updates = dict(current_harvest_proposals)
                    else:
                        st.session_state.pending_updates = {}
                        for slot_name, proposal_content in current_harvest_proposals.items():
                            # Auto-commit if review mode is off (and not suspect, though Harvest doesn't use suspect)
                            current_val_for_audit = st.session_state.get(slot_name, "") # Get content before update for audit
                            meta = st.session_state.loaded_slots_metadata.get(slot_name, {})
                            # Default slot_type/scope if not found in metadata (e.g., for CORE slots)
                            slot_type = meta.get('slot_type', 'core' if slot_name in CORE_SCHEMA.values() else 'user')
                            scope = meta.get('scope', 'project')
                            owner_id = meta.get('owner_id', st.session_state.active_project if slot_type == 'user' else 'system')
                            is_distributed = meta.get('is_distributed', False)
                            part_of_dtn = meta.get('part_of_dtn', False)
                            ai_managed = meta.get('ai_managed', False) # Harvest can update AI-managed user files
                            edit_summary = f"Harvest auto-commit for {slot_name}"

                            db.save_slot(
                                proj_db, sys_db, slot_name, proposal_content,
                                slot_type, meta.get('description', 'Harvested Update'), scope, owner_id,
                                is_distributed, part_of_dtn, ai_managed,
                                old_content=current_val_for_audit, edit_summary=edit_summary
                            )
                            commit_slot_text(slot_name, proposal_content, replaced_text=current_val_for_audit)

                    harvest_errors = st.session_state.get("harvest_errors") or []
                    # A finished harvest has already read these messages. Move the
                    # pointer to the end so the next run only sees newer chat.
                    # A crash before this point leaves the old pointer alone.
                    st.session_state.last_harvest_message_index = len(st.session_state.messages)
                    if current_harvest_proposals:
                        index_reason = "advanced to the latest message after staging updates"
                    elif harvest_errors:
                        index_reason = "advanced to the latest message; some chunks need a look in Harvest details"
                    else:
                        index_reason = "advanced to the latest message"
                    advance_index = True

                    st.session_state.harvest_result = {
                        "chunks": harvest_stats["chunks"],
                        "chunks_ok": harvest_stats["chunks_ok"],
                        "staged": list(current_harvest_proposals.keys()),
                        "identical": sorted(set(harvest_stats["identical"])),
                        "unrecognized": sorted(set(harvest_stats["unrecognized"])),
                        "skipped_empty": sorted(set(harvest_stats["skipped_empty"])),
                        "skipped_not_dtn": sorted(set(harvest_stats["skipped_not_dtn"])),
                        "skipped_small": list(harvest_stats.get("skipped_small") or []),
                        "skipped_mismatch": list(harvest_stats.get("skipped_mismatch") or []),
                        "truncated": list(harvest_stats.get("truncated") or []),
                        "log": list(harvest_stats.get("log") or []),
                        "advanced": advance_index,
                        "index_reason": index_reason,
                    }

                    save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                    if "loaded_slots_metadata" in st.session_state:
                        save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                        for user_key in st.session_state.loaded_slots_metadata.keys():
                            if user_key not in CORE_SCHEMA.values():
                                save_state_dict[user_key] = st.session_state.get(user_key, '')
                    save_state_dict["messages"] = st.session_state.messages
                    config_keys = ["chat_engine_set", "ingest_engine_set", "harvest_engine_set", "shadow_engine_set", "transcribe_engine_set",
                                "selected_lens_name", "temp_val", "top_p_val", "memory_depth_val", "compaction_threshold",
                                "last_harvest_message_index", "review_mode_active", "auto_shadow_active"]
                    for key in config_keys:
                        save_state_dict[key] = st.session_state.get(key)
                    try:
                        save_project_state(save_state_dict, st.session_state.active_project, silent=True)
                    except Exception as save_e:
                        report_harvest_error(
                            "Harvest staged updates but failed to save project state",
                            str(save_e),
                        )
                    # --- END CRITICAL BLOCK ---

                    st.session_state["harvest_in_progress"] = False # Reset flag after save

                    # --- COMPACTION CHECK (This can also trigger a rerun) ---
                    final_mr_content = st.session_state.get('MI_Master_Record', '')
                    threshold = st.session_state.get('compaction_threshold', 50000)

                    if len(final_mr_content) > threshold:
                        st.toast("Threshold exceeded! Archiving and initiating compaction.")
                        current_overflow = st.session_state.get('MI_Master_Record_Overflow', '')
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        archive_header = f"\n\n--- ARCHIVED ON {timestamp} ---\n"
                        new_overflow_content = current_overflow + archive_header + final_mr_content
                        st.session_state['MI_Master_Record_Overflow'] = new_overflow_content
                        save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                        save_state_dict["messages"] = st.session_state.messages
                        save_state_dict["last_harvest_message_index"] = st.session_state.get("last_harvest_message_index", 0)
                        save_project_state(save_state_dict, st.session_state.active_project, silent=True)
                        st.session_state['compaction_choice_required'] = True
                        should_rerun_after_harvest = True # NEW: Set flag if compaction UI is needed

                    # Always rerun so Staging / harvest summary appear in the main pane.
                    st.rerun()

                except Exception as e:
                    st.session_state["harvest_in_progress"] = False
                    rescued = {}
                    try:
                        rescued = dict(current_harvest_proposals or {})
                    except Exception:
                        rescued = {}
                    if rescued:
                        pending = st.session_state.get("pending_updates")
                        if not isinstance(pending, dict):
                            pending = {}
                        pending.update(rescued)
                        st.session_state.pending_updates = pending
                    report_harvest_error(
                        "Harvest stopped before finishing",
                        str(e) + (f" Staged {len(rescued)} update(s) collected before the stop." if rescued else ""),
                    )
                    if not st.session_state.get("harvest_result"):
                        stats = locals().get("harvest_stats") or {}
                        st.session_state.harvest_result = {
                            "chunks": stats.get("chunks", 0) if isinstance(stats, dict) else 0,
                            "chunks_ok": stats.get("chunks_ok", 0) if isinstance(stats, dict) else 0,
                            "staged": list(rescued.keys()),
                            "identical": [],
                            "unrecognized": [],
                            "skipped_empty": [],
                            "skipped_not_dtn": [],
                            "skipped_small": [],
                            "skipped_mismatch": [],
                            "truncated": [],
                            "log": list((stats.get("log") if isinstance(stats, dict) else None) or []),
                            "advanced": False,
                            "index_reason": "left unharvested because harvest stopped before the final save",
                        }
                    st.rerun() # Rerun so Staging / the error appear in the main pane

        # SPARK BUTTON
        with col_act2:
            if st.button("⚡ Spark Insight", use_container_width=True): # Add use_container_width for consistency
                try:
                    c_eng = st.session_state.get('chat_engine_set', 'gemini-2.5-flash')
                    st.session_state.role_stats["chat"] += 1
                    with st.spinner("Connecting dots..."):
                        spark_client = get_llm_client(c_eng)
                        #genai.configure(api_key=st.secrets.get("GEMINI_API_KEY", default_key))
                        p = f"""[SYSTEM JOB: LATERAL THINKING]\nCONTEXT: {st.session_state.get('MI_Master_Record','')}\nINSTRUCTIONS: Find unrelated facts and force a collision. Propose a high-potential, high-uncertainty idea."""
                        resp = spark_client.models.generate_content(model=c_eng,contents =p,config={"temperature": temp_val if 'temp_val' in locals() else 0.7, "top_p": top_p_val if 'top_p_val' in locals() else 0.9})
    #                    resp = spark_model.generate_content(p)
                        st.session_state.messages.append({"role": "model", "content": f"⚡ **SPARK:**\n{resp.text}"})
                        st.rerun()
                except Exception as e: st.error(f"Spark Error: {e}")
                
        with col_act3: # NEW: Add this block for the Shadow button
            if st.button("🕵️ Run Shadow Now", use_container_width=True, help="Manually trigger the Shadow Engine to analyze recent chat history and propose metacognitive updates."):
                # Initialize a specific log for manual shadow runs
                st.session_state.shadow_logs_manual_run = ["🕵️ Shadow (Manual): Starting run..."]
                should_rerun_after_shadow = False # NEW: Flag to control the final rerun for Shadow
                #print(f"One") # NEW DEBUG

                # Check if Harvest is in progress - Shadow should defer
                if st.session_state.get("harvest_in_progress", False):
                    st.warning("⚠️ Shadow cannot run: Harvest operation is currently in progress. Please wait for it to complete.")
                    st.session_state.shadow_logs_manual_run.append("⚠️ Deferring: Harvest in progress.")
                    should_rerun_after_shadow = True # NEW: Flag to control the final rerun for Shadow
                    #print(f"Two") # NEW DEBUG
                                # Check if there are pending updates - Shadow should defer
                elif st.session_state.get('pending_updates') and len(st.session_state.get('pending_updates', {})) > 0:
                    st.warning("⚠️ Shadow cannot run: Pending updates exist in the Staging Area. Please approve or reject them first.")
                    st.session_state.shadow_logs_manual_run.append("⚠️ Deferring: Pending updates in Staging Area.")
                    should_rerun_after_shadow = True # NEW: Flag to control the final rerun for Shadow
                    #print(f"Three") # NEW DEBUG
                else:
                    try:
                        with st.spinner(f"Manual Shadow running..."):
                            current_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                            if "loaded_slots_metadata" in st.session_state:
                                for k, meta in st.session_state.loaded_slots_metadata.items():
                                    if meta['slot_type'] == 'user':
                                        current_state_dict[k] = st.session_state.get(k, '')

                            last_harvest_idx = st.session_state.get('last_harvest_message_index', 0)
                            history_delta = st.session_state.messages[last_harvest_idx:]
                            recent_history = [m for m in history_delta if "**🕵️ SHADOW LOGS**" not in m["content"]]
                            s_eng = st.session_state.get('shadow_engine_set', 'gemini-2.5-flash-lite') # Use the configured Shadow engine

                            st.session_state.role_stats["shadow"] += 1
                            # Retrieve current temperature and top_p values
                            current_temp_val = st.session_state.get('temp_val', 0.7)
                            current_top_p_val = st.session_state.get('top_p_val', 0.9)
                            #print(f"four") # NEW DEBUG

                            #st.info(f"DEBUG UI: Just before run_shadow_dispatcher call. top_p: {domain_content}")
                            #print(f"DEBUG CONSOLE: Just before run_shadow_dispatcher call. s_eng: {s_eng}, temp: {current_temp_val}, top_p: {current_top_p_val}")
                            # If "four" was the last console output, this will now be the last UI/console output.
                            # If this doesn't show, the problem is even earlier.

                            # CRITICAL DEBUG: Add st.stop() right here to see if the problem is AT THIS LINE.
                            # If the app stops and you *don't* see the st.info above, the issue is before this line.
                            # If you *do* see the st.info, then the issue is precisely in the function call.
                            #st.stop() # Temporarily uncomment this line for the most aggressive debug.

                            updates, logs_str = run_shadow_dispatcher(
                                recent_history,
                                current_state_dict,
                                st.session_state.loaded_slots_metadata,
                                st.session_state.domain_content, # <--- MODIFIED LINE
                                st.session_state.gemini_api_key,
                                current_temp_val,
                                current_top_p_val,
                                s_eng
                            )

                            # Append dispatcher logs to manual run logs
                            st.session_state.shadow_logs_manual_run.append(logs_str)

                            if updates:
                                is_review_active = st.session_state.get('review_mode_active', True)
                                any_staged = False
                                for k, update_data in updates.items():
                                    v_str = update_data["content"]
                                    is_suspect_from_shadow = update_data.get("suspect", False)

                                    # Smart Diff check (re-using logic from main Shadow loop)
                                    current_val = st.session_state.get(k, "")
                                    is_different = False

                                    # Normalize 'v_str' to string if it isn't already
                                    if isinstance(v_str, (dict, list)): v_str_norm = json.dumps(v_str, indent=2)
                                    else: v_str_norm = str(v_str)

                                    try:
                                        # 1. Try JSON Comparison
                                        obj_new = json.loads(v_str_norm)
                                        obj_curr = json.loads(str(current_val))
                                        if obj_new != obj_curr: is_different = True
                                    except:
                                        # 2. Fallback to a more robust String Comparison
                                        clean_v = v_str_norm.strip()
                                        if clean_v.startswith("---"):
                                            clean_v = clean_v[3:].lstrip()
                                        if clean_v.endswith("---"):
                                            clean_v = clean_v[:-3].rstrip()

                                        clean_current = str(current_val).strip()
                                        if clean_current.startswith("---"):
                                            clean_current = clean_current[3:].lstrip()
                                        if clean_current.endswith("---"):
                                            clean_current = clean_current[:-3].rstrip()

                                        if clean_v != clean_current:
                                            is_different = True

                                    if not is_different:
                                        st.session_state.shadow_logs_manual_run.append(f"💤 Shadow (Manual): '{k}' content identical, skipping update.")
                                        continue

                                    if st.session_state.loaded_slots_metadata.get(k, {}).get("ai_managed", False) and not shadow_preserves_existing(current_val, v_str):
                                        st.session_state.shadow_logs_manual_run.append(
                                            f"🛑 Shadow (Manual): Discarded '{k}' rewrite that would revert the current file."
                                        )
                                        continue

                                    # Check if target is already pending (from another engine or a prior Shadow run this session)
                                    if k in st.session_state.get('pending_updates', {}):
                                        st.session_state.shadow_logs_manual_run.append(f"⚠️ Shadow (Manual): Skipping staging for '{k}' as a proposal is already pending. Please resolve it first.")
                                        continue

                                    # Determine if this update should be staged or committed
                                    ai_managed = st.session_state.loaded_slots_metadata.get(k, {}).get('ai_managed', False)

                                    # Rule 1: If Review Mode is ON OR if the update is suspect, ALWAYS STAGE.
                                    if is_review_active or is_suspect_from_shadow:
                                        if "pending_updates" not in st.session_state: st.session_state.pending_updates = {}
                                        # Store the dictionary with content and suspect flag for staging area to unpack
                                        st.session_state.pending_updates[k] = {"content": v_str, "suspect": is_suspect_from_shadow}
                                        st.session_state.shadow_logs_manual_run.append(f"📝 Shadow (Manual): Staged update for '{k}'.")
                                        any_staged = True
                                    # Rule 2: If Review Mode is OFF AND it's an AI-managed slot (and not suspect) -> AUTO-COMMIT.
                                    elif not is_review_active and ai_managed and not is_suspect_from_shadow:
                                        # Commit directly to database (and session state)
                                        proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
                                        sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
                                        meta = st.session_state.loaded_slots_metadata.get(k, {})
                                        edit_summary = "AI-generated edit (manual Shadow run auto-commit)" # Placeholder for now
                                        db.save_slot(proj_db, sys_db, k, v_str, meta['slot_type'], meta['description'], meta['scope'], meta['owner_id'], meta.get('is_distributed', False),meta.get('part_of_dtn', False), ai_managed=True, old_content=current_val, edit_summary=edit_summary)
                                        commit_slot_text(k, v_str, replaced_text=current_val)
                                        st.session_state.shadow_logs_manual_run.append(f"✅ Shadow (Manual): Auto-committed '{k}'.")
                                    # Rule 3: If Review Mode is OFF AND it's NOT an AI-managed slot (and not suspect) -> Shadow takes no action.
                                    else:
                                        st.session_state.shadow_logs_manual_run.append(f"💤 Shadow (Manual): '{k}': Not AI-managed, Review Mode OFF, and not suspect. Shadow took no action.")

                                if any_staged:
                                    persist_pending_updates_now()
                                    st.success("Shadow run complete. Proposed updates are in the Staging Area.")
                                    should_rerun_after_shadow = True # NEW: Set flag to rerun if updates are staged
                                else:
                                    st.success("Shadow run complete. No new updates proposed or all were auto-committed.")
                            else:
                                st.info("Shadow run complete. No updates required.")
                    except Exception as e:
                        err_msg = str(e)
                        if "429" in err_msg or "ResourceExhausted" in err_msg:
                            st.warning("AI rate limit reached for Shadow. Please try again in a moment.")
                            st.session_state.shadow_logs_manual_run.append("🚦 429 Quota Hit - Skipping Shadow.")
                        else:
                            st.error(f"Shadow (Manual) Error: {e}")
                            st.session_state.shadow_logs_manual_run.append(f"💀 CRASH (Manual Shadow): {err_msg}")
                        should_rerun_after_shadow = True # NEW: Set flag to rerun on error
                # --- FINAL RERUN CONTROL FOR MANUAL SHADOW ---
                if should_rerun_after_shadow:
                    st.rerun()

        if "auto_shadow_active" not in st.session_state:
            st.session_state.auto_shadow_active = True
        auto_shadow_key = f"auto_shadow_toggle::{st.session_state.active_project}"
        if auto_shadow_key not in st.session_state:
            st.session_state[auto_shadow_key] = bool(st.session_state.auto_shadow_active)
        auto_shadow_on = st.toggle(
            "🕵️ Auto Shadow after each prompt",
            help="When off, chat replies skip the automatic Shadow pass. Use Run Shadow Now to run it by hand.",
            key=auto_shadow_key,
        )
        if auto_shadow_on != bool(st.session_state.auto_shadow_active):
            turning_on = bool(auto_shadow_on) and not bool(st.session_state.auto_shadow_active)
            st.session_state.auto_shadow_active = auto_shadow_on
            persist_engine_room_settings(st.session_state.active_project)
            if turning_on:
                # Catch-up on this same paint left a pale double shell on mobile.
                st.session_state._defer_auto_shadow_catchup = True
                st.session_state.pending_auto_shadow = True
            # Remount immediately after the toggle — continuing the sidebar/main
            # paint in the same transition was stacking ghost UI on phones.
            st.rerun()

        # --- [NEW] DATA LIFECYCLE CONTROLS ---
        st.markdown('<div class="section-header">DATA LIFECYCLE</div>', unsafe_allow_html=True)

        # Get values needed for the check
        mr_content = st.session_state.get('MI_Master_Record', '')
        threshold = st.session_state.get('compaction_threshold', 50000)
        is_over_threshold = len(mr_content) > threshold

        # Use columns for a clean layout of the button and its warning
        col_compact_btn, col_compact_warn = st.columns(2)

        with col_compact_btn:
            if st.button("📦 Compact Master Record", use_container_width=True, help="Manually trigger the MI_Master_Record compaction choice (Summary or Refactor)."):
                st.toast("Archiving current Master Record...")
                # --- START: ADDED ARCHIVAL LOGIC ---
                current_mr_content = st.session_state.get('MI_Master_Record', '')
                current_overflow = st.session_state.get('MI_Master_Record_Overflow', '')
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                archive_header = f"\n\n--- ARCHIVED ON {timestamp} ---\n"

                # Append the current record to the overflow
                new_overflow_content = current_overflow + archive_header + current_mr_content
                st.session_state['MI_Master_Record_Overflow'] = new_overflow_content

                # Save the state immediately to commit the new Overflow content
                save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                save_state_dict["messages"] = st.session_state.messages
                save_project_state(save_state_dict, st.session_state.active_project, silent=True)
                # --- END: ADDED ARCHIVAL LOGIC ---

                st.session_state['compaction_choice_required'] = True
                st.rerun()
        with col_compact_warn:
            if is_over_threshold:
                st.caption("⚠️ **Over Threshold**")


        # SAVE BUTTON
        if st.button("💾 Save Project State", use_container_width=True):
            save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
            save_state_dict["messages"] = st.session_state.messages
            # Add User slots to manual save too
            if "loaded_slots_metadata" in st.session_state:
                save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                for u in st.session_state.loaded_slots_metadata:
                    save_state_dict[u] = st.session_state.get(u, "")

            # [NEW] Add Config Vars to the Save Dict
            save_state_dict["chat_engine_set"] = st.session_state.chat_engine_set
            save_state_dict["ingest_engine_set"] = st.session_state.ingest_engine_set
            save_state_dict["harvest_engine_set"] = st.session_state.harvest_engine_set
            save_state_dict["shadow_engine_set"] = st.session_state.shadow_engine_set
            save_state_dict["transcribe_engine_set"] = st.session_state.get("transcribe_engine_set", "gemini-2.5-flash-lite")
            save_state_dict["selected_lens_name"] = st.session_state.selected_lens_name
            save_state_dict["temp_val"] = st.session_state.get('temp_val', 0.7)
            save_state_dict["top_p_val"] = st.session_state.get('top_p_val', 0.9)
            save_state_dict["memory_depth_val"] = st.session_state.get('memory_depth_val', 3) # <--- ADD THIS LINE
            save_state_dict["compaction_threshold"] = st.session_state.get('compaction_threshold', 50000)
            save_state_dict["last_harvest_message_index"] = st.session_state.get('last_harvest_message_index', 0) # <--- ADD THIS LINE
            save_state_dict["review_mode_active"] = st.session_state.get('review_mode_active', True) # <--- ADD THIS LINE
            save_state_dict["auto_shadow_active"] = st.session_state.get('auto_shadow_active', True)
            save_project_state(save_state_dict, st.session_state.active_project)
            st.success("State Saved to Disk")

        # 5. RECENT PROMPTS (RESTORED)
        with st.expander("⏮️ Recent Prompts"):
            user_msgs = [m["content"] for m in st.session_state.messages if m["role"] == "user"]
            unique_msgs = list(dict.fromkeys(user_msgs))
            for msg in reversed(unique_msgs[-5:]):
                st.code(msg, language="markdown", wrap_lines=True)

        # --- NEW: PRINT CHAT CONVERSATION ---
        st.markdown('<div class="section-header">🖨️ Print</div>', unsafe_allow_html=True)
        if st.button("🖨️ Print Chat Conversation", use_container_width=True, help="Prints the current chat history and reloads."):
            chat_html_content = ""
            for msg in st.session_state.messages:
                role_class = "user-message" if msg["role"] == "user" else "model-message"
                escaped_content = html.escape(msg["content"])
                chat_html_content += f'<div class="chat-message {role_class}"><b>{msg["role"].capitalize()}:</b> {escaped_content}</div>'

            # 1. Prepare the full HTML document for the iframe
            # The chat-container div is now part of the content_html passed to prepare_printable_html
            full_html_document = prepare_printable_html(f'<div class="chat-container">{chat_html_content}</div>', "Gatewai Chat Conversation")

            # 2. Inject the JavaScript to create iframe and trigger print
            emit_browser_print(full_html_document)
            st.toast("Sending chat transcript to the printer…")
            # No st.rerun() here, as the JS will trigger a reload.
        # --- END NEW ---

	# [INSERT THIS BEFORE 'Recent Prompts' or 'Danger Zone']
        st.markdown("---")
        st.caption("History Control")
        if st.button("↩️ Undo Last Turn (Scrub Error)", use_container_width=True):
            msgs = st.session_state.get("messages") or []
            if len(msgs) >= 2:
                # Remove last AI response + user prompt
                st.session_state.messages.pop()
                st.session_state.messages.pop()
                scrub_chat_hold()
                save_state_dict = {k: st.session_state.get(k, "") for k in CORE_SCHEMA.values()}
                save_state_dict["messages"] = st.session_state.messages
                save_project_state(save_state_dict, st.session_state.active_project)
                st.toast("Rewound history by 1 turn.")
                # No st.rerun() — the button click already remounts; a second
                # rerun was stacking pale double UI on mobile.
            elif (
                len(msgs) == 1
                and isinstance(msgs[-1], dict)
                and msgs[-1].get("role") == "user"
            ):
                # Lone unanswered prompt (common after mobile drop mid-reply)
                st.session_state.messages.pop()
                scrub_chat_hold()
                save_state_dict = {k: st.session_state.get(k, "") for k in CORE_SCHEMA.values()}
                save_state_dict["messages"] = st.session_state.messages
                save_project_state(save_state_dict, st.session_state.active_project)
                st.toast("Discarded unanswered prompt.")
            else:
                st.warning("History is empty.")

        # 5c. THE HISTORY SURGEON (Deep Fix)
        with st.expander("🩺 History Surgeon"):
           # --- NEW: AI Edit History Section ---
            st.subheader("🤖 AI Edit History")
            selected_slot_for_edits = st.selectbox(
                "Select Slot for Edit History:",
                options=[""] + list(st.session_state.loaded_slots_metadata.keys()),
                label_visibility="collapsed",
                key="ai_edit_history_slot_selector"
            )

            if selected_slot_for_edits:
                _proj_db, _sys_db, _scope, slot_db = database_for_slot(selected_slot_for_edits)
                edit_history = db.get_ai_edit_history(slot_db, selected_slot_for_edits)

                if edit_history:
                    for edit in edit_history:
                        with st.container(border=True):
                            st.markdown(f"**Edit ID:** {edit['edit_id']}")
                            st.caption(f"Timestamp: {edit['timestamp']}")
                            if edit['edit_summary']:
                                st.info(f"Summary: {edit['edit_summary']}")

                            def _clip_preview(text, limit=700):
                                text = "" if text is None else str(text)
                                if len(text) <= limit:
                                    return text
                                return text[:limit] + f"\n… {len(text) - limit:,} more characters"

                            col_old, col_new = st.columns(2)
                            with col_old:
                                st.caption("Previous text")
                                st.code(_clip_preview(edit['old_content']), language="markdown", wrap_lines=True)
                            with col_new:
                                st.caption("Replaced with")
                                st.code(_clip_preview(edit['new_content']), language="markdown", wrap_lines=True)

                            if st.button("↩️ Undo This Edit", key=f"undo_{edit['edit_id']}", use_container_width=True):
                                restored = db.undo_ai_edit(slot_db, edit['edit_id'], selected_slot_for_edits)
                                if restored is not None:
                                    commit_slot_text(selected_slot_for_edits, restored)
                                    st.toast("Restored the previous text.")
                                    st.rerun()
                                st.error("That saved copy could not be found.")
                else:
                    st.info("No AI edits found for this slot.")
            else:
                st.caption("Select a slot to view its AI edit history.")
            st.markdown("---")
            # --- Existing User Prompt Deletion ---
            st.subheader("User Prompt Deletion")

            # Filter history to find User prompts (and their indices)
            # We skip index 0 if it's a system init message, but usually history is User/Model pairs
            user_turns = []
            for i, m in enumerate(st.session_state.messages):
                if m["role"] == "user":
                    # Store (Index, Preview Text)
                    preview = m["content"][:40] + "..." if len(m["content"]) > 40 else m["content"]
                    user_turns.append((i, preview))

            # Reverse list so most recent is at top
            user_turns.reverse()

            if user_turns:
                # Select box shows the text, returns the (index, preview) tuple
                selected_turn = st.selectbox(
                    "Select Turn to Delete:",
                    options=user_turns,
                    format_func=lambda x: f"#{x[0]}: {x[1]}",
                )

                if st.button(
                    "✂️ Amputate Selected Turn",
                    help="Deletes this User prompt AND the AI reply following it.",
                ):
                    idx = selected_turn[0] if isinstance(selected_turn, tuple) else selected_turn

                    # Pop larger index first so earlier indices stay valid.
                    if idx + 1 < len(st.session_state.messages):
                        st.session_state.messages.pop(idx + 1)
                    st.session_state.messages.pop(idx)
                    scrub_chat_hold()

                    save_state_dict = {k: st.session_state.get(k, "") for k in CORE_SCHEMA.values()}
                    save_state_dict["messages"] = st.session_state.messages
                    save_project_state(save_state_dict, st.session_state.active_project)

                    st.toast("Deletion Successful. Memory cleaned.")
                    st.rerun()
            else:
                st.caption("No history to edit.")

        # --- NEW: Harvest Rewind ---
        st.markdown('<div class="section-header">Harvest Rewind</div>', unsafe_allow_html=True)
        try:
            saved_pointer = int(st.session_state.get("last_harvest_message_index") or 0)
        except (TypeError, ValueError):
            saved_pointer = 0
        max_idx = len(st.session_state.messages)
        display_pointer = min(max(saved_pointer, 0), max_idx)
        pointer_key = f"harvest_pointer_input::{st.session_state.active_project}"
        sync_token = (st.session_state.active_project, saved_pointer)
        # Seed the box from this project's saved pointer. Leave a typed edit
        # alone until Harvest or Reset changes the saved value.
        if pointer_key not in st.session_state or st.session_state.get("_harvest_pointer_sync") != sync_token:
            st.session_state[pointer_key] = display_pointer
            st.session_state._harvest_pointer_sync = sync_token

        st.caption(f"Saved for **{st.session_state.active_project}**: message {saved_pointer} of {max_idx}.")
        new_harvest_idx = st.number_input(
            "Set Harvest Pointer to Message Index:",
            min_value=0,
            max_value=max_idx,
            step=1,
            key=pointer_key,
            help="Messages from this index onwards will be considered 'un-harvested'. Set to 0 to re-harvest everything."
        )

        if st.button("↩️ Reset Harvest Pointer", use_container_width=True, key="reset_harvest_pointer_btn"):
            st.session_state.last_harvest_message_index = int(new_harvest_idx)
            st.session_state._harvest_pointer_sync = (st.session_state.active_project, int(new_harvest_idx))

            # Build a COMPLETE snapshot of the entire current project state for saving.
            save_state_dict = {}
            for core_key in CORE_SCHEMA.values():
                save_state_dict[core_key] = st.session_state.get(core_key, '')
            if "loaded_slots_metadata" in st.session_state:
                save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                for user_key in st.session_state.loaded_slots_metadata.keys():
                    if user_key not in CORE_SCHEMA.values():
                        save_state_dict[user_key] = st.session_state.get(user_key, '')
            save_state_dict["messages"] = st.session_state.messages
            config_keys = ["chat_engine_set", "ingest_engine_set", "harvest_engine_set", "shadow_engine_set", "transcribe_engine_set",
                           "selected_lens_name", "temp_val", "top_p_val", "memory_depth_val", "compaction_threshold","last_harvest_message_index", "review_mode_active", "auto_shadow_active"] # ADD THIS KEY
            for key in config_keys:
                save_state_dict[key] = st.session_state.get(key) # Ensure it's pulled from session_state

            # Save the complete state snapshot to the database.
            save_project_state(save_state_dict, st.session_state.active_project, silent=True)

            st.toast(f"Harvest pointer reset to message index {new_harvest_idx}. Next Harvest will process from here.")
            st.rerun()
        st.info("⚠️ Use with caution! Resetting the pointer and re-harvesting can lead to duplicate content if not managed carefully.")


        # 6. DANGER ZONE (RESTORED)
        with st.expander("🚨 Danger Zone"):
            if st.button("🧹 Wipe Chat Only"):
                st.session_state.messages = []
                st.session_state.last_harvest_message_index = 0 # <-- ADDED
                scrub_chat_hold()
                # Simple save to clear history on disk
                save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                save_state_dict["messages"] = []
                save_project_state(save_state_dict, st.session_state.active_project)
                st.rerun()

            if st.button("🔴 Purge ALL Context"):
                st.session_state.messages = []
                st.session_state.last_harvest_message_index = 0 # <-- ADDED
                st.session_state.temp_web_ctx = ""
                scrub_chat_hold()
                save_state_dict = {k: st.session_state.get(k,'') for k in CORE_SCHEMA.values()}
                save_state_dict["messages"] = []
                save_project_state(save_state_dict, st.session_state.active_project)
                st.rerun()

# --- TAB 2: MANAGER (Fixed Attribution) ---

    with tab_manager:
        st.markdown('<div class="section-header">🆕 CREATE SLOT</div>', unsafe_allow_html=True)

        # 1. Name & Scope
        c_name, c_scope = st.columns(2)
        with c_name:
            new_draft_name = st.text_input("Name", placeholder="e.g. Script_v1", label_visibility="collapsed", key="create_name_input")
        with c_scope:
            # KEY FIX: Added 'key' to force immediate UI update
            new_scope = st.selectbox("Scope", ["Project", "Global", "Lens"], label_visibility="collapsed", key="create_scope_selector")

        # 2. Dynamic Lens Selector (Reactive)
        target_owner = "system" # Default

        if new_scope == "Lens":
            with st.container():
                # Fetch available lenses
                domain_opts = [f.replace(".txt", "") for f in os.listdir(DOMAINS_DIR) if f.endswith(".txt")]

                # Smart Default: Select the currently active lens
                cur_lens_idx = 0
                if st.session_state.get('selected_lens_name') in domain_opts:
                    cur_lens_idx = domain_opts.index(st.session_state.selected_lens_name)

                st.caption("🔍 **Lens Attribution**")
                target_lens = st.selectbox("Assign to:", domain_opts, index=cur_lens_idx, key="create_lens_selector", label_visibility="collapsed")
                st.info(f"File will be visible only when **{target_lens}** is active.")
                target_owner = target_lens

        elif new_scope == "Project":
            target_owner = st.session_state.active_project

        elif new_scope == "Global":
            target_owner = "system"

        # 3. Purpose
        new_desc = st.text_input("Purpose", placeholder="Instructions for AI...", key="create_desc_input")
        # --- NEW: AI-Managed Toggle on Creation ---
        ai_managed_on_create = st.checkbox("🤖 AI-Managed Draft", value=False, key="ai_managed_create", help="Allow AI to make edits without staging.")
        _, create_folder_val = render_folder_picker("create_slot", current="", metadata=st.session_state.get("loaded_slots_metadata", {}))

        # 4. Create Action
        if st.button("➕ Create Slot", use_container_width=True, key="create_slot_btn"):
            if new_draft_name:
                clean_name = new_draft_name.strip()
                # Replace spaces with underscores
                clean_name = clean_name.replace(" ", "_")
                # Ensure a file extension (default to .md if none or invalid)
                if "." not in clean_name or not clean_name.split(".")[-1].isalnum():
                    clean_name += ".md"
                # Convert to lowercase for internal consistency
                clean_name = clean_name.lower()
                create_folder = normalize_folder_name(create_folder_val)
                # --- END NEW: FILENAME NORMALIZATION ---

                # Database Save
                proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
                sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
                db.save_slot(proj_db, sys_db, clean_name, "", "user", new_desc if new_desc else "User Draft", new_scope.lower(), target_owner, ai_managed=ai_managed_on_create, folder=create_folder)

                # RAM Update
                if "loaded_slots_metadata" not in st.session_state: st.session_state.loaded_slots_metadata = {}
                st.session_state.loaded_slots_metadata[clean_name] = {
                    "slot_name": clean_name, "slot_type": "user", "scope": new_scope.lower(),
                    "owner_id": target_owner, "description": new_desc if new_desc else "User Draft", "content": "",
                    "ai_managed": ai_managed_on_create,
                    "folder": create_folder,
                }

                # Force Refresh
                st.session_state.loaded_project = None
                st.success(f"Created {clean_name} ({new_scope}: {target_owner}" + (f", {create_folder})" if create_folder else ")"))
                st.rerun()

        st.markdown("---")

        if "loaded_slots_metadata" in st.session_state:
            # Filter for User slots
            user_slots_list = sorted(
                [k for k, v in st.session_state.loaded_slots_metadata.items() if v.get('slot_type') == 'user'],
                key=str.lower,
            )

            def format_slot_label(key):
                if not key: return ""
                meta = st.session_state.loaded_slots_metadata.get(key, {})
                scope = meta.get('scope', 'project').title()
                owner = meta.get('owner_id', '')
                ai_managed = meta.get('ai_managed', False)
                ai_status = " (AI-Managed)" if ai_managed else ""
                folder = normalize_folder_name(meta.get("folder", ""))
                folder_bit = f" [{folder}]" if folder else ""
                if scope == "Lens": return f"{key}{folder_bit} ({owner}){ai_status}"
                return f"{key}{folder_bit} ({scope}){ai_status}"

            # 5. Assign existing slots to folders (bulk move / create folder by naming it)
            st.markdown('<div class="section-header">📁 ASSIGN TO FOLDER</div>', unsafe_allow_html=True)
            st.caption("Move one or more user slots into an existing folder, Ungrouped, or a new folder name.")
            assign_slots = st.multiselect(
                "Slots to move",
                options=user_slots_list,
                format_func=format_slot_label,
                key="assign_folder_slots",
                placeholder="Choose memory slots...",
            )
            assign_mode, assign_folder_val = render_folder_picker(
                "assign_folder",
                current="",
                metadata=st.session_state.loaded_slots_metadata,
            )
            # Only require slot selection here — new folder names are validated on click
            # so typing into "New folder name" doesn't leave the button stuck disabled.
            if st.button(
                "📁 Move selected to folder",
                use_container_width=True,
                key="assign_folder_btn",
                disabled=not assign_slots,
                help="Creates the folder name by assigning slots to it when you choose + New folder...",
            ):
                dest = normalize_folder_name(assign_folder_val)
                if assign_mode == "new" and not dest:
                    st.warning("Enter a new folder name, or pick Ungrouped / an existing folder.")
                else:
                    changed = 0
                    for slot_name in assign_slots:
                        if persist_slot_folder(slot_name, dest):
                            changed += 1
                    dest_label = dest if dest else "Ungrouped"
                    st.success(f"Assigned {len(assign_slots)} slot(s) to **{dest_label}** ({changed} updated).")
                    st.rerun()

            st.markdown("---")

            # 6. Enhanced Delete / Edit (Shows Attribution)
            st.markdown('<div class="section-header">🗂️ EDIT / DELETE SLOT</div>', unsafe_allow_html=True)

            to_del = st.selectbox("Select File", [""] + user_slots_list, format_func=format_slot_label, label_visibility="collapsed", key="delete_slot_selector")

            # --- NEW: Add a container to hold metadata and the AI-Managed toggle ---
            with st.container():
                if to_del: # Only show metadata if a slot is selected
                    meta = st.session_state.loaded_slots_metadata[to_del]
                    st.caption(f"Scope: {meta.get('scope', 'project').title()}")
                    st.caption(f"Description: {meta.get('description', '')}")
                    current_folder = normalize_folder_name(meta.get("folder", ""))
                    st.caption(f"Folder: {current_folder if current_folder else 'Ungrouped'}")

                    current_ai_managed_status = meta.get('ai_managed', False) # Get the actual status from metadata
                    ai_managed_checkbox_value = st.checkbox("🤖 AI-Managed Draft",
                                            value=current_ai_managed_status, # <-- CRITICAL FIX: Read from metadata
                                            key=f"ai_managed_toggle_{to_del}", # <-- Use a unique key to prevent conflicts
                                            help="Allow AI to make edits without staging.",
                                            disabled=to_del in CORE_SCHEMA.values())

                    if ai_managed_checkbox_value != current_ai_managed_status: # Compare against the loaded state
                        proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
                        sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
                        db.set_ai_managed_status(proj_db, to_del, ai_managed_checkbox_value)  # Function to update the DB
                        st.session_state.loaded_slots_metadata[to_del]['ai_managed'] = ai_managed_checkbox_value
                        st.rerun()  # Refresh UI
            # --- END NEW ---

            if st.button("❌ Delete File", disabled=(not to_del), use_container_width=True, key="delete_slot_btn"):
                proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
                sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
                meta = st.session_state.loaded_slots_metadata[to_del]

                db.delete_slot(proj_db, sys_db, to_del, meta['scope'])

                del st.session_state.loaded_slots_metadata[to_del]
                if to_del in st.session_state: del st.session_state[to_del]

                st.success(f"Deleted {to_del}")
                st.rerun()

# --- TAB 3: CONFIG (Full Spectrum: Persistence, Auto-Tune, & Utilities) ---
    with tab_config:

        st.markdown('<div class="section-header">⚙️ ENGINE ROOM</div>', unsafe_allow_html=True)

        # 1. MODEL DEFINITIONS
        model_options = [
            "gemini-2.0-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-3.1-pro-preview",
            "deepseek-flash",
            "deepseek-v4-pro",
        ]

        # 2. LENS PARAMETER MAP (Auto-Tune Logic)
        LENS_PARAMS = {
            "Creative Story":    {"t": 0.90, "p": 1.00},
            "Agile Strategy":    {"t": 0.70, "p": 0.90},
            "General":           {"t": 0.70, "p": 0.90},
            "PhD Operations":    {"t": 0.30, "p": 0.85},
            "Technical Writing": {"t": 0.20, "p": 0.80}
        }

        def coerce_choice(options, value, fallback):
            if value in options:
                return value
            if fallback in options:
                return fallback
            return options[0]

        def bind_choice(canonical, options, fallback, label, help_text=None):
            widget_key = engine_room_widget_key(canonical)
            if (
                st.session_state.get("_engine_room_reseed")
                or widget_key not in st.session_state
                or st.session_state.get(widget_key) not in options
            ):
                st.session_state[widget_key] = coerce_choice(options, st.session_state.get(canonical), fallback)
            choice = st.selectbox(label, options, key=widget_key, **({"help": help_text} if help_text else {}))
            st.session_state[canonical] = choice
            return choice

        def bind_number(canonical, low, high, fallback, as_int=False):
            widget_key = engine_room_widget_key(canonical)
            current = st.session_state.get(widget_key, st.session_state.get(canonical, fallback))
            try:
                number = float(current)
                valid = low <= number <= high
            except (TypeError, ValueError):
                number = float(fallback)
                valid = False
            if st.session_state.get("_engine_room_reseed") or widget_key not in st.session_state or not valid:
                seed = st.session_state.get(canonical, fallback)
                try:
                    number = float(seed)
                except (TypeError, ValueError):
                    number = float(fallback)
                number = min(high, max(low, number))
                st.session_state[widget_key] = int(number) if as_int else number
            return widget_key

        # Each project has its own menus. A shared menu was keeping one project's
        # engines and sometimes writing the built-in defaults over the saved ones.
        chat_engine = bind_choice("chat_engine_set", model_options, "gemini-2.5-flash", "Chat Engine:")
        ingest_engine = bind_choice("ingest_engine_set", model_options, "gemini-2.5-pro", "Ingest Engine (Files):", "For reading PDFs/Docs")
        harvest_engine = bind_choice("harvest_engine_set", model_options, "gemini-2.5-pro", "Harvest Engine (Sync):", "For compiling Master Record")
        st.caption("ℹ️ *Recommend 'Pro' for Harvest. Quality > Speed.*")
        shadow_engine = bind_choice("shadow_engine_set", model_options, "gemini-2.5-flash-lite", "Shadow Engine (Background):", "For quick triage")

        transcribe_options = [m for m in model_options if not is_deepseek_engine(m)]
        transcribe_engine = bind_choice(
            "transcribe_engine_set",
            transcribe_options,
            "gemini-2.5-flash-lite",
            "Transcription Engine (Dictation):",
            "Speech-to-text for the chat mic. Gemini only — DeepSeek cannot transcribe audio.",
        )
        st.caption("ℹ️ Dictation is Gemini-only. Flash-Lite is usually enough.")

        selected_engines = [chat_engine, ingest_engine, harvest_engine, shadow_engine]
        if any(is_deepseek_engine(eng) for eng in selected_engines):
            st.caption("ℹ️ DeepSeek engines use the DeepSeek API key in the sidebar. Google Search grounding stays Gemini-only.")
            if not st.session_state.get("deepseek_api_key"):
                st.warning("DeepSeek is selected, but no DeepSeek API key is set.")

        # --- DOMAIN LENS (With Auto-Tune) ---
        domain_files = [f.replace(".txt", "") for f in os.listdir(DOMAINS_DIR) if f.endswith(".txt")]
        project_name = st.session_state.active_project
        if domain_files:
            selected_domain = bind_choice("selected_lens_name", domain_files, "General", "Domain Lens:")
            applied_key = f"_applied_lens::{project_name}"
            if applied_key not in st.session_state:
                st.session_state[applied_key] = selected_domain
            elif selected_domain != st.session_state.get(applied_key):
                defaults = LENS_PARAMS.get(selected_domain, {"t": 0.7, "p": 0.9})
                st.session_state.temp_val = defaults["t"]
                st.session_state.top_p_val = defaults["p"]
                st.session_state[engine_room_widget_key("temp_val")] = defaults["t"]
                st.session_state[engine_room_widget_key("top_p_val")] = defaults["p"]
                st.session_state.domain_content = load_domain_content(selected_domain)
                st.session_state[applied_key] = selected_domain
                st.rerun()
        else:
            selected_domain = st.session_state.get("selected_lens_name", "General")

        st.markdown("---")

        # --- HYPERPARAMETERS ---
        st.caption("Hyperparameters")

        temp_key = bind_number("temp_val", 0.0, 2.0, 0.7)
        temp_val = st.slider("Temperature", 0.0, 2.0, step=0.05, key=temp_key)
        st.session_state.temp_val = temp_val

        top_p_key = bind_number("top_p_val", 0.0, 1.0, 0.9)
        top_p_val = st.slider("Top-P", 0.0, 1.0, step=0.05, key=top_p_key)
        st.session_state.top_p_val = top_p_val

        st.markdown("---")
        st.caption("Data Lifecycle")

        compaction_key = bind_number("compaction_threshold", 10000, 200000, 50000, as_int=True)
        compaction_threshold = st.number_input(
            "MR Compaction Threshold (chars)",
            min_value=10000,
            max_value=200000,
            step=5000,
            key=compaction_key,
            help="When MI_Master_Record exceeds this size after a Harvest, the Compaction Protocol will trigger."
        )
        st.session_state.compaction_threshold = compaction_threshold

        depth_key = bind_number("memory_depth_val", 1, 10, 3, as_int=True)
        memory_depth = st.slider("Context Depth (Turns)", 1, 10, key=depth_key)
        st.session_state.memory_depth_val = memory_depth

        st.markdown("---")

        depth = st.session_state.get("memory_depth_val", 3)
        recent = st.session_state.messages[-(depth * 2):]
        weight = len(str(recent)) + len(str(st.session_state.get("temp_web_ctx") or ""))
        for k in CORE_SCHEMA.values():
            val = st.session_state.get(k, "")
            weight += len(val if isinstance(val, str) else str(val))
        est_tokens = weight // 4
        st.caption(f"Context Weight: {est_tokens:,} tokens")
        st.progress(min(est_tokens / 1000000, 1.0))

        st.markdown("---")

        st.caption("🛡️ Safety Protocols")
        if "review_mode_active" not in st.session_state:
            st.session_state.review_mode_active = True
        review_key = engine_room_widget_key("review_mode_active")
        if st.session_state.get("_engine_room_reseed") or review_key not in st.session_state:
            st.session_state[review_key] = bool(st.session_state.get("review_mode_active", True))
        review_mode = st.checkbox(
            "Enable Review Mode (Check changes before saving)",
            key=review_key,
        )
        st.session_state.review_mode_active = review_mode

        persist_engine_room_settings(st.session_state.active_project)


        # --- UTILS: DATA RESCUE ---
        st.caption("🚑 Data Rescue")
        if st.button("♻️ Restore from .bak Files"):
            proj_dir = os.path.join(BASE_PROJECT_DIR, st.session_state.active_project)
            db_path = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
            restore_map = {
                "MI_Master_Record": "summary.txt.bak", "MC_Frontier": "frontier.txt.bak",
                "MI_Project_Status": "status.txt.bak", "MI_Sticky_Notes": "notes.txt.bak",
                "MC_Assumptions": "assumptions.txt.bak", "MC_Blindspots": "blindspots.txt.bak"
            }
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            count = 0
            for key, filename in restore_map.items():
                f_path = os.path.join(proj_dir, filename)
                if os.path.exists(f_path):
                    with open(f_path, "r", encoding="utf-8") as f:
                        c.execute('INSERT INTO memory_slots (slot_name, content, slot_type, description, scope, last_updated) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(slot_name) DO UPDATE SET content=excluded.content', (key, f.read(), 'core', 'Restored File', 'project', datetime.now()))
                        count += 1
            conn.commit(); conn.close()
            if count > 0: st.success(f"Restored {count} files!"); st.rerun()
            else: st.warning("No .bak files found.")

    # --- NEW TAB 4: HELP (AI-Assisted Help) ---
    with tab_help:
        st.markdown("### ❓ Gatewai Help Assistant")
        st.info("Ask me anything about using Gatewai! Your questions and my answers here are separate from your project's main chat and won't be saved to the project history.")

        # Display help chat messages
        help_chat_container = st.container(height=400, border=True)
        with help_chat_container:
            for msg in st.session_state.help_messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

        # Help chat input
        help_prompt = st.chat_input("Ask about Gatewai features, workflow, or troubleshooting...", key=f"help_chat_input_{st.session_state.help_chat_input_key}")

        if help_prompt:
            # Add user message to help chat history
            st.session_state.help_messages.append({"role": "user", "content": help_prompt})

            # Build context for the help AI
            # Include MI_Master_Record, MC_Focus, SI_Identity, SI_User_Profile, SI_Observed_Profile
            help_context = ""
            help_context += f"--- MI_Master_Record (Project Overview) ---\n{st.session_state.get('MI_Master_Record', 'No project overview available.')}\n\n"
            help_context += f"--- MC_Focus (Current Project Focus) ---\n{st.session_state.get('MC_Focus', 'No immediate project focus defined.')}\n\n"
            help_context += f"--- SI_Identity (Gatewai's Core Identity) ---\n{st.session_state.get('SI_Identity', 'Gatewai is a Cognitive IDE. User as Executive; AI as Proposer.')}\n\n"
            help_context += f"--- SI_User_Profile (Your Profile) ---\n{st.session_state.get('SI_User_Profile', 'No user profile defined.')}\n\n"
            help_context += f"--- SI_Observed_Profile (Your Observed Profile) ---\n{st.session_state.get('SI_Observed_Profile', 'No user observed profile defined.')}\n\n"

            # System instruction for the help AI
            help_sys_instr = (
                "You are the Gatewai Help Assistant. Your role is to provide clear, concise, and accurate information "
                "about how to use the Gatewai system. Refer to the provided context about Gatewai's identity, "
                "the user's project, and the user's profiles to tailor your answers. "
                "Focus strictly on explaining Gatewai's features, workflow, and troubleshooting. "
                "Do NOT engage in project-specific tasks or act as a co-pilot for the user's project. "
                "Your responses should be helpful and instructional. Do not output internal reasoning."
            )

            # Prepare history for the help AI (only the help chat history)
            help_history_for_ai = [{"role": m["role"], "parts": [{"text": m["content"]}]} for m in st.session_state.help_messages[:-1]]

            # AI call for help
            try:
                help_engine = st.session_state.get('chat_engine_set', 'gemini-2.5-flash-lite') # Use the chat engine for help
                help_ai_client = get_llm_client(help_engine)

                help_response = help_ai_client.models.generate_content(
                    model=help_engine,
                    contents=help_history_for_ai + [{"role": "user", "parts": [{"text": help_prompt}]}], # Add current prompt to contents
                    config=types.GenerateContentConfig(
                        system_instruction=help_sys_instr + "\n\n" + help_context, # Combine system instruction with context
                        temperature=0.5, # Keep help responses factual and less creative
                        top_p=0.9
                    )
                )

                help_ai_response_text = help_response.text.strip()
                st.session_state.help_messages.append({"role": "model", "content": help_ai_response_text})

            except Exception as e:
                print(f"DEBUG: Error in Help AI call: {e}") # Print the actual error for debugging
                st.error(f"Error getting help: {e}")
                st.session_state.help_messages.append({"role": "model", "content": "Sorry, I encountered an error trying to fetch help. Please try again."})

            # Increment key to clear input box
            st.session_state.help_chat_input_key += 1
            st.rerun()

        # Button to clear help chat history
        if st.button("🗑️ Clear Help Chat", use_container_width=True):
            st.session_state.help_messages = []
            st.session_state.help_chat_input_key += 1 # Increment key to reset input widget
            st.rerun()
    # --- END NEW TAB 4 ---



        st.markdown("---")

        # --- UTILS: SYSTEM SHUTDOWN ---
        st.markdown('<div class="section-header">🛑 SYSTEM CONTROL</div>', unsafe_allow_html=True)
        if st.button("🔌 System Shutdown", type="primary", use_container_width=True):
            st.warning("Shutting down system...")
            os._exit(0)


# --- MAIN LAYOUT (SECTIONS 7 & 8) ---
st.title(f"🚀 {st.session_state.active_project}")

# --- [NEW] COMPACTION PROTOCOL UI ---
# This block should be placed high up in the main execution flow,
# before the Staging Area is rendered.

if st.session_state.get('compaction_choice_required', False):
    with st.container(border=True):
        st.subheader("📦 Master Record Compaction Required")
        st.info(f"The `MI_Master_Record` has exceeded the threshold of **{st.session_state.get('compaction_threshold', 50000):,} characters**. "
                "Its content has been archived to `MI_Master_Record_Overflow`. Please choose how to proceed.")

        col1, col2, col3 = st.columns(3) # <-- MODIFIED

        with col1:
            if st.button("Generate Simple Summary", use_container_width=True, help="Create a new, concise MI_Master_Record summary guided by the current MI_Project_Status. The fastest option."):
                with st.spinner("Generating goal-oriented summary..."):
                    try:
                        # 1. Get content and configure AI
                        full_content = st.session_state.get('MI_Master_Record', '')
                        project_status = st.session_state.get('MI_Project_Status', '')
                        compact_eng = st.session_state.get("harvest_engine_set", "gemini-2.5-pro")
                        compact_client = get_llm_client(compact_eng)
                        
                        # 2. Create the prompt
                        p_summarize = f"""The user's MI_Master_Record has grown too large and needs to be compacted into a new, goal-oriented summary.

                        **CURRENT PROJECT STATUS:**
                        {project_status}

                        **FULL MASTER RECORD CONTENT TO SUMMARIZE:**
                        {full_content}

                        **TASK:**
                        Generate a new, concise summary of the provided content. The summary MUST be optimized to support the CURRENT PROJECT STATUS. Retain all critical facts, decisions, and historical context relevant to achieving the stated project objective, while omitting superfluous details. The output should be only the text of the new summary, ready to become the new MI_Master_Record.
                        """

                        # 3. Get AI response
                        response = compact_client.models.generate_content(model=compact_eng, contents=p_summarize,config={"temperature": temp_val if 'temp_val' in locals() else 0.7, "top_p": top_p_val if 'top_p_val' in locals() else 0.9})
                        summary_text = response.text

                        # 4. Queue the proposal for the Staging Area
                        if "pending_updates" not in st.session_state:
                            st.session_state.pending_updates = {}
                        st.session_state.pending_updates["MI_Master_Record"] = summary_text

                        # 5. Reset flag and rerun
                        st.session_state['compaction_choice_required'] = False
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error during summarization: {e}")


        with col2:
            if st.button("🤖 AI-Assisted Refactoring", use_container_width=True, help="Propose a new, lean MI_Master_Record AND a set of new, modular User Files to hold the details. More powerful, takes longer."):
                with st.spinner("Performing deep analysis for refactoring proposal..."):
                    try:
                        # 1. Get content and configure AI
                        full_content = st.session_state.get('MI_Master_Record', '')
                        project_status = st.session_state.get('MI_Project_Status', '')
                        compact_eng = st.session_state.get("harvest_engine_set", "gemini-2.5-pro")
                        compact_client = get_llm_client(compact_eng)

                        # 2. Create the new JSON-based prompt
                        p_refactor = f"""
                        The user's MI_Master_Record is too large. Your task is to propose a refactoring plan. Analyze the full content and the project status, then generate a response as a single, valid JSON object.

                        **CURRENT PROJECT STATUS:**
                        {project_status}

                        **FULL MASTER RECORD CONTENT TO REFACTOR:**
                        {full_content}

                        **TASK:**
                        Generate a refactoring proposal in the following JSON format.
                        **CRITICAL JSON FORMATTING RULES:**
                        1. All string values, especially multi-line content, MUST have newlines escaped as `\\n`.
                        2. All double quotes `"` within string values MUST be escaped as `\\"`.
                        3. The entire output MUST be a single, valid JSON object.

                        {{
                            "MI_Master_Record": {{
                                "content": "A new, lean executive summary of the project...",
                                "is_distributed": true
                            }},
                            "new_user_files": [
                                {{
                                    "filename": "suggested_filename_1.md",
                                    "description": "A brief, one-sentence purpose for this file.",
                                    "content": "The detailed content that belongs in this new file.",
                                    "part_of_dtn": true
                                }}
                            ]
                        }}

                        **CRITICAL PRESERVATION RULE:** While being concise, the new `MI_Master_Record` content MUST retain foundational architectural details (key modules, tech stacks, core pillars).
                        """

                        # 3. Get AI response
                        response = compact_client.models.generate_content(model=compact_eng, contents=p_refactor,config={"temperature": temp_val if 'temp_val' in locals() else 0.7, "top_p": top_p_val if 'top_p_val' in locals() else 0.9})
                        refactor_plan_text = response.text.strip()
                        # Step 1: More robustly find the JSON block within the AI's response.
                        # Prioritize code fences, then a standalone JSON object.
                        json_str_to_parse = ""
                        # 1. Try to find ```json\n{...}```
                        json_match = re.search(r"```json\n(\{.*?\})\n```", refactor_plan_text, re.DOTALL)
                        if json_match:
                            json_str_to_parse = json_match.group(1).strip()
                        else:
                            # 2. Try to find ```\n{...}```
                            json_match = re.search(r"```\n(\{.*?\})\n```", refactor_plan_text, re.DOTALL)
                            if json_match:
                                json_str_to_parse = json_match.group(1).strip()
                            else:
                                # 3. Fallback: try to find a standalone JSON object { ... }
                                # Use a non-greedy match for content within the object
                                json_match = re.search(r"(\{.*?\})", refactor_plan_text, re.DOTALL)
                                if json_match:
                                    json_str_to_parse = json_match.group(1).strip()
                                else:
                                    # If no specific JSON block found, assume the entire response *should* be JSON.
                                    # This is a fallback and might still fail if the AI included non-JSON text.
                                    json_str_to_parse = refactor_plan_text.strip()

                        # --- ROBUST JSON PARSING (ADDED) ---
                        try:
                            refactor_plan = json.loads(refactor_plan_text, strict=False)
                        except json.JSONDecodeError as e:
                            st.error(f"JSON Decode Error during refactoring. Attempting to fix... Original Error: {e}")

                            # --- TEMPORARY DEBUGGING UI ---
                            with st.expander("🔍 Raw AI Refactoring Output (for Debugging)"):
                                st.code(refactor_plan_text, language="json", wrap_lines=True)
                                st.warning("Please inspect the raw output above for malformed JSON, especially around the indicated character position.")
                            # --- END TEMPORARY DEBUGGING UI ---

                            try:
                                # Attempt to fix common newline issue within string values
                                # This regex looks for content between double quotes preceded by a colon
                                fixed_text = re.sub(r'(?<=: ")(.*?)(?=")', lambda m: m.group(1).replace('\n', '\\n'), json_str_to_parse, flags=re.DOTALL)

                                refactor_plan = json.loads(fixed_text, strict=False)
                                st.success("JSON fixed and parsed successfully.")
                            except Exception as fix_e:
                                st.error(f"Failed to fix JSON. Original Error: {e}. Fix Attempt Error: {fix_e}")
                                # If fixing also fails, we must reset to prevent further errors
                                st.session_state['compaction_choice_required'] = False # Reset flag to prevent loop
                                st.stop()

                        # 4. Parse the JSON and queue proposals
                        if "pending_updates" not in st.session_state:
                            st.session_state.pending_updates = {}
                        # Stage Master Record update and its metadata
                        if "MI_Master_Record" in refactor_plan:
                            mr_proposal = refactor_plan["MI_Master_Record"]
                            st.session_state.pending_updates["MI_Master_Record"] = str(mr_proposal["content"]) # Ensure content is a string
                            st.session_state.pending_updates["__metadata_MI_Master_Record"] = {"is_distributed": mr_proposal.get("is_distributed", False)}
                        # Stage new file creations and their metadata
                        if "new_user_files" in refactor_plan:
                            for file_proposal in refactor_plan["new_user_files"]:
                                filename = file_proposal["filename"]
                                st.session_state.pending_updates[filename] = str(file_proposal["content"]) # Ensure content is a string

                                # Pre-populate the full metadata for when it gets approved
                                if "loaded_slots_metadata" not in st.session_state: st.session_state.loaded_slots_metadata = {}
                                st.session_state.loaded_slots_metadata[filename] = {
                                    "slot_name": filename, "slot_type": "user", "scope": "project",
                                    "owner_id": st.session_state.active_project,
                                    "description": file_proposal.get("description", "AI-generated file."),
                                    "active_in_context": False,
                                    "part_of_dtn": file_proposal.get("part_of_dtn", False),
                                    "folder": ""
                                }

                        # 5. Reset flag and rerun
                        st.session_state['compaction_choice_required'] = False
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error during refactoring: {e}")
        # --- [NEW] CANCEL BUTTON ---
        with col3:
            if st.button("❌ Cancel", use_container_width=True, help="Dismiss this choice for now. The system will remind you on the next Harvest or Deep Read."):
                st.session_state['compaction_choice_required'] = False
                # --- NEW: Clear any pending updates that might have been generated by AI-Assisted Refactoring ---
                if "pending_updates" in st.session_state:
                    st.session_state.pending_updates = {} # Clear the dictionary
                # --- END NEW ---
                st.rerun()

# --- TEMPORARY DEBUG: Staging Area Check ---
#print(f"DEBUG STAGING AREA CHECK: pending_updates = {st.session_state.get('pending_updates', {})}")
#st.text(f"DEBUG STAGING AREA CHECK: pending_updates = {st.session_state.get('pending_updates', {})}")
# --- END TEMPORARY DEBUG ---


# Harvest status belongs in the main pane — the Harvest button lives in the sidebar.
if st.session_state.get("harvest_result") or st.session_state.get("harvest_errors"):
    st.divider()
    with st.container(border=True):
        result = st.session_state.get("harvest_result") or {}
        staged = result.get("staged") or []
        if staged:
            st.success(f"Harvest staged **{len(staged)}** update(s). Each one is collapsed under its slot name below.")
        elif result:
            st.warning("Harvest finished without staging any updates.")
        if result:
            with st.expander("Harvest details", expanded=False):
                st.caption(
                    f"Chunks parsed: {result.get('chunks_ok', 0)}/{result.get('chunks', 0)}. "
                    f"Counter {result.get('index_reason', 'unchanged')}."
                )
                details = []
                if result.get("identical"):
                    details.append("Unchanged: " + ", ".join(result["identical"]))
                if result.get("unrecognized"):
                    details.append("Unrecognized keys: " + ", ".join(result["unrecognized"]))
                if result.get("skipped_empty"):
                    details.append("Empty content: " + ", ".join(result["skipped_empty"]))
                if result.get("skipped_not_dtn"):
                    details.append("Skipped (not DTN): " + ", ".join(result["skipped_not_dtn"]))
                if result.get("skipped_small"):
                    details.append("Rejected undersized proposals: " + ", ".join(result["skipped_small"]))
                if result.get("skipped_mismatch"):
                    details.append("Rejected off-purpose copies: " + ", ".join(result["skipped_mismatch"]))
                for line in details:
                    st.caption(line)
                if result.get("truncated"):
                    st.caption("Some segments were truncated; complete fields were kept.")
                    for note in result["truncated"]:
                        st.caption(note)
                if result.get("log"):
                    st.caption("Chunk log")
                    for line in result["log"]:
                        st.write(line)
        if st.session_state.get("harvest_errors"):
            st.error("Harvest reported a problem. Successful segments (if any) are still staged below.")
            for err in st.session_state.harvest_errors:
                with st.expander(err.get("title", "Harvest error"), expanded=False):
                    st.write(err.get("message", ""))
                    if err.get("context"):
                        st.code(err["context"], language="json")
        if st.button("Dismiss harvest status", key="dismiss_harvest_status"):
            st.session_state.harvest_errors = []
            st.session_state.harvest_result = None
            st.rerun()

# Staging Area renders inside GateWai via render_staging_area().

# Keyed radio (not st.tabs): Streamlit always resets st.tabs to the first tab
# after any widget remount, so Show File List kicked users back to GateWai.
_MAIN_GATEWAI = "💬 GateWai"
_MAIN_EDITOR = "📝 Editor"
if "main_view_selector" not in st.session_state:
    st.session_state.main_view_selector = _MAIN_GATEWAI

# App-level Memory sidebar (symmetrical to st.sidebar): always mounted on desktop.
# Open/close is client-side CSS (like the left sidebar) — no remount / no chat jump.
if "show_memory_slots_pref" not in st.session_state:
    st.session_state.show_memory_slots_pref = False
_cookie_open = read_rsb_open_cookie()
if _cookie_open is not None:
    st.session_state.show_memory_slots_pref = _cookie_open
_show_slots = bool(st.session_state.show_memory_slots_pref)
st.session_state.show_memory_slots = _show_slots
_rsb_width = memory_slots_width_px()
_mobile_shell = is_mobile_client()
# Desktop: keep the pane in the DOM always; JS only collapses its width.
_use_app_rsb = not _mobile_shell

main_view = st.radio(
    "Main view",
    [_MAIN_GATEWAI, _MAIN_EDITOR],
    horizontal=True,
    key="main_view_selector",
    label_visibility="collapsed",
)

if _use_app_rsb:
    render_app_memory_sidebar()
install_right_sidebar_chrome(_show_slots if _use_app_rsb else False, _rsb_width)

# =========================================================
# VIEW 1: GateWai (Chat & Artifacts)
# =========================================================
if main_view == _MAIN_GATEWAI:
    # Split preview crushes the chat pane on phones after submit; keep one column
    # while a turn is in flight (restart "fixes" UI mainly by clearing this state).
    _pending_now = read_pending_turn()
    _pending_now_prompt = str((_pending_now or {}).get("prompt") or "").strip()
    _turn_busy = bool(st.session_state.get("held_chat_prompt")) or bool(
        st.session_state.get("prompt_submit_in_flight")
    ) or bool(_pending_now_prompt) or bool(st.session_state.get("pending_updates"))
    _mobile = _mobile_shell
    # Desktop Live Preview = app-level top-to-bottom panel (pushes whole app left).
    # Hidden on Editor; skipped while a turn is busy / on mobile (expander instead).
    _use_app_lp = bool(st.session_state.show_preview) and not _turn_busy and not _mobile
    if _use_app_lp:
        render_app_live_preview()
    install_live_preview_chrome(_use_app_lp, live_preview_width_px())

    # Last chance: strip abandoned user bubbles before chat paints (no banner).
    if (
        not st.session_state.get("_chat_submit_this_run")
        and not st.session_state.get("_run_model_after_remount")
        and not st.session_state.get("prompt_submit_in_flight")
        and reconcile_stale_chat_turn()
    ):
        st.rerun()

    chat_container = render_project_chat(chat_pane_height(), "chat_earlier_main")
    restore_gatewai_chat_scroll_if_needed()

    _send_err = st.session_state.pop("chat_send_error", None)
    if _send_err:
        st.error(f"Last send failed: {_send_err}")
    # No "Sending…" strip — it was getting stuck across reruns. The Thinking
    # spinner during the API call is enough feedback.

    # Staging lives here (same tab as chat) — not above the tabs.
    render_staging_area()

    # Mobile: Memory + Preview under chat (fixed panes are desktop-only).
    if _show_slots and _mobile:
        with st.expander("📂 Memory slots", expanded=True):
            render_memory_slot_sidebar("memory", height=280, show_meta=False)
    if bool(st.session_state.show_preview) and _mobile and not _turn_busy:
        with st.expander("📋 Live Preview", expanded=False):
            render_live_preview_pane()

    # Collapsed by default — an open debugger after submit was blowing mobile layout
    # and stealing scroll away from the chat pane.
    _dbg_logs = None
    if st.session_state.get("shadow_logs_manual_run"):
        _dbg_logs = st.session_state.shadow_logs_manual_run
    elif st.session_state.get("shadow_logs"):
        _dbg_logs = st.session_state.shadow_logs
    _dbg_has = bool(_dbg_logs) and not (
        isinstance(_dbg_logs, list)
        and len(_dbg_logs) == 1
        and "Waiting" in str(_dbg_logs[0])
    )
    _dbg_label = "🕵️ Shadow Debugger"
    if _dbg_has:
        _dbg_label += " · updated"
    with st.expander(_dbg_label, expanded=False):
        with st.container(height=160):
            if isinstance(_dbg_logs, list) and _dbg_logs:
                st.code("\n".join(str(x) for x in _dbg_logs), wrap_lines=True)
            elif _dbg_logs:
                st.code(str(_dbg_logs), wrap_lines=True)
            else:
                st.code("Waiting for input...", wrap_lines=True)

# =========================================================
# VIEW 2: EDITOR (The Integrated IDE)
# =========================================================
else:
    # Live Preview is GateWai-only — clear its push width on Editor.
    install_live_preview_chrome(False, 0)
    pin_editor_tab_to_top()
    # Memory sidebar is app-level (right); Editor uses the full remaining main pane.
    if "selected_file" not in st.session_state:
        st.session_state.selected_file = CORE_SCHEMA.get("summary.txt", "")
    selected_file = st.session_state.get("selected_file", CORE_SCHEMA.get("summary.txt", ""))

    if _show_slots and _mobile_shell:
        with st.expander("📂 Memory slots", expanded=True):
            render_memory_slot_sidebar("memory", height=280, show_meta=False)

    col_editor = st.container()
    with col_editor:
        st.subheader(f"✏️ {st.session_state.active_project}  ·  {selected_file}")
        st.caption("💡 Pro-Tip: Use Ctrl+F (or Cmd+F on Mac) to search within the editor.")
        # Keyed checkbox only — extra st.rerun() was kicking back to the GateWai tab.
        if "show_editor_chat" not in st.session_state:
            st.session_state.show_editor_chat = False
        show_editor_chat = st.checkbox(
            "💬 Show Chat Window",
            key="show_editor_chat",
            help="Display the chat window next to the editor for AI assistance.",
        )

        if show_editor_chat:
            editor_col1, editor_col2 = st.columns([6, 5]) # <--- CRITICAL: Set to 2 columns
        else:
            editor_col1 = st.container() # Use a container to occupy the column
            editor_col2 = None

        with editor_col1:

            # --- NEW: Preview Toggles (Mutually Exclusive) ---
            # Assign the two column objects to individual variables
            col_markdown_preview, col_html_preview = st.columns(2)

            with col_markdown_preview:
                if "show_markdown_preview" not in st.session_state:
                    st.session_state.show_markdown_preview = False
                st.checkbox("👁️ Toggle Markdown Preview", key="show_markdown_preview")
            with col_html_preview:
                if "show_html_preview" not in st.session_state:
                    st.session_state.show_html_preview = False
                st.checkbox("👁️ Toggle HTML Preview", key="show_html_preview")
            # --- END NEW: Preview Toggles ---

            # --- Initialize Session State ---
            if 'importer_key' not in st.session_state: st.session_state.importer_key = 0
            if 'editor_key_version' not in st.session_state: st.session_state.editor_key_version = 0
            if 'show_explain_ui' not in st.session_state: st.session_state.show_explain_ui = False
            if 'show_refactor_ui' not in st.session_state: st.session_state.show_refactor_ui = False
            if 'original_for_refactor' not in st.session_state: st.session_state.original_for_refactor = ""
            if 'refactor_proposal' not in st.session_state: st.session_state.refactor_proposal = ""
            if 'explain_text' not in st.session_state: st.session_state.explain_text = ""

            # --- NEW LOGIC TO FORCE ST_ACE RERENDER ON FILE SELECTION ---
            # Add a new session state variable to track the last selected file in the editor
            if 'last_selected_file_for_editor' not in st.session_state:
                st.session_state.last_selected_file_for_editor = selected_file

            # If the currently selected file is different from the last one, increment the version
            # and force a rerun to ensure the new key is applied immediately.
            if st.session_state.last_selected_file_for_editor != selected_file:
                st.session_state.editor_key_version += 1
                st.session_state.last_selected_file_for_editor = selected_file
    #            st.rerun() # This is crucial to apply the new key immediately
            # --- END NEW LOGIC ---

            # If a stale editor overwrite already landed, put the committed text back
            # before ACE mounts so the widget is seeded with the approved file.
            _committed = st.session_state.get(_slot_committed_key(selected_file))
            _replaced = st.session_state.get(_slot_replaced_key(selected_file))
            _stored = st.session_state.get(selected_file, "")
            if (
                _committed
                and _replaced
                and normalize_harvest_text(_stored) == normalize_harvest_text(_replaced)
            ):
                st.session_state[selected_file] = _committed

            # Typing updates this fragment only, so the file list stays clickable.
            render_live_editor(selected_file)
            editor_text = str(st.session_state.get(selected_file, "") or "")

        # --- Display Chat Window (Reusing from Tab 1) ---
        if show_editor_chat and editor_col2:
            with editor_col2:
                st.subheader("AI Assistance")
                st.markdown("### 💬 Chat History")
                st.caption("Latest messages. The rest of the history stays saved.")
                render_project_chat(500, "chat_earlier_editor")



        # --- File Operation Buttons ---
        st.markdown("---")
        project_name = st.session_state.active_project
        raw_ready = st.session_state.get("raw_download_payload")
        if not (
            isinstance(raw_ready, dict)
            and raw_ready.get("slot") == selected_file
            and raw_ready.get("project") == project_name
        ):
            raw_ready = None
            st.session_state.pop("raw_download_payload", None)
        html_ready = st.session_state.get("html_download_payload")
        if not (
            isinstance(html_ready, dict)
            and html_ready.get("slot") == selected_file
            and html_ready.get("project") == project_name
        ):
            html_ready = None
            st.session_state.pop("html_download_payload", None)

        # Save runs in a fragment so the Editor tab stays selected.
        render_editor_save(selected_file)

        col_undo, col_copy, col_download_raw, col_download_html = st.columns(4)

        with col_undo:
            if st.button("↩️ Undo last save", use_container_width=True, help="Put back the text from the previous save of this slot."):
                _proj_db, _sys_db, _scope, slot_db = database_for_slot(selected_file)
                history = db.get_ai_edit_history(slot_db, selected_file)
                if not history:
                    st.warning("No earlier save is stored for this slot yet.")
                else:
                    latest = history[0]
                    restored = db.undo_ai_edit(slot_db, latest["edit_id"], selected_file)
                    if restored is not None:
                        commit_slot_text(selected_file, restored)
                        st.toast(f"Restored the previous {selected_file}.")
                        st.rerun()
                    st.error("That saved copy could not be found.")

        with col_copy:
            if st.button("↪️ Copy to Slot", use_container_width=True):
                st.session_state.show_copy_to_slot_form = not st.session_state.get('show_copy_to_slot_form', False)

        with col_download_raw:
            if st.button("📥 Download Raw Markdown", use_container_width=True, help="Prepare the current editor text, then download it."):
                st.session_state.raw_download_payload = {
                    "slot": selected_file,
                    "project": project_name,
                    "text": editor_text,
                }
                raw_ready = st.session_state.raw_download_payload
            if raw_ready:
                st.download_button(
                    label="⬇️ Save Markdown file",
                    data=raw_ready["text"],
                    file_name=f"{selected_file}.md",
                    mime="text/markdown",
                    use_container_width=True,
                )

        with col_download_html:
            if st.button("📥 Download Rendered HTML", use_container_width=True, help="Build HTML from this slot, then download it. Not built on an ordinary save."):
                rendered_html_content = markdown.markdown(editor_text)
                st.session_state.html_download_payload = {
                    "slot": selected_file,
                    "project": project_name,
                    "html": prepare_printable_html(rendered_html_content, f"Gatewai Rendered Markdown - {selected_file}"),
                }
                html_ready = st.session_state.html_download_payload
            if html_ready:
                st.download_button(
                    label="⬇️ Save HTML file",
                    data=html_ready["html"],
                    file_name=f"{selected_file}.html",
                    mime="text/html",
                    use_container_width=True,
                )

        # --- AI Tool Buttons ---
        st.markdown("##### 🤖 AI Tools")
        # CRITICAL FIX: Unpack the list of column objects into individual variables
        col_explain, col_refactor, _, _, _ = st.columns(5) # Keeping 5 columns, but using only the first two explicitly

        with col_explain:
            if st.button("🧠 Explain", use_container_width=True, help="Explain a piece of copied code."):
                st.session_state.show_explain_ui = True
                st.session_state.show_refactor_ui = False
                st.session_state.explain_text="" # Ensure only one is open
  #              st.rerun()
        with col_refactor:
            if st.button("✨ Refactor/Improve", use_container_width=True, help="Improve a piece of copied code or text."):
                st.session_state.show_refactor_ui = True
                st.session_state.show_explain_ui = False # Ensure only one is open
                st.session_state.refactor_proposal = "" # Clear old proposals
                st.session_state.original_for_refactor = "" # Clear old originals
  #              st.rerun()


        # --- UI FOR THE "EXPLAIN" FEATURE ---
        if st.session_state.show_explain_ui:
            # ... (Existing explain UI logic remains the same)
            st.info("📋 Paste the copied text below to have the AI explain it.")
            pasted_text = st.text_area("Pasted Content for Analysis:", height=150, label_visibility="collapsed", key="explain_paste_area")
            b_col1, b_col2, _ = st.columns([1,1,3]) # <--- CRITICAL: Set to 3 columns
            if b_col1.button("✅ Analyze Text", use_container_width=True, type="primary"):
                if pasted_text.strip():
                    with st.spinner("AI is analyzing the selection..."):
                        p_explain = f"[SYSTEM JOB: EXPLAIN CODE SNIPPET]\nTASK: Provide a clear and concise explanation of the following code/text snippet.\n\n--- SNIPPET TO EXPLAIN ---\n```\n{pasted_text}\n```"
                        explain_eng = st.session_state.get("chat_engine_set", "gemini-2.5-flash")
                        explain_client = get_llm_client(explain_eng)
                        response = explain_client.models.generate_content(model=explain_eng, contents=p_explain,config={"temperature": temp_val if 'temp_val' in locals() else 0.7, "top_p": top_p_val if 'top_p_val' in locals() else 0.9})
                        st.session_state.explain_text = response.text
                        if st.session_state.explain_text:
                            st.markdown("---")
                            st.subheader("🔍 Explanation...")
                            st.code(st.session_state.explain_text, language="markdown", wrap_lines=True)

#                        st.session_state.messages.append({"role": "user", "content": f"Explain this selection:\n```\n{pasted_text}\n```"})
#                        st.session_state.messages.append({"role": "model", "content": response.text})
#                        st.toast("Explanation received in Co-Pilot tab.")
                        st.session_state.show_explain_ui = False # Close the UI
#                    st.session_state.show_explain_ui = False
#                        st.rerun()
                else:
                    st.warning("Please paste some text before analyzing.")
            if b_col2.button("❌ Cancel", use_container_width=True):
                st.session_state.show_explain_ui = False
 #               st.rerun()

        # --- UI FOR THE "REFACTOR" FEATURE ---
        if st.session_state.show_refactor_ui:
            st.info("📋 Paste the code/text you want to improve below.")
            pasted_text_refactor = st.text_area("Pasted Content for Refactoring:", height=150, label_visibility="collapsed", key="refactor_paste_area")

            ref_b_col1, ref_b_col2, _ = st.columns([1,1,3]) # <--- CRITICAL: Set to 3 columns

            if ref_b_col1.button("✅ Generate Proposal", use_container_width=True, type="primary"): 
                if pasted_text_refactor.strip():
                    st.session_state.original_for_refactor = pasted_text_refactor
                    with st.spinner("AI is generating an improved version..."):
                        p_refactor = f"[SYSTEM JOB: REFACTOR & IMPROVE]\nTASK: You are an expert code reviewer and technical writer. Analyze the following snippet and rewrite it to improve its clarity, efficiency, and adherence to best practices. Respond ONLY with the raw, improved code/text. Do not add any explanations, conversational filler, or markdown code fences (```).\n\n--- SNIPPET TO IMPROVE ---\n{pasted_text_refactor}"
                        refactor_eng = st.session_state.get("harvest_engine_set", "gemini-2.5-pro") # Use a powerful model
                        refactor_client = get_llm_client(refactor_eng)
                        response = refactor_client.models.generate_content(model=refactor_eng, contents=p_refactor,config={"temperature": temp_val if 'temp_val' in locals() else 0.7, "top_p": top_p_val if 'top_p_val' in locals() else 0.9})
                        #actor)
                        st.session_state.refactor_proposal = response.text
   #                     st.rerun()
                else:
                    st.warning("Please paste some text before generating a proposal.")

            if ref_b_col2.button("❌ Cancel", use_container_width=True):
                st.session_state.show_refactor_ui = False
    #            st.rerun()

            # --- Display Diff View if a proposal exists ---
            if st.session_state.refactor_proposal:
                st.markdown("---")
                st.subheader("🔍 Refactoring Proposal")

                diff_col1, diff_col2 = st.columns(2)
                with diff_col1:
                    st.caption("Original Version")
                    st.code(st.session_state.original_for_refactor, language="markdown", wrap_lines=True)
                with diff_col2:
                    st.caption("✨ Proposed Improvement (Click icon to copy)")
                    st.code(st.session_state.refactor_proposal, language="markdown", wrap_lines=True)

        # --- NEW: PRINT OPTIONS FOR EDITOR CONTENT ---
        st.markdown("---")
        st.subheader("🖨️ Print Options")
        print_cols = st.columns(3)

        with print_cols[0]:
            if st.button("🖨️ Print Editor Content", use_container_width=True, help="Print the raw content currently in the editor."):
                escaped_editor_content = html.escape(editor_text)
                # Ensure <pre> tag for raw content
                full_html_document = prepare_printable_html(f'<pre>{escaped_editor_content}</pre>', f"Gatewai Editor - {selected_file}")
                emit_browser_print(full_html_document)
                st.toast("Sending current editor content to the printer…")

        with print_cols[1]:
            if st.button("🖨️ Print Markdown Preview", use_container_width=True, help="Print the rendered Markdown preview.", disabled=not st.session_state.show_markdown_preview):
                if st.session_state.show_markdown_preview:
                    # Print the same text the preview is showing (approved/saved slot, not a stale ACE buffer).
                    full_html_document = prepare_printable_html(editor_text, f"Gatewai Markdown Preview - {selected_file}", is_markdown=True)
                    emit_browser_print(full_html_document)
                    st.toast("Sending current markdown preview to the printer…")


        with print_cols[2]:
            if st.button("🖨️ Print HTML Preview", use_container_width=True, help="Print the rendered HTML preview.", disabled=not st.session_state.show_html_preview):
                if st.session_state.show_html_preview: # Check if the preview itself is active
                    # For HTML Preview, new_content is already HTML.
                    full_html_document = prepare_printable_html(editor_text, f"Gatewai HTML Preview - {selected_file}")
                    emit_browser_print(full_html_document)
                    st.toast("Sending current HTML preview to the printer…")

        # --- END NEW ---


        # --- Conditional Displays (Preview, Copy Form, etc.) ---
        if st.session_state.get('show_html_preview', False):
            # ... (HTML preview logic remains the same)
            st.markdown("---"); st.subheader("HTML Preview")
            components.html(editor_text, height=600, scrolling=True)
            st.markdown("---")

        # --- NEW: Render Markdown Preview (if active) ---
        if st.session_state.show_markdown_preview:
            st.markdown("---")
            st.subheader("Markdown Preview")
            with st.container(height=400, border=True):
                st.markdown(editor_text) # Streamlit's markdown renders directly here
            st.markdown("---")
        # --- END NEW ---

        if st.session_state.get('show_copy_to_slot_form', False):
            # ... (Copy to slot logic remains the same)
            with st.form(key="copy_to_slot_form"):
                st.info(f"This will overwrite the content of the selected User slot with the current content from '{selected_file}'.")
                user_slots = [slot for slot, meta in st.session_state.loaded_slots_metadata.items() if slot not in CORE_SCHEMA.values() and slot != selected_file]
                if user_slots:
                    target_slot = st.selectbox("Select Target Slot", options=user_slots)
                    confirmed = st.checkbox("⚠️ Yes, I'm sure I want to overwrite the content of the target slot.")
                    submitted = st.form_submit_button("Confirm Copy")
                    if submitted:
                        if not confirmed: st.error("You must check the confirmation box to proceed.")
                        else:
                            proj_db = db.get_project_db_path(BASE_PROJECT_DIR, st.session_state.active_project)
                            sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
                            db.save_slot(proj_db, sys_db, target_slot, editor_text, "user", f"Copied from {selected_file}", "project")
                            st.session_state[target_slot] = editor_text
                            if target_slot == selected_file: st.session_state.editor_key_version += 1
                            st.toast(f"Successfully copied content to '{target_slot}'!")
                            st.session_state.show_copy_to_slot_form = False
#                            st.rerun()
                else:
                    st.warning("No other User slots available to copy to.")
                    if st.form_submit_button("Close"): st.session_state.show_copy_to_slot_form = False; st.rerun()

        st.markdown("---"); st.subheader("📤 Import Local File into Editor")
        # ... (Import logic remains the same)
        uploaded_file = st.file_uploader("Choose a file to load into the editor.", type=['txt', 'md', 'py', 'json', 'html', 'css', 'js'], key=f"uploader_{st.session_state.importer_key}")
        if uploaded_file is not None:
            try:
                uploaded_content = uploaded_file.getvalue().decode("utf-8")
                st.info(f"File '{uploaded_file.name}' is ready to import into **{selected_file}**.")
                imp_col1, imp_col2, imp_col3, _ = st.columns([1, 1, 1, 2])
                if imp_col1.button("Append Content", use_container_width=True):
                    st.session_state[selected_file] = editor_text + "\n\n" + uploaded_content
                    st.session_state.importer_key += 1; st.session_state.editor_key_version += 1; st.rerun()
                if imp_col2.button("Replace Content", use_container_width=True):
                    st.session_state[selected_file] = uploaded_content
                    st.session_state.importer_key += 1; st.session_state.editor_key_version += 1; st.rerun()
                if imp_col3.button("Cancel", use_container_width=True):
                    st.session_state.importer_key += 1; st.rerun()
            except Exception as e:
                st.error(f"Error reading file: {e}")
            pass

# --- 9. CHAT + AUTO-SHADOW (AFTER both tabs so Editor always mounts before the API call) ---
# After Submit we remount once, then run the model on this following pass.
if st.session_state.pop("_run_model_after_remount", None):
    st.session_state._chat_submit_this_run = True

prompt = st.session_state.get("held_chat_prompt")
# Do not silently resurrect pending_turn into a send — that left mobile stuck on
# "Sending…". Only an explicit Submit remount (_run_model_after_remount) may send.
if prompt and not st.session_state.get("_chat_submit_this_run"):
    # Hold leftover from a prior run without an explicit send this run.
    prompt = None
    st.session_state.pop("held_chat_prompt", None)
    st.session_state.prompt_submit_in_flight = False
answered_already = False
if prompt and len(st.session_state.get("messages") or []) >= 2:
    previous = st.session_state.messages[-2]
    latest = st.session_state.messages[-1]
    answered_already = (
        isinstance(previous, dict)
        and isinstance(latest, dict)
        and previous.get("role") == "user"
        and previous.get("content") == prompt
        and latest.get("role") == "model"
        and str(latest.get("content") or "").strip()
    )
if answered_already:
    scrub_chat_hold()
    prompt = None

should_rerun = False

# After a model reply: one extra remount to clear pale ghost UI, then shadow.
if not prompt and st.session_state.pop("_post_reply_second_remount", None):
    should_rerun = True
    if not needs_auto_shadow_catchup():
        st.session_state.pop("_defer_auto_shadow_catchup", None)
    else:
        set_shadow_debug_logs([
            "✅ Reply on screen — one more refresh, then auto-shadow.",
        ])
elif not prompt and needs_auto_shadow_catchup():
    # After enabling Auto Shadow / post-reply defer, wait one clean remount first.
    if st.session_state.pop("_defer_auto_shadow_catchup", None):
        st.session_state.pending_auto_shadow = True
        set_shadow_debug_logs([
            "🕵️ Auto Shadow — refreshing once, then catch-up runs.",
        ])
        should_rerun = True
    else:
        st.session_state.pending_auto_shadow = True
        set_shadow_debug_logs(["🚦 Auto-shadow catch-up starting…"])
        catchup_logs, catchup_staged, catchup_done = run_auto_shadow_turn()
        set_shadow_debug_logs(catchup_logs)
        if catchup_done:
            mark_auto_shadow_complete()
            # Always remount once after shadow — clears harvest/shadow ghost shells on mobile.
            should_rerun = True
        elif catchup_staged:
            should_rerun = True
        else:
            # Remount even when nothing staged — long spinner still leaves pale shells.
            should_rerun = True

if prompt:
    response = None
    shadow_logs = set_shadow_debug_logs([
        "🚦 Status: Chat submit received — calling model…",
    ])

    try:
        last_message = st.session_state.messages[-1] if st.session_state.messages else None
        already_stored = (
            isinstance(last_message, dict)
            and last_message.get("role") == "user"
            and last_message.get("content") == prompt
        )
        if not already_stored:
            # Do NOT draw into chat_container here — history already rendered above.
            # Extra st.chat_message calls create the mobile "dual dialog" until restart.
            if not st.session_state.ai_response_generated_for_current_turn:
                st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.req_count += 1

        mem_depth = st.session_state.get('memory_depth_val', 3)
        history_to_send = st.session_state.messages[-(mem_depth * 2 + 1):-1]
        active_assets = sel_assets if 'sel_assets' in locals() else st.session_state.get('last_sel_assets', [])
        _asset_dir = asset_dir if 'asset_dir' in locals() else os.path.join(
            BASE_PROJECT_DIR, st.session_state.active_project, "Assets"
        )
        injected_data = st.session_state.temp_web_ctx + "\n" + get_asset_content(active_assets, _asset_dir)

        try:
            c_eng = chat_engine if 'chat_engine' in locals() else st.session_state.get('chat_engine_set', 'gemini-2.5-flash')
            chat_client = get_llm_client(c_eng)

            memory_block = ""
            manifest_rows = []
            context_files = []

            summary_content = st.session_state.get('MI_Master_Record_Summary', '')
            master_content = st.session_state.get('MI_Master_Record', '')

            if summary_content.strip():
                memory_block += f"\n{summary_content}\n\n"
                manifest_rows.append(f"| {'MI_Master_Record_Summary':<20} | CORE    | Summary of Source of Truth |")
                context_files.append("MI_Master_Record_Summary")
            elif master_content:
                memory_block += f"\n{master_content}\n\n"
                manifest_rows.append(f"| {'MI_Master_Record':<20} | CORE    | System Source of Truth |")
                context_files.append("MI_Master_Record")

            for key, slot_name in CORE_SCHEMA.items():
                if slot_name in ["MI_Master_Record", "MI_Master_Record_Summary"]:
                    continue
                val = st.session_state.get(slot_name, "")
                if val:
                    memory_block += f"\n{val}\n\n"
                    manifest_rows.append(f"| {slot_name:<20} | CORE    | System Source of Truth |")
                    context_files.append(slot_name)

            if "loaded_slots_metadata" in st.session_state:
                core_names = set(CORE_SCHEMA.values())
                for slot_name, meta in st.session_state.loaded_slots_metadata.items():
                    if slot_name in core_names:
                        continue
                    is_active = meta.get('active_in_context', True)
                    if not is_active:
                        if slot_name.lower() in prompt.lower():
                            is_active = True
                            st.toast(f"🔦 Smart Fetch: {slot_name} (Name Match)")
                        elif meta.get('description', '').strip() and meta['description'].lower() in prompt.lower():
                            is_active = True
                            st.toast(f"🔦 Smart Fetch: {slot_name} (Desc Match)")
                    if not is_active:
                        continue
                    if meta.get('slot_type') == 'user':
                        val = st.session_state.get(slot_name, "")
                        desc = meta.get('description', 'User Attachment')
                        manifest_rows.append(f"| {slot_name:<20} | USER    | {desc} |")
                        if val:
                            memory_block += f"--- {slot_name} (USER FILE) ---\n{val}\n\n"
                            context_files.append(slot_name)

            manifest_table = (
                "--- MEMORY SLOT MANIFEST ---\n"
                "| SLOT NAME            | TYPE    | PURPOSE/CONTENT        |\n"
                "|----------------------|---------|------------------------|\n"
                + "\n".join(manifest_rows) + "\n"
            )

            sys_instr = (
                f"--- DOMAIN LENS ({st.session_state.selected_lens_name}) ---\n{st.session_state.domain_content}\n\n"
                f"{manifest_table}\n"
                f"\n{memory_block}\n"
                f"--- DATA STREAM ---\n{injected_data}\n\n"
                "INSTRUCTIONS:\n"
                "1. USE THE MANIFEST to understand the purpose of each file.\n"
                "2. 'SYSTEM CORE' files are your brain.\n"
                "3. 'USER FILES' are attachments. Read/Write/Delete them as requested.\n"
                "4. CRITICAL NEGATIVE CONSTRAINT: You are a conversational Co-Pilot. You MUST NOT propose any memory slot updates, output JSON for system changes, or attempt to directly modify the system state in your responses. All system updates are handled by other engines and the Staging Area.\n"
                "5. OUTPUT RULE: Do not output internal reasoning or 'scratchpad' thoughts. Respond DIRECTLY to the user.\n"
            )

            final_prompt = prompt
            print("DEBUG: final prompt completed.")
            st.session_state.role_stats["chat"] += 1

            if not st.session_state.ai_response_generated_for_current_turn:
                print("DEBUG: response_generated_for_current_turn=false.")
                chat = [
                    {
                        "role": "user" if m["role"] == "user" else "model",
                        "parts": [{"text": m["content"]}]
                    }
                    for m in history_to_send
                ]
                chat.append({"role": "user", "parts": [{"text": final_prompt}]})

                current_temp_val = st.session_state.get('temp_val', 0.7)
                current_top_p_val = st.session_state.get('top_p_val', 0.9)
                try:
                    set_shadow_debug_logs([
                        "🚦 Calling chat model…",
                        f"engine={c_eng}",
                    ])
                    with st.spinner("Thinking..."):
                        chat_config_kwargs = {
                            "system_instruction": sys_instr,
                            "temperature": current_temp_val,
                            "top_p": current_top_p_val,
                        }
                        if not is_deepseek_engine(c_eng):
                            chat_config_kwargs["tools"] = [google_search_tool]
                        response = chat_client.models.generate_content(
                            model=c_eng,
                            contents=chat,
                            config=types.GenerateContentConfig(**chat_config_kwargs)
                        )

                    st.session_state["harvest_in_progress"] = False
                    st.session_state.ai_response_generated_for_current_turn = True

                except Exception as e:
                    err_msg = str(e)
                    error_detail = f"Error during AI send_message: {err_msg}"
                    shadow_logs = set_shadow_debug_logs([
                        "💀 Chat model call failed — auto-shadow not started.",
                        f"CRASH: {err_msg}",
                    ])
                    print(f"ERROR: {error_detail}")
                    st.session_state["harvest_in_progress"] = False
                    st.session_state.chat_send_error = err_msg
                    # Leave the user bubble + pending for Resume, but stop the
                    # stuck "Sending…" strip after a failed call.
                    st.session_state.pop("held_chat_prompt", None)
                    st.session_state.prompt_submit_in_flight = False
                    st.session_state.pop("_chat_submit_this_run", None)
                    response = None

                if response:
                    clean_text = response.text
                    if clean_text is None:
                        clean_text = ""
                        print("WARNING: response.text was None, defaulting to empty string.")
                    print(f"DEBUG: Current model response length: {len(clean_text)}")

                    scrub_patterns = [
                        r"^\s*\n*",
                        r"✅ Approve\n",
                        r"❌ Reject\n",
                        r"## (MI_Master_Record|MI_Project_Status|MC_Frontier|MC_Assumptions|MC_Blindspots|MC_Focus|SI_Observed_Profile)\n+",
                    ]
                    for p in scrub_patterns:
                        clean_text = re.sub(p, "", clean_text, flags=re.DOTALL)
                    final_text = clean_text.strip()

                    assimilation_marker_match = re.search(
                        r'\*\(✅ (.*?) assimilated into Master Record\)\*', final_text
                    )
                    if assimilation_marker_match:
                        consumed_content = assimilation_marker_match.group(1).strip()
                        st.session_state.auto_deselect_list = [consumed_content]
                        st.session_state.asset_selector_state_needs_update = True
                        display_text = re.sub(
                            r'\*\(✅ (.*?) assimilated into Master Record\)\*',
                            '',
                            final_text,
                        ).strip()
                        st.session_state.MI_Master_Record += (
                            f"\n\n--- ASSIMILATED: {consumed_content} ---\n{display_text}"
                        )
                        final_text = display_text + f"\n\n*(✅ {consumed_content} assimilated into Master Record)*"
                        if "WEB" == consumed_content:
                            st.session_state.temp_web_ctx = ""

                    # Always save the reply (not only when assimilation markers appear).
                    st.session_state.messages.append({"role": "model", "content": final_text})
                    # Purge any older unanswered user bubbles left by early history saves.
                    _scrubbed, _scrub_changed = scrub_orphan_user_turns(
                        st.session_state.messages,
                        keep_trailing_in_flight=False,
                    )
                    if _scrub_changed:
                        st.session_state.messages = _scrubbed
                    request_chat_autoscroll()
                    if st.session_state.get("held_chat_prompt") == prompt:
                        st.session_state.pop("held_chat_prompt", None)
                    st.session_state.prompt_submit_in_flight = False
                    st.session_state.pop("_chat_submit_this_run", None)
                    clear_pending_turn()
                    print(f"DEBUG: Appending model response. Current messages length: {len(st.session_state.messages)}")

                    universal_save_state_dict = {}
                    for core_key in CORE_SCHEMA.values():
                        universal_save_state_dict[core_key] = st.session_state.get(core_key, '')
                    if "loaded_slots_metadata" in st.session_state:
                        universal_save_state_dict["loaded_slots_metadata"] = st.session_state.loaded_slots_metadata
                        for user_key in st.session_state.loaded_slots_metadata.keys():
                            if user_key not in CORE_SCHEMA.values() and user_key in st.session_state:
                                universal_save_state_dict[user_key] = st.session_state.get(user_key, '')
                    universal_save_state_dict["messages"] = st.session_state.messages
                    config_keys = [
                        "chat_engine_set", "ingest_engine_set", "harvest_engine_set",
                        "shadow_engine_set", "transcribe_engine_set",
                        "selected_lens_name", "temp_val", "top_p_val", "memory_depth_val",
                        "compaction_threshold", "last_harvest_message_index",
                        "review_mode_active", "auto_shadow_active",
                    ]
                    for key in config_keys:
                        universal_save_state_dict[key] = st.session_state.get(key)
                    should_rerun = True
                    save_project_state(universal_save_state_dict, st.session_state.active_project, silent=True)

                    # Remount #1 paints the reply; remount #2 clears ghost shells
                    # from the Thinking wait; then auto-shadow may run.
                    st.session_state.pending_auto_shadow = True
                    write_pending_shadow(len(st.session_state.messages))
                    st.session_state._post_reply_second_remount = True
                    st.session_state._defer_auto_shadow_catchup = True
                    set_shadow_debug_logs([
                        "✅ Chat reply saved — refreshing display.",
                        "🕵️ Auto-shadow will run after the UI settles.",
                    ])
                    request_chat_autoscroll()

        except Exception as e:
            st.session_state.chat_send_error = str(e)
            set_shadow_debug_logs([f"System Crash: {e}"])

    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "ResourceExhausted" in err_msg:
            set_shadow_debug_logs(["🚦 429 Quota Hit - Skipping Shadow."])
            st.session_state.chat_send_error = "AI rate limit reached. Please try again in a moment."
        else:
            error_detail = f"Error during input processing: {err_msg}"
            set_shadow_debug_logs([f"💀 CRASH: {err_msg}"])
            print(f"ERROR: {error_detail}")
            st.session_state.chat_send_error = "An unexpected error occurred. Please try again."

if should_rerun:
    request_chat_autoscroll()
    # Strategic remount after a completed turn — clears pale ghost shells on mobile.
    st.rerun()
