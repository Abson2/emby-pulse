import sqlite3
import os
from app.core.config import cfg, DB_PATH

def init_db():
    # 确保数据库目录存在
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except: pass

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # 1. 只初始化机器人专属配置表 (不碰插件的表)
        c.execute('''CREATE TABLE IF NOT EXISTS users_meta (
                        user_id TEXT PRIMARY KEY,
                        expire_date TEXT,
                        note TEXT,
                        created_at TEXT
                    )''')
        
        # 2. 🔥 新增/更新：邀请码表 (加入 template_user_id)
        c.execute('''CREATE TABLE IF NOT EXISTS invitations (
                        code TEXT PRIMARY KEY,
                        days INTEGER,        -- 有效期天数 (-1为永久)
                        used_count INTEGER DEFAULT 0,
                        max_uses INTEGER DEFAULT 1,
                        created_at TEXT,
                        template_user_id TEXT -- 🔥 绑定的权限模板用户
                    )''')
        
        # 🔥 兼容老版本数据库：尝试追加列 (如果列已存在会抛异常，忽略即可)
        try:
            c.execute("ALTER TABLE invitations ADD COLUMN template_user_id TEXT")
        except:
            pass

        conn.commit()
        conn.close()
        print("✅ Database initialized (Plugin Read-Only Mode).")
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
    
    # 注意：插件数据库列名通常是 UserId (PascalCase)
    # 如果您的插件版本不同，可能需要改为 user_id，但标准版是 UserId
    if user_id_filter and user_id_filter != 'all':
        where += " AND UserId = ?"
        params.append(user_id_filter)
    
    # 隐藏用户过滤
    hidden = cfg.get("hidden_users")
    if (not user_id_filter or user_id_filter == 'all') and hidden and len(hidden) > 0:
        placeholders = ','.join(['?'] * len(hidden))
        where += f" AND UserId NOT IN ({placeholders})"
        params.extend(hidden)
        
    return where, params