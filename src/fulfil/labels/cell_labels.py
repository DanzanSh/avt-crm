"""PDF-печать ШК ячеек: 58x40 и 120x75 мм (Scope IN п.3).

Свои этикетки рендерит сервер — на клиенте только предпросмотр (см. DEV-PLAN.md,
раздел «Печать этикеток»). Здесь же выполняется полная печать в PDF.
"""

import io

import barcode
from barcode.writer import ImageWriter
from reportlab.lib.pagesizes import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdf_canvas

from fulfil.models.storage import Cell

_SIZES_MM: dict[str, tuple[float, float]] = {
    "58x40": (58.0, 40.0),
    "120x75": (120.0, 75.0),
}

# Было Helvetica-Bold 10pt на 58×40 — заказчик не мог прочитать длинный адрес
# вида B1-12-4-10 (п.1.1). Базовый кегль поднят до ~18pt на 58×40 и масштабируется
# пропорционально ширине этикетки для 120×75; при этом автоподгонка через
# stringWidth не даёт адресу вылезти за поля, если он всё же длиннее обычного.
_ADDRESS_BASE_FONT = 18.0
_ADDRESS_MIN_FONT = 8.0
_ADDRESS_SIDE_MARGIN_MM = 4.0


def _barcode_png(value: str) -> bytes:
    buf = io.BytesIO()
    code128 = barcode.get("code128", value, writer=ImageWriter())
    code128.write(buf, options={"write_text": False, "quiet_zone": 1, "module_height": 10})
    return buf.getvalue()


def _fit_font_size(text: str, max_width: float, base_size: float, font: str = "Helvetica-Bold") -> float:
    """Подбирает наибольший кегль не выше `base_size`, при котором `text` умещается
    в `max_width` пунктов — чтобы длинный адрес не вылезал за край этикетки."""
    size = base_size
    while size > _ADDRESS_MIN_FONT and stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def render_cell_labels_pdf(cells: list[Cell], size: str = "58x40") -> bytes:
    if size not in _SIZES_MM:
        size = "58x40"
    width_mm, height_mm = _SIZES_MM[size]
    width, height = width_mm * mm, height_mm * mm

    # Базовый кегль адреса масштабируем от ширины этикетки: 58 мм — эталонные 18pt,
    # 120 мм — пропорционально крупнее, иначе адрес теряется на большой этикетке.
    base_font = _ADDRESS_BASE_FONT * (width_mm / _SIZES_MM["58x40"][0])
    max_text_width = width - _ADDRESS_SIDE_MARGIN_MM * 2 * mm

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(width, height))

    for cell in cells:
        font_size = _fit_font_size(cell.address, max_text_width, base_font)
        c.setFont("Helvetica-Bold", font_size)
        # Отступ сверху зависит от подобранного кегля — так текст не наезжает
        # на штрихкод ниже, даже когда кегль пришлось уменьшить.
        c.drawCentredString(width / 2, height - font_size - 4, cell.address)

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
