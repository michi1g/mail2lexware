import logging
import os

from waitress import serve

from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = create_app()
port = int(os.environ.get("PORT", "8080"))
logging.getLogger("mail2lexware").info("GUI läuft auf Port %s", port)
serve(app, host="0.0.0.0", port=port, threads=4)
