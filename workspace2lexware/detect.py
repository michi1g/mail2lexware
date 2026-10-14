"""Einfache Erkennung, ob ein Anhang wie eine Rechnung aussieht (Textprüfung ohne OCR)."""
import io
import re

from pypdf import PdfReader

STRONG = [
    "rechnungsnummer", "rechnungsnr", "rechnung nr", "rechnungsdatum",
    "invoice number", "invoice no", "invoice date", "rechnung-nr",
]
MEDIUM = [
    "rechnung", "invoice", "iban", "mwst", "mehrwertsteuer", "umsatzsteuer", "ust-id", "ust.-id",
    "gesamtbetrag", "rechnungsbetrag", "zahlbetrag", "fälligkeit", "zahlungsziel", "nettobetrag",
    "bruttobetrag", "vat", "total amount",
]
NEGATIVE = ["allgemeine geschäftsbedingungen", "widerrufsbelehrung", "datenschutzerklärung", "newsletter"]
XML_MARKERS = ("crossindustryinvoice", "<invoice", "xrechnung", "urn:cen.eu:en16931")


def extract_text(data: bytes, ext: str, max_pages: int = 5) -> str:
    try:
        if ext == ".pdf":
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
        if ext == ".xml":
            return data[:200_000].decode("utf-8", errors="ignore")
    except Exception:
        pass  # kaputte oder verschlüsselte PDFs: nur Dateiname bewerten
    return ""


def score(filename: str, text: str, ext: str) -> int:
    """Höher = eher eine Rechnung. XRechnung/ZUGFeRD-XML zählt immer als eindeutig."""
    low = text.lower()
    if ext == ".xml" and any(m in low for m in XML_MARKERS):
        return 10
    pts = sum(2 for t in STRONG if t in low) + sum(1 for t in MEDIUM if t in low)
    pts -= sum(2 for t in NEGATIVE if t in low)
    if re.search(r"rechnung|invoice|beleg", filename, re.IGNORECASE):
        pts += 1
    return max(pts, 0)
