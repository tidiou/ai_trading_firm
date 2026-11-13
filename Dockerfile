# =====================================================================
# MBY-Trading — one image, two Railway services.
#
#   desk       cron every 15 min:  python -m orchestrator auto
#   dashboard  always on:          streamlit run dashboard/app.py ...
#
# The start command for each service lives in deploy/*.railway.json,
# not here, so both services are built from exactly the same code and
# can never drift apart.
#
# WHY python:3.10-slim — it matches the interpreter the desk has been
# running on (3.10.12 under pyenv). "slim" is Debian without compilers
# and docs: every dependency here ships a prebuilt wheel
# (psycopg2-binary, pandas, numpy), so nothing has to be compiled.
# =====================================================================
FROM python:3.10-slim

# PYTHONUNBUFFERED      print() reaches the Railway log immediately,
#                       instead of sitting in a buffer that is lost if
#                       the process dies.
# PYTHONDONTWRITEBYTECODE  no __pycache__ in a throwaway container.
# TZ                    the desk already thinks in ET through now_et();
#                       this makes any stray date.today() agree with it
#                       instead of silently using UTC.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/New_York

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, code second: Docker caches each step, so a code
# change re-uses the installed packages instead of reinstalling them.
# constraints.txt pins the versions the desk has actually been tested
# with — requirements.txt alone would install "whatever is newest".
COPY requirements.txt ./
COPY deploy/constraints.txt ./deploy/constraints.txt
RUN pip install --no-cache-dir -r requirements.txt -c deploy/constraints.txt

COPY . .

# Never run as root: if anything in the container is compromised it
# holds the Alpaca and Anthropic keys, and should hold nothing else.
RUN useradd --create-home --uid 1000 desk && chown -R desk:desk /app
USER desk

# Harmless default; each Railway service overrides it.
CMD ["python", "-m", "orchestrator", "status"]
