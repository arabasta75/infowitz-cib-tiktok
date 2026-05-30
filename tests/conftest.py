"""
Fixtures pytest. Tekkai n'a PAS de bypass localhost (auth propre) → on simule une
session admin via session_transaction dans les tests qui en ont besoin.
"""
import pytest  # noqa: F401
import app as tk_app  # noqa: F401  (l'import lance _startup_init : admin + init_db)
