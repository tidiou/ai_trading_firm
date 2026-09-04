"""
Shared pytest setup.

WHY THIS FILE EXISTS AT ALL: every agent module reaches out to the
environment at IMPORT time — core/db.py does `os.environ["DATABASE_URL"]`
and builds an engine, core/clients/claude_client.py does
`os.environ["ANTHROPIC_API_KEY"]` and constructs an Anthropic client,
and the Alpaca and FMP clients do the same with their keys. So
`import agents.nora` fails on a machine without a full .env, which is
a large part of why this test suite was nine empty placeholder files.

Nothing here connects to anything. SQLAlchemy's create_engine is lazy
(it does not dial the database until a connection is requested) and
the Anthropic client only makes a request when called, so dummy values
are enough to get the modules imported. The tests below then exercise
the pure decision functions directly, with hand-built ORM objects and
no session at all.

Longer term the cleaner fix is lazy initialisation — a get_engine()
and a get_client() called at use time rather than module level — which
would make every agent importable anywhere. Noted, not done here: this
change set is about the risk limits, and quietly restructuring module
initialisation alongside it would make the diff much harder to review.
"""

import os
import sys
from pathlib import Path

# Project root on the path, so `import agents.nora` works when pytest
# is run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import-time environment. Deliberately obviously fake — if any of
# these ever reached a real service, the failure should be loud and
# unmistakable rather than quietly hitting something real.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost:1/test_never_connected")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
os.environ.setdefault("ALPACA_API_KEY", "test-not-a-real-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-not-a-real-key")
os.environ.setdefault("ALPACA_PAPER", "true")
os.environ.setdefault("FMP_API_KEY", "test-not-a-real-key")