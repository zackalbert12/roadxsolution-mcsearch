web: gunicorn app:app
# NOTE: gunicorn settings live in gunicorn.conf.py (workers=1, timeout=120).
# Do NOT add --workers 2 here: scan stop/resume/status depend on the
# in-process RUNNING_SCANS dict (see gunicorn.conf.py), so a second
# worker would silently break Stop and misreport scan status.
