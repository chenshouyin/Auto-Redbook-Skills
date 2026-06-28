#!/usr/bin/env python3
"""
小红书卡片渲染脚本 - 增强版
支持多种排版样式和智能分页策略

使用方法:
    python render_xhs.py <markdown_file> [options]

选项:
    --output-dir, -o     输出目录（默认为当前工作目录）
    --theme, -t          排版主题：default, playful-geometric, neo-brutalism, 
                         botanical, professional, retro, terminal, sketch
    --mode, -m           分页模式：
                         - separator  : 按 --- 分隔符手动分页（默认）
                         - auto-fit   : 自动缩放文字以填满固定尺寸
                         - auto-split : 根据内容高度自动切分
                         - dynamic    : 根据内容动态调整图片高度
    --width, -w          图片宽度（默认 1080）
    --height, -h         图片高度（默认 1440，dynamic 模式下为最小高度）
    --max-height         dynamic 模式下的最大高度（默认 4320
    --dpr                设备像素比（默认 2）

依赖安装:
    pip install markdown pyyaml playwright
    playwright install chromium
"""

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import html as html_lib
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    import markdown
    import yaml
    from playwright.async_api import async_playwright
except ImportError as e:
    print(f"缺少依赖: {e}")
    print("请运行: pip install markdown pyyaml playwright && playwright install chromium")
    sys.exit(1)


# 获取脚本所在目录
SCRIPT_DIR = Path(__file__).parent.parent
ASSETS_DIR = SCRIPT_DIR / "assets"
THEMES_DIR = ASSETS_DIR / "themes"

# 默认卡片尺寸配置 (3:4 比例)
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1440
MAX_HEIGHT = 4320  # dynamic 模式最大高度

# 可用主题列表
AVAILABLE_THEMES = [
    'default',
    'playful-geometric',
    'neo-brutalism',
    'botanical',
    'professional',
    'retro',
    'terminal',
    'sketch'
]

# 分页模式
PAGING_MODES = ['separator', 'auto-fit', 'auto-split', 'dynamic']


def parse_markdown_file(file_path: str) -> dict:
    """解析 Markdown 文件，提取 YAML 头部和正文内容"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 解析 YAML 头部
    yaml_pattern = r'^---\s*\n(.*?)\n---\s*\n'
    yaml_match = re.match(yaml_pattern, content, re.DOTALL)
    
    metadata = {}
    body = content
    
    if yaml_match:
        try:
            metadata = yaml.safe_load(yaml_match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
        body = content[yaml_match.end():]
    
    return {
        'metadata': metadata,
        'body': body.strip()
    }


def split_content_by_separator(body: str) -> List[str]:
    """按照 --- 分隔符拆分正文为多张卡片内容"""
    parts = re.split(r'\n---+\n', body)
    return [part.strip() for part in parts if part.strip()]


def escape_html(text: str) -> str:
    return html_lib.escape(text, quote=True)


def shorten_text(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def make_cover_hook(title: str, subtitle: str) -> str:
    raw = f"{title} {subtitle}".strip()
    if any(k in raw for k in ["教程", "制作", "怎么", "如何", "方法", "步骤"]):
        return "收藏这篇，照着做就行"
    if any(k in raw for k in ["名片", "简历", "头像", "封面", "海报"]):
        return "3分钟做出专业感"
    return "一看就会的实用教程"


def build_cover_meta(metadata: dict) -> dict:
    title = str(metadata.get("title", "标题")).strip()
    subtitle = str(metadata.get("subtitle", "")).strip()
    hook = make_cover_hook(title, subtitle)
    accent = "商务" if any(k in f"{title} {subtitle}" for k in ["名片", "商务", "职业", "公司"]) else "教程"
    top_tag = "电子名片制作教程" if any(k in f"{title} {subtitle}" for k in ["名片", "商务", "职业", "公司"]) else "实用教程"
    if subtitle and (subtitle == title or subtitle == top_tag or subtitle in top_tag or top_tag in subtitle):
        subtitle = ""
    return {
        "title": title,
        "subtitle": subtitle or hook,
        "hook": hook,
        "accent": accent,
        "top_tag": top_tag,
    }


def normalize_cover_text(text: str) -> str:
    return re.sub(r'\s+', '', text.strip())


def split_cover_title_lines(title: str) -> List[str]:
    """标题尽量保持单行，超长时才启用自适应换行。"""
    text = normalize_cover_text(title)
    if not text:
        return ["标题"]
    return [text]


def convert_markdown_to_html(md_content: str) -> str:
    """将 Markdown 转换为 HTML"""
    # 处理 tags（以 # 开头的标签）
    tags_pattern = r'((?:#[\w\u4e00-\u9fa5]+\s*)+)$'
    tags_match = re.search(tags_pattern, md_content, re.MULTILINE)
    tags_html = ""
    
    if tags_match:
        tags_str = tags_match.group(1)
        md_content = md_content[:tags_match.start()].strip()
        tags = re.findall(r'#([\w\u4e00-\u9fa5]+)', tags_str)
        if tags:
            tags_html = '<div class="tags-container">'
            for tag in tags:
                tags_html += f'<span class="tag">#{tag}</span>'
            tags_html += '</div>'
    
    # 转换 Markdown 为 HTML
    html = markdown.markdown(
        md_content,
        extensions=['extra', 'codehilite', 'tables', 'nl2br']
    )
    
    return html + tags_html


def load_theme_css(theme: str) -> str:
    """加载主题 CSS 样式"""
    theme_file = THEMES_DIR / f"{theme}.css"
    if theme_file.exists():
        with open(theme_file, 'r', encoding='utf-8') as f:
            return f.read()
    else:
        # 如果主题不存在，使用默认主题
        default_file = THEMES_DIR / "default.css"
        if default_file.exists():
            with open(default_file, 'r', encoding='utf-8') as f:
                return f.read()
        return ""


def generate_cover_html(metadata: dict, theme: str, width: int, height: int) -> str:
    """生成封面 HTML"""
    emoji = metadata.get('emoji', '📝')
    cover_meta = build_cover_meta(metadata)
    title = cover_meta["title"]
    subtitle = cover_meta["subtitle"]
    hook = cover_meta["hook"]
    accent = cover_meta["accent"]
    top_tag = cover_meta["top_tag"]
    title_lines = split_cover_title_lines(title)
    title_html = "".join(f'<div class="cover-title-line">{escape_html(line)}</div>' for line in title_lines)
    inner_width = int(width * 0.86)
    title_box_width = int(inner_width * 0.78)
    
    # 动态调整标题字体大小
    title_len = len(re.sub(r'\s+', '', title))
    if title_len <= 4:
        title_size = int(width * 0.13)
    elif title_len <= 6:
        title_size = int(width * 0.115)
    elif title_len <= 8:
        title_size = int(width * 0.10)
    elif title_len <= 12:
        title_size = int(width * 0.088)
    elif title_len <= 16:
        title_size = int(width * 0.078)
    else:
        title_size = int(width * 0.068)
    title_size = min(title_size, int(title_box_width / max(title_len, 1) * 0.95))
    title_size = max(title_size, int(width * 0.052))
    title_gap = int(title_size * 0.12)

    # 获取主题背景色
    theme_backgrounds = {
        'default': 'radial-gradient(circle at top left, rgba(37,99,235,0.16), transparent 40%), linear-gradient(180deg, #F5F7FA 0%, #ECF2FF 100%)',
        'playful-geometric': 'radial-gradient(circle at top left, rgba(255,255,255,0.25), transparent 30%), linear-gradient(180deg, #7C3AED 0%, #F472B6 100%)',
        'neo-brutalism': 'radial-gradient(circle at top left, rgba(255,255,255,0.2), transparent 30%), linear-gradient(180deg, #111827 0%, #FECA57 100%)',
        'botanical': 'radial-gradient(circle at top left, rgba(255,255,255,0.18), transparent 35%), linear-gradient(180deg, #1F7A57 0%, #9FD8B5 100%)',
        'professional': 'radial-gradient(circle at top left, rgba(255,255,255,0.14), transparent 35%), linear-gradient(180deg, #0F3D91 0%, #3B82F6 100%)',
        'retro': 'radial-gradient(circle at top left, rgba(255,255,255,0.18), transparent 35%), linear-gradient(180deg, #A84300 0%, #F5B041 100%)',
        'terminal': 'radial-gradient(circle at top left, rgba(57,211,83,0.16), transparent 40%), linear-gradient(180deg, #0D1117 0%, #161B22 100%)',
        'sketch': 'radial-gradient(circle at top left, rgba(255,255,255,0.18), transparent 35%), linear-gradient(180deg, #4B5563 0%, #A8B0BB 100%)'
    }
    bg = theme_backgrounds.get(theme, theme_backgrounds['default'])

    # 封面标题文字渐变随主题变化
    title_gradients = {
        'default': 'linear-gradient(180deg, #0F172A 0%, #2563EB 100%)',
        'playful-geometric': 'linear-gradient(180deg, #111111 0%, #111111 100%)',
        'neo-brutalism': 'linear-gradient(180deg, #111111 0%, #111111 100%)',
        'botanical': 'linear-gradient(180deg, #111111 0%, #111111 100%)',
        'professional': 'linear-gradient(180deg, #111111 0%, #111111 100%)',
        'retro': 'linear-gradient(180deg, #111111 0%, #111111 100%)',
        'terminal': 'linear-gradient(180deg, #111111 0%, #111111 100%)',
        'sketch': 'linear-gradient(180deg, #111111 0%, #111111 100%)',
    }
    title_bg = title_gradients.get(theme, title_gradients['default'])
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width={width}, height={height}">
    <title>小红书封面</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Heiti SC', sans-serif;
            width: {width}px;
            height: {height}px;
            overflow: hidden;
        }}
        
        .cover-container {{
            width: {width}px;
            height: {height}px;
            background:
                radial-gradient(circle at 18% 14%, rgba(56,189,248,0.16), transparent 18%),
                radial-gradient(circle at 86% 18%, rgba(59,130,246,0.12), transparent 16%),
                radial-gradient(circle at 78% 84%, rgba(34,197,94,0.08), transparent 20%),
                linear-gradient(180deg, #eff8ff 0%, #f8fbff 52%, #eef6ff 100%);
            position: relative;
            overflow: hidden;
        }}

        .cover-accent {{
            position: absolute;
            inset: 0;
            background:
                radial-gradient(circle at 14% 18%, rgba(255,255,255,0.30), transparent 14%),
                radial-gradient(circle at 82% 80%, rgba(255,255,255,0.18), transparent 16%);
            pointer-events: none;
        }}
        
        .cover-inner {{
            position: absolute;
            width: {inner_width}px;
            height: {int(height * 0.88)}px;
            left: {int(width * 0.07)}px;
            top: {int(height * 0.055)}px;
            background: #ffffff;
            border-radius: 36px;
            display: flex;
            flex-direction: column;
            padding: {int(width * 0.045)}px {int(width * 0.05)}px;
            box-shadow: 0 18px 50px rgba(15,23,42,0.10);
            border: 1px solid rgba(37,99,235,0.08);
        }}
        
        .cover-badge {{
            display: inline-flex;
            align-items: center;
            width: fit-content;
            padding: 10px 18px;
            border-radius: 999px;
            background: #d9efff;
            color: #1d4ed8;
            font-weight: 800;
            font-size: {int(width * 0.034)}px;
            margin-bottom: {int(height * 0.012)}px;
        }}

        .cover-top {{
            flex: 0 0 auto;
            min-height: {int(height * 0.16)}px;
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            margin-bottom: {int(height * 0.012)}px;
        }}

        .cover-stack {{
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            gap: 10px;
            width: 100%;
        }}

        .cover-middle {{
            flex: 1 1 auto;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 0;
        }}

        .cover-right {{
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .cover-title-stage {{
            width: {title_box_width}px;
            max-width: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 12px;
            margin: 0 auto;
        }}

        .cover-title-wrap {{
            display: flex;
            flex-direction: column;
            gap: {title_gap}px;
            margin-top: 0;
            width: 100%;
            align-items: center;
        }}

        .cover-title-line {{
            font-weight: 900;
            font-size: {title_size}px;
            line-height: 0.95;
            color: #111111;
            display: inline-block;
            white-space: nowrap;
            word-break: keep-all;
            letter-spacing: -0.08em;
            max-width: 100%;
            text-align: center;
        }}
        
        .cover-bottom {{
            flex: 0 0 auto;
            min-height: {int(height * 0.16)}px;
            display: flex;
            align-items: flex-end;
            justify-content: center;
            padding-top: 8px;
        }}

        .cover-subtitle {{
            font-weight: 600;
            font-size: {int(width * 0.024)}px;
            line-height: 1.35;
            color: #111111;
            max-width: 100%;
            text-align: center;
        }}

        .cover-underline {{
            width: 100%;
            max-width: 100%;
            height: 9px;
            border-radius: 999px;
            background: linear-gradient(90deg, #38bdf8 0%, #22c55e 100%);
            margin-top: 4px;
            box-shadow: 0 4px 0 #111111;
        }}

    </style>
</head>
<body>
    <div class="cover-container">
        <div class="cover-accent"></div>
        <div class="cover-inner">
            <div class="cover-top">
                <div class="cover-stack">
                    <div class="cover-badge">{top_tag}</div>
                </div>
            </div>
            <div class="cover-middle">
                <div class="cover-right">
                    <div class="cover-title-stage">
                    <div class="cover-title-wrap">{title_html}</div>
                    <div class="cover-underline"></div>
                    </div>
                </div>
            </div>
            <div class="cover-bottom">
                <div class="cover-subtitle">{subtitle}</div>
            </div>
        </div>
        <script>
        (function() {{
            const stage = document.querySelector('.cover-title-stage');
            const wrap = document.querySelector('.cover-title-wrap');
            const line = document.querySelector('.cover-title-line');
            const underline = document.querySelector('.cover-underline');
            if (!stage || !wrap || !line || !underline) return;

            const minFontSize = {max(int(width * 0.052), 52)};
            const maxFontSize = {title_size};
            const maxWidth = stage.clientWidth;
            const maxHeight = Math.max(140, Math.floor({int(height * 0.18)}));

            let size = maxFontSize;
            line.style.whiteSpace = 'nowrap';
            line.style.fontSize = size + 'px';
            line.style.lineHeight = '0.95';
            line.style.letterSpacing = '-0.08em';

            let steps = 0;
            while ((line.scrollWidth > maxWidth || wrap.scrollHeight > maxHeight) && size > minFontSize && steps < 40) {{
                size -= 2;
                steps += 1;
                line.style.fontSize = size + 'px';
            }}

            if (line.scrollWidth > maxWidth || wrap.scrollHeight > maxHeight) {{
                line.style.whiteSpace = 'normal';
                line.style.wordBreak = 'break-word';
                line.style.lineHeight = '0.98';
                line.style.letterSpacing = '-0.06em';
                size = Math.max(minFontSize, size - 2);
                line.style.fontSize = size + 'px';
            }}

            underline.style.width = Math.min(line.getBoundingClientRect().width, maxWidth) + 'px';
        }})();
        </script>
    </div>
</body>
</html>'''
    return html


def generate_card_html(content: str, theme: str, page_number: int = 1, 
                       total_pages: int = 1, width: int = DEFAULT_WIDTH, 
                       height: int = DEFAULT_HEIGHT, mode: str = 'separator') -> str:
    """生成正文卡片 HTML"""
    
    html_content = convert_markdown_to_html(content)
    theme_css = load_theme_css(theme)
    
    page_text = f"{page_number}/{total_pages}" if total_pages > 1 else ""
    
    # 获取主题背景色
    theme_backgrounds = {
        'default': 'linear-gradient(180deg, #f3f3f3 0%, #f9f9f9 100%)',
        'playful-geometric': 'linear-gradient(135deg, #8B5CF6 0%, #F472B6 100%)',
        'neo-brutalism': 'linear-gradient(135deg, #FF4757 0%, #FECA57 100%)',
        'botanical': 'linear-gradient(135deg, #4A7C59 0%, #8FBC8F 100%)',
        'professional': 'linear-gradient(135deg, #2563EB 0%, #3B82F6 100%)',
        'retro': 'linear-gradient(135deg, #D35400 0%, #F39C12 100%)',
        'terminal': 'linear-gradient(135deg, #0D1117 0%, #161B22 100%)',
        'sketch': 'linear-gradient(135deg, #555555 0%, #888888 100%)'
    }
    bg = theme_backgrounds.get(theme, theme_backgrounds['default'])
    
    # 根据模式设置不同的容器样式
    if mode == 'auto-fit':
        container_style = f'''
            width: {width}px;
            height: {height}px;
            background: {bg};
            position: relative;
            padding: 50px;
            overflow: hidden;
        '''
        inner_style = f'''
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 60px;
            height: calc({height}px - 100px);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            overflow: hidden;
            display: flex;
            flex-direction: column;
        '''
        content_style = '''
            flex: 1;
            overflow: hidden;
        '''
    elif mode == 'dynamic':
        container_style = f'''
            width: {width}px;
            min-height: {height}px;
            background: {bg};
            position: relative;
            padding: 50px;
        '''
        inner_style = '''
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 60px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
        '''
        content_style = ''
    else:  # separator 和 auto-split
        container_style = f'''
            width: {width}px;
            min-height: {height}px;
            background: {bg};
            position: relative;
            padding: 50px;
            overflow: hidden;
        '''
        inner_style = f'''
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 60px;
            min-height: calc({height}px - 100px);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
        '''
        content_style = ''

    page_header = ""
    body = content.strip()
    heading_match = re.match(r'^(#{1,2})\s+(.+)$', body, re.MULTILINE)
    if heading_match:
        heading_text = heading_match.group(2).strip()
        page_header = shorten_text(heading_text, 20)
    else:
        page_header = f"第 {page_number} 页"

    # 给正文增加更明确的视觉引导
    def enhance_body(raw: str) -> str:
        lines = raw.splitlines()
        out = []
        for line in lines:
            m = re.match(r'^(#{1,3})\s+(.+)$', line)
            if m:
                level = len(m.group(1))
                text = m.group(2).strip()
                out.append(f'<div class="step-head step-head-{level}">{escape_html(text)}</div>')
                continue
            bullet = re.match(r'^[-*]\s+(.+)$', line)
            if bullet:
                out.append(f'<div class="bullet-line">• {escape_html(bullet.group(1).strip())}</div>')
                continue
            numbered = re.match(r'^\d+\.\s+(.+)$', line)
            if numbered:
                out.append(f'<div class="number-line">{escape_html(line.strip())}</div>')
                continue
            out.append(line)
        return "\n".join(out)

    content = enhance_body(content)
    html_content = convert_markdown_to_html(content)
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width={width}">
    <title>小红书卡片</title>
    <style>

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Heiti SC', sans-serif;
            width: {width}px;
            overflow: hidden;
            background: transparent;
        }}
        
        .card-container {{
            {container_style}
        }}

        .card-shell {{
            position: absolute;
            top: 28px;
            left: 28px;
            right: 28px;
            height: 88px;
            display: flex;
            align-items: center;
            padding: 0 32px;
            border-radius: 22px;
            background: rgba(255,255,255,0.22);
            backdrop-filter: blur(14px);
            border: 1px solid rgba(255,255,255,0.3);
            color: white;
            z-index: 2;
        }}

        .card-shell-title {{
            font-size: 34px;
            font-weight: 900;
            letter-spacing: 0.02em;
        }}
        
        .card-inner {{
            {inner_style}
            position: relative;
            top: 72px;
            height: calc(100% - 72px);
        }}
        
        .card-content {{
            line-height: 1.7;
            {content_style}
            margin-top: 10px;
        }}

        /* auto-fit 用：对整个内容块做 transform 缩放 */
        .card-content-scale {{
            transform-origin: top left;
            will-change: transform;
        }}
        
        {theme_css}

        .card-content :not(pre) > code {{
            overflow-wrap: anywhere;
            word-break: break-word;
        }}

        .page-number {{
            position: absolute;
            bottom: 80px;
            right: 80px;
            font-size: 36px;
            color: rgba(255, 255, 255, 0.8);
            font-weight: 500;
        }}

        .step-head {{
            display: inline-block;
            margin: 38px 0 22px 0;
            padding: 14px 22px;
            border-radius: 16px;
            background: linear-gradient(135deg, rgba(139,92,246,0.16), rgba(244,114,182,0.08));
            color: #111827;
            border-left: 10px solid #8B5CF6;
            font-weight: 800;
            box-shadow: 5px 5px 0 rgba(30,41,59,0.12);
        }}

        .step-head-1 {{
            font-size: 58px;
        }}

        .step-head-2 {{
            font-size: 48px;
        }}

        .step-head-3 {{
            font-size: 42px;
        }}

        .bullet-line {{
            margin: 12px 0 12px 8px;
            padding-left: 18px;
            border-left: 4px solid rgba(139,92,246,0.22);
        }}

        .number-line {{
            margin: 12px 0;
            padding: 12px 18px;
            background: rgba(251,191,36,0.15);
            border-radius: 14px;
            color: #111827;
            font-weight: 700;
        }}
    </style>
</head>
<body>
    <div class="card-container">
        <div class="card-shell">
            <div class="card-shell-title">{escape_html(page_header)}</div>
        </div>
        <div class="card-inner">
            <div class="card-content">
                <div class="card-content-scale">{html_content}</div>
            </div>
        </div>
        <div class="page-number">{page_text}</div>
    </div>
</body>
</html>'''
    return html


async def render_html_to_image(html_content: str, output_path: str, 
                               width: int = DEFAULT_WIDTH, 
                               height: int = DEFAULT_HEIGHT,
                               mode: str = 'separator',
                               max_height: int = MAX_HEIGHT,
                               dpr: int = 2):
    """使用 Playwright 将 HTML 渲染为图片"""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        
        # 设置视口大小
        viewport_height = height if mode != 'dynamic' else max_height
        page = await browser.new_page(
            viewport={'width': width, 'height': viewport_height},
            device_scale_factor=dpr
        )
        
        # 创建临时 HTML 文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html_content)
            temp_html_path = f.name
        
        try:
            await page.goto(f'file://{temp_html_path}')
            await page.wait_for_load_state('networkidle')
            
            # 等待字体加载
            await page.wait_for_timeout(500)
            
            if mode == 'auto-fit':
                # 自动缩放模式：对整个内容块做 transform 缩放（标题/代码块等固定 px 也会一起缩放）
                await page.evaluate('''() => {
                    const viewportContent = document.querySelector('.card-content');
                    const scaleEl = document.querySelector('.card-content-scale');
                    if (!viewportContent || !scaleEl) return;

                    // 先重置，测量原始尺寸
                    scaleEl.style.transform = 'none';
                    scaleEl.style.width = '';
                    scaleEl.style.height = '';

                    const availableWidth = viewportContent.clientWidth;
                    const availableHeight = viewportContent.clientHeight;

                    // scrollWidth/scrollHeight 反映内容的自然尺寸
                    const contentWidth = Math.max(scaleEl.scrollWidth, scaleEl.getBoundingClientRect().width);
                    const contentHeight = Math.max(scaleEl.scrollHeight, scaleEl.getBoundingClientRect().height);

                    if (!contentWidth || !contentHeight || !availableWidth || !availableHeight) return;

                    // 只缩小不放大，避免“撑太大”
                    const scale = Math.min(1, availableWidth / contentWidth, availableHeight / contentHeight);

                    // 为避免 transform 后布局尺寸不匹配导致裁切，扩大布局盒子
                    scaleEl.style.width = (availableWidth / scale) + 'px';

                    // 顶部对齐更稳；如需居中可计算 offset
                    const offsetX = 0;
                    const offsetY = 0;

                    scaleEl.style.transformOrigin = 'top left';
                    scaleEl.style.transform = `translate(${offsetX}px, ${offsetY}px) scale(${scale})`;
                }''')
                await page.wait_for_timeout(100)
                actual_height = height
                
            elif mode == 'dynamic':
                # 动态高度模式：根据内容调整图片高度
                content_height = await page.evaluate('''() => {
                    const container = document.querySelector('.card-container');
                    return container ? container.scrollHeight : document.body.scrollHeight;
                }''')
                # 确保高度在合理范围内
                actual_height = max(height, min(content_height, max_height))
                
            else:  # separator 和 auto-split
                # 获取实际内容高度
                content_height = await page.evaluate('''() => {
                    const container = document.querySelector('.card-container');
                    return container ? container.scrollHeight : document.body.scrollHeight;
                }''')
                actual_height = max(height, content_height)
            
            # 截图
            await page.screenshot(
                path=output_path,
                clip={'x': 0, 'y': 0, 'width': width, 'height': actual_height},
                type='png'
            )
            
            print(f"  ✅ 已生成: {output_path} ({width}x{actual_height})")
            return actual_height
            
        finally:
            os.unlink(temp_html_path)
            await browser.close()


async def auto_split_content(body: str, theme: str, width: int, height: int, 
                             dpr: int = 2) -> List[str]:
    """自动切分内容：根据渲染后的高度自动分页"""
    def semantic_blocks(text: str) -> List[str]:
        """优先按标题语义切块，尽量保留标题与其后正文在同一页。"""
        lines = text.splitlines()
        blocks: List[str] = []
        current: List[str] = []

        def flush() -> None:
            nonlocal current
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []

        for line in lines:
            stripped = line.strip()
            if re.match(r'^#{1,3}\s+', stripped):
                if current:
                    flush()
                current.append(stripped)
                continue

            if not stripped and current and current[-1] == "":
                continue

            current.append(line)

        flush()
        return blocks

    paragraphs = semantic_blocks(body)
    
    cards = []
    current_content = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={'width': width, 'height': height * 2},
            device_scale_factor=dpr
        )
        
        try:
            for para in paragraphs:
                # 尝试将当前块加入
                test_content = current_content + [para]
                test_md = '\n\n'.join(test_content)
                
                html = generate_card_html(test_md, theme, 1, 1, width, height, 'auto-split')
                
                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                    f.write(html)
                    temp_path = f.name
                
                await page.goto(f'file://{temp_path}')
                await page.wait_for_load_state('networkidle')
                await page.wait_for_timeout(200)
                
                content_height = await page.evaluate('''() => {
                    const content = document.querySelector('.card-content');
                    return content ? content.scrollHeight : 0;
                }''')
                
                os.unlink(temp_path)
                
                # 内容区域的可用高度（去除 padding 等）
                available_height = height - 220  # 50*2 padding + 60*2 inner padding

                if content_height > available_height and current_content:
                    # 当前卡片已满，保存并开始新卡片
                    cards.append('\n\n'.join(current_content))
                    current_content = [para]
                else:
                    current_content = test_content
            
            # 保存最后一张卡片
            if current_content:
                cards.append('\n\n'.join(current_content))
                
        finally:
            await browser.close()
    
    return cards


async def render_markdown_to_cards(md_file: str, output_dir: str, 
                                   theme: str = 'default',
                                   mode: str = 'separator',
                                   width: int = DEFAULT_WIDTH,
                                   height: int = DEFAULT_HEIGHT,
                                   max_height: int = MAX_HEIGHT,
                                   dpr: int = 2):
    """主渲染函数：将 Markdown 文件渲染为多张卡片图片"""
    print(f"\n🎨 开始渲染: {md_file}")
    print(f"  📐 主题: {theme}")
    print(f"  📏 模式: {mode}")
    print(f"  📐 尺寸: {width}x{height}")
    
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # 解析 Markdown 文件
    data = parse_markdown_file(md_file)
    metadata = data['metadata']
    body = data['body']
    
    # 根据模式处理内容分割
    if mode == 'auto-split':
        print("  ⏳ 自动分析内容并切分...")
        card_contents = await auto_split_content(body, theme, width, height, dpr)
    else:
        card_contents = split_content_by_separator(body)
    
    total_cards = len(card_contents)
    print(f"  📄 检测到 {total_cards} 张正文卡片")
    
    # 生成封面
    if metadata.get('emoji') or metadata.get('title'):
        print("  📷 生成封面...")
        cover_html = generate_cover_html(metadata, theme, width, height)
        cover_path = os.path.join(output_dir, 'cover.png')
        await render_html_to_image(cover_html, cover_path, width, height, 'separator', max_height, dpr)
    
    # 生成正文卡片
    for i, content in enumerate(card_contents, 1):
        print(f"  📷 生成卡片 {i}/{total_cards}...")
        card_html = generate_card_html(content, theme, i, total_cards, width, height, mode)
        card_path = os.path.join(output_dir, f'card_{i}.png')
        await render_html_to_image(card_html, card_path, width, height, mode, max_height, dpr)
    
    print(f"\n✨ 渲染完成！图片已保存到: {output_dir}")
    return total_cards


def main():
    parser = argparse.ArgumentParser(
        description='将 Markdown 文件渲染为小红书风格的图片卡片（支持多种样式和分页模式）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
可用主题:
  default           - 默认紫色渐变风格
  playful-geometric - 活泼几何风格（Memphis 设计）
  neo-brutalism     - 新粗野主义风格
  botanical         - 植物园自然风格
  professional      - 专业商务风格
  retro             - 复古怀旧风格
  terminal          - 终端/命令行风格
  sketch            - 手绘素描风格

分页模式:
  separator   - 按 --- 分隔符手动分页（默认）
  auto-fit    - 自动缩放文字以填满固定尺寸
  auto-split  - 根据内容高度自动切分
  dynamic     - 根据内容动态调整图片高度
'''
    )
    parser.add_argument(
        'markdown_file',
        help='Markdown 文件路径'
    )
    parser.add_argument(
        '--output-dir', '-o',
        default=os.getcwd(),
        help='输出目录（默认为当前工作目录）'
    )
    parser.add_argument(
        '--theme', '-t',
        choices=AVAILABLE_THEMES,
        default='sketch',
        help='排版主题（默认: sketch）'
    )
    parser.add_argument(
        '--mode', '-m',
        choices=PAGING_MODES,
        default='separator',
        help='分页模式（默认: separator）'
    )
    parser.add_argument(
        '--width', '-w',
        type=int,
        default=DEFAULT_WIDTH,
        help=f'图片宽度（默认: {DEFAULT_WIDTH}）'
    )
    parser.add_argument(
        '--height',
        type=int,
        default=DEFAULT_HEIGHT,
        help=f'图片高度（默认: {DEFAULT_HEIGHT}）'
    )
    parser.add_argument(
        '--max-height',
        type=int,
        default=MAX_HEIGHT,
        help=f'dynamic 模式下的最大高度（默认: {MAX_HEIGHT}）'
    )
    parser.add_argument(
        '--dpr',
        type=int,
        default=2,
        help='设备像素比（默认: 2）'
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.markdown_file):
        print(f"❌ 错误: 文件不存在 - {args.markdown_file}")
        sys.exit(1)
    
    asyncio.run(render_markdown_to_cards(
        args.markdown_file,
        args.output_dir,
        theme=args.theme,
        mode=args.mode,
        width=args.width,
        height=args.height,
        max_height=args.max_height,
        dpr=args.dpr
    ))


if __name__ == '__main__':
    main()
