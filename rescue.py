import streamlit as st
import sqlite3
import os
import json
import pandas as pd
import database as db

st.set_page_config(layout="wide", page_title="🚑 Data X-Ray")
st.title("🚑 Emergency Data X-Ray")

BASE_PROJECT_DIR = "Projects"
BASE_SYSTEM_DIR = "System"

# 1. Project Selector
if not os.path.exists(BASE_PROJECT_DIR):
    st.error(f"❌ Project Directory not found: {BASE_PROJECT_DIR}")
    st.stop()

projects = [d for d in os.listdir(BASE_PROJECT_DIR) if os.path.isdir(os.path.join(BASE_PROJECT_DIR, d))]
active_project = st.selectbox("Select Project to Inspect:", projects)

if active_project:
    proj_dir = os.path.join(BASE_PROJECT_DIR, active_project)
    proj_db = db.get_project_db_path(BASE_PROJECT_DIR, active_project)
    sys_db = db.get_system_db_path(BASE_SYSTEM_DIR)
    hist_path = os.path.join(proj_dir, "history.json")

    col1, col2, col3 = st.columns(3)

    # --- CHECK 1: HISTORY.JSON (The Ultimate Backup) ---
    with col1:
        st.header("1. Chat History")
        if os.path.exists(hist_path):
            size = os.path.getsize(hist_path)
            st.info(f"✅ Found `history.json` ({size} bytes)")
            try:
                with open(hist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    st.write(f"**Total Turns:** {len(data)}")
                    st.json(data[-2:]) # Show last 2 messages
            except Exception as e:
                st.error(f"Read Error: {e}")
        else:
            st.error("❌ `history.json` MISSING!")

    # --- CHECK 2: PROJECT DB (Local Memory) ---
    with col2:
        st.header("2. Project DB")
        if os.path.exists(proj_db):
            st.info("✅ Found `project.db`")
            st.caption(proj_db)
            try:
                conn = sqlite3.connect(proj_db)
                df = pd.read_sql_query("SELECT slot_name, length(content) as chars, scope FROM memory_slots", conn)
                conn.close()
                if not df.empty:
                    st.dataframe(df)
                else:
                    st.warning("⚠️ Table is EMPTY")
            except Exception as e:
                st.error(f"DB Error: {e}")
        else:
            st.error("❌ `project.db` MISSING!")

    # --- CHECK 3: SYSTEM DB (Global Memory) ---
    with col3:
        st.header("3. System DB")
        if os.path.exists(sys_db):
            st.info(f"✅ Found `system.db`")
            try:
                conn = sqlite3.connect(sys_db)
                df = pd.read_sql_query("SELECT slot_name, length(content) as chars, scope FROM memory_slots", conn)
                conn.close()
                if not df.empty:
                    st.dataframe(df)
                else:
                    st.warning("⚠️ Table is EMPTY")
            except Exception as e:
                st.error(f"DB Error: {e}")
        else:
            st.warning("⚠️ `system.db` not found (Normal if new)")

    # --- RECOVERY ACTIONS ---
    st.divider()
    st.subheader("🛠️ Recovery Actions")
    
    if st.button("♻️ FORCE REGENERATE DATABASE FROM HISTORY"):
        st.warning("This will replay your entire chat history to rebuild the memory slots.")
        # Logic to read history.json and parsing the LAST state of memory slots
        # This is complex, but often the history contains the "Harvest" outputs which contain the data.
        
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                history = json.load(f)
            
            # Scan backwards for the last time the AI outputted a Memory Manifest
            found_slots = {}
            
            # Simple Regex to find content blocks in history (Looking for "--- MI_Master_Record ---" etc)
            import re
            
            # Common Core Keys
            keys_to_find = ["MI_Master_Record", "MC_Frontier", "MI_Project_Status", "SI_User_Profile", "SI_Observed_Profile"]
            
            for msg in reversed(history):
                content = msg.get("content", "")
                
                # Check for standard JSON updates (Harvests)
                if "MI_Master_Record" in content and "{" in content:
                    try:
                        # Try to extract JSON blob
                        json_match = re.search(r"\{.*\}", content, re.DOTALL)
                        if json_match:
                            data = json.loads(json_match.group(0))
                            for k, v in data.items():
                                if k not in found_slots and v:
                                    found_slots[k] = v
                    except: pass
                
            if found_slots:
                st.success(f"Found salvageable data for: {list(found_slots.keys())}")
                st.json(found_slots)
                
                if st.button("💾 SAVE RECOVERED DATA TO DB"):
                    import database as db
                    p_db = db.get_project_db_path("Projects", active_project)
                    s_db = db.get_system_db_path("System")
                    db.init_db(p_db)
                    db.init_db(s_db)
                    
                    for k, v in found_slots.items():
                        # Determine scope
                        scope = "global" if "SI_" in k else "project"
                        target_db = s_db if scope == "global" else p_db
                        
                        conn = sqlite3.connect(target_db)
                        c = conn.cursor()
                        c.execute("INSERT OR REPLACE INTO memory_slots (slot_name, content, scope) VALUES (?, ?, ?)", (k, str(v), scope))
                        conn.commit()
                        conn.close()
                    st.success("Database Repopulated! Restart main app.")
            else:
                st.warning("Could not automatically scrape data from history. You may need to manually copy text from the 'Chat History' column.")

        except Exception as e:
            st.error(f"Recovery Failed: {e}")