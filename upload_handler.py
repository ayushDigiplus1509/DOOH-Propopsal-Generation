import base64
import binascii
import logging
from io import BytesIO
from pypdf import PdfReader, PdfWriter

try:
    from hybrid_handler import process_hybrid_overlay
except ImportError:
    process_hybrid_overlay = None

logger = logging.getLogger(__name__)

def normalize_template_page(source_page, target_width, target_height):
    source_width = float(source_page.mediabox.width)
    source_height = float(source_page.mediabox.height)

    if source_width <= 0 or source_height <= 0:
        raise ValueError("Template page has invalid dimensions.")

    # We don't deepcopy, we just return the page, scaling will happen in the writer's context or on a copy if needed.
    # Actually, pypdf allows scaling the page object itself before adding to writer.
    source_page.scale_to(target_width, target_height)
    return source_page

def insert_template_pages(generated_stream, proposal_data):
    template_base64 = proposal_data.get("templatePDF")
    if not template_base64:
        generated_stream.seek(0)
        return generated_stream

    try:
        template_bytes = base64.b64decode(template_base64)
    except (binascii.Error, ValueError, Exception) as exc:
        logger.error(f"Failed to decode template base64: {exc}")
        generated_stream.seek(0)
        return generated_stream # Fallback to generated stream without template

    generated_stream.seek(0)
    generated_reader = PdfReader(generated_stream)
    template_reader = PdfReader(BytesIO(template_bytes))
    if template_reader.is_encrypted:
        try:
            template_reader.decrypt("")
        except Exception:
            logger.error("Template PDF is encrypted and could not be decrypted.")
            generated_stream.seek(0)
            return generated_stream

    # Robust value extraction
    raw_prefix = proposal_data.get("templatePrefixPageCount")
    try:
        template_prefix_count = int(float(raw_prefix)) if raw_prefix is not None else 0
    except (ValueError, TypeError):
        template_prefix_count = 0

    available_template_pages = len(template_reader.pages)
    
    # Hybrid Mode: Delegate to specialised handler
    if str(proposal_data.get("mode")).lower() == "hybrid" and process_hybrid_overlay:
        return process_hybrid_overlay(generated_stream, proposal_data)

    # Normal modes (create / upload): Original concatenation logic
    # NOTE: 'upload' mode is currently disabled from the UI (UPLOAD BASE FEATURE - COMMENTED OUT in SettingsPage.jsx)
    # This code path still handles 'create' mode and remains here for when 'upload' is re-enabled.
    writer = PdfWriter()
    if generated_reader.pages:
        target_width = float(generated_reader.pages[0].mediabox.width)
        target_height = float(generated_reader.pages[0].mediabox.height)
    elif template_reader.pages:
        target_width = float(template_reader.pages[0].mediabox.width)
        target_height = float(template_reader.pages[0].mediabox.height)
    else:
        target_width, target_height = 842, 595 # A4 Landscape fallback
    
    # 1. Add requested prefix pages from template
    prefix_count = min(max(0, template_prefix_count), available_template_pages)
    for i in range(prefix_count):
        writer.add_page(
            normalize_template_page(template_reader.pages[i], target_width, target_height)
        )
        
    # 2. Add all generated proposal pages
    for gen_page in generated_reader.pages:
        writer.add_page(gen_page)
        
    # 3. Add any remaining pages from template
    for i in range(prefix_count, available_template_pages):
        writer.add_page(
            normalize_template_page(template_reader.pages[i], target_width, target_height)
        )

    merged_stream = BytesIO()
    writer.write(merged_stream)
    merged_stream.seek(0)
    return merged_stream
