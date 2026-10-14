import os
import tempfile

# Muss vor dem Import des Pakets gesetzt sein, weil config den Pfad beim Import liest.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="w2l-test-")
