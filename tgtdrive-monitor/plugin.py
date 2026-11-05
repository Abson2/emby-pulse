# -*- coding: utf-8 -*-
PLUGIN_MANIFEST = {
    'schema_version': 1,
    'id': 'emby-pulse-monitor',
    'name': '求片完结关键词清理',
    'version': '1.0.0',
    'description': '读取 TgtoDrive 白名单里本插件写入的 kw_ 规则，按影视是否已入库 Emby 判定完结，满 cleanup_days 天后删除对应关键词（只删本插件的，不动用户规则）。',
    'author': 'Abson2',
    'timeout': 120,
    'inputs': [
        {'name': 'tgtdrive_base_url', 'label': 'TgtoDrive 地址', 'type': 'text',
         'default': 'http://127.0.0.1:12366', 'placeholder': 'http://127.0.0.1:12366'},
        {'name': 'tgtdrive_token', 'label': 'TgtoDrive OpenAPI 令牌', 'type': 'password',
         'default': '', 'placeholder': 'Bearer 令牌'},
        {'name': 'scene', 'label': '白名单场景', 'type': 'text',
         'default': 'ENV_FILTER_GUANGYA', 'placeholder': 'ENV_FILTER_GUANGYA'},
        {'name': 'whitelist_endpoint', 'label': '白名单接口', 'type': 'text',
         'default': '/api/visual-filter/config', 'placeholder': '/api/visual-filter/config'},
        {'name': 'emby_url', 'label': 'Emby 地址', 'type': 'text',
         'default': 'http://127.0.0.1:8096', 'placeholder': 'http://127.0.0.1:8096'},
        {'name': 'emby_key', 'label': 'Emby API Key', 'type': 'password',
         'default': '', 'placeholder': 'Emby 设置 → API 密钥'},
        {'name': 'cleanup_days', 'label': '完结后清理天数', 'type': 'number',
         'default': 5, 'placeholder': '5'},
    ],
}

"""TgtoDrive 插件：求片完结关键词清理（v1.0.0）

只使用 Python 标准库（urllib / json / pathlib / re）。

分工（与 EMBYPULSE 侧「求片同步 TgtoDrive 白名单」插件 A 配合）：
- 插件 A（运行在 EMBYPULSE）把审批通过/手动接单的求片**推送**进 TgtoDrive 的
  「频道监控 → 关键词白名单」，规则 id 带 kw_ 前缀（如 kw_123_0），且**永不删除**任何规则。
- 本插件（运行在 TgtoDrive 容器内，由 OpenAPI 按计划周期调用）负责**清理**：
  读取白名单里本插件写入的 kw_ 规则，按「影视已入库 Emby」判定完结；
  完结满 cleanup_days（默认 5）天后，删除对应的 kw_ 关键词，释放 tg 频道监控配额。
  只删 kw_ 前缀、属于本插件所有的规则，绝不碰用户其它关键词。

完结判据：影视已在 Emby 媒体库中存在（通过 ProviderIds.Tmdb 精确匹配，兜底按名称+年份）。
未入库则不计时、不删除；一旦入库开始计时，满期才删。删除采用「读-过滤-整体 POST 覆盖」，
同样丢弃陈旧的 raw_regex 让后端重新编译。

注意：本目录不在凭证扫描器的豁免名单内，故默认地址一律用 127.0.0.1（回环），
不出现任何具体内网 IP 或第三方域名。
"""
import json
import time
import urllib.request
import urllib.error
import re
from pathlib import Path


RULE_ID_PREFIX = 'kw_'
KW_RE = re.compile(r'^kw_(\d+)_(\d+)$')


def _http_json(url, token=None, method='GET', payload=None, timeout=20):
    """极简 HTTP 客户端（urllib）。返回 (status_code, parsed_json_or_None, text)。"""
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode('utf-8', 'replace')
            status = resp.getcode()
            try:
                return status, json.loads(text), text
            except ValueError:
                return status, None, text
    except urllib.error.HTTPError as e:
        text = ''
        try:
            text = e.read().decode('utf-8', 'replace')
        except Exception:
            pass
        try:
            return e.code, json.loads(text), text
        except ValueError:
            return e.code, None, text
    except urllib.error.URLError as e:
        return 0, None, str(getattr(e, 'reason', e))


def _extract_rules(cfg):
    if not isinstance(cfg, dict):
        return []
    rules = cfg.get('rules')
    if isinstance(rules, list):
        return rules
    data = cfg.get('data')
    if isinstance(data, dict):
        r = data.get('rules')
        if isinstance(r, list):
            return r
    return []


def _norm(s):
    import re
    if not s:
        return ''
    return re.sub(r'[\s\-_.:，。！!?()（）\[\]【】]+', '', str(s)).lower()


def _emby_has_item(emby_url, emby_key, tmdb_id, title, media_type, year, timeout=15):
    """判断影视是否已入库 Emby。命中返回 True，Emby 不可达/异常返回 None（保守：不删）。"""
    if not emby_url:
        return None
    base = emby_url.rstrip('/')
    item_types = 'Series' if media_type == 'tv' else 'Movie'
    params = {
        'api_key': emby_key or '',
        'Recursive': 'true',
        'IncludeItemTypes': item_types,
        'Fields': 'ProviderIds,ProductionYear',
        'Limit': '20',
    }
    # 优先：ProviderIds.Tmdb 精确匹配
    exact = f'{base}/Items?' + _urlencode({'api_key': params['api_key'],
                                           'Recursive': 'true',
                                           'IncludeItemTypes': item_types,
                                           'AnyProviderIdEquals': 'Tmdb.' + str(tmdb_id),
                                           'Limit': '1'})
    status, body, _ = _http_json(exact, timeout=timeout)
    if status == 200 and isinstance(body, dict):
        items = body.get('Items') or []
        if items:
            return True
    # 兜底：按名称 + 年份（Emby 不可达时返回 None，保守不删）
    if not title:
        return None
    search = f'{base}/Items?' + _urlencode({'api_key': params['api_key'],
                                            'Recursive': 'true',
                                            'IncludeItemTypes': item_types,
                                            'SearchTerm': title,
                                            'Fields': 'ProviderIds,ProductionYear',
                                            'Limit': '20'})
    status2, body2, _ = _http_json(search, timeout=timeout)
    if status2 != 200 or not isinstance(body2, dict):
        return None
    items = body2.get('Items') or []
    norm_title = _norm(title)
    for it in items:
        prov = it.get('ProviderIds') or {}
        if prov.get('Tmdb') == str(tmdb_id):
            return True
        if (year and it.get('ProductionYear') and str(it.get('ProductionYear')) == str(year)
                and norm_title and _norm(it.get('Name')) == norm_title):
            return True
    return False


def _urlencode(d):
    import urllib.parse
    return urllib.parse.urlencode(d)


def run(params, context):
    p = params or {}
    tg_url = (p.get('tgtdrive_base_url') or 'http://127.0.0.1:12366').rstrip('/')
    tg_token = (p.get('tgtdrive_token') or '').strip()
    scene = (p.get('scene') or 'ENV_FILTER_GUANGYA').strip()
    endpoint = (p.get('whitelist_endpoint') or '/api/visual-filter/config').strip()
    emby_url = (p.get('emby_url') or '').strip()
    emby_key = (p.get('emby_key') or '').strip()
    # 显式 0 表示「永不清理」；仅当未配置（None/空）才回落默认 5。
    # 注意：不能用 `p.get('cleanup_days') or 5`，否则用户填 0 会被误当成未配置。
    raw_days = p.get('cleanup_days')
    if raw_days is None or raw_days == '':
        cleanup_days = 5
    else:
        try:
            cleanup_days = max(0, int(raw_days))
        except (TypeError, ValueError):
            cleanup_days = 5

    if cleanup_days <= 0:
        print('cleanup_days<=0，跳过清理')
        return {'ok': True, 'skipped': True, 'detail': 'cleanup_days<=0'}

    stats_file = Path(context['data_dir']) / 'stats.json'
    try:
        completed = json.loads(stats_file.read_text(encoding='utf-8'))
        if not isinstance(completed, dict):
            completed = {}
    except (OSError, ValueError, TypeError):
        completed = {}

    # 1) 读取白名单
    get_url = f'{tg_url}{endpoint}' + ('?' in endpoint and '&' or '?') + _urlencode({'scene': scene})
    status, cfg, _ = _http_json(get_url, token=tg_token, timeout=20)
    if status == 404:
        cfg = {}
    elif status != 200 or not isinstance(cfg, dict):
        msg = f'读取白名单失败 HTTP {status}'
        print(msg)
        return {'ok': False, 'detail': msg}
    rules = _extract_rules(cfg)

    now = time.time()
    threshold = cleanup_days * 86400
    to_remove = set()
    checked = 0
    ingested = 0
    not_ingested = 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rid = rule.get('id') or ''
        if not rid.startswith(RULE_ID_PREFIX):
            continue
        m = KW_RE.match(rid)
        if not m:
            continue
        tmdb_id = int(m.group(1))
        season = int(m.group(2))
        checked += 1
        in_lib = _emby_has_item(emby_url, emby_key, tmdb_id,
                                rule.get('title'), rule.get('media_type'), rule.get('year'))
        if in_lib is None:
            # Emby 不可达：保守处理，不清也不计时（保留原有计时）
            continue
        if not in_lib:
            # 未入库：清掉计时，等真正入库再计时
            if rid in completed:
                del completed[rid]
            not_ingested += 1
            continue
        ingested += 1
        if rid not in completed:
            completed[rid] = now
            continue
        if now - completed[rid] >= threshold:
            to_remove.add(rid)

    # 2) 删除到期规则（读-过滤-整体 POST 覆盖，只删 kw_ 本插件所有）
    removed = 0
    if to_remove:
        kept = [r for r in rules if not (isinstance(r, dict) and r.get('id') in to_remove)]
        removed = len(rules) - len(kept)
        if removed > 0:
            payload = dict(cfg)
            payload['scene'] = scene
            payload['rules'] = kept
            payload.pop('raw_regex', None)
            payload.pop('advanced_raw_regex', None)
            post_url = f'{tg_url}{endpoint}'
            pstatus, _, _ = _http_json(post_url, token=tg_token, method='POST',
                                       payload=payload, timeout=20)
            if pstatus != 200:
                print(f'删除关键词失败 HTTP {pstatus}')
                return {'ok': False, 'detail': f'POST 删除失败 HTTP {pstatus}'}

    # 3) 持久化计时状态
    try:
        stats_file.write_text(json.dumps(completed), encoding='utf-8')
    except OSError:
        pass

    detail = (f'检查 {checked} 条 kw_ 规则，已入库 {ingested}，未入库 {not_ingested}，'
              f'到期删除 {removed} 条（cleanup_days={cleanup_days}）')
    print(detail)
    return {
        'ok': True,
        'checked': checked,
        'ingested': ingested,
        'not_ingested': not_ingested,
        'removed': removed,
        'pending_cleanup': len(completed),
        'detail': detail,
    }
