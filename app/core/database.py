import sqlite3
import os
from app.core.config import cfg, DB_PATH

def init_db():
    # 确保目录存在
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except: pass

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # 1. 用户表
        c.execute('''CREATE TABLE IF NOT EXISTS users_meta (
                        user_id TEXT PRIMARY KEY,
                        expire_date TEXT,
                        note TEXT,
                        created_at TEXT
                    )''')
        
        # 2. 播放记录表
        c.execute('''CREATE TABLE IF NOT EXISTS PlaybackActivity (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        user_name TEXT,
                        item_id TEXT,
                        item_name TEXT,
                        item_type TEXT,
                        device_name TEXT,
                        client TEXT,
                        date_created TEXT,
                        playback_ticks INTEGER DEFAULT 0
                    )''')
        
        # 3. 自动迁移：检查并补全字段
        c.execute("PRAGMA table_info(PlaybackActivity)")
        columns = [row[1] for row in c.fetchall()]
        
        # 需要确保存在的字段列表
        required_cols = [
            ("user_id", "TEXT"),
            ("user_name", "TEXT"),
            ("item_id", "TEXT"),
            ("item_name", "TEXT"),
            ("item_type", "TEXT"), 
            ("device_name", "TEXT"),
            ("client", "TEXT"),
            ("playback_ticks", "INTEGER DEFAULT 0") # 🔥 新增：记录播放时长
        ]
        
        for col_name, col_type in required_cols:
            if col_name not in columns:
                print(f"🛠️ 数据库升级: 正在添加列 '{col_name}'...")
                try:
                    c.execute(f"ALTER TABLE PlaybackActivity ADD COLUMN {col_name} {col_type}")
                except Exception as e:
                    print(f"⚠️ 添加列失败: {e}")

        conn.commit()
        conn.close()
        print("✅ Database initialized & checked.")
    except Exception as e: 
        print(f"❌ DB Init Error: {e}")

def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): return None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=20.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        if query.strip().upper().startswith("SELECT"):
            rv = cur.fetchall()
            conn.close()
            return (rv[0] if rv else None) if one else rv
        else:
            conn.commit()
            conn.close()
            return True
    except Exception as e: 
        print(f"SQL Error: {e}")
        return None

def get_base_filter(user_id_filter):
    where = "WHERE 1=1"
    params = []
    if user_id_filter and user_id_filter != 'all':
        where += " AND user_id = ?"
        params.append(user_id_filter)
    
    hidden = cfg.get("hidden_users")
    if (not user_id_filter or user_id_filter == 'all') and hidden and len(hidden) > 0:
        placeholders = ','.join(['?'] * len(hidden))
        where += f" AND user_id NOT IN ({placeholders})"
        params.extend(hidden)
        
    return where, params