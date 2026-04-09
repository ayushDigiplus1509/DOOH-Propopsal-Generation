from io import BytesIO
from copy import deepcopy
from flask import Flask, request, send_file, jsonify
from fpdf import FPDF
import os
import requests
import re
from flask_cors import CORS
from datetime import datetime
import logging
import sys
import hashlib
import binascii
import base64
import tempfile
from io import BytesIO
from pypdf import PdfReader, PdfWriter
try:
    from PIL import Image
except ImportError:
    Image = None

logger = logging.getLogger(__name__)

# Import modular handlers
try:
    from upload_handler import insert_template_pages
except ImportError:
    logger.error("Could not import upload_handler. Ensure upload_handler.py exists.")
    insert_template_pages = None

app = Flask(__name__)
CORS(app)

# Environment configuration for Linux deployment
PORT = int(os.environ.get('PORT', 5000))
FLASK_ENV = os.environ.get('FLASK_ENV', 'production')
DEBUG = FLASK_ENV == 'development'
HOT_RELOAD = os.environ.get('FLASK_HOT_RELOAD', '1').lower() not in ('0', 'false', 'no')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_AWS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
TMP_ROOT = os.environ.get("PROPOSAL_TMP_DIR") or (
    os.path.join(tempfile.gettempdir(), "proposal_generation")
    if IS_AWS_LAMBDA
    else os.path.abspath(os.path.join(BASE_DIR, ".."))
)
UPLOADS_DIR = os.path.join(TMP_ROOT, "uploads")

# Lambda can only write under /tmp, so keep all generated assets there.
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Log startup information
logger.info(f"Starting Proposal PDF Service on port {PORT}")
logger.info(f"Environment: {FLASK_ENV}")
logger.info(f"AWS Lambda runtime: {IS_AWS_LAMBDA}")
logger.info(f"Uploads directory: {UPLOADS_DIR}")

class ProposalPDF(FPDF):
    def __init__(self, *args, **kwargs):
        self.background_color  = kwargs.pop("background_color",  (255, 248, 230))
        self.primary_color     = kwargs.pop("primary_color",     (0, 70, 127))
        self.font_color        = kwargs.pop("font_color",        (0, 0, 0))
        self.logo_path         = kwargs.pop("logo_path",         None)
        self.header_img_path   = kwargs.pop("header_img_path",   None)
        self.tagline           = kwargs.pop("tagline",           "")
        self.skip_background   = kwargs.pop("skip_background",   False)
        self.is_hybrid         = kwargs.pop("is_hybrid",         False)
        super().__init__(*args, **kwargs)

    def add_page(self, *args, **kwargs):
        super().add_page(*args, **kwargs)
        if not self.skip_background:
            self.set_fill_color(*self.background_color)
            self.rect(0, 0, self.w, self.h, "F")

    def header(self):
        # Show logo in top-right corner on every page except page 1
        if self.page_no() > 1 and self.logo_path:
            try:
                self.image(self.logo_path, x=self.w - 40, y=8, w=30)
            except Exception as e:
                logger.error(f"Failed to load header logo: {e}")

    def footer(self):
        # Footer intentionally left blank to remove page numbering
        pass

    def get_scale_factor(self):
        avail_w = self.w - self.l_margin - self.r_margin
        return avail_w / 277.0

    def chapter_title(self, label):
        sf = self.get_scale_factor()
        self.set_font("helvetica", "B", int(18 * sf))
        self.set_text_color(*self.font_color)
        self.multi_cell(0, 12 * sf, label, border=0, align="L", new_x="LMARGIN", new_y="NEXT")
        self.ln(2 * sf)

    def draw_line(self):
        sf = self.get_scale_factor()
        r, g, b = self.primary_color
        self.set_draw_color(r, g, b)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(5 * sf)

def _estimate_wrapped_lines(pdf: FPDF, text: str, w: float) -> int:
    """
    Roughly estimate how many lines MultiCell would wrap to for the current font.
    This is used only for page-break decisions (layout), not for rendering.
    """
    if not text:
        return 1
    # Normalize whitespace similar to how MultiCell treats it
    words = str(text).replace("\r", " ").split()
    if not words:
        return 1
    space_w = pdf.get_string_width(" ")
    lines = 1
    line_w = 0.0
    for word in words:
        ww = pdf.get_string_width(word)
        if line_w == 0:
            line_w = ww
            continue
        if line_w + space_w + ww <= w:
            line_w += space_w + ww
        else:
            lines += 1
            line_w = ww
    return max(1, lines)

def extract_coord_value(val):
    if isinstance(val, (int, float)): return float(val)
    if not val or str(val).strip() == "N/A": return None
    match = re.search(r"[-+]?\d*\.\d+|\d+", str(val))
    return float(match.group()) if match else None

def fetch_map_with_retry(url, headers, timeout=20):
    for attempt in range(2):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200 and len(response.content) > 500:
                return response.content
            logger.warning(f"Map fetch attempt {attempt+1} failed with status {response.status_code}")
        except Exception as e:
            logger.error(f"Map fetch attempt {attempt+1} exception: {e}")
    return None

def get_multi_marker_map(markers):
    processed = []
    for m in markers:
        lat = extract_coord_value(m.get('latitude'))
        lng = extract_coord_value(m.get('longitude'))
        if lat is not None and lng is not None:
            processed.append((lat, lng))
            
    if not processed:
        return None
        
    pt_parts = [f"{lng},{lat},pm2rdl" for lat, lng in processed]
    pt_str = "~".join(pt_parts)
    
    url = f"https://static-maps.yandex.ru/1.x/?l=map&pt={pt_str}&size=600,450&lang=en_RU"
    headers = {"User-Agent": "proposal-pdf-service/1.0"}
    
    marker_hash = hashlib.md5(f"{pt_str}_en_RU".encode()).hexdigest()
    map_name = f"multi_map_{marker_hash}_en.png"
    map_path = os.path.join(UPLOADS_DIR, map_name)
    
    if os.path.exists(map_path) and os.path.getsize(map_path) > 1000:
        return map_name

    content = fetch_map_with_retry(url, headers)
    if content:
        with open(map_path, "wb") as f:
            f.write(content)
        return map_name
        
    return None

def get_static_map_image(lat, lng):
    lat_val = extract_coord_value(lat)
    lng_val = extract_coord_value(lng)
    
    if lat_val is None or lng_val is None:
        return None
        
    headers = {"User-Agent": "proposal-pdf-service/1.0"}
    
    providers = [
        f"https://static-maps.yandex.ru/1.x/?ll={lng_val},{lat_val}&z=15&l=map&pt={lng_val},{lat_val},pm2rdl&size=450,450&lang=en_RU",
        f"https://maps.wikimedia.org/osm-intl/15/{lat_val}/{lng_val}/500x500.png",
        f"https://staticmap.openstreetmap.de/staticmap.php?center={lat_val},{lng_val}&zoom=15&size=500x500&maptype=mapnik&markers={lat_val},{lng_val},ol-marker"
    ]
    
    map_name = f"inv_map_{str(lat_val).replace('.','_')}_{str(lng_val).replace('.','_')}_en.png"
    map_path = os.path.join(UPLOADS_DIR, map_name)
    
    if os.path.exists(map_path) and os.path.getsize(map_path) > 1000:
        return map_name

    for i, url in enumerate(providers):
        content = fetch_map_with_retry(url, headers)
        if content:
            with open(map_path, "wb") as f:
                f.write(content)
            return map_name
            
    return None


def get_image_path(image_name):
    if not image_name:
        return None
    
    # If it's a full URL, we need to download it or handle it
    if image_name.startswith("http"):
        return image_name # FPDF can handle some URLs if configured, but downloading is safer
    
    # Check local uploads directory
    local_path = os.path.join(UPLOADS_DIR, image_name)
    if os.path.exists(local_path):
        return local_path
    
def parse_hex_color(color_value, fallback=(255, 248, 230)):
    try:
        if not color_value:
            return fallback

        # If it's already a list or tuple of 3 ints, use it directly
        if isinstance(color_value, (list, tuple)) and len(color_value) == 3:
            return tuple(int(c) for c in color_value)

        color = str(color_value).strip().lstrip("#")
        if len(color) == 3:
            color = "".join(ch * 2 for ch in color)

        if len(color) != 6:
            return fallback

        return tuple(int(color[idx:idx + 2], 16) for idx in (0, 2, 4))
    except Exception:
        return fallback

def blend_color(color, target=(255, 255, 255), amount=0.5):
    amount = max(0.0, min(1.0, float(amount)))
    return tuple(
        int(round((1 - amount) * base + amount * dest))
        for base, dest in zip(color, target)
    )


def is_temp_file(file_path):
    if not file_path:
        return False

    try:
        return os.path.commonpath([os.path.abspath(file_path), tempfile.gettempdir()]) == tempfile.gettempdir()
    except ValueError:
        return False


def decode_base64_image(b64_string, suffix=".png"):
    """
    Write a base64-encoded image (with or without data-URL prefix) to a
    temporary file and return its path.  Returns None on failure.
    """
    if not b64_string:
        return None
    try:
        # Strip data-URL prefix if present (e.g. "data:image/png;base64,")
        if "," in b64_string:
            b64_string = b64_string.split(",", 1)[1]
        img_bytes = base64.b64decode(b64_string)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(img_bytes)
        tmp.flush()
        tmp.close()
        return tmp.name
    except Exception as exc:
        logger.error(f"Failed to decode base64 image: {exc}")
        return None

def parse_page_range(page_range, total_pages):
    if total_pages <= 0:
        return []

    if not page_range or str(page_range).strip() == "":
        return [0]

    selected_pages = []
    seen_pages = set()

    for part in str(page_range).split(","):
        token = part.strip()
        if not token:
            continue

        if "-" in token:
            start_str, end_str = token.split("-", 1)
            start = int(start_str.strip())
            end = int(end_str.strip())
            step = 1 if start <= end else -1
            for page_num in range(start, end + step, step):
                page_index = page_num - 1
                if 0 <= page_index < total_pages and page_index not in seen_pages:
                    selected_pages.append(page_index)
                    seen_pages.add(page_index)
            continue

        page_index = int(token) - 1
        if 0 <= page_index < total_pages and page_index not in seen_pages:
            selected_pages.append(page_index)
            seen_pages.add(page_index)

    if not selected_pages:
        raise ValueError("No valid template pages were selected.")

    return selected_pages

def parse_insert_after_page(insert_after_page, total_pages):
    if insert_after_page is None or str(insert_after_page).strip() == "":
        return 1 if total_pages > 0 else 0

    try:
        insert_index = int(str(insert_after_page).strip())
    except ValueError as exc:
        raise ValueError("Insert position must be a whole number.") from exc

    if insert_index < 0:
        raise ValueError("Insert position cannot be negative.")

    if insert_index > total_pages:
        raise ValueError(f"Insert position cannot be greater than generated PDF page count ({total_pages}).")

    return insert_index


def create_pdf(proposal_data):
    # ── Resolve theme values from payload ────────────────────────────────
    background_color = parse_hex_color(
        proposal_data.get("themeColor") or proposal_data.get("theme_color") or proposal_data.get("defaultThemeColor"),
        fallback=(255, 248, 230)
    )
    primary_color = parse_hex_color(
        proposal_data.get("primaryColor") or proposal_data.get("primary_color") or proposal_data.get("defaultPrimaryColor"),
        fallback=(0, 70, 127)
    )
    font_color = parse_hex_color(
        proposal_data.get("fontColor") or proposal_data.get("font_color") or proposal_data.get("defaultFontColor"),
        fallback=(0, 0, 0)
    )
    table_bg_color = parse_hex_color(
        proposal_data.get("tableColor") or proposal_data.get("table_color"),
        fallback=primary_color
    )
    table_font_color = parse_hex_color(
        proposal_data.get("tableFontColor") or proposal_data.get("table_font_color"),
        fallback=(255, 255, 255)
    )
    muted_font_color = blend_color(font_color, amount=0.45)
    light_primary    = blend_color(primary_color, amount=0.75)
    soft_primary     = blend_color(primary_color, amount=0.35)
    tagline          = str(proposal_data.get("tagline") or "").strip()

    # ── Decode dynamic images from base64 (stored via Settings page) ─────
    logo_path       = decode_base64_image(proposal_data.get("logoBase64"),   suffix=".png")
    header_img_path = decode_base64_image(proposal_data.get("headerBase64"), suffix=".png")

    has_template_pdf = bool(proposal_data.get("templatePDF"))
    
    target_orientation = "L"
    custom_format = "A4"
    if has_template_pdf:
        try:
            template_bytes = base64.b64decode(proposal_data.get("templatePDF"))
            tr = PdfReader(BytesIO(template_bytes))
            
            # Determine which page is truly the background page
            bg_idx_raw = int(proposal_data.get("backgroundPageIndex", 1)) - 1
            bg_idx = max(0, min(bg_idx_raw, len(tr.pages) - 1)) if tr.pages else 0
            
            if tr.pages:
                pg = tr.pages[bg_idx]
                pw = float(pg.mediabox.width)
                ph = float(pg.mediabox.height)
                
                # 1 pt = 25.4 / 72 mm
                to_mm = 25.4 / 72.0
                w_mm = pw * to_mm
                h_mm = ph * to_mm
                
                # FPDF strictly expects format to be (smaller, larger) natively, and flips it if orientation="L" is passed.
                base_w = min(w_mm, h_mm)
                base_h = max(w_mm, h_mm)
                custom_format = (base_w, base_h)
                
                if pw < ph:
                    target_orientation = "P"
                else:
                    target_orientation = "L"
                    
        except Exception as e:
            logger.error(f"Failed to extract orientation from template: {e}")
    
    logger.info(f"Using Colors - Background: {background_color}, Primary: {primary_color}, Font: {font_color}")
    logger.info(f"Has uploaded template: {has_template_pdf}, Orientation: {target_orientation}, Format: {custom_format}")

    template_mode = str(proposal_data.get("mode", "create")).lower()
    is_hybrid = template_mode == "hybrid"

    pdf = ProposalPDF(
        orientation=target_orientation,
        unit="mm",
        format=custom_format,
        background_color=background_color,
        primary_color=primary_color,
        font_color=font_color,
        logo_path=logo_path,
        header_img_path=header_img_path,
        tagline=tagline,
        skip_background=is_hybrid,
        is_hybrid=is_hybrid
    )
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=15)

    if not has_template_pdf:
        pdf.add_page()

        # ── Header / Banner image ──────────────────────────────────────
        if header_img_path and os.path.exists(header_img_path):
            header_w = 180
            pdf.image(header_img_path, x=(pdf.w - header_w) / 2, y=10, w=header_w)
            pdf.set_y(54)
        else:
            pdf.ln(20)

        # ── Tagline ────────────────────────────────────────────────────
        if tagline:
            pdf.set_font("helvetica", "I", 14)
            pdf.set_text_color(*muted_font_color)
            pdf.cell(0, 10, tagline, align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(5)

        # ── Logo on cover ──────────────────────────────────────────────
        if logo_path:
            try:
                pdf.image(logo_path, x=(pdf.w - 35) / 2, y=pdf.get_y(), w=35)
                pdf.ln(35)
            except Exception as e:
                logger.error(f"Failed to load cover logo: {e}")
                pdf.ln(10)

        # ── Cover titles ───────────────────────────────────────────────
        pr, pg, pb = primary_color
        fr, fg, fb = font_color

        pdf.set_font("helvetica", "B", 30)
        pdf.set_text_color(pr, pg, pb)
        pdf.cell(0, 15, "BUSINESS PROPOSAL", new_x="LMARGIN", new_y="NEXT", align="C")

        pdf.set_font("helvetica", "", 20)
        pdf.set_text_color(*muted_font_color)
        campaign_name = proposal_data.get("campaignName", "Custom Campaign")
        pdf.cell(0, 12, campaign_name.upper(), new_x="LMARGIN", new_y="NEXT", align="C")

        pdf.ln(10)
        pdf.set_font("helvetica", "B", 16)
        pdf.set_text_color(fr, fg, fb)
        pdf.cell(0, 8, f"Client: {proposal_data.get('clientName', 'N/A')}", new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.set_font("helvetica", "", 14)
        pdf.cell(0, 8, f"Company: {proposal_data.get('companyName', 'N/A')}", new_x="LMARGIN", new_y="NEXT", align="C")

        pdf.ln(15)
        pdf.set_font("helvetica", "I", 12)
        pdf.set_text_color(*muted_font_color)
        pdf.cell(0, 8, f"Generated on: {datetime.now().strftime('%d %b %Y')}", new_x="LMARGIN", new_y="NEXT", align="C")

    # --- GEO MAPPING PAGE ---
    inventories = proposal_data.get("inventories", [])
    if inventories:
        pdf.add_page()
        pdf.chapter_title("Geo Mapping")
        pdf.draw_line()
        
        multi_map = get_multi_marker_map(inventories)
        if multi_map:
            map_path = get_image_path(multi_map)
            if map_path:
                try:
                    # Render a large map in the center, dynamically proportional to the active template size
                    avail_w = pdf.w - pdf.l_margin - pdf.r_margin
                    scale_factor = avail_w / 277.0
                    y_start = pdf.get_y() + (10 * scale_factor)
                    avail_h = pdf.h - y_start - pdf.b_margin - (10 * scale_factor)
                    
                    map_w = min(220 * scale_factor, avail_w - (10 * scale_factor))
                    map_h = map_w * (140.0 / 220.0)
                    
                    # If the naturally scaled aspect ratio clips into the bottom margin, scale it down proportionately
                    if map_h > avail_h:
                        map_h = avail_h
                        map_w = map_h * (220.0 / 140.0)

                    pdf.image(map_path, x=(pdf.w - map_w)/2, y=y_start, w=map_w, h=map_h)
                except Exception as e:
                    logger.error(f"Error rendering geo mapping image: {e}")
        else:
            pdf.ln(20)
            pdf.set_font("helvetica", "I", 12)
            pdf.set_text_color(*muted_font_color)
            pdf.cell(0, 10, "Map data not available for all locations.", align="C")
    
    # --- INVENTORY PAGES ---
    # Sort inventories by Space Type and City to ensure clean categorical grouping
    inventories = proposal_data.get("inventories", [])
    inventories.sort(key=lambda x: (str(x.get("spaceType", "")), str(x.get("city", ""))))
    
    current_group = None
    
    for inv in inventories:
        # Check if we need to show a Category Divider
        ctype = str(inv.get("spaceType", "Outdoor Media")).strip()
        ccity = str(inv.get("city", "India")).strip()
        group_key = (ctype, ccity)
        
        if group_key != current_group:
            logger.info(f"Adding Category Divider: {ctype} - {ccity}")
            pdf.add_page()
            
            # Disable auto page break strictly for the divider to prevent split text on short pages
            pdf.set_auto_page_break(False)
            
            # "Outdoor" Badge with rounded look
            badge_y = pdf.h * 0.15
            if not is_hybrid:
                pdf.set_fill_color(255, 255, 255)
                pdf.rect(0, badge_y, 120, 25, "F")
            pdf.set_xy(15, badge_y + 4)
            pdf.set_font("helvetica", "B", 30)
            pdf.set_text_color(*font_color if is_hybrid else primary_color)
            pdf.cell(100, 18, "Outdoor", align="L")
            
            # Category Name
            cat_y = pdf.h * 0.45
            pdf.set_xy(30, cat_y)
            pdf.set_font("helvetica", "B", 60)
            pdf.set_text_color(*font_color)
            pdf.cell(0, 25, ctype.upper(), new_x="LMARGIN", new_y="NEXT")
            
            # City Name
            pdf.set_x(30)
            pdf.set_font("helvetica", "B", 45)
            pdf.set_text_color(*soft_primary)
            pdf.cell(0, 25, ccity, new_x="LMARGIN", new_y="NEXT")
            
            # Re-enable auto page break for subsequent pages
            pdf.set_auto_page_break(True, margin=15)
            
            current_group = group_key

        # Prepare list of photos for this inventory
        photo_list = []
        if inv.get("mainPhoto"): photo_list.append((inv.get("mainPhoto"), "Main View"))
        if inv.get("longShot"): photo_list.append((inv.get("longShot"), "Long Shot"))
        if inv.get("closeShot"): photo_list.append((inv.get("closeShot"), "Close Shot"))
        if inv.get("nightPhoto"): photo_list.append((inv.get("nightPhoto"), "Night Image"))
        
        # If no photos, we still need one page for the inventory details
        if not photo_list:
            photo_list = [(None, "Overview")]

        for photo, photo_label in photo_list:
            pdf.add_page()
            
            # Title - keep it clean without the photo label
            pdf.chapter_title(inv.get('name', 'Unnamed'))
            pdf.draw_line()
            
            y_content_start = pdf.get_y() + 5
            
            avail_w = pdf.w - pdf.l_margin - pdf.r_margin
            scale_factor = avail_w / 277.0
            
            # Pre-calculate precise table height to guarantee images don't push the table off the bottom edge
            w_city, w_type, w_dims, w_facing, w_yield, w_foot, w_costs = (
                30 * scale_factor, 40 * scale_factor, 40 * scale_factor, 
                50 * scale_factor, 27 * scale_factor, 30 * scale_factor, 60 * scale_factor
            )
            
            row_font = (10 if pdf.h > pdf.w else 14) * scale_factor
            header_font = (10 if pdf.h > pdf.w else 13) * scale_factor
            
            city_text = str(inv.get("city", "N/A"))
            type_text = str(inv.get("spaceType", "N/A"))
            facing_text = str(inv.get("faciaTowards", "N/A"))
            dims_text = f"{inv.get('width', '0')} x {inv.get('height', '0')}"
            yield_text = str(inv.get("yield", "Moderate"))
            footfall_text = str(inv.get("footfall", "N/A"))
            cost_text = f"INR {inv.get('final_cost_gst', 0):,}"
            
            pad_x, line_h = 2 * scale_factor, 6 * scale_factor
            
            # We must set the font temporarily to allow precise dimension estimation
            pdf.set_font("helvetica", "", row_font)
            
            c_l = _estimate_wrapped_lines(pdf, city_text, w_city - (2 * pad_x) - (4 * scale_factor))
            f_l = _estimate_wrapped_lines(pdf, facing_text, w_facing - (2 * pad_x) - (4 * scale_factor))
            t_l = _estimate_wrapped_lines(pdf, type_text, w_type - (2 * pad_x) - (4 * scale_factor))
            d_l = _estimate_wrapped_lines(pdf, dims_text, w_dims - (2 * pad_x) - (4 * scale_factor))
            y_l = _estimate_wrapped_lines(pdf, yield_text, w_yield - (2 * pad_x) - (4 * scale_factor))
            ft_l = _estimate_wrapped_lines(pdf, footfall_text, w_foot - (2 * pad_x) - (4 * scale_factor))
            
            max_l = max(c_l, f_l, t_l, d_l, y_l, ft_l)
            r_h = max(10 * scale_factor, (max_l * line_h) + (4 * scale_factor))
            
            h_cost = 10 * scale_factor
            table_height_exact = h_cost + r_h + (10 * scale_factor) # Exact height + footer safety pad
            
            # Radically optimize image sizing logic: force image bounds to precisely accommodate the actual table
            # Adding extra 10mm safety to b_margin so it never touches the "Page 8/13" text
            remaining_h = pdf.h - y_content_start - pdf.b_margin - (10 * scale_factor)
            max_content_h = max(40 * scale_factor, remaining_h - table_height_exact)
            
            map_w = 110 * scale_factor
            map_h = min(90 * scale_factor, max_content_h)
            col_gap = 10 * scale_factor
            
            lat = inv.get("latitude")
            lng = inv.get("longitude")
            maps_url = None
            if lat and lng:
                try:
                    lat_str = extract_coord_value(lat)
                    lng_str = extract_coord_value(lng)
                    if lat_str and lng_str:
                        maps_url = f"https://www.google.com/maps?q={lat_str},{lng_str}"
                except: pass

            map_image = inv.get("mapImage") or inv.get("map")
            if not map_image and lat and lng:
                map_image = get_static_map_image(lat, lng)
            
            drawn_map_h = map_h
            map_rendered = False
            if map_image:
                map_path = get_image_path(map_image)
                if map_path:
                    try:
                        # Attempt to calculate true rendered height using aspect ratio
                        if Image:
                            try:
                                with Image.open(map_path) as im:
                                    iw, ih = im.size
                                    if iw > 0:
                                        calc_h = min(map_h, map_w * (ih / iw))
                                        drawn_map_h = calc_h
                            except: pass
                        pdf.image(map_path, x=pdf.l_margin, y=y_content_start, w=map_w, h=map_h, keep_aspect_ratio=True)
                        map_rendered = True
                    except: pass
            
            if not map_rendered:
                pdf.set_xy(pdf.l_margin, y_content_start)
                pdf.set_font("helvetica", "I", int(10 * scale_factor))
                pdf.set_text_color(*muted_font_color)
                pdf.cell(map_w, map_h, "Map not available", border=1, align="C")
                drawn_map_h = map_h
            
            # Map Link directly beneath the ACTUALLY drawn image bounds
            if maps_url:
                pdf.set_xy(pdf.l_margin, y_content_start + drawn_map_h + (3 * scale_factor))
                pdf.set_font("helvetica", "U", int(12 * scale_factor))
                pdf.set_text_color(*font_color if is_hybrid else primary_color)
                pdf.cell(map_w, 10 * scale_factor, "View on Google Maps", 0, align="C", link=maps_url)
                pdf.set_text_color(*font_color)
            
            # --- RIGHT: INVENTORY IMAGE ---
            img_x = pdf.l_margin + map_w + col_gap
            img_w = avail_w - map_w - col_gap
            img_h = min(95 * scale_factor, max_content_h)
            drawn_img_h = img_h
            
            if photo:
                img_path = get_image_path(photo)
                if img_path:
                    try:
                        if Image:
                            try:
                                with Image.open(img_path) as im:
                                    iw, ih = im.size
                                    if iw > 0:
                                        calc_h = min(img_h, img_w * (ih / iw))
                                        drawn_img_h = calc_h
                            except: pass
                        pdf.image(img_path, x=img_x, y=y_content_start, w=img_w, h=img_h, keep_aspect_ratio=True)
                        
                        # Add image label directly beneath the ACTUAL drawn image box
                        pdf.set_xy(img_x, y_content_start + drawn_img_h + (2 * scale_factor))
                        pdf.set_font("helvetica", "BI", int(14 * scale_factor))
                        pdf.set_text_color(*font_color)
                        pdf.cell(img_w, 10 * scale_factor, photo_label, align="C")
                    except Exception as e:
                        pdf.set_xy(img_x, y_content_start)
                        pdf.cell(img_w, img_h, "Image error", border=1, align="C")
                else:
                    pdf.set_xy(img_x, y_content_start)
                    pdf.cell(img_w, img_h, "Image not found", border=1, align="C")
            else:
                pdf.set_xy(img_x, y_content_start)
                pdf.set_font("helvetica", "I", int(12 * scale_factor))
                pdf.set_text_color(*muted_font_color)
                pdf.cell(img_w, img_h, "N/A", border=1, align="C")

            # Dynamically place table right below the TRUE drawn bottoms
            actual_content_bottom = y_content_start + max(drawn_map_h, drawn_img_h) + (15 * scale_factor)
            
            # Position table appropriately, ensuring it's never placed on top of images
            if pdf.h > pdf.w:
                table_y = max(actual_content_bottom, (pdf.h - 24) / 2)
            else:
                # Flow naturally below images, but apply a healthy standard gap
                table_y = actual_content_bottom
            
            pdf.set_y(table_y)
            
            # Temporarily disable auto page break so headers and rows are never split across pages
            pdf.set_auto_page_break(False)
            
            pdf.set_font("helvetica", "B", header_font)
            pdf.set_fill_color(*table_bg_color)
            pdf.set_text_color(*table_font_color)
            
            border_opt = 1 if not is_hybrid else 0 # No outside border in hybrid mode's cells if needed, but headers usually look ok
            pdf.cell(w_city, h_cost, "City", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
            pdf.cell(w_type, h_cost, "Space Type", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
            pdf.cell(w_dims, h_cost, "Dimensions", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
            pdf.cell(w_facing, h_cost, "Facing", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
            pdf.cell(w_yield, h_cost, "Yield", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
            pdf.cell(w_foot, h_cost, "Footfall", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
            pdf.cell(w_costs, h_cost, "Total Costs", border=1, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")
            
            pdf.set_text_color(*table_font_color)
            pdf.set_font("helvetica", "", row_font)
            
            y0 = pdf.get_y()
            x0 = pdf.l_margin
            
            # Use table background for row shading too, or a light blend if it's the header color
            pdf.set_fill_color(*blend_color(table_bg_color, amount=0.15))
            
            pdf.cell(w_city, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_type, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_dims, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_facing, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_yield, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_foot, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_costs, r_h, "", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
            
            pdf.set_xy(x0 + pad_x, y0 + (r_h - (c_l * line_h)) / 2)
            pdf.multi_cell(w_city - (2 * pad_x), line_h, city_text, border=0, align="C")
            pdf.set_xy(x0 + w_city + pad_x, y0 + (r_h - (t_l * line_h)) / 2)
            pdf.multi_cell(w_type - (2 * pad_x), line_h, type_text, border=0, align="C")
            pdf.set_xy(x0 + w_city + w_type + pad_x, y0 + (r_h - (d_l * line_h)) / 2)
            pdf.multi_cell(w_dims - (2 * pad_x), line_h, dims_text, border=0, align="C")
            pdf.set_xy(x0 + w_city + w_type + w_dims + pad_x, y0 + (r_h - (f_l * line_h)) / 2)
            pdf.multi_cell(w_facing - (2 * pad_x), line_h, facing_text, border=0, align="C")
            pdf.set_xy(x0 + w_city + w_type + w_dims + w_facing + pad_x, y0 + (r_h - (y_l * line_h)) / 2)
            pdf.multi_cell(w_yield - (2 * pad_x), line_h, yield_text, border=0, align="C")
            pdf.set_xy(x0 + w_city + w_type + w_dims + w_facing + w_yield + pad_x, y0 + (r_h - (ft_l * line_h)) / 2)
            pdf.multi_cell(w_foot - (2 * pad_x), line_h, footfall_text, border=0, align="C")
            pdf.set_xy(x0 + w_city + w_type + w_dims + w_facing + w_yield + w_foot, y0 + (r_h - line_h) / 2)
            pdf.cell(w_costs, line_h, cost_text, border=0, align="C")
            
            # Re-enable auto page break for the rest of the document
            pdf.set_auto_page_break(auto=True, margin=15)

    # --- SUMMARY PAGE ---
    if inventories:
        # Forcibly place the summary table onto a separate page
        pdf.add_page()
            
        pdf.chapter_title("Investment Summary")
        pdf.draw_line()
        
        avail_w = pdf.w - pdf.l_margin - pdf.r_margin
        scale_factor = avail_w / 277.0

        pdf.set_font("helvetica", "B", 13 * scale_factor)
        pdf.set_fill_color(*table_bg_color)
        pdf.set_text_color(*table_font_color)
        
        w_name, w_city, w_init, w_final = 120 * scale_factor, 50 * scale_factor, 50 * scale_factor, 57 * scale_factor
        
        h_sum = 12 * scale_factor
        pdf.cell(w_name, h_sum, "Inventory Name", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
        pdf.cell(w_city, h_sum, "City", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
        pdf.cell(w_init, h_sum, "Initial Cost", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
        pdf.cell(w_final, h_sum, "Final Cost (GST)", border=1, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")
        
        # Use table font color
        pdf.set_text_color(*table_font_color)
        total_initial, total_final = 0, 0
        def safe_num(v):
            if not v: return 0
            if isinstance(v, (int, float)): return v
            try: return float(str(v).replace(",", "").strip())
            except: return 0
            
        for _, inv in enumerate(inventories):
            name, city = str(inv.get("name", "N/A")), str(inv.get("city", "N/A"))
            initial, final = safe_num(inv.get("initial_cost", 0)), safe_num(inv.get("final_cost_gst", 0))
            total_initial += initial
            total_final += final
            
            pdf.set_font("helvetica", "", 12 * scale_factor)
            pad_x, line_h = 2 * scale_factor, 6 * scale_factor
            n_l = _estimate_wrapped_lines(pdf, name, w_name - (2 * pad_x) - (4 * scale_factor))
            c_l = _estimate_wrapped_lines(pdf, city, w_city - (2 * pad_x) - (4 * scale_factor))
            max_l = max(n_l, c_l)
            r_h = max(10 * scale_factor, (max_l * line_h) + (4 * scale_factor))
            
            y0, x0 = pdf.get_y(), pdf.l_margin
            if y0 + r_h > (pdf.h - pdf.b_margin):
                pdf.add_page()
                pdf.set_font("helvetica", "B", 13 * scale_factor)
                pdf.set_fill_color(*table_bg_color)
                pdf.set_text_color(*table_font_color)
                pdf.cell(w_name, h_sum, "Inventory Name", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
                pdf.cell(w_city, h_sum, "City", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
                pdf.cell(w_init, h_sum, "Initial Cost", border=1, new_x="RIGHT", new_y="TOP", align="C", fill=True)
                pdf.cell(w_final, h_sum, "Final Cost (GST)", border=1, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(*table_font_color)
                pdf.set_font("helvetica", "", 12 * scale_factor)
                y0 = pdf.get_y()
            
            str_init = f"INR {int(initial):,}" if initial else "INR 0"
            str_final = f"INR {int(final):,}" if final else "INR 0"
            
            # Alternating row background
            if _ % 2 == 1:
                pdf.set_fill_color(*blend_color(table_bg_color, amount=0.15))
            else:
                pdf.set_fill_color(*background_color)

            pdf.cell(w_name, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_city, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_init, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            pdf.cell(w_final, r_h, "", border=1, new_x="RIGHT", new_y="TOP", fill=True)
            
            pdf.set_xy(x0 + pad_x, y0 + (r_h - (n_l * line_h)) / 2)
            pdf.multi_cell(w_name - (2 * pad_x), line_h, name, border=0, align="L")
            pdf.set_xy(x0 + w_name + pad_x, y0 + (r_h - (c_l * line_h)) / 2)
            pdf.multi_cell(w_city - (2 * pad_x), line_h, city, border=0, align="C")
            pdf.set_xy(x0 + w_name + w_city, y0)
            pdf.cell(w_init - pad_x, r_h, str_init, border=0, align="R", new_x="RIGHT", new_y="TOP")
            pdf.set_xy(x0 + w_name + w_city + w_init, y0)
            pdf.cell(w_final - pad_x, r_h, str_final, border=0, align="R", new_x="RIGHT", new_y="TOP")
            pdf.set_xy(x0, y0 + r_h)
            
        total_label_w = w_name + w_city
        total_value_w = w_init + w_final
        total_fill = blend_color(table_bg_color, amount=0.25)

        pdf.set_font("helvetica", "B", 13 * scale_factor)
        pdf.set_text_color(*table_font_color)
        pdf.set_fill_color(*blend_color(table_bg_color, amount=0.15))
        pdf.cell(total_label_w, h_sum, "TOTAL INVENTORIES", border=1, new_x="RIGHT", new_y="TOP", align="R", fill=True)
        pdf.set_fill_color(*total_fill)
        pdf.cell(total_value_w, h_sum, str(len(inventories)), border=1, align="R", fill=True, new_x="LMARGIN", new_y="NEXT")

        pdf.set_fill_color(*blend_color(table_bg_color, amount=0.15))
        pdf.cell(total_label_w, h_sum, "TOTAL INITIAL COST", border=1, new_x="RIGHT", new_y="TOP", align="R", fill=True)
        pdf.set_fill_color(*total_fill)
        pdf.cell(total_value_w, h_sum, f"INR {int(total_initial):,}", border=1, align="R", fill=True, new_x="LMARGIN", new_y="NEXT")

        pdf.set_fill_color(*blend_color(table_bg_color, amount=0.15))
        pdf.cell(total_label_w, h_sum, "TOTAL COST", border=1, new_x="RIGHT", new_y="TOP", align="R", fill=True)
        pdf.set_fill_color(*total_fill)
        pdf.cell(total_value_w, h_sum, f"INR {int(total_final):,}", border=1, align="R", fill=True, new_x="LMARGIN", new_y="NEXT")

    pdf_output = BytesIO()
    content = pdf.output()
    pdf_output.write(content)
    pdf_output.seek(0)

    # Cleanup temp image files written during this request
    for tmp_path in filter(None, [logo_path, header_img_path]):
        # Only delete paths inside the system temp dir (not static files)
        if is_temp_file(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    if insert_template_pages:
        return insert_template_pages(pdf_output, proposal_data)
    else:
        logger.error("insert_template_pages is not available. upload_handler might be missing.")
        return pdf_output

@app.route("/generate-pdf", methods=["POST"])
def generate_pdf():
    logger.info("Received PDF generation request")
    data = request.get_json(silent=True) or {}
    logger.info(f"Full Data Keys: {list(data.keys())}")
    logger.info(f"Color Inputs - themeColor: {data.get('themeColor')}, primaryColor: {data.get('primaryColor')}, fontColor: {data.get('fontColor')}")
    inventories = data.get("inventories", [])
    
    if not inventories:
        logger.error("No inventories provided in request")
        return jsonify({"error": "No inventories provided"}), 400
    
    logger.info(f"Generating PDF for {len(inventories)} inventories")
    
    # Enrich data with proposal level info if needed
    # The frontend is sending { inventories: [...] }
    # but we can also expect campaignName, clientName, etc.
    # For now, let's extract them from the first inventory if present, or just use what's passed
    
    try:
        pdf_stream = create_pdf(data)
        campaign_name = str(data.get("campaignName", "custom_proposal"))
        filename = campaign_name.replace(" ", "_") + ".pdf"
        
        logger.info(f"PDF generated successfully: {filename}")
        
        return send_file(
            pdf_stream,
            as_attachment=True,
            download_name=filename,
            mimetype="application/pdf"
        )
    except ValueError as e:
        logger.error(f"Invalid PDF generation request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("Error generating PDF")
        return jsonify({"error": "Failed to generate PDF"}), 500
    finally:
        # Cleanup: removes map images to save space and keep it clean as requested
        try:
            for filename in os.listdir(UPLOADS_DIR):
                if filename.startswith(("inv_map_", "multi_map_")) and filename.endswith(".png"):
                    file_path = os.path.join(UPLOADS_DIR, filename)
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        # logger.info(f"Deleted temporary map: {filename}")
                    except Exception as cleanup_err:
                        logger.warning(f"Failed to delete {filename}: {cleanup_err}")
        except Exception as dir_err:
            logger.error(f"Error accessing uploads directory for cleanup: {dir_err}")

@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint for monitoring"""
    return jsonify({
        "status": "healthy",
        "service": "Proposal PDF Generation Service",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat()
    })

application = app

if __name__ == "__main__":
    logger.info(f"Starting Flask app on port {PORT} in {FLASK_ENV} mode")
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=DEBUG or HOT_RELOAD,
        use_reloader=HOT_RELOAD
    )
