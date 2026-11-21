import sqlite3
import os

# --- 1. DATABASE PATH HELPERS ---
# The project folder is on a normal local disk. project.db stays in that folder.
def get_project_db_path(base_dir, project_name):
    """Path to the project database inside the project folder."""
    proj_dir = os.path.join(base_dir, project_name)
    if not os.path.exists(proj_dir):
        os.makedirs(proj_dir)
    return os.path.join(proj_dir, "project.db")

def get_system_db_path(base_dir):
    """Path to the global system database inside the System folder."""
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)
    return os.path.join(base_dir, "system.db")

def add_ai_managed_column(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE memory_slots ADD COLUMN ai_managed BOOLEAN DEFAULT FALSE")
        conn.commit()
        print("Added 'ai_managed' column to memory_slots")
    except sqlite3.OperationalError as e:
        print(f"Column 'ai_managed' likely already exists: {e}")
    conn.close()

def create_ai_edits_table(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_edits (
            edit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_name TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            old_content TEXT,
            new_content TEXT,
            edit_summary TEXT,
            FOREIGN KEY (slot_name) REFERENCES memory_slots(slot_name)
        )
    """)
    conn.commit()
    conn.close()

# database.py

def get_ai_edit_history(db_path, slot_name):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT edit_id, timestamp, old_content, new_content, edit_summary FROM ai_edits WHERE slot_name = ? ORDER BY timestamp DESC", (slot_name,))
    rows = c.fetchall()
    conn.close()
    # Convert rows to a list of dictionaries
    history = []
    for row in rows:
        history.append({
            'edit_id': row[0],
            'timestamp': row[1],
            'old_content': row[2],
            'new_content': row[3],
            'edit_summary': row[4]
        })
    return history

def undo_ai_edit(db_path, edit_id, slot_name):
    """Put the stored previous text back. Returns that text, or None if the edit is missing."""
    conn = sqlite3.connect(db_path, timeout=30)
    c = conn.cursor()
    restored = None
    c.execute("SELECT old_content FROM ai_edits WHERE edit_id = ?", (edit_id,))
    result = c.fetchone()
    if result:
        restored = result[0] if result[0] is not None else ""
        c.execute("UPDATE memory_slots SET content = ? WHERE slot_name = ?", (restored, slot_name))
        c.execute("DELETE FROM ai_edits WHERE edit_id = ?", (edit_id,))
        conn.commit()
    else:
        print(f"Edit with id {edit_id} not found.")
    conn.close()
    return restored

def get_slot_content(db_path, slot_name):
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

def set_ai_managed_status(db_path, slot_name, ai_managed):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("UPDATE memory_slots SET ai_managed = ? WHERE slot_name = ?", (ai_managed, slot_name))
    conn.commit()
    conn.close()

def migrate_db(db_path):
    add_ai_managed_column(db_path)
    create_ai_edits_table(db_path)
    with sqlite3.connect(db_path) as conn:
        _add_column_if_not_exists(conn.cursor(), 'memory_slots', 'folder', "TEXT DEFAULT ''")
        conn.commit()



# --- 2. DATABASE INITIALIZATION & MIGRATION ---
def _add_column_if_not_exists(cursor, table_name, column_name, column_def):
    """Utility to safely add a column to a table if it doesn't exist."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
        print(f"Added column '{column_name}' to table '{table_name}'.")

def init_db(db_path):
    """Initializes the database, creating tables and adding new columns if needed."""
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_slots (
                slot_name TEXT PRIMARY KEY,
                content TEXT,
                slot_type TEXT,
                description TEXT,
                scope TEXT,
                owner_id TEXT,
                last_updated TIMESTAMP
            )
        """)

        # --- [NEW] DTN METADATA MIGRATION ---
        # Safely add the new columns for the Distributed Truth Network
        _add_column_if_not_exists(c, 'memory_slots', 'is_distributed', 'BOOLEAN DEFAULT 0')
        _add_column_if_not_exists(c, 'memory_slots', 'part_of_dtn', 'BOOLEAN DEFAULT 0')
        _add_column_if_not_exists(c, 'memory_slots', 'ai_managed', 'BOOLEAN DEFAULT 0') # Ensure this is also added
        _add_column_if_not_exists(c, 'memory_slots', 'folder', "TEXT DEFAULT ''")

        c.execute("""
            CREATE TABLE IF NOT EXISTS ai_edits (
                edit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_name TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                old_content TEXT,
                new_content TEXT,
                edit_summary TEXT
            )
        """)

        conn.commit()

# This function remains for legacy projects but is less critical now.
def migrate_flat_files(project_dir, db_path):
    """One-time migration of legacy flat files to the SQLite database."""
    # This function would need to be updated if we expect flat files to have this new metadata,
    # but for now, we assume it's for migrating old data which will have default False values.
    pass

# --- 3. CORE CRUD OPERATIONS (Create, Read, Update, Delete) ---
# database.py
def save_slot(proj_db, sys_db, slot_name, content, slot_type, description, scope, owner_id='system', is_distributed=False, part_of_dtn=False, ai_managed=False, old_content=None, edit_summary=None, folder=None):
    # Determine which database to connect to based on scope
    target_db_path = sys_db if scope == 'global' else proj_db
    if not target_db_path:
        print(f"ERROR: No database path provided for scope '{scope}' for slot '{slot_name}'. Skipping save.")
        return

    conn = sqlite3.connect(target_db_path, timeout=30)
    c = conn.cursor()
    try:
        # Check if the slot already exists (preserve folder when caller omits it)
        c.execute("SELECT content, COALESCE(folder, '') FROM memory_slots WHERE slot_name = ?", (slot_name,))
        existing_row = c.fetchone()
        stored_content = existing_row[0] if existing_row else None
        if folder is None:
            folder = existing_row[1] if existing_row else ""
        else:
            folder = str(folder or "")

        # Keep the previous text before replacing it, manual saves included.
        # Unchanged rewrites (a full project save) do not add a history row.
        if stored_content is not None and str(stored_content) != str(content if content is not None else ""):
            summary = edit_summary or description or "Save"
            c.execute(
                "INSERT INTO ai_edits (slot_name, old_content, new_content, edit_summary) VALUES (?, ?, ?, ?)",
                (slot_name, stored_content, content, summary),
            )

        # Update or Insert the slot
        c.execute("""
            INSERT INTO memory_slots (slot_name, content, slot_type, description, scope, owner_id, is_distributed, part_of_dtn, ai_managed, folder)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slot_name) DO UPDATE SET
                content = excluded.content,
                slot_type = excluded.slot_type,
                description = excluded.description,
                scope = excluded.scope,
                owner_id = excluded.owner_id,
                is_distributed = excluded.is_distributed,
                part_of_dtn = excluded.part_of_dtn,
                ai_managed = excluded.ai_managed,
                folder = excluded.folder
        """, (slot_name, content, slot_type, description, scope, owner_id, int(is_distributed), int(part_of_dtn), ai_managed, folder))

        conn.commit()
    except Exception as e:
        print(f"Database save error: {e}")
    finally:
        conn.close()


def load_all_slots(proj_db=None, sys_db=None, active_lens=None):
    """Loads all relevant slots from a database, now including DTN and ai_managed metadata."""
    slots_meta = {}
    db_paths = [p for p in [proj_db, sys_db] if p]

    for db_path in db_paths:
        if not os.path.exists(db_path): continue
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            base_query = """
                SELECT
                    slot_name, content, slot_type, description, scope, owner_id, last_updated,
                    is_distributed, part_of_dtn, ai_managed, COALESCE(folder, '')
                FROM memory_slots
            """

            query = ""
            params_to_bind = () # Initialize as an empty tuple for sqlite3

            # Sanitize active_lens to ensure it's always a string or None
            # This is a double-check, as app.py should ideally handle this
            sanitized_active_lens = None
            if active_lens is not None:
                if isinstance(active_lens, list):
                    # If active_lens is a list, try to take the first element or default to None
                    temp_lens = active_lens[0] if active_lens else None
                    sanitized_active_lens = str(temp_lens) if temp_lens is not None else None
                else:
                    # Otherwise, ensure it's a string (handles str or None gracefully)
                    sanitized_active_lens = str(active_lens) if active_lens is not None else None

            # If sanitized_active_lens is None or an empty string, or "General", treat as no specific lens filter
            if not sanitized_active_lens or sanitized_active_lens == "General":
                # If no specific lens is active (or "General"), get all non-lens slots
                query = base_query + " WHERE scope != 'lens'"
                params_to_bind = () # No parameters needed for this query
            else:
                # If a specific lens is active, filter by that lens, and also include non-lens slots
                query = base_query + " WHERE scope != 'lens' OR (scope = 'lens' AND owner_id = ?)"
                params_to_bind = (sanitized_active_lens,) # Pass as a tuple with the single parameter

            c.execute(query, params_to_bind)
            rows = c.fetchall()

            for row in rows:
                slot_name = row[0]
                # Project DB always overrides System DB if a slot with the same name exists
                if slot_name not in slots_meta:
                    slots_meta[slot_name] = {
                        "slot_name": slot_name,
                        "content": row[1],
                        "slot_type": row[2],
                        "description": row[3],
                        "scope": row[4],
                        "owner_id": row[5],
                        "last_updated": row[6],
                        "is_distributed": bool(row[7]), # Cast to boolean
                        "part_of_dtn": bool(row[8]),     # Cast to boolean
                        "ai_managed": bool(row[9]), # Cast to boolean and retrieve
                        "folder": row[10] if row[10] is not None else ""
                    }
    return slots_meta

def delete_slot(proj_db, sys_db, slot_name, scope):
    """Deletes a slot from the appropriate database based on its scope."""
    db_path = sys_db if scope == 'global' else proj_db
    if not db_path or not os.path.exists(db_path): return

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM memory_slots WHERE slot_name = ?", (slot_name,))
        conn.commit()