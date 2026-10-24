#!/usr/bin/env python3
"""Загрузить документы из папки в админку: python scripts/seed.py ПОЛИПЛАСТИК/ [http://localhost:8003]"""
import base64
import mimetypes
import os
import sys
import uuid
import urllib.request
from pathlib import Path

SKIP = ("КЕЙС", ".xlsx", ".~lock")  # кейс хакатона и ТЗ — не регламенты компании


def post(url, path, auth):
    b = uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f'Content-Type: {mimetypes.guess_type(path.name)[0] or "application/octet-stream"}\r\n\r\n').encode()
    body += path.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(url + "/api/ingest/documents", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={b}",
        "Authorization": "Basic " + base64.b64encode(auth.encode()).decode()})
    return urllib.request.urlopen(req, timeout=300).status


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8003"
    auth = f"{os.getenv('ADMIN_USER', 'hr')}:{os.getenv('ADMIN_PASSWORD', 'admin')}"
    for f in sorted(folder.iterdir()):
        if f.is_file() and not any(s in f.name for s in SKIP):
            print(f.name, "->", post(url, f, auth))
