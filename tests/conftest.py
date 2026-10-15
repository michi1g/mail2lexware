import os
import tempfile

# Muss vor dem Import des Pakets gesetzt sein, weil config den Pfad beim Import liest.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="w2l-test-")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _keine_postfaecher_zwischen_tests():
    """Postfächer sind globaler Zustand in der Test-Datenbank; jeder Test startet ohne."""
    from mail2lexware import db
    db.init()
    for account in db.list_accounts():
        db.delete_account(account["id"])
    yield
