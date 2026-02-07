import os
import io
import requests
import datetime
from app.core.config import cfg, FONT_PATH, FONT_URL, THEMES
from app.core.database import query_db, get_base_filter
from app.core.database import DB_PATH 

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("⚠️ Pillow not found. Report generation disabled.")

def get_user_map_internal():
    # 简单的内部获取，避免循环引用
    user_map = {}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if key and host:
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=2)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except: pass
    return user_map

class ReportGenerator:
    def __init__(self):
        if HAS_PIL: self.check_font()
    
    def check_font(self):
        if not os.path.exists(FONT_PATH):
            try:
                # 确保父目录存在
                os.makedirs(os.path.dirname(FONT_PATH), exist_ok=True)
                res = requests.get(FONT_URL, timeout=30)
                if res.status_code == 200:
                    with open(FONT_PATH, 'wb') as f: f.write(res.content)
            except: pass

    def draw_rounded_rect(self, draw, xy, color, radius=15):
        if not HAS_PIL: return
        draw.rounded_rectangle(xy, radius=radius, fill=color)

    def generate_report(self, user_id, period, theme_name="black_gold"):
        if not HAS_PIL: return None
        theme = THEMES.get(theme_name, THEMES["black_gold"])
        width, height = 800, 1200
        
        # 获取基础过滤条件 (处理 UserId 和隐藏用户)
        where_base, params = get_base_filter(user_id)
        
        date_filter = ""
        title_period = "全量"
        
        # 多周期支持逻辑
        if period == 'week': 
            date_filter = " AND DateCreated > date('now', '-7 days')"
            title_period = "本周观影周报"
        elif period == 'month': 
            date_filter = " AND DateCreated > date('now', '-30 days')"
            title_period = "本月观影月报"
        elif period == 'year': 
            date_filter = " AND DateCreated > date('now', '-1 year')"
            title_period = "年度观影报告"
        elif period == 'day': 
            date_filter = " AND DateCreated > date('now', 'start of day')"
            title_period = "今日日报"
        else: 
            title_period = "全量观影报告"

        full_where = where_base + date_filter
        
        # 1. 基础数据查询 (适配插件列名 PlayDuration)
        # 注意：Emby插件表 PlayDuration 单位通常是秒
        plays_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {full_where}", params)
        plays = plays_res[0]['c'] if plays_res else 0
        
        dur_res = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {full_where}", params)
        dur = dur_res[0]['c'] if dur_res and dur_res[0]['c'] else 0
        hours = round(dur / 3600, 1)
        
        # 获取用户名
        user_name = "Emby Server"
        if user_id != 'all': 
            user_name = get_user_map_internal().get(user_id, "User")
        
        # 2. 排行榜查询 (适配插件列名 ItemName, ItemId)
        top_list = []
        if plays > 0:
            sql = f"SELECT ItemName, ItemId, COUNT(*) as C, SUM(PlayDuration) as D FROM PlaybackActivity {full_where} GROUP BY ItemName ORDER BY C DESC LIMIT 8"
            top_list = query_db(sql, params)

        # 3. 绘图逻辑
        try: 
            font_lg = ImageFont.truetype(FONT_PATH, 60)
            font_md = ImageFont.truetype(FONT_PATH, 40)
            font_sm = ImageFont.truetype(FONT_PATH, 28)
            font_xs = ImageFont.truetype(FONT_PATH, 22)
        except: 
            font_lg = font_md = font_sm = font_xs = ImageFont.load_default()

        img = Image.new('RGB', (width, height), theme['bg'])
        draw = ImageDraw.Draw(img)
        
        # 头部文字
        draw.text((40, 60), user_name, font=font_lg, fill=theme['text'])
        draw.text((40, 140), f"{title_period}", font=font_sm, fill=theme['text'])
        
        # 播放次数卡片
        self.draw_rounded_rect(draw, (40, 220, 390, 370), theme['card'])
        draw.text((70, 250), str(plays), font=font_lg, fill=theme['highlight'])
        draw.text((70, 320), "播放次数", font=font_sm, fill=theme['text'])
        
        # 专注时长卡片
        self.draw_rounded_rect(draw, (410, 220, 760, 370), theme['card'])
        draw.text((440, 250), str(hours), font=font_lg, fill=theme['highlight'])
        draw.text((440, 320), "专注时长(H)", font=font_sm, fill=theme['text'])

        # 榜单列表
        list_y = 420
        draw.text((40, list_y), "🏆 内容风云榜", font=font_md, fill=theme['text'])
        item_y = list_y + 70
        
        if top_list:
            for i, item in enumerate(top_list):
                self.draw_rounded_rect(draw, (40, item_y, 760, item_y+60), theme['card'], radius=10)
                
                # 截取过长标题
                name_raw = item['ItemName']
                name = name_raw[:20] + "..." if len(name_raw) > 20 else name_raw
                
                draw.text((60, item_y+15), str(i+1), font=font_sm, fill=theme['highlight'])
                draw.text((120, item_y+15), name, font=font_sm, fill=theme['text'])
                
                # 右侧显示次数
                count_txt = f"{item['C']}次"
                # 简单右对齐处理
                try: w = draw.textlength(count_txt, font=font_sm)
                except: w = 40
                draw.text((720-w, item_y+15), count_txt, font=font_sm, fill=theme['text'])
                
                item_y += 70
        else:
            draw.text((300, item_y+50), "暂无数据", font=font_md, fill=(100,100,100))

        # 底部水印
        draw.text((250, 1150), "Generated by EmbyPulse", font=font_xs, fill=(80, 80, 80))

        output = io.BytesIO()
        img.save(output, format='JPEG', quality=95)
        output.seek(0)
        return output

report_gen = ReportGenerator()