"""PDF-Seiten serverseitig als Bilder rendern.

Der eingebaute PDF-Viewer des Browsers steht in eingebetteten Fenstern nicht überall zur Verfügung
(z. B. wenn Chrome PDFs herunterladen statt öffnen soll, oder auf Mobilgeräten). Bilder gehen immer.
"""
import io
import threading
from functools import lru_cache
from pathlib import Path

import pypdfium2 as pdfium

MAX_PAGES = 30          # mehr Seiten zeigt die Vorschau nicht, der Rest ist im neuen Tab erreichbar
TARGET_WIDTH = 1400     # Pixel; scharf genug für Retina in einer ~460px breiten Spalte
MAX_SCALE = 4.0

# PDFium ist nicht threadsicher, Waitress bedient Anfragen aber parallel.
_lock = threading.Lock()


class PreviewError(Exception):
    pass


def page_count(path: Path) -> int:
    with _lock:
        try:
            pdf = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as e:
            raise PreviewError(str(e)) from e
        try:
            return len(pdf)
        finally:
            pdf.close()


# Dateien sind nach SHA-256 benannt, ihr Inhalt ändert sich also nie: Pfad + Seite reichen als Schlüssel.
@lru_cache(maxsize=48)
def render_page(path: Path, index: int) -> bytes:
    """Seite `index` (0-basiert) als WebP."""
    with _lock:
        try:
            pdf = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as e:
            raise PreviewError(str(e)) from e
        try:
            if not 0 <= index < min(len(pdf), MAX_PAGES):
                raise PreviewError("Seite nicht vorhanden")
            page = pdf[index]
            width = page.get_width() or 1
            image = page.render(scale=min(TARGET_WIDTH / width, MAX_SCALE)).to_pil()
            page.close()
        finally:
            pdf.close()
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "WEBP", quality=82, method=4)
    return buf.getvalue()
