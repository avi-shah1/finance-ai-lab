"""Entry point for the production web server (gunicorn), used on Railway.

    gunicorn wsgi:app --workers 1 --threads 8

One worker process on purpose: searches and deep research run as
background threads inside the app's process, and jobs.py tracks what is
running there. Threads let that one process serve several page requests
at once. Locally, keep using `python app.py`.
"""

import jobs
from app import app

# Background jobs die with the process: after a restart or redeploy, mark any
# search / deep research that was mid-run as interrupted so it can be re-run.
jobs.fail_interrupted()

__all__ = ["app"]
