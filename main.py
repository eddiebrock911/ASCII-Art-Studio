"""
ASCII Art Studio v2 - advanced image -> ASCII converter (Streamlit)

Run:    streamlit run main.py
Needs:  pip install -r requirements.txt   (streamlit, pillow, numpy, pandas)

What's new in v2
  * One-click "quick looks" (presets) + save / load your settings as JSON
  * Dithering: Bayer 4x4 / 8x8, Floyd-Steinberg, Atkinson
  * Direction-aware edge detection (Sobel -> | - / \\ glyphs), edges-only line art
  * Glyph-density auto-sorted ramps, gamma / equalise / blur / rotate / flip
  * 9 colour styles incl. multi-stop gradient maps, rainbow, k-colour palette, blueprint
  * Interactive preview (zoom, fit, copy, glow / CRT toggles) and a reveal slider compare
  * Insights tab (histogram, glyph usage, dominant palette)
  * Exports: TXT, HTML, SVG, ANSI (.ans), PNG (optionally transparent), batch ZIP
Design tokens and component specs live in DESIGN_SYSTEM.md.
"""
from __future__ import annotations

import base64
import functools
import glob
import hashlib
import html
import io
import ipaddress
import json
import re
import socket
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

st.set_page_config(
    page_title="ASCII Art Studio",
    page_icon="🎨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================== CONSTANTS
# Ramps are ordered DENSE -> SPARSE. They are flipped automatically on dark
# backgrounds so that bright pixels always get the "heavier" glyphs.

CHARSETS = {
    "Classic": "@%#*+=-:. ",
    "Detailed": "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. ",
    "Blocks": "█▓▒░ ",
    "Binary": "10 ",
    "Letters": "abcdeg@%# ",
    "Minimal": "#. ",
    "Dots": "●◉○◌·  ",
    "Custom": "",
}

STYLES = [
    "Original Color", "Grayscale", "Matrix", "Amber CRT", "Gradient Map",
    "Rainbow", "Palette Quantize", "Blueprint", "Ink on Paper",
]
STYLE_DEFAULT_BG = {
    "Original Color": "#0b0f1a", "Grayscale": "#0b0f1a", "Matrix": "#000000",
    "Amber CRT": "#140b00", "Gradient Map": "#0b0f1a", "Rainbow": "#0b0f1a",
    "Palette Quantize": "#0b0f1a", "Blueprint": "#0d3b86", "Ink on Paper": "#fafaf7",
}

GRADIENTS = {
    "Cyberpunk": ["#1e1b4b", "#7c3aed", "#22d3ee"],
    "Sunset": ["#4c0519", "#be185d", "#fb923c", "#fde047"],
    "Ocean": ["#0b1f4d", "#1d4ed8", "#38bdf8", "#a7f3d0"],
    "Inferno": ["#000004", "#7f1d1d", "#f97316", "#fde68a"],
    "Vaporwave": ["#2b0a4a", "#ff4fd8", "#7df9ff"],
    "Forest": ["#052e16", "#15803d", "#bef264"],
    "Ice": ["#0f172a", "#38bdf8", "#f0f9ff"],
    "Custom": [],
}

DITHERS = ["None", "Ordered (Bayer 4×4)", "Ordered (Bayer 8×8)", "Floyd–Steinberg", "Atkinson"]
EDGE_MODES = ["Off", "Edges + shading", "Edges only"]
EDGE_GLYPHS = np.array(["-", "\\", "|", "/"])

# Every user-facing setting: default + valid range / choices. Used for widgets,
# presets and for validating imported settings files.
SPEC: dict[str, dict] = {
    # shape
    "cols": dict(default=120, lo=30, hi=300, step=5),
    "aspect": dict(default=0.50, lo=0.35, hi=0.80, step=0.01),
    "rotate": dict(default=0, choices=[0, 90, 180, 270]),
    "flip_h": dict(default=False),
    "flip_v": dict(default=False),
    # tone
    "autoc": dict(default=True),
    "equalize": dict(default=False),
    "invert": dict(default=False),
    "brightness": dict(default=1.0, lo=0.3, hi=2.0, step=0.05),
    "contrast": dict(default=1.2, lo=0.3, hi=2.5, step=0.05),
    "gamma": dict(default=1.0, lo=0.4, hi=2.5, step=0.05),
    "saturation": dict(default=1.2, lo=0.0, hi=2.5, step=0.05),
    "sharpness": dict(default=1.5, lo=0.0, hi=3.0, step=0.1),
    "blur": dict(default=0.0, lo=0.0, hi=3.0, step=0.1),
    # glyphs
    "charset": dict(default="Classic", choices=list(CHARSETS)),
    "custom_ramp": dict(default="@#S%?*+;:,. ", kind="text"),
    "sort_ramp": dict(default=False),
    "dither": dict(default="None", choices=DITHERS),
    "dither_strength": dict(default=1.0, lo=0.0, hi=1.0, step=0.05),
    "edge_mode": dict(default="Off", choices=EDGE_MODES),
    "edge_sens": dict(default=0.55, lo=0.05, hi=0.95, step=0.01),
    "edge_tint": dict(default=False),
    "edge_color": dict(default="#22d3ee", kind="color"),
    # colour
    "style": dict(default="Original Color", choices=STYLES),
    "bg": dict(default="#0b0f1a", kind="color"),
    "grad": dict(default="Cyberpunk", choices=list(GRADIENTS)),
    "grad_c1": dict(default="#6366f1", kind="color"),
    "grad_c2": dict(default="#f472b6", kind="color"),
    "palette_k": dict(default=6, lo=2, hi=16, step=1),
    # display / fx
    "autofit": dict(default=True),
    "font_px": dict(default=7, lo=3, hi=14, step=1),
    "preview_h": dict(default=600, lo=300, hi=1000, step=50),
    "glow": dict(default=False),
    "scan": dict(default=False),
}
# Presets leave the geometry / display choices alone and reset everything else.
KEEP_KEYS = {"cols", "aspect", "rotate", "flip_h", "flip_v", "font_px", "preview_h", "autofit"}
LOOK_KEYS = [k for k in SPEC if k not in KEEP_KEYS]

PRESETS = {
    "Classic": dict(label="📷 Classic", s={}),
    "Neon": dict(label="🌃 Neon", s=dict(
        charset="Detailed", style="Gradient Map", grad="Cyberpunk", bg="#07050f", glow=True,
        contrast=1.35, edge_mode="Edges + shading", edge_sens=0.45, edge_tint=True, edge_color="#22d3ee")),
    "Terminal": dict(label="📟 Terminal", s=dict(
        charset="Classic", style="Matrix", bg="#000000", scan=True, glow=True,
        dither="Ordered (Bayer 4×4)", dither_strength=0.6)),
    "Amber": dict(label="🟠 Amber", s=dict(
        charset="Letters", style="Amber CRT", bg="#140b00", scan=True, glow=True)),
    "Newsprint": dict(label="📰 Paper", s=dict(
        charset="Classic", style="Ink on Paper", bg="#fafaf7", dither="Floyd–Steinberg", contrast=1.3)),
    "Blueprint": dict(label="📐 Blueprint", s=dict(
        charset="Minimal", style="Blueprint", bg="#0d3b86", edge_mode="Edges + shading",
        edge_sens=0.6, contrast=1.3)),
    "Vapor": dict(label="🌴 Vapor", s=dict(
        charset="Blocks", style="Gradient Map", grad="Vaporwave", bg="#12002b", glow=True,
        dither="Ordered (Bayer 8×8)")),
    "Pixel": dict(label="🧱 Pixel", s=dict(
        charset="Blocks", style="Original Color", dither="Ordered (Bayer 4×4)", saturation=1.5)),
    "Line art": dict(label="✏️ Line art", s=dict(
        charset="Classic", style="Ink on Paper", bg="#fafaf7", edge_mode="Edges only",
        edge_sens=0.6, sharpness=2.0)),
}
for _p in PRESETS.values():  # fail fast on typos
    assert all(k in SPEC for k in _p["s"]), _p

LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)

# ============================================================== STYLING
# Tokens (see DESIGN_SYSTEM.md): every colour / radius / space / motion value
# used by components is a CSS variable declared once in :root.

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800&family=JetBrains+Mono:wght@500&display=swap');

:root{
  --bg-0:#070a14; --bg-1:#0b0f1a;
  --surface-1:rgba(255,255,255,.04); --surface-2:rgba(255,255,255,.07);
  --border-1:rgba(255,255,255,.09); --border-2:rgba(255,255,255,.20);
  --text-1:#e6eaf5; --text-2:#a3b0c5; --text-3:#7b8aa3;
  --accent-1:#38bdf8; --accent-2:#a78bfa; --accent-3:#f472b6; --ok:#34d399;
  --grad:linear-gradient(90deg,var(--accent-1),var(--accent-2) 55%,var(--accent-3));
  --grad-btn:linear-gradient(90deg,#2563eb,#7c3aed);
  --r-sm:8px; --r-md:12px; --r-lg:16px; --r-xl:22px;
  --s-1:4px; --s-2:8px; --s-3:12px; --s-4:16px; --s-5:24px; --s-6:32px;
  --shadow-1:0 1px 0 rgba(255,255,255,.05) inset,0 10px 30px rgba(0,0,0,.30);
  --shadow-glow:0 10px 28px rgba(124,58,237,.38);
  --ease:cubic-bezier(.2,.8,.2,1); --dur:160ms;
  --font-ui:'Inter',system-ui,sans-serif; --font-mono:'JetBrains Mono',ui-monospace,monospace;
}

.stApp{
  background:
    radial-gradient(1100px 560px at 6% -12%,#27226b 0%,transparent 60%),
    radial-gradient(900px 480px at 100% -4%,#0c4a6e 0%,transparent 55%),
    radial-gradient(800px 500px at 50% 120%,#3b0a45 0%,transparent 60%),
    var(--bg-1);
  color:var(--text-1); font-family:var(--font-ui);
}
[data-testid="stHeader"]{background:transparent;}
#MainMenu, footer, [data-testid="stAppDeployButton"]{visibility:hidden;}
.block-container{padding-top:1.4rem; max-width:1500px;}
[data-testid="stSidebar"]{
  background:linear-gradient(180deg,rgba(17,23,41,.96),rgba(10,14,26,.96));
  border-right:1px solid var(--border-1);
}
[data-testid="stSidebar"] h2{font-size:1.05rem; letter-spacing:.02em;}

/* ---------- hero ---------- */
.hero{text-align:center; padding:var(--s-4) 0 var(--s-2); font-family:var(--font-ui);}
.badge{
  display:inline-block; padding:4px 14px; border-radius:999px; font-size:12px; font-weight:600;
  letter-spacing:.16em; color:#7dd3fc; background:rgba(56,189,248,.10); border:1px solid rgba(56,189,248,.30);
}
.hero-title{
  font-weight:800; font-size:clamp(2.2rem,5vw,3.7rem); line-height:1.08; margin:14px 0 6px;
  background:linear-gradient(90deg,#38bdf8,#a78bfa,#f472b6,#38bdf8); background-size:300% 100%;
  -webkit-background-clip:text; background-clip:text; color:transparent;
  animation:flow 9s linear infinite;
}
@keyframes flow{to{background-position:300% 0;}}
.ribbon{font-family:var(--font-mono); color:var(--text-3); letter-spacing:.3em; font-size:13px;}
.hero p{color:var(--text-2); font-size:1.05rem; max-width:700px; margin:10px auto 0;}

.section-label{
  font-size:11px; font-weight:600; letter-spacing:.14em; text-transform:uppercase; color:var(--text-3);
  margin:var(--s-4) 0 var(--s-2);
}

/* ---------- cards / feature grid ---------- */
.feat-grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; margin-top:22px;}
.feat{
  background:var(--surface-1); border:1px solid var(--border-1); border-radius:var(--r-lg);
  padding:18px; transition:transform var(--dur) var(--ease),border-color var(--dur) var(--ease),background var(--dur);
}
.feat:hover{transform:translateY(-3px); border-color:rgba(56,189,248,.55); background:var(--surface-2);}
.feat .ico{font-size:26px;}
.feat h4{margin:8px 0 4px; color:var(--text-1); font-size:1rem;}
.feat p{margin:0; color:var(--text-2); font-size:.88rem;}
[class*="st-key-card_"]{
  background:var(--surface-1); border:1px solid var(--border-1)!important; border-radius:var(--r-lg)!important;
  box-shadow:var(--shadow-1);
}

/* ---------- metrics ---------- */
[data-testid="stMetric"]{
  background:var(--surface-1); border:1px solid var(--border-1); border-radius:var(--r-lg);
  padding:14px 18px; backdrop-filter:blur(8px); box-shadow:var(--shadow-1);
}
[data-testid="stMetricValue"]{color:var(--text-1);}
[data-testid="stMetricLabel"] p{color:var(--text-2);}

/* ---------- buttons: primary = gradient, secondary = glass ---------- */
button[data-testid="stBaseButton-primary"], .stDownloadButton button[kind="primary"]{
  background:var(--grad-btn); color:#fff; border:0; border-radius:var(--r-md); font-weight:600;
  transition:transform var(--dur) var(--ease),box-shadow var(--dur) var(--ease);
}
button[data-testid="stBaseButton-primary"]:hover{transform:translateY(-1px); box-shadow:var(--shadow-glow); color:#fff;}
button[data-testid="stBaseButton-secondary"]{
  background:var(--surface-1); color:var(--text-1); border:1px solid var(--border-1); border-radius:var(--r-md);
  font-weight:500; transition:all var(--dur) var(--ease); white-space:nowrap;
}
button[data-testid="stBaseButton-secondary"]:hover{
  border-color:rgba(56,189,248,.6); background:rgba(56,189,248,.10); color:#fff; transform:translateY(-1px);
}
button:focus-visible, [data-baseweb="tab"]:focus-visible{outline:2px solid var(--accent-1)!important; outline-offset:2px;}

/* ---------- tabs ---------- */
.stTabs [data-baseweb="tab-list"]{gap:var(--s-1); border-bottom:1px solid var(--border-1);}
.stTabs [data-baseweb="tab"]{border-radius:var(--r-sm) var(--r-sm) 0 0; padding:8px 14px; color:var(--text-2);}
.stTabs [aria-selected="true"]{color:var(--text-1)!important;}
.stTabs [data-baseweb="tab-highlight"]{background:var(--grad)!important; height:3px; border-radius:3px;}
.stTabs [data-baseweb="tab-border"]{background:transparent;}

/* ---------- inputs ---------- */
[data-testid="stFileUploaderDropzone"]{
  border:2px dashed rgba(56,189,248,.45); background:rgba(56,189,248,.04); border-radius:var(--r-lg);
  transition:background var(--dur),border-color var(--dur);
}
[data-testid="stFileUploaderDropzone"]:hover{background:rgba(56,189,248,.09); border-color:var(--accent-1);}
iframe{border-radius:var(--r-md); border:1px solid var(--border-1); box-shadow:var(--shadow-1);}

.grad-bar{height:12px; border-radius:999px; border:1px solid var(--border-1); margin:2px 0 var(--s-3);}
.swatches{display:grid; grid-template-columns:repeat(3,1fr); gap:var(--s-2);}
.sw{
  border-radius:var(--r-md); padding:30px 8px 8px; border:1px solid var(--border-2);
  font:500 11px var(--font-mono); text-align:center;
}
.footer{text-align:center; color:var(--text-3); font-size:.85rem; padding:22px 0 6px;}

@media (prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important;}}
</style>
"""

HERO_HTML = """
<div class="hero">
<div class="badge">STUDIO · V2</div>
<div class="hero-title">ASCII Art Studio</div>
<div class="ribbon" aria-hidden="true">░▒▓█ PIXELS → GLYPHS █▓▒░</div>
<p>Turn any photo into colour, neon or retro-terminal ASCII art. Dither it, trace its edges,
compare it live, then export as TXT, HTML, SVG, ANSI or PNG.</p>
</div>
"""

FEATURES_HTML = """
<div class="feat-grid">
<div class="feat"><div class="ico">⚡</div><h4>One-click quick looks</h4><p>Neon, Terminal, Newsprint, Blueprint, Vapor, Line art and more. Save or load your own settings.</p></div>
<div class="feat"><div class="ico">🎚️</div><h4>Real dithering</h4><p>Bayer 4×4 / 8×8, Floyd–Steinberg and Atkinson for rich tone from tiny ramps.</p></div>
<div class="feat"><div class="ico">📐</div><h4>Direction-aware edges</h4><p>Sobel edge tracing draws | - / \\ glyphs along contours. Go full line-art with edges-only.</p></div>
<div class="feat"><div class="ico">🌈</div><h4>9 colour styles</h4><p>Original, grayscale, Matrix, Amber, multi-stop gradients, rainbow, k-colour palette, blueprint, ink.</p></div>
<div class="feat"><div class="ico">🔤</div><h4>Smart ramps</h4><p>8 character sets plus a custom ramp; auto-sort any ramp by real glyph ink density.</p></div>
<div class="feat"><div class="ico">🔍</div><h4>Interactive preview</h4><p>Zoom, fit, copy, glow and CRT toggles, plus a reveal slider to compare with the photo.</p></div>
<div class="feat"><div class="ico">📊</div><h4>Insights</h4><p>Brightness histogram, glyph usage and the dominant colour palette of your image.</p></div>
<div class="feat"><div class="ico">📦</div><h4>TXT · HTML · SVG · ANSI · PNG</h4><p>Transparent PNGs, vector SVG, terminal-ready ANSI and batch ZIP export for many images.</p></div>
</div>
"""

st.markdown(CSS, unsafe_allow_html=True)

# ============================================================== SMALL HELPERS


def stretch(fn, *args, **kwargs):
    """Full-width widget that works on old and new Streamlit versions."""
    try:
        return fn(*args, width="stretch", **kwargs)
    except Exception:  # older Streamlit: no `width="stretch"`
        return fn(*args, use_container_width=True, **kwargs)


def show_html(doc: str, height: int):
    """Render an HTML document. `st.iframe` replaces the deprecated
    `components.html` in recent Streamlit versions. `doc` only embeds
    html-escaped content, so this is safe."""
    if hasattr(st, "iframe"):
        return st.iframe(doc, height=int(height))
    return components.html(doc, height=int(height), scrolling=True)


def dl(label, data, file_name, mime, key):
    """Full-width primary download button that doesn't trigger a rerun."""
    try:
        return stretch(st.download_button, label, data=data, file_name=file_name,
                       mime=mime, key=key, type="primary", on_click="ignore")
    except Exception:  # older Streamlit
        return stretch(st.download_button, label, data=data, file_name=file_name,
                       mime=mime, key=key, type="primary")


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def hsv_to_rgb(h, s, v):
    """Vectorised HSV -> RGB (all inputs in 0..1, returns 0..255 floats)."""
    h6 = h * 6.0
    i = np.floor(h6).astype(int) % 6
    f = h6 - np.floor(h6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1) * 255.0


def demo_image(size: int = 640) -> Image.Image:
    """Procedural demo (shaded sphere on a gradient) so the app works with no upload."""
    y, x = np.mgrid[0:size, 0:size] / (size - 1)
    radius = 0.32
    nx, ny = (x - 0.5) / radius, (y - 0.5) / radius
    inside = nx ** 2 + ny ** 2 <= 1
    nz = np.sqrt(np.clip(1 - nx ** 2 - ny ** 2, 0, 1))
    diffuse = np.clip(-0.4 * nx - 0.5 * ny + 0.75 * nz, 0, 1)
    sphere = np.array([255, 120, 60]) * (0.12 + 0.88 * diffuse[..., None])
    bg = np.stack([40 + 90 * x, 30 + 60 * y, 120 + 110 * (1 - x)], axis=-1)
    img = np.where(inside[..., None], sphere, bg)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


# ----------------------------------------------------------------- online images

MAX_URL_BYTES = 25 * 1024 * 1024  # same limit as file uploads


def _assert_public_host(host: str) -> None:
    """Refuse localhost / private / link-local targets (SSRF protection), since
    this server, not the visitor's browser, performs the download."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve “{host}”.") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("That address points to a private or local network, which isn't allowed.")


@st.cache_data(ttl=3600, max_entries=32, show_spinner=False)
def fetch_image_bytes(url: str) -> bytes:
    """Download an image over http(s): max 5 redirects (each re-checked), 25 MB cap."""
    cur = url.strip()
    for _ in range(6):
        parts = urlparse(cur)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Please enter a full http:// or https:// link.")
        _assert_public_host(parts.hostname)
        with requests.get(cur, stream=True, timeout=(5, 20), allow_redirects=False,
                          headers={"User-Agent": "ASCII-Art-Studio/2.0 (+image fetch)"}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                cur = urljoin(cur, r.headers["Location"])
                continue
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").lower()
            if ctype and not ctype.startswith(("image/", "application/octet-stream", "binary/")):
                raise ValueError(f"That link is not an image (it returned “{ctype.split(';')[0]}”).")
            declared = r.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_URL_BYTES:
                raise ValueError("That image is larger than 25 MB.")
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf.extend(chunk)
                if len(buf) > MAX_URL_BYTES:
                    raise ValueError("That image is larger than 25 MB.")
            return bytes(buf)
    raise ValueError("Too many redirects.")


def url_display_name(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    stem = Path(name).stem[:40]
    return f"{stem or 'url_image'}.png"


def load_url_image():
    """Form callback: download, validate it decodes, and remember it in session state."""
    url = (st.session_state.get("url_input") or "").strip()
    st.session_state.pop("url_error", None)
    if not url:
        return
    try:
        data = fetch_image_bytes(url)
        Image.open(io.BytesIO(data)).verify()  # fail early on corrupt / non-image data
        st.session_state["url_image"] = (url, data)
    except Exception as exc:  # noqa: BLE001
        st.session_state.pop("url_image", None)
        msg = str(exc) if isinstance(exc, ValueError) else f"Could not load that image ({type(exc).__name__}: {exc})"
        st.session_state["url_error"] = msg[:300]


def clear_url_image():
    for k in ("url_image", "url_error"):
        st.session_state.pop(k, None)
    st.session_state["url_input"] = ""


# ----------------------------------------------------------------- fonts


@functools.lru_cache(maxsize=1)
def _find_font_path() -> str | None:
    for name in ("DejaVuSansMono.ttf", "CascadiaMono.ttf", "consola.ttf",
                 "Menlo.ttc", "LiberationMono-Regular.ttf", "cour.ttf"):
        try:
            ImageFont.truetype(name, 12)
            return name
        except OSError:
            continue
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", "/Library/Fonts",
                 "/System/Library/Fonts", "C:/Windows/Fonts"):
        for pat in ("**/DejaVuSansMono.ttf", "**/LiberationMono-Regular.ttf", "**/*Mono*.ttf"):
            hits = glob.glob(f"{root}/{pat}", recursive=True)
            if hits:
                return hits[0]
    return None


def load_mono_font(size: int):
    path = _find_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


@st.cache_data(show_spinner=False)
def sort_by_density(chars: str) -> str:
    """Order glyphs dense -> sparse by measured ink coverage in a real font."""
    font = load_mono_font(28)
    score: dict[str, float] = {}
    for ch in dict.fromkeys(chars):
        im = Image.new("L", (40, 40), 0)
        ImageDraw.Draw(im).text((6, 2), ch, font=font, fill=255)
        score[ch] = float(np.asarray(im, dtype=np.float32).mean())
    return "".join(sorted(score, key=lambda c: -score[c]))


def resolve_ramp(S: dict) -> str:
    base = S["custom_ramp"] if S["charset"] == "Custom" else CHARSETS[S["charset"]]
    base = base or " "
    return sort_by_density(base) if S["sort_ramp"] else base


# ============================================================== SETTINGS STATE


def sanitize(key: str, value):
    """Validate one imported setting. Returns (ok, clean_value)."""
    spec = SPEC.get(key)
    if spec is None:
        return False, None
    d = spec["default"]
    if "choices" in spec:
        return (not isinstance(value, bool) and value in spec["choices"]), value
    if isinstance(d, bool):
        return isinstance(value, bool), value
    if isinstance(d, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, None
        v = min(max(value, spec["lo"]), spec["hi"])
        return True, int(round(v)) if isinstance(d, int) else float(v)
    if spec.get("kind") == "color":
        return (isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None), value
    if isinstance(value, str) and len(value) <= 120:
        return True, value
    return False, None


def apply_preset(name: str):
    for k in LOOK_KEYS:
        st.session_state[k] = SPEC[k]["default"]
    for k, v in PRESETS[name]["s"].items():
        st.session_state[k] = v


def on_style_change():
    st.session_state["bg"] = STYLE_DEFAULT_BG[st.session_state["style"]]


def apply_settings_file():
    f = st.session_state.get("settings_file")
    if f is None:
        return
    try:
        data = json.loads(f.getvalue().decode("utf-8"))
        vals = data.get("settings", data)
        n = 0
        for k, v in vals.items():
            ok, clean = sanitize(k, v)
            if ok:
                st.session_state[k] = clean
                n += 1
        st.session_state["_msg"] = ("success", f"Applied {n} settings.")
    except Exception as exc:  # noqa: BLE001
        st.session_state["_msg"] = ("error", f"Could not read settings: {exc}")


# Initialise / persist every setting. Re-assigning each run keeps values alive
# even when their widget is hidden (Streamlit drops state of unrendered widgets).
for _k, _spec in SPEC.items():
    st.session_state[_k] = st.session_state.get(_k, _spec["default"])
S: dict = {k: st.session_state[k] for k in SPEC}

# ============================================================== IMAGE PIPELINE


def prepare_image(image: Image.Image, S: dict) -> Image.Image:
    img = ImageOps.exif_transpose(image).convert("RGB")
    img.thumbnail((1600, 1600))
    if S["flip_h"]:
        img = ImageOps.mirror(img)
    if S["flip_v"]:
        img = ImageOps.flip(img)
    if S["rotate"]:
        img = img.rotate(-S["rotate"], expand=True)
    if S["blur"] > 0:
        img = img.filter(ImageFilter.GaussianBlur(S["blur"]))
    if S["autoc"]:
        img = ImageOps.autocontrast(img, cutoff=1)
    if S["equalize"]:
        img = ImageOps.equalize(img)
    if abs(S["gamma"] - 1.0) > 1e-6:
        inv = 1.0 / S["gamma"]
        img = img.point([int(255 * ((i / 255) ** inv) + 0.5) for i in range(256)] * 3)
    img = ImageEnhance.Brightness(img).enhance(S["brightness"])
    img = ImageEnhance.Contrast(img).enhance(S["contrast"])
    img = ImageEnhance.Color(img).enhance(S["saturation"])
    img = ImageEnhance.Sharpness(img).enhance(S["sharpness"])
    return img


def bayer_matrix(n: int) -> np.ndarray:
    m = np.array([[0]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / (n * n)


FS_KERNEL = [(1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)]
ATKINSON_KERNEL = [(1, 0, .125), (2, 0, .125), (-1, 1, .125), (0, 1, .125), (1, 1, .125), (0, 2, .125)]


def error_diffuse(v: np.ndarray, kernel, hi: int, strength: float) -> np.ndarray:
    h, w = v.shape
    buf = v.astype(float).tolist()  # python lists are much faster than numpy scalars here
    out = [[0] * w for _ in range(h)]
    for y in range(h):
        row, orow = buf[y], out[y]
        for x in range(w):
            old = row[x]
            q = 0 if old < 0 else hi if old > hi else int(old + 0.5)
            orow[x] = q
            err = max(-1.5, min(1.5, (old - q) * strength))
            if err:
                for dx, dy, wt in kernel:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and ny < h:
                        buf[ny][nx] += err * wt
    return np.array(out)


def quantise(lum: np.ndarray, n: int, S: dict) -> np.ndarray:
    """Map 0..255 luminance to glyph indices 0..n-1, optionally dithered."""
    if n <= 1:
        return np.zeros(lum.shape, dtype=int)
    hi = n - 1
    v = lum / 255.0 * hi
    mode, k = S["dither"], S["dither_strength"]
    if mode.startswith("Ordered"):
        size = 8 if "8×8" in mode else 4
        reps = (lum.shape[0] // size + 1, lum.shape[1] // size + 1)
        thr = np.tile(bayer_matrix(size), reps)[:lum.shape[0], :lum.shape[1]]
        idx = np.floor(v + 0.5 + (thr - 0.5) * k)
    elif mode == "Floyd–Steinberg":
        idx = error_diffuse(v, FS_KERNEL, hi, k)
    elif mode == "Atkinson":
        idx = error_diffuse(v, ATKINSON_KERNEL, hi, k)
    else:
        idx = np.rint(v)
    return np.clip(idx, 0, hi).astype(int)


def detect_edges(lum: np.ndarray, sens: float):
    """Sobel edges. Returns (mask, glyph array of | - / \\ , strength 0..1)."""
    p = np.pad(lum.astype(np.float32), 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])
    mag = np.hypot(gx, gy)
    norm = np.clip(mag / (np.percentile(mag, 99) + 1e-6), 0, 1)
    mask = norm >= (1.0 - sens)
    ang = np.degrees(np.arctan2(gx, -gy)) % 180.0  # direction of the edge line (y points down)
    bins = np.select([ang < 22.5, ang < 67.5, ang < 112.5, ang < 157.5], [0, 1, 2, 3], default=0)
    return mask, EDGE_GLYPHS[bins], norm


def gradient_map(b: np.ndarray, stops_hex: list[str]) -> np.ndarray:
    stops = np.array([hex_to_rgb(h) for h in stops_hex], dtype=np.float32)
    m = len(stops)
    pos = np.clip(b, 0, 1) * (m - 1)
    i = np.clip(np.floor(pos).astype(int), 0, m - 2)
    t = (pos - i)[..., None]
    return stops[i] * (1 - t) + stops[i + 1] * t


def gradient_stops(S: dict) -> list[str]:
    return [S["grad_c1"], S["grad_c2"]] if S["grad"] == "Custom" else GRADIENTS[S["grad"]]


def make_colors(rgb: np.ndarray, lum: np.ndarray, dark: bool, S: dict) -> np.ndarray:
    """Per-character colours as a uint8 array (rows, cols, 3)."""
    style = S["style"]
    b = np.clip(lum / 255.0, 0, 1)
    b3 = b[..., None]
    rows, cols = lum.shape

    def lift(c):  # keep photo colours readable on the chosen background
        return 40 + c * (215 / 255) if dark else c * 0.85

    if style == "Original Color":
        col = lift(rgb.astype(np.float32))
    elif style == "Grayscale":
        col = np.repeat((45 + 210 * b3) if dark else (15 + 150 * b3), 3, axis=2)
    elif style == "Matrix":
        col = np.array([0, 255, 65]) * (0.3 + 0.7 * b3)
    elif style == "Amber CRT":
        col = np.array([255, 176, 0]) * (0.3 + 0.7 * b3)
    elif style == "Gradient Map":
        col = gradient_map(b, gradient_stops(S))
    elif style == "Rainbow":
        yy, xx = np.mgrid[0:rows, 0:cols]
        hue = (xx / max(cols, 1) * 0.75 + yy / max(rows, 1) * 0.25) % 1.0
        col = hsv_to_rgb(hue, np.full_like(b, 0.72), 0.45 + 0.55 * b)
    elif style == "Palette Quantize":
        q = Image.fromarray(rgb).quantize(colors=int(S["palette_k"]), method=Image.Quantize.MEDIANCUT)
        col = lift(np.asarray(q.convert("RGB"), dtype=np.float32))
    elif style == "Blueprint":
        col = np.array([170, 210, 255]) * (0.4 + 0.6 * b3)
    else:  # Ink on Paper
        ink = np.array([235, 235, 240.0]) if dark else np.array([24, 24, 30.0])
        col = np.broadcast_to(ink, rgb.shape)
    col = np.clip(col, 0, 255).astype(np.uint8)
    # quantise a little -> far fewer <span>s in the HTML output
    return ((col >> 3) << 3) | 4


@dataclass
class Render:
    grid: np.ndarray
    colors: np.ndarray
    lum: np.ndarray
    tuned: Image.Image
    bg: str
    bg_rgb: tuple
    dark: bool
    ramp: str
    edge_mask: np.ndarray | None

    @property
    def rows(self) -> int:
        return self.grid.shape[0]

    @property
    def cols(self) -> int:
        return self.grid.shape[1]

    @property
    def txt(self) -> str:
        return "\n".join("".join(r).rstrip() for r in self.grid)


def convert(image: Image.Image, S: dict) -> Render:
    tuned = prepare_image(image, S)
    cols = S["cols"]
    rows = max(1, int(round(cols * tuned.height / tuned.width * S["aspect"])))
    small = tuned.resize((cols, rows), Image.Resampling.LANCZOS)
    rgb = np.asarray(small, dtype=np.uint8)

    bg_rgb = hex_to_rgb(S["bg"])
    dark = luminance(bg_rgb) < 128
    ramp = resolve_ramp(S)
    eff = ramp[::-1] if dark else ramp

    lum = rgb.astype(np.float32) @ LUMA
    if S["invert"]:
        lum = 255 - lum
    grid = np.array(list(eff))[quantise(lum, len(eff), S)]

    mask = None
    if S["edge_mode"] != "Off":
        mask, glyphs, _ = detect_edges(lum, S["edge_sens"])
        grid = np.where(mask, glyphs, " " if S["edge_mode"] == "Edges only" else grid)

    colors = make_colors(rgb, lum, dark, S)
    if mask is not None and S["edge_tint"]:
        colors[mask] = hex_to_rgb(S["edge_color"])
    return Render(grid, colors, lum, tuned, S["bg"], bg_rgb, dark, ramp, mask)


# ============================================================== OUTPUT BUILDERS


def color_runs(grid: np.ndarray, colors: np.ndarray):
    """Run-length encode each row into [(hex colour | None, text), ...].
    Spaces join the current run, so colour changes only happen on visible glyphs."""
    out = []
    for y in range(grid.shape[0]):
        runs, cur, buf = [], None, []
        crow = colors[y].tolist()
        for x, ch in enumerate(grid[y].tolist()):
            if ch == " ":
                buf.append(ch)
                continue
            r, g, b = crow[x]
            col = f"#{r:02x}{g:02x}{b:02x}"
            if col != cur:
                if buf:
                    runs.append((cur, "".join(buf)))
                buf, cur = [], col
            buf.append(ch)
        if buf:
            runs.append((cur, "".join(buf)))
        out.append(runs)
    return out


def runs_to_html(runs) -> str:
    esc = html.escape
    lines = []
    for row in runs:
        lines.append("".join(esc(t) if c is None else f'<span style="color:{c}">{esc(t)}</span>' for c, t in row))
    return "\n".join(lines)


def build_ansi(runs) -> str:
    lines = []
    for row in runs:
        s = ""
        for c, t in row:
            if c is None:
                s += t
            else:
                s += f"\x1b[38;2;{int(c[1:3], 16)};{int(c[3:5], 16)};{int(c[5:7], 16)}m{t}\x1b[0m"
        lines.append(s)
    return "\n".join(lines) + "\n"


def build_svg(runs, cols: int, bg: str, aspect: float, font_px: int = 14) -> str:
    esc = html.escape
    cw = font_px * 0.6
    ch = cw / aspect
    pad = cw * 2
    W, H = cols * cw + pad * 2, len(runs) * ch + pad * 2
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.1f} {H:.1f}" width="{W:.0f}" height="{H:.0f}">',
        f'<rect width="100%" height="100%" fill="{bg}"/>',
        f'<g font-family="Menlo, Consolas, DejaVu Sans Mono, monospace" font-size="{font_px}" xml:space="preserve" fill="#e5e7eb">',
    ]
    for y, row in enumerate(runs):
        if not row:
            continue
        n = sum(len(t) for _, t in row)
        base = pad + y * ch + (ch - font_px) / 2 + font_px * 0.8
        spans = "".join(esc(t) if c is None else f'<tspan fill="{c}">{esc(t)}</tspan>' for c, t in row)
        out.append(f'<text x="{pad:.1f}" y="{base:.1f}" textLength="{n * cw:.1f}" lengthAdjust="spacing">{spans}</text>')
    out.append("</g></svg>")
    return "\n".join(out)


DOC_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;background:__BG__;}
body{padding:__PAD__px;}
.wrap{position:relative;display:inline-block;min-width:100%;box-sizing:border-box;}
pre{display:inline-block;margin:0;font-family:'Cascadia Mono',Consolas,'DejaVu Sans Mono','Liberation Mono',Menlo,monospace;font-size:__FS__px;line-height:__LH__em;white-space:pre;color:#e5e7eb;__ANIM__}
.glow,.glow span{text-shadow:0 0 6px currentColor;}
.crt::after{content:"";position:absolute;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,rgba(0,0,0,.28) 0,rgba(0,0,0,.28) 1px,transparent 1px,transparent 3px);}
@keyframes reveal{from{clip-path:inset(0 0 100% 0);}to{clip-path:inset(0 0 0 0);}}
.tb{display:none;position:fixed;top:8px;right:12px;z-index:5;gap:4px;align-items:center;padding:5px 6px;border-radius:999px;background:rgba(10,14,25,.78);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.14);font:600 11px/1 system-ui,sans-serif;color:#cbd5e1;}
.tb button{all:unset;cursor:pointer;padding:5px 9px;border-radius:999px;color:#e2e8f0;}
.tb button:hover,.tb button:focus-visible{background:rgba(125,211,252,.22);}
.tb span{min-width:34px;text-align:center;color:#94a3b8;}
.foot{margin-top:22px;font:12px sans-serif;color:#64748b;text-align:center;}
@media print{.tb{display:none!important}}
</style></head>
<body>
<div class="tb" id="tb" role="toolbar" aria-label="Preview controls">
<button id="zo" title="Zoom out" aria-label="Zoom out">−</button><span id="zv"></span><button id="zi" title="Zoom in" aria-label="Zoom in">+</button>
<button id="fit" title="Fit to width">Fit</button><button id="gl" title="Toggle glow">Glow</button><button id="sc" title="Toggle scanlines">CRT</button><button id="cp" title="Copy as text">Copy</button>
</div>
<div class="wrap __WRAPCLS__"><pre class="__PRECLS__">__PRE__</pre></div>__FOOTER__
<script>__SCRIPT__</script>
</body></html>"""

DOC_SCRIPT = r"""
(function(){try{
 var pre=document.querySelector('pre'),wrap=document.querySelector('.wrap'),tb=document.getElementById('tb'),lab=document.getElementById('zv');
 var base=parseFloat(getComputedStyle(pre).fontSize),z=base;
 function set(v){z=Math.max(2,Math.min(48,v));pre.style.fontSize=z+'px';lab.textContent=Math.round(z)+'px';}
 function fit(){pre.style.fontSize=base+'px';var w=pre.scrollWidth,avail=document.documentElement.clientWidth-__PADX2__;if(w>0)set(base*avail/w);}
 document.getElementById('zi').onclick=function(){set(z*1.15)};
 document.getElementById('zo').onclick=function(){set(z/1.15)};
 document.getElementById('fit').onclick=fit;
 document.getElementById('gl').onclick=function(){pre.classList.toggle('glow')};
 document.getElementById('sc').onclick=function(){wrap.classList.toggle('crt')};
 document.getElementById('cp').onclick=function(){
  var t=pre.innerText,b=this;
  function ok(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1200)}
  function fb(){var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();try{document.execCommand('copy');ok()}catch(e){}document.body.removeChild(a)}
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(ok,fb)}else{fb()}
 };
 tb.style.display='flex';set(base);
 if(__AUTOFIT__){fit();window.addEventListener('resize',fit);}
}catch(e){}})();
"""


def build_doc(pre_html, bg, font_px, aspect, glow, scan, *, full_page=False, animate=False,
              autofit=False, title="ASCII Art") -> str:
    pad = 28 if full_page else 14
    anim = "animation:reveal 3s steps(80,end) both;" if (full_page and animate) else ""
    footer = '<div class="foot">Generated with ASCII Art Studio</div>' if full_page else ""
    script = (DOC_SCRIPT.replace("__PADX2__", str(pad * 2))
              .replace("__AUTOFIT__", "true" if autofit else "false"))
    doc = DOC_TEMPLATE
    for k, v in (("__TITLE__", html.escape(title)), ("__BG__", bg), ("__PAD__", str(pad)),
                 ("__FS__", str(font_px)), ("__LH__", str(round(0.58 / aspect, 3))), ("__ANIM__", anim),
                 ("__WRAPCLS__", "crt" if scan else ""), ("__PRECLS__", "glow" if glow else ""),
                 ("__FOOTER__", footer), ("__SCRIPT__", script)):
        doc = doc.replace(k, v)
    return doc.replace("__PRE__", pre_html)  # user content last so it is never re-processed


CMP_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#0b0f1a;font-family:system-ui,sans-serif;color:#cbd5e1;overflow:hidden}
.stage{display:flex;flex-direction:column;align-items:center;height:100vh;box-sizing:border-box;padding:8px}
.cmp{position:relative;overflow:hidden;line-height:0;border-radius:10px;width:min(100%,calc((100vh - 56px) * __AR__))}
.cmp img{width:100%;display:block}
.asc{position:absolute;inset:0;background:__BG__;clip-path:inset(0 0 0 50%);overflow:hidden}
.asc pre{display:inline-block;margin:0;font-family:'Cascadia Mono',Consolas,'DejaVu Sans Mono','Liberation Mono',Menlo,monospace;font-size:8px;line-height:__LH__em;white-space:pre;color:#e5e7eb;transform-origin:0 0}
.glow,.glow span{text-shadow:0 0 4px currentColor}
.bar{position:absolute;top:0;bottom:0;left:50%;width:2px;margin-left:-1px;background:#fff;box-shadow:0 0 12px rgba(255,255,255,.8);pointer-events:none}
.lbl{position:absolute;top:8px;font:600 11px/1 system-ui;letter-spacing:.08em;padding:5px 9px;border-radius:999px;background:rgba(0,0,0,.6);color:#fff}
.l{left:8px}.r{right:8px}
.rng{width:min(100%,520px);margin-top:14px;accent-color:#a78bfa}
</style></head><body><div class="stage">
<div class="cmp" id="cmp"><img src="data:image/jpeg;base64,__IMG__" alt="Tuned input image">
<div class="asc" id="asc"><pre id="pre" class="__PRECLS__">__PRE__</pre></div>
<div class="bar" id="bar"></div><span class="lbl l">INPUT</span><span class="lbl r">ASCII</span></div>
<input class="rng" id="rng" type="range" min="0" max="100" value="50" aria-label="Reveal slider">
</div><script>
(function(){try{
 var cmp=document.getElementById('cmp'),pre=document.getElementById('pre'),asc=document.getElementById('asc'),bar=document.getElementById('bar'),rng=document.getElementById('rng');
 function fit(){pre.style.transform='none';var w=pre.scrollWidth,cw=cmp.clientWidth;if(w>0)pre.style.transform='scale('+(cw/w)+')';}
 function upd(){var v=rng.value;asc.style.clipPath='inset(0 0 0 '+v+'%)';bar.style.left=v+'%';}
 rng.addEventListener('input',upd);window.addEventListener('resize',fit);fit();upd();
}catch(e){}})();
</script></body></html>"""


def build_compare_doc(tuned: Image.Image, pre_html: str, bg: str, aspect: float, glow: bool) -> str:
    im = tuned.copy()
    im.thumbnail((900, 2700))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    doc = CMP_TEMPLATE
    for k, v in (("__AR__", f"{tuned.width / tuned.height:.4f}"), ("__BG__", bg),
                 ("__LH__", str(round(0.58 / aspect, 3))), ("__PRECLS__", "glow" if glow else ""),
                 ("__IMG__", base64.b64encode(buf.getvalue()).decode())):
        doc = doc.replace(k, v)
    return doc.replace("__PRE__", pre_html)


def render_png(r: Render, aspect: float, font_px: int, transparent: bool = False) -> bytes:
    font = load_mono_font(font_px)
    try:
        cw = font.getlength("M")
    except AttributeError:
        cw = font.getsize("M")[0]
    ch = cw / aspect
    pad = int(cw * 2)
    bg = (0, 0, 0, 0) if transparent else (*r.bg_rgb, 255)
    img = Image.new("RGBA", (int(r.cols * cw + pad * 2), int(r.rows * ch + pad * 2)), bg)
    draw = ImageDraw.Draw(img)
    for y in range(r.rows):
        py = pad + y * ch
        crow = r.colors[y].tolist()
        for x in range(r.cols):
            c = r.grid[y, x]
            if c != " ":
                draw.text((pad + x * cw, py), c, font=font, fill=(*crow[x], 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def make_exports(r: Render, S: dict, fmts: list[str], png_px: int, transparent: bool) -> dict[str, bytes]:
    """Build the requested export formats (used by the batch ZIP)."""
    runs = color_runs(r.grid, r.colors)
    out: dict[str, bytes] = {}
    if "TXT" in fmts:
        out["txt"] = r.txt.encode()
    if "HTML" in fmts:
        out["html"] = build_doc(runs_to_html(runs), r.bg, S["font_px"], S["aspect"], S["glow"], S["scan"],
                                full_page=True, autofit=True).encode()
    if "SVG" in fmts:
        out["svg"] = build_svg(runs, r.cols, r.bg, S["aspect"]).encode()
    if "ANSI" in fmts:
        out["ans"] = build_ansi(runs).encode()
    if "PNG" in fmts:
        out["png"] = render_png(r, S["aspect"], png_px, transparent)
    return out


def dominant_palette(img: Image.Image, k: int = 6):
    small = img.copy()
    small.thumbnail((160, 160))
    q = small.quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette() or []
    total = small.width * small.height
    out = []
    for count, idx in sorted(q.getcolors() or [], reverse=True):
        rgb = tuple(pal[idx * 3: idx * 3 + 3])
        out.append((f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}", count / total))
    return out


# ============================================================== HEADER

st.markdown(HERO_HTML, unsafe_allow_html=True)

# ============================================================== SIDEBAR


def slider(label: str, key: str, help: str | None = None, disabled: bool = False):
    sp = SPEC[key]
    return st.slider(label, sp["lo"], sp["hi"], step=sp["step"], key=key, help=help, disabled=disabled)


with st.sidebar:
    st.markdown("## 🎨 Controls")
    msg = st.session_state.pop("_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])

    t_shape, t_tone, t_glyph, t_color, t_fx = st.tabs(["Shape", "Tone", "Glyphs", "Color", "FX"])

    with t_shape:
        slider("Columns (detail)", "cols", "More columns = more detail, but a bigger output.")
        slider("Character aspect", "aspect",
               "Glyph width ÷ height. Raise it if the art looks stretched tall, lower it if it looks squashed.")
        st.selectbox("Rotate (°)", SPEC["rotate"]["choices"], key="rotate")
        c1, c2 = st.columns(2)
        c1.toggle("Flip ↔", key="flip_h")
        c2.toggle("Flip ↕", key="flip_v")

    with t_tone:
        c1, c2 = st.columns(2)
        c1.toggle("Auto contrast", key="autoc")
        c2.toggle("Equalise", key="equalize", help="Histogram equalisation: spreads tones for flat photos.")
        st.toggle("Invert brightness", key="invert")
        slider("Brightness", "brightness")
        slider("Contrast", "contrast")
        slider("Gamma", "gamma", "Below 1 darkens mid-tones, above 1 lifts them.")
        slider("Saturation", "saturation")
        slider("Sharpness", "sharpness")
        slider("Denoise blur", "blur", "Gaussian blur before sampling. Calms noisy photos.")

    with t_glyph:
        st.selectbox("Character set", list(CHARSETS), key="charset")
        if S["charset"] == "Custom":
            st.text_input("Ramp (dense → sparse)", key="custom_ramp", max_chars=120)
        st.toggle("Auto-sort by glyph ink density", key="sort_ramp",
                  help="Measures how much ink each glyph really uses in a monospace font and orders the ramp accordingly.")
        st.code(resolve_ramp(S), language=None)
        st.selectbox("Dithering", DITHERS, key="dither",
                     help="Trades flat banding for texture: great with short ramps like Binary or Blocks.")
        slider("Dither strength", "dither_strength", disabled=S["dither"] == "None")
        st.selectbox("Edge tracing", EDGE_MODES, key="edge_mode",
                     help="Draws | - / \\ glyphs along contours. 'Edges only' gives line art.")
        off = S["edge_mode"] == "Off"
        slider("Edge sensitivity", "edge_sens", disabled=off)
        e1, e2 = st.columns([3, 1])
        e1.toggle("Tint edges", key="edge_tint", disabled=off)
        e2.color_picker("Edge colour", key="edge_color", label_visibility="collapsed",
                        disabled=off or not S["edge_tint"])

    with t_color:
        st.selectbox("Colour style", STYLES, key="style", on_change=on_style_change)
        st.color_picker("Background", key="bg")
        if S["style"] == "Gradient Map":
            st.selectbox("Gradient preset", list(GRADIENTS), key="grad")
            if S["grad"] == "Custom":
                g1, g2 = st.columns(2)
                g1.color_picker("Shadows", key="grad_c1")
                g2.color_picker("Highlights", key="grad_c2")
            st.markdown(f'<div class="grad-bar" style="background:linear-gradient(90deg,{",".join(gradient_stops(S))})"></div>',
                        unsafe_allow_html=True)
        if S["style"] == "Palette Quantize":
            slider("Palette size", "palette_k", "Number of colours extracted from the image.")

    with t_fx:
        st.toggle("Auto-fit preview to width", key="autofit",
                  help="Scales the font so the whole art fits. Needs scripts in the preview frame; otherwise the size below is used.")
        slider("Preview font size (px)", "font_px", disabled=S["autofit"])
        slider("Preview height (px)", "preview_h")
        st.toggle("Neon glow", key="glow")
        st.toggle("CRT scanlines", key="scan")

    with st.expander("💾 Save / load settings"):
        st.download_button(
            "⬇️ Download settings", data=json.dumps({"app": "ascii-art-studio", "version": 2, "settings": S}, indent=2),
            file_name="ascii_studio_settings.json", mime="application/json", key="dl_settings")
        st.file_uploader("Load settings (.json)", type=["json"], key="settings_file")
        st.button("Apply loaded settings", on_click=apply_settings_file, key="apply_settings")

# ============================================================== QUICK LOOKS

st.markdown('<div class="section-label">Quick looks</div>', unsafe_allow_html=True)
for col, (pname, p) in zip(st.columns(len(PRESETS)), PRESETS.items()):
    with col:
        stretch(st.button, p["label"], key=f"preset_{pname}", on_click=apply_preset, args=(pname,))

# ============================================================== SOURCE

st.markdown('<div class="section-label">Source</div>', unsafe_allow_html=True)
tab_up, tab_url, tab_cam = st.tabs(["📁 Upload images", "🔗 Image URL", "📷 Use camera"])

with tab_up:
    uploaded = st.file_uploader(
        "Upload images", type=["jpg", "jpeg", "png", "webp", "bmp"],
        accept_multiple_files=True, label_visibility="collapsed",
        help="Upload several images to preview one and batch-export them all.")
    if st.button("✨ Load demo image"):
        st.session_state["demo"] = True

with tab_url:
    with st.form("url_form", border=False):
        f1, f2 = st.columns([6, 1], vertical_alignment="bottom")
        f1.text_input("Image URL", key="url_input", placeholder="https://example.com/photo.png",
                      help="Direct link to a JPG, PNG, WEBP or BMP image (max 25 MB).")
        f2.form_submit_button("🔗 Load", on_click=load_url_image, type="primary", use_container_width=True)
    if st.session_state.get("url_error"):
        st.error(st.session_state["url_error"])
    if st.session_state.get("url_image"):
        c1, c2 = st.columns([6, 1], vertical_alignment="center")
        c1.caption(f"Loaded: {st.session_state['url_image'][0][:110]}")
        c2.button("✖ Remove", key="url_clear", on_click=clear_url_image)

shot = None
with tab_cam:
    if st.checkbox("Enable camera"):
        shot = st.camera_input("Take a photo", label_visibility="collapsed")

images: list[tuple[str, Image.Image]] = []
for f in uploaded or []:
    try:
        im = Image.open(f)
        im.load()
        images.append((f.name, im))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read {f.name}: {exc}")
if st.session_state.get("url_image"):
    _url, _data = st.session_state["url_image"]
    try:
        im = Image.open(io.BytesIO(_data))
        im.load()
        images.append((url_display_name(_url), im))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not decode the downloaded image: {exc}")
if shot is not None:
    try:
        im = Image.open(shot)
        im.load()
        images.append(("camera.png", im))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read the camera photo: {exc}")
if not images and st.session_state.get("demo"):
    images.append(("demo.png", demo_image()))

# ============================================================== MAIN

if not images:
    st.info("👆 Upload an image, paste an image URL, use your camera, or load the demo image to get started. "
            "Tip: pick a quick look above first, then fine-tune in the sidebar.")
    st.markdown(FEATURES_HTML, unsafe_allow_html=True)
else:
    names = [n for n, _ in images]
    active = names[0]
    if len(images) > 1:
        active = st.selectbox("Active image", names, help="The preview shows this one; batch export uses them all.")
    image = images[names.index(active)][1]

    t0 = time.perf_counter()
    R = convert(image, S)
    runs = color_runs(R.grid, R.colors)
    pre_html = runs_to_html(runs)
    txt = R.txt
    elapsed_ms = (time.perf_counter() - t0) * 1000

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Grid", f"{R.cols} × {R.rows}")
    m2.metric("Characters", f"{R.cols * R.rows:,}")
    m3.metric("Glyphs used", f"{len(np.unique(R.grid))}")
    m4.metric("Text size", f"{len(txt.encode()) / 1024:.1f} KB")
    m5.metric("Render time", f"{elapsed_ms:.0f} ms")

    t_prev, t_cmp, t_txt, t_ins, t_exp = st.tabs(
        ["✨ Preview", "🔍 Compare", "📝 Plain text", "📊 Insights", "📦 Export"])

    with t_prev:
        doc = build_doc(pre_html, R.bg, S["font_px"], S["aspect"], S["glow"], S["scan"], autofit=S["autofit"])
        show_html(doc, S["preview_h"])
        st.caption("Use the toolbar in the preview's top-right corner to zoom, fit, copy and toggle effects.")

    with t_cmp:
        mode = st.radio("Compare mode", ["Side by side", "Reveal slider"], horizontal=True,
                        label_visibility="collapsed", key="cmp_mode")
        if mode == "Reveal slider":
            show_html(build_compare_doc(R.tuned, pre_html, R.bg, S["aspect"], S["glow"]), 600)
            st.caption("Drag the slider to wipe between the tuned photo and its ASCII version.")
        else:
            left, right = st.columns(2)
            with left:
                st.markdown("**Tuned input**")
                stretch(st.image, R.tuned)
            with right:
                st.markdown("**ASCII output**")
                show_html(build_doc(pre_html, R.bg, S["font_px"], S["aspect"], S["glow"], S["scan"], autofit=True), 480)

    with t_txt:
        st.code(txt, language=None)

    with t_ins:
        a, b = st.columns(2)
        with a:
            st.markdown("**Brightness distribution**")
            hist, _ = np.histogram(R.lum, bins=32, range=(0, 255))
            st.bar_chart(hist)
            st.markdown("**Glyph usage**")
            vals, counts = np.unique(R.grid, return_counts=True)
            usage = pd.Series(counts, index=["␣" if v == " " else str(v) for v in vals]).sort_values(ascending=False)
            st.bar_chart(usage.head(24))
        with b:
            st.markdown("**Dominant palette**")
            sw = "".join(
                f'<div class="sw" style="background:{c};color:{"#0b0f1a" if luminance(hex_to_rgb(c)) > 150 else "#fff"}">'
                f'{c} · {p:.0%}</div>' for c, p in dominant_palette(R.tuned, 6))
            st.markdown(f'<div class="swatches">{sw}</div>', unsafe_allow_html=True)
            st.markdown("**Stats**")
            emask, _, _ = detect_edges(R.lum, S["edge_sens"])
            s1, s2 = st.columns(2)
            s1.metric("Mean brightness", f"{R.lum.mean() / 255:.0%}")
            s2.metric("Contrast (σ)", f"{R.lum.std() / 255:.0%}")
            s1.metric("Edge density", f"{emask.mean():.0%}")
            s2.metric("Background", "dark" if R.dark else "light")

    with t_exp:
        stem = Path(active).stem or "ascii_art"
        e1, e2, e3 = st.columns(3)
        with e1, st.container(border=True, key="card_txt"):
            st.markdown("#### 📄 Text")
            st.caption("Plain monospace text.")
            dl("⬇️ Download TXT", txt, f"{stem}.txt", "text/plain", "dl_txt")
        with e2, st.container(border=True, key="card_ansi"):
            st.markdown("#### 🖥️ ANSI")
            st.caption("24-bit colour for terminals: `cat file.ans`.")
            dl("⬇️ Download ANSI", build_ansi(runs), f"{stem}.ans", "text/plain", "dl_ansi")
        with e3, st.container(border=True, key="card_svg"):
            st.markdown("#### ✒️ SVG")
            st.caption("Crisp vector output that scales to any size.")
            dl("⬇️ Download SVG", build_svg(runs, R.cols, R.bg, S["aspect"]), f"{stem}.svg", "image/svg+xml", "dl_svg")

        e4, e5 = st.columns(2)
        with e4, st.container(border=True, key="card_html"):
            st.markdown("#### 🌐 HTML")
            st.caption("Standalone interactive page with zoom, copy and effect toggles.")
            animate = st.toggle("Reveal animation", key="html_anim")
            full_doc = build_doc(pre_html, R.bg, S["font_px"], S["aspect"], S["glow"], S["scan"],
                                 full_page=True, animate=animate, autofit=True, title=stem)
            dl("⬇️ Download HTML", full_doc, f"{stem}.html", "text/html", "dl_html")
        with e5, st.container(border=True, key="card_png"):
            st.markdown("#### 🖼️ PNG")
            p1, p2 = st.columns([2, 1])
            png_px = p1.slider("PNG font size", 8, 28, 14, key="png_px")
            transparent = p2.toggle("Transparent", key="png_transparent")
            sig = hashlib.md5(txt.encode() + R.colors.tobytes()
                              + f"{R.bg}|{png_px}|{S['aspect']}|{transparent}".encode()).hexdigest()
            if st.button("Generate PNG", key="gen_png"):
                with st.spinner("Rendering PNG…"):
                    st.session_state["png"] = (sig, render_png(R, S["aspect"], png_px, transparent))
            cached = st.session_state.get("png")
            if cached and cached[0] == sig:
                dl("⬇️ Download PNG", cached[1], f"{stem}.png", "image/png", "dl_png")
            else:
                st.caption("Click generate, then download.")

        with st.container(border=True, key="card_batch"):
            st.markdown("#### 🗂️ Batch export (ZIP)")
            if len(images) < 2:
                st.caption("Upload two or more images to convert them all with the current settings.")
            else:
                fmts = st.multiselect("Formats", ["TXT", "HTML", "SVG", "ANSI", "PNG"],
                                      default=["TXT", "HTML"], key="batch_fmts")
                if st.button(f"Build ZIP for {len(images)} images", key="gen_zip", disabled=not fmts):
                    with st.spinner("Converting…"):
                        buf = io.BytesIO()
                        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                            used: set[str] = set()
                            for n, im in images:
                                base = Path(n).stem or "image"
                                while base in used:
                                    base += "_"
                                used.add(base)
                                for ext, data in make_exports(convert(im, S), S, fmts, png_px, transparent).items():
                                    z.writestr(f"{base}.{ext}", data)
                        st.session_state["zip"] = buf.getvalue()
                if st.session_state.get("zip"):
                    dl("⬇️ Download ZIP", st.session_state["zip"], "ascii_art_batch.zip", "application/zip", "dl_zip")

# ============================================================== FOOTER

st.markdown('<div class="footer">Built with Python · Pillow · NumPy · Streamlit</div>', unsafe_allow_html=True)