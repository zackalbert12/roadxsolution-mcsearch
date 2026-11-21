# Gunicorn settings for roadxsolution-mcsearch.
# Loaded automatically when the service starts with plain
# `gunicorn app:app` (Render dashboard start command) — this file is the
# single source of truth. Procfile and render.yaml use the same bare
# command so no CLI flag can silently override these values.
#
# workers MUST stay 1: scan stop/resume/status depend on the in-process
# RUNNING_SCANS dict (app.py):
#   - api_stop_scan (app.py:1399) reads RUNNING_SCANS only in the owning
#     process — a Stop landing on another worker would 400 with "No scan
#     is running." while the scan keeps running.
#   - api_status (app.py:1319) reports "running" from RUNNING_SCANS, so
#     a status poll on another worker would misreport a live scan as
#     stopped.
#   - mark_stale_scans_paused (app.py:505) marks a DB-'running' scan
#     Paused when the local process has no entry — on another worker it
#     would mark a LIVE scan Paused, and a Resume (app.py:1412, whose
#     already-running check is also local-only) could then start a
#     second scan thread over the same range.
# Making workers > 1 safe would need a DB-backed stop flag + heartbeat;
# that is a bigger change and is deliberately NOT done here.
#
# timeout is only a backstop: the hard fetch deadlines in safer.py cap
# a single lookup at ~20 s and an auto-mode search (USDOT + MC) at
# ~40 s. 120 s also covers a large parallel bulk search in the
# pathological all-hang case; a genuinely wedged worker still gets
# killed and replaced instead of stalling the service forever.
workers = 1
timeout = 120
