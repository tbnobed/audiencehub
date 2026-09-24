"""CSV import API and processing helpers.

API integration: ``app.main`` must include ``app.imports.api.router`` with
``app.include_router(router)``.

Worker integration: register the ``import.run`` job and call
``run_import(int(payload["import_id"]), progress_callback=callback)`` from the
worker. The callback receives ``{"done", "total", "message"}``; persist it to
the job progress and heartbeat. A worker-owned SQLAlchemy Session can be passed
as ``db=...``. The handler must mark the job succeeded/failed around this call.
"""

from app.imports.service import run_import

__all__ = ["run_import"]