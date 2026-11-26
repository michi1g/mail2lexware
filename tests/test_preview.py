import hashlib
import io

import pypdfium2 as pdfium
import pytest
from PIL import Image

from mail2lexware import config, db
from mail2lexware.web import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app(start_scheduler=False)
    app.config["TESTING"] = True
    return app.test_client()


def make_pdf(pages):
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(595, 842)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()


def add_file(name, data):
    sha = hashlib.sha256(data + name.encode()).hexdigest()
    config.FILES_DIR.mkdir(parents=True, exist_ok=True)
    (config.FILES_DIR / (sha + "." + name.rsplit(".", 1)[-1])).write_bytes(data)
    return db.add_document(msg_key="prev-" + sha, message_id="prev-" + sha, sha256=sha, filename=name,
                           mime_type="application/octet-stream", size=len(data), status="pending")


def test_pdf_pages_and_render(client):
    doc_id = add_file("zwei.pdf", make_pdf(2))
    r = client.get(f"/file/{doc_id}/pages")
    assert r.status_code == 200 and r.get_json() == {"pages": 2, "shown": 2}

    r = client.get(f"/file/{doc_id}/page/1")
    assert r.status_code == 200 and r.mimetype == "image/webp"
    img = Image.open(io.BytesIO(r.data))
    assert img.width == 1400 and img.height > img.width  # Hochformat, auf Zielbreite skaliert

    assert client.get(f"/file/{doc_id}/page/2").status_code == 404


def test_broken_pdf_and_non_pdf(client):
    broken = add_file("kaputt.pdf", b"%PDF-1.4 kein echtes pdf")
    assert client.get(f"/file/{broken}/pages").status_code == 422
    assert client.get(f"/file/{broken}/page/0").status_code == 404

    xml = add_file("rechnung.xml", b"<x/>")
    assert client.get(f"/file/{xml}/pages").status_code == 404
    assert client.get("/file/999999/pages").status_code == 404
