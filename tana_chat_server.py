import os
import json
import sqlite3
import time
import re
import subprocess
import requests
from dotenv import load_dotenv

# --- Local Path Resolution ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TANA_TOKEN = os.getenv("TANA_TOKEN")
TANA_URL = os.getenv("TANA_URL", "http://127.0.0.1:8262/mcp")
# Users should set this in their .env
GEMINI_PATH = os.getenv("GEMINI_PATH", "gemini")
DB_PATH = os.path.join(BASE_DIR, "state.db")

# Framework definitions (Tag IDs from the official template)
TAG_CONFIGS = {
    "rz6VnOCKtT2r": { "FIELD_CHAT_ID": "SzMaBrkt7Hkc", "name": "Ask Tana" },
    "Y_bFazilblQ2": { "FIELD_CHAT_ID": "b1L_j8Bfspju", "name": "AI Chat" }
}

# --- Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS processed (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def is_done(node_id):
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute("SELECT id FROM processed WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    return res is not None

def mark_done(node_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO processed (id) VALUES (?)", (node_id,))
    conn.commit()
    conn.close()

# --- Tana Communication ---
def call_mcp(method, params):
    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": method, "arguments": params}, "id": 1}
    headers = {
        "Authorization": f"Bearer {TANA_TOKEN}", 
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    try:
        r = requests.post(TANA_URL, json=payload, headers=headers, timeout=15)
        if r.status_code != 200: return None
        res = r.json().get("result", {})
        if "content" in res and isinstance(res["content"], list):
            txt = res["content"][0].get("text", "")
            try: return json.loads(txt)
            except: return txt
        return res
    except: return None

# --- AI Inference ---
def get_ai(prompt, sid=None):
    # YOLO mode with isolated execution (no local MCP tools to prevent hangs)
    cmd = [GEMINI_PATH, "--prompt", prompt, "--approval-mode", "yolo", "--allowed-mcp-server-names", "none", "--output-format", "json"]
    if sid: cmd.extend(["--resume", sid])
    try:
        res = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120).decode().strip()
        j_start = res.find('{')
        if j_start != -1:
            data = json.loads(res[j_start:])
            raw_resp = data.get("response", "").strip()
            # CLEANUP: Remove technical noise patterns
            clean_resp = re.sub(r'(?i)(YOLO mode is enabled|Loaded cached credentials|Loading extension|Both GOOGLE_API_KEY|Using GOOGLE_API_KEY|All tool calls).*?\n?', '', raw_resp)
            return clean_resp.strip(), data.get("session_id")
        return res, None
    except: return None, None

# --- Processing Logic ---
def process(c_id, config):
    nodes = call_mcp("get_children", {"nodeId": c_id})
    if not nodes or "children" not in nodes: return
    items = [c for c in nodes["children"] if c.get("docType") != "tuple"]
    if not items: return

    for i in range(len(items)-1, 0, -1):
        p, m = items[i], items[i-1]
        p_txt, m_txt, m_id = p["name"].strip(), m["name"].strip(), m["id"]
        
        # Implementation: Only process if a prompt is followed by an empty slot or "Thinking..."
        if m_text and not m_text.startswith("🤖") and (not p_txt or p_txt == "⏳ Thinking...") and not is_done(m_id):
            print(f"[{time.strftime('%H:%M:%S')}] 📬 Processing: {m_text[:30]}...")
            call_mcp("edit_node", {"nodeId": p["id"], "name": {"old_string": p["name"], "new_string": "⏳ Thinking..."}})
            
            # Get Session ID from metadata
            meta = call_mcp("read_node", {"nodeId": c_id, "maxDepth": 0})
            sid = None
            if meta and isinstance(meta, str):
                match = re.search(r'Chat ID\*\*:\s*([a-zA-Z0-9-]+)', meta)
                if match: sid = match.group(1)
            
            ans, new_sid = get_ai(m_txt, sid)
            if ans:
                # Deliver Response with Bare Header and Hierarchy
                paste = f"%%tana%%\n- !! 🤖 Assistant:\n"
                
                ans_lines = [l.strip() for l in ans.split('\n') if l.strip()]
                # Strip redundant headers
                while ans_lines and (re.search(r'(?i)assistant:', ans_lines[0]) or len(ans_lines[0]) < 3):
                    ans_lines = ans_lines[1:]
                
                current_indent = "  "
                for line in ans_lines:
                    # Detect list items or bolded Step headers
                    is_list_item = re.match(r'^(\*\*|__)?(\d+\.|\*|-|#+|Step \d+:?)\s+', line)
                    if is_list_item:
                        paste += f"  - {line}\n"
                        current_indent = "    "
                    else:
                        paste += f"{current_indent}- {line}\n"
                
                if call_mcp("import_tana_paste", {"parentNodeId": c_id, "content": paste}):
                    mark_done(m_id)
                    call_mcp("trash_node", {"nodeId": p["id"]})
                    if new_sid and new_sid != sid:
                        call_mcp("set_field_content", {"nodeId": c_id, "attributeId": config["FIELD_CHAT_ID"], "content": new_sid})
                    print(f"[{time.strftime('%H:%M:%S')}] ✅ Delivered.")
            break

def main():
    print(f"[{time.strftime('%H:%M:%S')}] 🚀 Ask Tana Server Active.")
    init_db()
    while True:
        try:
            for tid, cfg in TAG_CONFIGS.items():
                chats = call_mcp("search_nodes", {"query": {"hasType": tid}})
                if isinstance(chats, list):
                    for c in chats:
                        if not c.get("inTrash"): process(c["id"], cfg)
            print(".", end="", flush=True)
        except Exception as e: 
            print(f"\n❌ Error: {e}")
            time.sleep(2)
        time.sleep(5)

if __name__ == "__main__":
    main()
