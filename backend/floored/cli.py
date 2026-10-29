"""Command line: ``floored serve``, ``floored crawl <url>``, ``floored convert <image>``."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="floored")
    sub = ap.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the API (and the built UI)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    crawl = sub.add_parser("crawl", help="crawl a site for floor plans and queue them for review")
    crawl.add_argument("url", nargs="+")
    crawl.add_argument("--name")
    crawl.add_argument("--hint", help="location appended when geocoding, e.g. 'Madison, WI'")
    crawl.add_argument("--max-pages", type=int, default=200)
    crawl.add_argument("--depth", type=int, default=3)

    conv = sub.add_parser("convert", help="run the converter on an image and print the rooms as JSON")
    conv.add_argument("image")
    conv.add_argument("--converter")

    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("floored.api.app:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.cmd == "convert":
        from .converters import get_converter

        with open(args.image, "rb") as fh:
            result = get_converter(args.converter).convert(fh.read())
        json.dump(result.to_dict(), sys.stdout, indent=1)
        print()
        return 0

    if args.cmd == "crawl":
        from urllib.parse import urlsplit

        from . import jobs, pipeline  # noqa: F401  (registers job handlers)
        from .db import init_db, session_scope
        from .models import Job, Source

        init_db()
        jobs.synchronous = True
        with session_scope() as s:
            src = Source(
                name=args.name or urlsplit(args.url[0]).netloc,
                seed_urls=args.url,
                allowed_domains=[],
                location_hint=args.hint,
                max_pages=args.max_pages,
                max_depth=args.depth,
            )
            s.add(src)
            s.flush()
            source_id = src.id
        job_id = jobs.submit("crawl", {"source_id": source_id})
        with session_scope() as s:
            job = s.get(Job, job_id)
            print(json.dumps({"status": job.status, "message": job.message, "result": job.result}, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
