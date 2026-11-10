"""
ASCII Art Studio - advanced image -> ASCII converter (Streamlit)

Run:   streamlit run ascii_studio.py
Needs: pip install streamlit pillow numpy
Tip:   best look with Streamlit's dark theme
       (.streamlit/config.toml ->  [theme]  base = "dark")
"""
from __future__ import annotations

import hashlib
import html
import io
import time

import numpy as np
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

# ============================================================== PAGE CONFIG

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
    "Custom": "",
}

STYLES = [
    "Original Color",
    "Grayscale",
    "Matrix",
    "Amber CRT",
    "Duotone Gradient",
    "Ink on Paper",
]

STYLE_DEFAULT_BG = {
    "Original Color": "#0b0f1a",
    "Grayscale": "#0b0f1a",
    "Matrix": "#000000",
    "Amber CRT": "#140b00",
    "Duotone Gradient": "#0b0f1a",
    "Ink on Paper": "#fafaf7",
}
BG_EDITABLE = {"Original Color", "Grayscale", "Duotone Gradient"}

DUOTONE_PRESETS = {
    "Cyberpunk": ("#7c3aed", "#22d3ee"),
    "Sunset": ("#be185d", "#fde047"),
    "Ocean": ("#1d4ed8", "#a7f3d0"),
    "Fire": ("#7f1d1d", "#fbbf24"),
    "Custom": ("#6366f1", "#f472b6"),
}

# ============================================================== STYLING

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@500&display=swap');

.stApp {
    background:
        radial-gradient(1100px 560px at 8% -10%, #1e1b4b 0%, transparent 60%),
        radial-gradient(900px 480px at 100% 0%, #0c4a6e 0%, transparent 55%),
        #0b0f1a;
    color: #e2e8f0;
}
[data-testid="stSidebar"] {
    background: rgba(15, 20, 35, .92);
    border-right: 1px solid rgba(255,255,255,.06);
}
#MainMenu, footer { visibility: hidden; }

.hero { text-align: center; padding: 26px 0 6px; font-family: 'Inter', sans-serif; }
.hero .badge {
    display: inline-block; padding: 4px 14px; border-radius: 999px;
    font-size: 12px; font-weight: 600; letter-spacing: .16em; color: #7dd3fc;
    background: rgba(56,189,248,.10); border: 1px solid rgba(56,189,248,.30);
}
.hero-title {
    font-weight: 800; font-size: clamp(2.2rem, 5vw, 3.6rem); line-height: 1.1;
    margin: 14px 0 6px;
    background: linear-gradient(90deg, #38bdf8, #a78bfa 50%, #f472b6);
    -webkit-background-clip: text; background-clip: text; color: transparent;
}
.hero-ascii { font-family: 'JetBrains Mono', monospace; color: #475569; letter-spacing: .3em; font-size: 13px; }
.hero p { color: #94a3b8; font-size: 1.05rem; max-width: 660px; margin: 10px auto 0; }

.feat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; margin-top: 22px; font-family: 'Inter', sans-serif; }
.feat {
    background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
    border-radius: 16px; padding: 18px; transition: transform .2s, border-color .2s;
}
.feat:hover { transform: translateY(-3px); border-color: rgba(56,189,248,.55); }
.feat .ico { font-size: 26px; }
.feat h4 { margin: 8px 0 4px; color: #e2e8f0; font-size: 1rem; }
.feat p { margin: 0; color: #94a3b8; font-size: .88rem; }

[data-testid="stMetric"] {
    background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
    border-radius: 16px; padding: 14px 18px; backdrop-filter: blur(8px);
}
[data-testid="stMetricValue"] { color: #e2e8f0; }
[data-testid="stMetricLabel"] p { color: #94a3b8; }

.stButton button, .stDownloadButton button {
    background: linear-gradient(90deg, #2563eb, #7c3aed); color: #fff; border: 0;
    border-radius: 12px; font-weight: 600; padding: .55rem 1rem;
    transition: transform .15s, box-shadow .15s;
}
.stButton button:hover, .stDownloadButton button:hover {
    transform: translateY(-1px); box-shadow: 0 8px 24px rgba(124,58,237,.38); color: #fff;
}
.stTabs [data-baseweb="tab-list"] { gap: 6px; }
.stTabs [data-baseweb="tab"] { border-radius: 10px 10px 0 0; padding: 8px 16px; }
[data-testid="stFileUploaderDropzone"] {
    border: 2px dashed rgba(56,189,248,.45); background: rgba(56,189,248,.04); border-radius: 16px;
}
iframe { border-radius: 14px; border: 1px solid rgba(255,255,255,.08); }
.footer { text-align: center; color: #64748b; font-size: .85rem; padding: 18px 0 6px; font-family: 'Inter', sans-serif; }
</style>
"""

HERO_HTML = """
<div class="hero">
<div class="badge">STUDIO EDITION</div>
<div class="hero-title">ASCII Art Studio</div>
<div class="hero-ascii">░▒▓█ PIXELS → GLYPHS █▓▒░</div>
<p>Turn any photo into colour, neon or retro-terminal ASCII art. Tune it live, then export as TXT, HTML or PNG.</p>
</div>
"""

FEATURES_HTML = """
<div class="feat-grid">
<div class="feat"><div class="ico">🌈</div><h4>6 visual styles</h4><p>Original colour, grayscale, Matrix, Amber CRT, gradients and ink on paper.</p></div>
<div class="feat"><div class="ico">🔤</div><h4>7 character ramps</h4><p>Classic, detailed, blocks, binary, letters, minimal or your own custom ramp.</p></div>
<div class="feat"><div class="ico">🎛️</div><h4>Live image tuning</h4><p>Brightness, contrast, sharpness, saturation, auto-contrast and invert.</p></div>
<div class="feat"><div class="ico">📷</div><h4>Camera input</h4><p>Snap a photo from your webcam and convert it instantly.</p></div>
<div class="feat"><div class="ico">✨</div><h4>Glow &amp; CRT effects</h4><p>Neon glow, scanlines and an animated reveal in the HTML export.</p></div>
<div class="feat"><div class="ico">📦</div><h4>TXT · HTML · PNG</h4><p>Copy the text, save a standalone web page or render a high-res PNG.</p></div>
</div>
"""

st.markdown(CSS, unsafe_allow_html=True)

# ============================================================== HELPERS


def stretch(fn, *args, **kwargs):
    """Full-width widget that works on old and new Streamlit versions."""
    try:
        return fn(*args, width="stretch", **kwargs)
    except Exception:  # older Streamlit: no `width="stretch"`
        return fn(*args, use_container_width=True, **kwargs)


def show_html(doc: str, height: int):
    """Render an HTML document. `st.components.v1.html` is deprecated in
    recent Streamlit versions in favour of `st.iframe`; fall back for old ones.
    `doc` is built only from html-escaped content, so embedding it is safe."""
    if hasattr(st, "iframe"):
        return st.iframe(doc, height=int(height))
    return components.html(doc, height=int(height), scrolling=True)


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


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


def process_image(image, cols, aspect, brightness, contrast, sharpness, saturation, autoc):
    img = ImageOps.exif_transpose(image).convert("RGB")
    img.thumbnail((1600, 1600))
    if autoc:
        img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Brightness(img).enhance(brightness)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    img = ImageEnhance.Color(img).enhance(saturation)
    img = ImageEnhance.Sharpness(img).enhance(sharpness)
    rows = max(1, int(round(cols * img.height / img.width * aspect)))
    small = img.resize((cols, rows), Image.LANCZOS)
    return np.asarray(small, dtype=np.uint8), img


def make_colors(style, rgb, lum, dark, c1, c2):
    """Per-character colours as a uint8 array (rows, cols, 3)."""
    b = (lum / 255.0)[..., None]
    if style == "Original Color":
        col = rgb.astype(np.float32)
        col = 40 + col * (215 / 255) if dark else col * 0.85
    elif style == "Grayscale":
        col = np.repeat((45 + 210 * b) if dark else (15 + 150 * b), 3, axis=2)
    elif style == "Matrix":
        col = np.array([0, 255, 65]) * (0.3 + 0.7 * b)
    elif style == "Amber CRT":
        col = np.array([255, 176, 0]) * (0.3 + 0.7 * b)
    elif style == "Duotone Gradient":
        a, z = np.array(hex_to_rgb(c1), float), np.array(hex_to_rgb(c2), float)
        col = a + (z - a) * b
    else:  # Ink on Paper
        col = np.broadcast_to(np.array([24, 24, 30.0]), rgb.shape)
    col = np.clip(col, 0, 255).astype(np.uint8)
    # quantise a little -> far fewer <span>s in the HTML output
    return ((col >> 3) << 3) | 4


def _span(color, buf):
    body = "".join(buf)
    return body if color is None else f'<span style="color:{color}">{body}</span>'


def grid_to_html(grid, colors) -> str:
    esc = html.escape
    out = []
    for y in range(grid.shape[0]):
        parts, buf, run = [], [], None
        row, crow = grid[y], colors[y].tolist()
        for x, ch in enumerate(row):
            if ch == " ":
                buf.append(" ")
                continue
            r, g, b = crow[x]
            col = f"#{r:02x}{g:02x}{b:02x}"
            if col != run:
                if buf:
                    parts.append(_span(run, buf))
                buf, run = [], col
            buf.append(esc(ch))
        if buf:
            parts.append(_span(run, buf))
        out.append("".join(parts))
    return "\n".join(out)


def build_doc(pre_html, bg, font_px, aspect, glow, scan, *, full_page=False, animate=False, title="ASCII Art"):
    line_h = round(0.58 / aspect, 3)
    pre_cls = "glow" if glow else ""
    wrap_cls = "crt" if scan else ""
    anim = "animation:reveal 3s steps(80,end) both;" if (full_page and animate) else ""
    pad = 28 if full_page else 14
    footer = '<div class="foot">Generated with ASCII Art Studio</div>' if full_page else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{{margin:0;background:{bg};}}
body{{padding:{pad}px;}}
.wrap{{position:relative;display:inline-block;min-width:100%;box-sizing:border-box;}}
pre{{margin:0;font-family:'Cascadia Mono',Consolas,'DejaVu Sans Mono','Liberation Mono',Menlo,monospace;font-size:{font_px}px;line-height:{line_h}em;white-space:pre;color:#e5e7eb;{anim}}}
.glow,.glow span{{text-shadow:0 0 6px currentColor;}}
.crt::after{{content:"";position:absolute;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,rgba(0,0,0,.28) 0,rgba(0,0,0,.28) 1px,transparent 1px,transparent 3px);}}
@keyframes reveal{{from{{clip-path:inset(0 0 100% 0);}}to{{clip-path:inset(0 0 0 0);}}}}
.foot{{margin-top:22px;font:12px sans-serif;color:#64748b;text-align:center;}}
</style></head>
<body><div class="wrap {wrap_cls}"><pre class="{pre_cls}">{pre_html}</pre></div>{footer}</body></html>"""


def load_mono_font(size: int):
    for name in ("DejaVuSansMono.ttf", "CascadiaMono.ttf", "consola.ttf",
                 "Menlo.ttc", "LiberationMono-Regular.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def render_png(grid, colors, bg_rgb, aspect, font_px) -> bytes:
    font = load_mono_font(font_px)
    try:
        cw = font.getlength("M")
    except AttributeError:
        cw = font.getsize("M")[0]
    ch = cw / aspect
    rows, cols = grid.shape
    pad = int(cw * 2)
    img = Image.new("RGB", (int(cols * cw + pad * 2), int(rows * ch + pad * 2)), bg_rgb)
    draw = ImageDraw.Draw(img)
    for y in range(rows):
        py = pad + y * ch
        crow = colors[y].tolist()
        for x in range(cols):
            c = grid[y, x]
            if c != " ":
                draw.text((pad + x * cw, py), c, font=font, fill=tuple(crow[x]))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ============================================================== HEADER

st.markdown(HERO_HTML, unsafe_allow_html=True)

# ============================================================== SIDEBAR

with st.sidebar:
    st.markdown("## 🎨 Controls")

    with st.expander("📐 Resolution & shape", expanded=True):
        cols = st.slider("Columns (detail)", 30, 300, 120, 5,
                         help="More columns = more detail, but a bigger output.")
        aspect = st.slider("Character aspect", 0.35, 0.80, 0.50, 0.01,
                           help="Glyph width ÷ height. Raise it if the art looks stretched tall, lower it if it looks squashed.")

    with st.expander("🔤 Characters", expanded=True):
        cs_name = st.selectbox("Character set", list(CHARSETS))
        if cs_name == "Custom":
            ramp = st.text_input("Ramp (dense → sparse)", value="@#S%?*+;:,. ") or " "
        else:
            ramp = CHARSETS[cs_name]
        st.code(ramp, language=None)
        invert = st.checkbox("Invert brightness", value=False)

    with st.expander("🎛️ Image tuning"):
        autoc = st.checkbox("Auto contrast", value=True)
        brightness = st.slider("Brightness", 0.3, 2.0, 1.0, 0.05)
        contrast = st.slider("Contrast", 0.3, 2.5, 1.2, 0.05)
        saturation = st.slider("Saturation", 0.0, 2.5, 1.2, 0.05)
        sharpness = st.slider("Sharpness", 0.0, 3.0, 1.5, 0.1)

    with st.expander("🌈 Style", expanded=True):
        style = st.selectbox("Colour style", STYLES)
        if style in BG_EDITABLE:
            bg = st.color_picker("Background", STYLE_DEFAULT_BG[style], key=f"bg_{style}")
        else:
            bg = STYLE_DEFAULT_BG[style]
        c1, c2 = DUOTONE_PRESETS["Cyberpunk"]
        if style == "Duotone Gradient":
            preset = st.selectbox("Gradient preset", list(DUOTONE_PRESETS))
            c1, c2 = DUOTONE_PRESETS[preset]
            if preset == "Custom":
                c1 = st.color_picker("Shadows", c1, key="duo_c1")
                c2 = st.color_picker("Highlights", c2, key="duo_c2")

    with st.expander("🖥️ Display & FX"):
        font_px = st.slider("Preview font size (px)", 3, 14, 7)
        preview_h = st.slider("Preview height (px)", 300, 1000, 600, 50)
        glow = st.checkbox("Neon glow", value=False)
        scan = st.checkbox("CRT scanlines", value=False)

# ============================================================== SOURCE

tab_up, tab_cam = st.tabs(["📁 Upload image", "📷 Use camera"])

with tab_up:
    uploaded = st.file_uploader(
        "Upload an image",
        type=["jpg", "jpeg", "png", "webp", "bmp"],
        label_visibility="collapsed",
    )
    if st.button("✨ Load demo image"):
        st.session_state["demo"] = True

shot = None
with tab_cam:
    if st.checkbox("Enable camera"):
        shot = st.camera_input("Take a photo", label_visibility="collapsed")

image = None
try:
    if uploaded is not None:
        image = Image.open(uploaded)
    elif shot is not None:
        image = Image.open(shot)
    elif st.session_state.get("demo"):
        image = demo_image()
except Exception as exc:
    st.error(f"Could not read that image: {exc}")
    image = None

# ============================================================== MAIN

if image is None:
    st.info("👆 Upload an image, use your camera, or load the demo image to get started.")
    st.markdown(FEATURES_HTML, unsafe_allow_html=True)
else:
    t0 = time.perf_counter()

    rgb, tuned = process_image(image, cols, aspect, brightness, contrast,
                               sharpness, saturation, autoc)
    rows = rgb.shape[0]

    bg_rgb = hex_to_rgb(bg)
    dark = luminance(bg_rgb) < 128
    eff_ramp = ramp[::-1] if dark else ramp

    lum = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if invert:
        lum = 255 - lum
    idx = np.rint(lum / 255 * (len(eff_ramp) - 1)).astype(int)
    grid = np.array(list(eff_ramp))[idx]

    colors = make_colors(style, rgb, lum, dark, c1, c2)
    pre_html = grid_to_html(grid, colors)
    txt = "\n".join("".join(r).rstrip() for r in grid)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Grid", f"{cols} × {rows}")
    m2.metric("Characters", f"{cols * rows:,}")
    m3.metric("Text size", f"{len(txt.encode()) / 1024:.1f} KB")
    m4.metric("Render time", f"{elapsed_ms:.0f} ms")

    t_prev, t_cmp, t_txt, t_exp = st.tabs(
        ["✨ ASCII preview", "🔍 Compare", "📝 Plain text", "📦 Export"]
    )

    with t_prev:
        doc = build_doc(pre_html, bg, font_px, aspect, glow, scan)
        show_html(doc, preview_h)
        st.caption(
            f"Canvas ≈ {int(cols * font_px * 0.6)} × {int(rows * font_px * 0.58 / aspect)} px. "
            "Scroll inside the preview, or lower the font size / columns to fit."
        )

    with t_cmp:
        left, right = st.columns(2)
        with left:
            st.markdown("**Tuned input**")
            stretch(st.image, tuned)
        with right:
            st.markdown("**ASCII output**")
            show_html(doc, 480)
        with st.expander("📊 Brightness distribution"):
            hist, _ = np.histogram(lum, bins=32, range=(0, 255))
            st.bar_chart(hist)

    with t_txt:
        st.code(txt, language=None)

    with t_exp:
        e1, e2, e3 = st.columns(3)

        with e1:
            st.markdown("#### 📄 Text")
            st.caption("Plain monospace text file.")
            stretch(st.download_button, "⬇️ Download TXT", data=txt,
                    file_name="ascii_art.txt", mime="text/plain", key="dl_txt")

        with e2:
            st.markdown("#### 🌐 HTML")
            animate = st.checkbox("Reveal animation", value=False)
            full_doc = build_doc(pre_html, bg, font_px, aspect, glow, scan,
                                 full_page=True, animate=animate)
            stretch(st.download_button, "⬇️ Download HTML", data=full_doc,
                    file_name="ascii_art.html", mime="text/html", key="dl_html")

        with e3:
            st.markdown("#### 🖼️ PNG")
            png_px = st.slider("PNG font size", 8, 28, 14)
            sig = hashlib.md5(
                txt.encode() + colors.tobytes() + f"{bg}|{png_px}|{aspect}".encode()
            ).hexdigest()
            if st.button("Generate PNG", key="gen_png"):
                with st.spinner("Rendering PNG…"):
                    st.session_state["png"] = (sig, render_png(grid, colors, bg_rgb, aspect, png_px))
            cached = st.session_state.get("png")
            if cached and cached[0] == sig:
                stretch(st.download_button, "⬇️ Download PNG", data=cached[1],
                        file_name="ascii_art.png", mime="image/png", key="dl_png")
            else:
                st.caption("Click generate, then download.")

# ============================================================== FOOTER

st.markdown(
    '<div class="footer">Built with Python · Pillow · NumPy · Streamlit</div>',
    unsafe_allow_html=True,
)