"""PDF-печать ШК ячеек: 58x40 и 120x75 мм (Scope IN п.3).

Свои этикетки рендерит сервер — на клиенте только предпросмотр (см. DEV-PLAN.md,
раздел «Печать этикеток»). Здесь же выполняется полная печать в PDF.
"""

import io

import barcode
from barcode.writer import ImageWriter
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas as pdf_canvas

from fulfil.models.storage import Cell

_SIZES_MM: dict[str, tuple[float, float]] = {
    "58x40": (58.0, 40.0),
    "120x75": (120.0, 75.0),
}


def _barcode_png(value: str) -> bytes:
    buf = io.BytesIO()
    code128 = barcode.get("code128", value, writer=ImageWriter())
    code128.write(buf, options={"write_text": False, "quiet_zone": 1, "module_height": 10})
    return buf.getvalue()


def render_cell_labels_pdf(cells: list[Cell], size: str = "58x40") -> bytes:
    if size not in _SIZES_MM:
        size = "58x40"
    width_mm, height_mm = _SIZES_MM[size]
    width, height = width_mm * mm, height_mm * mm

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(width, height))

    for cell in cells:
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(width / 2, height - 10, cell.address)

        png_bytes = _barcode_png(cell.address)
        img = io.BytesIO(png_bytes)
        from reportlab.lib.utils import ImageReader

        img_reader = ImageReader(img)
        bc_width = width * 0.85
        bc_height = height * 0.35
        c.drawImage(
            img_reader,
            (width - bc_width) / 2,
            height * 0.35,
            width=bc_width,
            height=bc_height,
            preserveAspectRatio=True,
            mask="auto",
        )

        c.setFont("Helvetica", 8)
        c.drawCentredString(width / 2, height * 0.15, cell.address)

        c.showPage()
        c.setPageSize((width, height))

    c.save()
    return buf.getvalue()
