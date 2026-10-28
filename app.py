import argparse
import io
import json
import math
import os
import re
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 / 2.0
except ImportError:
    import fitz  # PyMuPDF legacy fallback
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(
    title="Kaviaarder",
    description="Minimalist destructive PDF redaction tool (Rectangles & Polygons) powered by PyMuPDF",
    version="2.1.0",
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SESSIONS: Dict[str, dict] = {}
RESULTS: Dict[str, dict] = {}
SESSION_TTL_SECONDS = 3600 * 4  # 4 hours retention


def cleanup_old_entries() -> None:
    """Evict expired sessions and processed results from memory."""
    now = time.time()
    for store in (SESSIONS, RESULTS):
        expired = [k for k, v in store.items() if now - v.get("created_at", now) > SESSION_TTL_SECONDS]
        for k in expired:
            store.pop(k, None)


def hex_to_rgb_float(hex_color: str) -> Tuple[float, float, float]:
    """Convert HEX color (#RRGGBB) to 0.0-1.0 RGB tuple for PyMuPDF."""
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(c * 2 for c in cleaned)
    if len(cleaned) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", cleaned):
        return (1.0, 1.0, 1.0)
    return (
        int(cleaned[0:2], 16) / 255.0,
        int(cleaned[2:4], 16) / 255.0,
        int(cleaned[4:6], 16) / 255.0,
    )


def parse_page_scope(
    scope: str,
    custom_range: str,
    max_pages: int,
) -> List[int]:
    """
    Returns sorted 0-indexed page list based on targeting scope:
    - 'all'      : All document pages (1..N)
    - 'no_cover' : Pages 2..N (preserves page 1 / cover intact)
    - 'cover'    : Page 1 only
    - 'even'     : Even pages (2, 4, 6...)
    - 'odd'      : Odd pages (1, 3, 5...)
    - 'custom'   : Custom page range string (e.g. '1-60', '2-15, 20-30')
    """
    if scope == "all":
        return list(range(max_pages))
    if scope == "no_cover":
        return list(range(1, max_pages)) if max_pages > 1 else []
    if scope == "cover":
        return [0] if max_pages >= 1 else []
    if scope == "even":
        return [i for i in range(max_pages) if (i + 1) % 2 == 0]
    if scope == "odd":
        return [i for i in range(max_pages) if (i + 1) % 2 == 1]

    # Custom range mode
    cleaned = custom_range.strip().lower()
    if not cleaned or cleaned in ("all", "toutes", "*"):
        return list(range(max_pages))

    pages_set = set()
    for part in [p.strip() for p in cleaned.split(",") if p.strip()]:
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = int(bounds[0].strip())
                end_raw = bounds[1].strip()
                end = max_pages if end_raw in ("", "end", "fin", "n") else int(end_raw)
            except ValueError:
                raise ValueError(f"Invalid range format: '{part}'.")
            if start < 1 or end < 1 or start > end:
                raise ValueError(f"Invalid page bounds: '{part}' (document has {max_pages} pages).")
            if start > max_pages:
                continue
            for p in range(start, min(end, max_pages) + 1):
                pages_set.add(p - 1)
        else:
            try:
                p = int(part)
            except ValueError:
                raise ValueError(f"Invalid page number: '{part}'.")
            if 1 <= p <= max_pages:
                pages_set.add(p - 1)

    return sorted(pages_set)


def polygon_scanline_rects(
    points: List[Tuple[float, float]],
    step_pt: float = 2.0,
) -> List[fitz.Rect]:
    """
    Decompose arbitrary polygon (convex/concave) into horizontal strip rects
    to allow PyMuPDF destructive redactions to remove text and graphics strictly inside.
    """
    if len(points) < 3:
        return []

    ys = [p[1] for p in points]
    min_y = min(ys)
    max_y = max(ys)
    height = max_y - min_y
    if height <= 0.1:
        return []

    band_h = max(step_pt, height / 180.0)
    num_bands = max(1, int(math.ceil(height / band_h)))
    rects: List[fitz.Rect] = []
    n = len(points)

    for i in range(num_bands):
        y0 = min_y + i * band_h
        y1 = min(max_y, y0 + band_h)
        ym = 0.5 * (y0 + y1)

        x_intersections: List[float] = []
        for j in range(n):
            x_a, y_a = points[j]
            x_b, y_b = points[(j + 1) % n]
            if (y_a <= ym < y_b) or (y_b <= ym < y_a):
                t = (ym - y_a) / (y_b - y_a)
                x_intersections.append(x_a + t * (x_b - x_a))

        x_intersections.sort()
        for k in range(0, len(x_intersections) - 1, 2):
            rx0 = x_intersections[k]
            rx1 = x_intersections[k + 1]
            if rx1 - rx0 > 0.1:
                rects.append(fitz.Rect(rx0, y0, rx1, y1))

    return rects


class PointPt(BaseModel):
    x: float
    y: float


class RedactionMask(BaseModel):
    id: str = Field(default="")
    shape: Literal["rect", "polygon"] = Field(default="rect")
    # Rectangle parameters
    x_pt: float = Field(default=0.0)
    y_pt: float = Field(default=0.0)
    w_pt: float = Field(default=0.0)
    h_pt: float = Field(default=0.0)
    # Polygon parameters
    points_pt: List[PointPt] = Field(default_factory=list)
    # Color & scope
    fill_color: str = Field(default="#FFFFFF")
    scope: Literal["all", "no_cover", "cover", "even", "odd", "custom"] = Field(default="all")
    custom_range: str = Field(default="")
    ref_width_pt: Optional[float] = None
    ref_height_pt: Optional[float] = None


class PreviewRequest(BaseModel):
    session_id: str
    page_num: int = Field(default=1, ge=1)
    scale: float = Field(default=2.0, ge=0.5, le=4.0)
    masks: List[RedactionMask]


class BatchRedactRequest(BaseModel):
    session_id: str
    masks: List[RedactionMask]


def apply_masks_on_page(
    page: fitz.Page,
    masks: List[RedactionMask],
    default_ref_w: float,
    default_ref_h: float,
) -> Tuple[int, int]:
    """
    Applies real destructive PyMuPDF redaction for all masks targeting this page.
    Returns (chars_before, chars_after) inside masked areas.
    """
    if not masks:
        return (0, 0)

    page_rect = page.rect
    clip_rects: List[fitz.Rect] = []
    polygons_to_finish: List[Tuple[List[fitz.Point], Tuple[float, float, float]]] = []

    for m in masks:
        ref_w = m.ref_width_pt or default_ref_w
        ref_h = m.ref_height_pt or default_ref_h
        sx = (page_rect.width / ref_w) if ref_w > 0 else 1.0
        sy = (page_rect.height / ref_h) if ref_h > 0 else 1.0
        fill_rgb = hex_to_rgb_float(m.fill_color)

        if m.shape == "rect":
            if m.w_pt <= 0 or m.h_pt <= 0:
                continue
            rx0 = page_rect.x0 + m.x_pt * sx
            ry0 = page_rect.y0 + m.y_pt * sy
            rx1 = rx0 + m.w_pt * sx
            ry1 = ry0 + m.h_pt * sy
            r = fitz.Rect(rx0, ry0, rx1, ry1) & page_rect
            if not r.is_empty and not r.is_infinite:
                clip_rects.append(r)
                page.add_redact_annot(r, fill=fill_rgb)

        elif m.shape == "polygon" and len(m.points_pt) >= 3:
            scaled_pts: List[Tuple[float, float]] = [
                (page_rect.x0 + p.x * sx, page_rect.y0 + p.y * sy) for p in m.points_pt
            ]
            strips = polygon_scanline_rects(scaled_pts, step_pt=2.0)
            for sr in strips:
                clipped = sr & page_rect
                if not clipped.is_empty and not clipped.is_infinite:
                    clip_rects.append(clipped)
                    page.add_redact_annot(clipped, fill=fill_rgb)

            fitz_pts = [fitz.Point(px, py) for px, py in scaled_pts]
            polygons_to_finish.append((fitz_pts, fill_rgb))

    if not clip_rects:
        return (0, 0)

    # Count text characters prior to redaction
    chars_before = sum(len(page.get_text("text", clip=r).strip()) for r in clip_rects)

    # Permanent destructive redaction of text, vector line art, and images
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    # Vector anti-aliased polygon smoothing
    for fitz_pts, fill_rgb in polygons_to_finish:
        shape = page.new_shape()
        shape.draw_polyline(fitz_pts)
        shape.finish(fill=fill_rgb, color=fill_rgb, closePath=True, width=0.5)
        shape.commit()

    chars_after = sum(len(page.get_text("text", clip=r).strip()) for r in clip_rects)
    return (chars_before, chars_after)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="File static/index.html not found.")
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    cleanup_old_entries()

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please select a valid PDF file (.pdf).")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded PDF file is empty.")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF file: {exc}")

    if doc.is_encrypted:
        doc.close()
        raise HTTPException(
            status_code=400,
            detail="This PDF is password-protected/encrypted. Please decrypt it before uploading.",
        )

    if doc.page_count < 1:
        doc.close()
        raise HTTPException(status_code=400, detail="The PDF contains no pages.")

    first_rect = doc[0].rect
    width_pt = round(float(first_rect.width), 2)
    height_pt = round(float(first_rect.height), 2)
    page_count = doc.page_count
    doc.close()

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "filename": file.filename,
        "pdf_bytes": pdf_bytes,
        "file_size": len(pdf_bytes),
        "page_count": page_count,
        "width_pt": width_pt,
        "height_pt": height_pt,
        "created_at": time.time(),
    }

    return {
        "session_id": session_id,
        "filename": file.filename,
        "file_size": len(pdf_bytes),
        "page_count": page_count,
        "width_pt": width_pt,
        "height_pt": height_pt,
    }


@app.get("/api/page/{session_id}/{page_num}")
async def render_page(
    session_id: str,
    page_num: int,
    scale: float = Query(default=2.0, ge=0.5, le=4.0),
):
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session expired. Please re-upload the PDF.")

    doc = fitz.open(stream=session["pdf_bytes"], filetype="pdf")
    try:
        if page_num < 1 or page_num > doc.page_count:
            raise HTTPException(status_code=400, detail="Page number out of bounds.")

        page = doc[page_num - 1]
        page_rect = page.rect
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)

        headers = {
            "X-Page-Width-Pt": str(round(float(page_rect.width), 2)),
            "X-Page-Height-Pt": str(round(float(page_rect.height), 2)),
            "X-Pixmap-Width": str(pix.width),
            "X-Pixmap-Height": str(pix.height),
            "X-Scale": str(scale),
            "Cache-Control": "no-store",
        }
        return Response(content=pix.tobytes("png"), media_type="image/png", headers=headers)
    finally:
        doc.close()


@app.post("/api/preview")
async def preview_redacted_page(req: PreviewRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session expired.")

    max_pages = session["page_count"]
    target_idx = req.page_num - 1

    applicable_masks: List[RedactionMask] = []
    for m in req.masks:
        try:
            pages = parse_page_scope(m.scope, m.custom_range, max_pages)
            if target_idx in pages:
                applicable_masks.append(m)
        except ValueError:
            continue

    src_doc = fitz.open(stream=session["pdf_bytes"], filetype="pdf")
    try:
        preview_doc = fitz.open()
        preview_doc.insert_pdf(src_doc, from_page=target_idx, to_page=target_idx)
        page = preview_doc[0]

        chars_before, chars_after = apply_masks_on_page(
            page=page,
            masks=applicable_masks,
            default_ref_w=session["width_pt"],
            default_ref_h=session["height_pt"],
        )

        pix = page.get_pixmap(matrix=fitz.Matrix(req.scale, req.scale), alpha=False)
        png_bytes = pix.tobytes("png")
        preview_doc.close()

        headers = {
            "X-Removed-Chars": str(chars_before),
            "X-Remaining-Chars": str(chars_after),
            "X-Active-Masks": str(len(applicable_masks)),
            "Cache-Control": "no-store",
        }
        return Response(content=png_bytes, media_type="image/png", headers=headers)
    finally:
        src_doc.close()


@app.post("/api/redact-stream")
async def redact_pdf_stream(req: BatchRedactRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session expired.")

    if not req.masks:
        raise HTTPException(status_code=400, detail="No redaction masks defined.")

    max_pages = session["page_count"]
    page_to_masks: Dict[int, List[RedactionMask]] = {}

    try:
        for m in req.masks:
            target_indices = parse_page_scope(m.scope, m.custom_range, max_pages)
            for idx in target_indices:
                page_to_masks.setdefault(idx, []).append(m)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    if not page_to_masks:
        raise HTTPException(status_code=400, detail="No pages match the selected criteria.")

    sorted_pages = sorted(page_to_masks.keys())
    ref_w = session["width_pt"]
    ref_h = session["height_pt"]

    def event_generator():
        doc = fitz.open(stream=session["pdf_bytes"], filetype="pdf")
        try:
            total = len(sorted_pages)
            yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

            for step, page_index in enumerate(sorted_pages, start=1):
                page = doc[page_index]
                apply_masks_on_page(
                    page=page,
                    masks=page_to_masks[page_index],
                    default_ref_w=ref_w,
                    default_ref_h=ref_h,
                )

                if step == 1 or step == total or step % max(1, total // 40) == 0:
                    percent = round((step / total) * 90)
                    yield f"data: {json.dumps({'type': 'progress', 'current': step, 'total': total, 'page_num': page_index + 1, 'percent': percent})}\n\n"

            yield f"data: {json.dumps({'type': 'optimizing', 'percent': 95})}\n\n"

            output_buffer = io.BytesIO()
            doc.save(output_buffer, garbage=4, deflate=True)
            redacted_bytes = output_buffer.getvalue()

            download_id = str(uuid.uuid4())
            orig_stem = Path(session["filename"]).stem
            out_filename = f"{orig_stem}_redacted.pdf"

            RESULTS[download_id] = {
                "filename": out_filename,
                "pdf_bytes": redacted_bytes,
                "created_at": time.time(),
            }

            yield f"data: {json.dumps({'type': 'done', 'percent': 100, 'download_id': download_id, 'filename': out_filename, 'pages_redacted': total, 'total_pages': max_pages, 'original_size': session['file_size'], 'output_size': len(redacted_bytes)})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            doc.close()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/download/{download_id}")
async def download_redacted_pdf(download_id: str):
    result = RESULTS.get(download_id)
    if not result:
        raise HTTPException(status_code=404, detail="File expired or not found.")

    headers = {
        "Content-Disposition": f'attachment; filename="{result["filename"]}"',
        "Content-Length": str(len(result["pdf_bytes"])),
    }
    return Response(content=result["pdf_bytes"], media_type="application/pdf", headers=headers)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Kaviaarder — Destructive PDF Redaction Tool")
    parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open the web browser")
    parser.add_argument("--reload", action="store_true", help="Enable code auto-reloading")
    args = parser.parse_args()

    app_url = f"http://{args.host}:{args.port}"
    print(f"\n=======================================================")
    print(f"  Kaviaarder — Destructive PDF Redaction Tool")
    print(f"  Local Web App URL: {app_url}")
    print(f"=======================================================\n")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(app_url)).start()

    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)
