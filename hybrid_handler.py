import base64
import binascii
import os
import logging
from io import BytesIO
from pypdf import PdfReader, PdfWriter
from copy import copy

logger = logging.getLogger(__name__)

def normalize_template_page(source_page, target_width, target_height):
    """Scales a template page to fit the target dimensions only if needed."""
    source_width = float(source_page.mediabox.width)
    source_height = float(source_page.mediabox.height)
    if source_width <= 0 or source_height <= 0:
        return source_page
    # Only scale if differences are noticeable (prevents cumulative scaling issues)
    if abs(source_width - target_width) > 1.0 or abs(source_height - target_height) > 1.0:
        source_page.scale_to(target_width, target_height)
    return source_page

def process_hybrid_overlay(generated_stream, proposal_data):
    """
    Overlays generated content onto a specific master page from an uploaded PDF.
    Also prepends static prefix pages from the same PDF.
    """
    template_base64 = proposal_data.get("templatePDF")
    if not template_base64:
        generated_stream.seek(0)
        return generated_stream

    try:
        template_bytes = base64.b64decode(template_base64)
    except Exception as exc:
        logger.error(f"Failed to decode template for hybrid mode: {exc}")
        generated_stream.seek(0)
        return generated_stream

    generated_stream.seek(0)
    generated_reader = PdfReader(generated_stream)
    template_reader = PdfReader(BytesIO(template_bytes))
    
    if template_reader.is_encrypted:
        try: template_reader.decrypt("")
        except: pass
    
    writer = PdfWriter()
    available_template_pages = len(template_reader.pages)
    
    # Extract Configuration
    raw_prefix = proposal_data.get("templatePrefixPageCount", 0)
    try: template_prefix_count = int(float(raw_prefix))
    except: template_prefix_count = 0

    bg_idx_raw = proposal_data.get("backgroundPageIndex", 1)
    try: bg_idx = int(float(bg_idx_raw)) - 1
    except: bg_idx = 0
    if bg_idx < 0 or bg_idx >= available_template_pages:
        bg_idx = 0
        
    suffix_raw = proposal_data.get("templateSuffixStartPage", 0)
    try: suffix_start = (int(float(suffix_raw)) - 1) if int(float(suffix_raw)) > 0 else (bg_idx + 1)
    except: suffix_start = bg_idx + 1
    
    # Safely get the background page
    bg_page_original = template_reader.pages[bg_idx]
    
    # 1. Add requested prefix pages (Static)
    prefix_count = min(max(0, template_prefix_count), available_template_pages)
    for i in range(prefix_count):
        writer.add_page(template_reader.pages[i])

    # 2. Overlay generated content onto Master Background
    # Convert the PDF background block securely without shrinking dynamically generated data dimensions
    for gen_page in generated_reader.pages:
        new_page = writer.add_blank_page(
            width=float(gen_page.mediabox.width), 
            height=float(gen_page.mediabox.height)
        )
        new_page.merge_page(bg_page_original)
        new_page.merge_page(gen_page)
        
    # 3. Add remaining suffix pages (Static)
    suffix_start_safe = max(0, min(suffix_start, available_template_pages))
    for i in range(suffix_start_safe, available_template_pages):
        writer.add_page(template_reader.pages[i])
        
    merged_stream = BytesIO()
    writer.write(merged_stream)
    merged_stream.seek(0)
    
    return merged_stream
