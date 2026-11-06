# -*- coding: utf-8 -*-
"""
Security utilities for input sanitization
"""

import html
import re
from html.parser import HTMLParser


# 危险模式（供 sanitize_html 与 to_plain_text 共用，避免两处各写一份）
_DANGEROUS_PATTERNS = [
    r'javascript:',
    r'on\w+\s*=',  # 事件处理器，例如 onclick=
    r'data:text/html',
    r'vbscript:',
]


def sanitize_html(text: str, max_length: int = 500) -> str:
    """
    Sanitize user input to prevent XSS attacks
    
    Args:
        text: Input text to sanitize
        max_length: Maximum allowed length (default 500)
    
    Returns:
        Sanitized text safe for storage and display
    """
    if not text:
        return ""
    
    # Convert to string
    text = str(text)
    
    # Truncate to max length
    if len(text) > max_length:
        text = text[:max_length]
    
    # HTML escape
    text = html.escape(text, quote=True)
    
    # Remove potentially dangerous patterns
    for pattern in _DANGEROUS_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    return text


def to_plain_text(text: str, max_length: int = 2000) -> str:
    """把富文本 HTML 转为纯文本通道（用户级 Bark / Webhook 通知）可用的正文。

    修复 2.0 bug：公告正文是 Quill 产出的 HTML（<p>测试推送</p>），此前用
    sanitize_html() 做 html.escape，变成 &lt;p&gt;测试推送&lt;/p&gt;；Bark 是纯
    文本通道，既不渲染标签也不解码实体，用户收到字面量 &lt;p&gt;。
    bark_push._strip_html() 只能剥离真实标签，无法处理实体，故必须在内容源头转换。

    与 sanitize_html 的关系：
    - 纯文本输入：两者输出一致（不引入任何新转义），保证原有行为不变；
    - 富文本输入：本函数剥离标签与实体（修正乱码），sanitize_html 仍用于需要转义的场景。

    步骤：截断 → 实体反转义（兼容历史二次转义数据）→ 剥离标签 → 危险模式清理。
    """
    if not text:
        return ""
    text = str(text)
    if len(text) > max_length:
        text = text[:max_length]
    # 先反转义：&lt;p&gt; → <p>（同时兼容历史被整体转义的公告数据）
    text = html.unescape(text)
    # 剥离所有 HTML 标签（Quill 富文本 / Telegram <b> 等），保留标签内文本
    text = re.sub(r"<[^>]*>", "", text)
    # 与 sanitize_html 相同的安全清理，防止反转义后残留危险模式
    for pattern in _DANGEROUS_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return text


def sanitize_sql(text: str) -> str:
    """
    Additional SQL sanitization for user inputs
    Note: Always use parameterized queries instead of string concatenation
    
    Args:
        text: Input text to sanitize
    
    Returns:
        Sanitized text
    """
    if not text:
        return ""
    
    text = str(text)
    
    # Remove null bytes
    text = text.replace('\x00', '')
    
    # Remove control characters
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    
    return text


def is_safe_redirect(url: str, allowed_hosts: list = None) -> bool:
    """
    Check if a redirect URL is safe (prevents open redirect attacks)
    
    Args:
        url: URL to check
        allowed_hosts: List of allowed hostnames
    
    Returns:
        True if safe, False otherwise
    """
    if not url:
        return False
    
    # Relative URLs are safe
    if url.startswith('/') and not url.startswith('//'):
        return True
    
    # Check for allowed hosts
    if allowed_hosts:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            return parsed.hostname in allowed_hosts
        except:
            return False
    
    return False


def validate_redirect_url(url: str) -> str:
    """
    Validate and sanitize redirect URL
    
    Args:
        url: URL to validate
    
    Returns:
        Safe URL or empty string if invalid
    """
    if not url:
        return ""
    
    # Only allow http/https protocols
    if not url.startswith(('http://', 'https://')):
        return ""
    
    # Block javascript: and data: protocols
    lower_url = url.lower()
    if lower_url.startswith(('javascript:', 'data:', 'vbscript:')):
        return ""
    
    return url


# 🔥 富文本允许的标签 / 属性（与历史 bleach 配置保持一致）
_RICH_ALLOWED_TAGS = {
    'p', 'br', 'strong', 'em', 'u', 's', 'a', 'ul', 'ol', 'li',
    'blockquote', 'code', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'img', 'span', 'div', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'hr', 'sub', 'sup', 'mark',
}
_RICH_VOID_TAGS = {'br', 'hr', 'img'}  # 自闭合，不入栈
_RICH_ALLOWED_ATTRS = {
    'a': {'href', 'title', 'target'},
    'img': {'src', 'alt', 'width', 'height'},
    'span': {'style'},
    'div': {'style'},
    'td': {'colspan', 'rowspan'},
    'th': {'colspan', 'rowspan'},
}
_RICH_SAFE_URL_PREFIXES = ('http://', 'https://', 'mailto:', '/', '#')


class _RichHtmlSanitizer(HTMLParser):
    """依赖标准库、无第三方依赖的富文本净化器。

    与 bleach.clean(strip=True) 语义对齐：保留白名单标签及其安全属性，
    删除非白名单标签（保留其文本子节点），对所有文本节点做 HTML 转义。
    """

    def __init__(self):
        # convert_charrefs=True：实体（&lt; 等）在 handle_data 中先解码为真实字符，
        # 再由我们统一重新转义，避免二次转义残留。
        super().__init__(convert_charrefs=True)
        self._out = []
        self._stack = []
        self._skip_depth = 0  # 处于非白名单标签内部时，仅透传文本、不渲染标签

    def _safe_attrs(self, tag, attrs):
        allowed = _RICH_ALLOWED_ATTRS.get(tag, set())
        safe = []
        for name, value in attrs:
            if name not in allowed:
                continue
            # 拦截 javascript:/data: 等危险协议
            if name in ('href', 'src') and value is not None:
                lowered = value.strip().lower()
                if lowered and not lowered.startswith(_RICH_SAFE_URL_PREFIXES):
                    continue
            safe.append(f' {name}="{html.escape(value or "", quote=True)}"')
        return ''.join(safe)

    def handle_starttag(self, tag, attrs):
        if self._skip_depth > 0:
            self._skip_depth += 1
            return
        if tag in _RICH_ALLOWED_TAGS:
            self._out.append(f'<{tag}{self._safe_attrs(tag, attrs)}>')
            if tag not in _RICH_VOID_TAGS:
                self._stack.append(tag)
        else:
            self._skip_depth = 1

    def handle_startendtag(self, tag, attrs):
        if self._skip_depth > 0:
            return
        if tag in _RICH_ALLOWED_TAGS:
            self._out.append(f'<{tag}{self._safe_attrs(tag, attrs)} />')

    def handle_endtag(self, tag):
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in _RICH_ALLOWED_TAGS and self._stack and self._stack[-1] == tag:
            self._out.append(f'</{tag}>')
            self._stack.pop()

    def handle_data(self, data):
        self._out.append(html.escape(data))

    def get_data(self):
        while self._stack:
            self._out.append(f'</{self._stack.pop()}>')
        return ''.join(self._out)


def sanitize_rich_html(text: str, max_length: int = 50000) -> str:
    """净化富文本 HTML（Quill 编辑器产出），保留安全标签，不整体转义。

    修复历史缺陷：早期依赖 bleach，但 bleach 未列入依赖，导致 ImportError 退化为
    html.escape，把 <p>...</p> 整体转义成 &lt;p&gt;...&lt;/p&gt; 存入数据库，
    前端渲染时露出 &lt;p&gt; 字面量（公告内容乱码）。
    现改为标准库自实现，零外部依赖，确保富文本标签被保留。
    """
    if not text:
        return ""
    text = str(text)
    if len(text) > max_length:
        text = text[:max_length]
    parser = _RichHtmlSanitizer()
    parser.feed(text)
    return parser.get_data()


def decode_announcement_content(content: str) -> str:
    """兼容历史二次转义数据。

    仅当内容不含真实标签字符（< 或 >），却含有 &lt; / &gt; 实体时，判定为早期
    html.escape 退化的二次转义残留，解码一次还原为真实 HTML。
    若内容已是正确的富文本（含真实 < >），则原样返回，避免误伤。
    """
    if not content:
        return content
    if '<' in content or '>' in content:
        return content
    if '&lt;' in content or '&gt;' in content:
        return html.unescape(content)
    return content


def fix_unclosed_html_tags(text: str) -> str:
    """
    Fix unclosed HTML tags by properly parsing and reconstructing the HTML.
    Uses a stack-based approach to close any unclosed tags.
    """
    # Self-closing tags that don't need closing
    self_closing = {'br', 'hr', 'img', 'input', 'meta', 'link', 'area', 'base', 'col', 'embed', 'param', 'source', 'track', 'wbr'}
    
    # Stack to track open tags
    open_tags = []
    result = []
    pos = 0
    
    # Regex to find tags
    tag_pattern = re.compile(r'<(/?)(\w+)([^>]*)>', re.IGNORECASE)
    
    for match in tag_pattern.finditer(text):
        # Add text before this tag
        result.append(text[pos:match.start()])
        pos = match.end()
        
        is_closing = match.group(1) == '/'
        tag_name = match.group(2).lower()
        attrs = match.group(3)
        
        # Skip self-closing tags
        if tag_name in self_closing:
            result.append(match.group(0))
            continue
        
        # Check if it's a self-closing tag with />
        if attrs.strip().endswith('/'):
            result.append(match.group(0))
            continue
        
        if is_closing:
            # Closing tag
            if open_tags and open_tags[-1] == tag_name:
                open_tags.pop()
                result.append(match.group(0))
            elif tag_name in open_tags:
                # Close all tags up to this one
                while open_tags and open_tags[-1] != tag_name:
                    unclosed = open_tags.pop()
                    result.append(f'</{unclosed}>')
                if open_tags:
                    open_tags.pop()
                    result.append(match.group(0))
            else:
                # Orphan closing tag, just include it
                result.append(match.group(0))
        else:
            # Opening tag
            open_tags.append(tag_name)
            result.append(match.group(0))
    
    # Add remaining text
    result.append(text[pos:])
    
    # Close any remaining open tags
    while open_tags:
        tag = open_tags.pop()
        result.append(f'</{tag}>')
    
    return ''.join(result)


def safe_error_message(error: Exception, default_msg: str = "操作失败") -> str:
    """
    Generate safe error message that doesn't expose internal details

    Args:
        error: Exception object
        default_msg: Default message to return

    Returns:
        Safe error message for user display
    """
    # Log the actual error for debugging
    import logging
    logger = logging.getLogger("uvicorn")
    logger.error(f"[Security] Internal error: {str(error)}")

    # Return generic message to user
    return default_msg


def safe_http_exception(status_code: int, default_msg: str, error: Exception = None):
    """构造 HTTPException，记录原始异常但只返回通用文案。

    Args:
        status_code: HTTP 状态码
        default_msg: 面向客户端的通用消息
        error: 内部异常对象（可选），仅写入服务端日志

    Returns:
        fastapi.HTTPException 实例（调用方 raise 即可）
    """
    from fastapi import HTTPException
    import logging
    logger = logging.getLogger("uvicorn")
    if error is not None:
        logger.exception(f"[Security] HTTP {status_code}: {default_msg} - {error!r}")
    return HTTPException(status_code=status_code, detail=default_msg)
