# DrillAI Backend

Well Engineering Intelligence Platform — core backend service (Python 3.11, FastAPI, SQLAlchemy 2.0).

Run everything from this directory with the project virtualenv:

```bash
./.venv/bin/python -m pytest        # tests
./.venv/bin/python -m ruff check .   # lint
./.venv/bin/python -m alembic upgrade head
```

Architecture documentation and ADRs are **not written yet**; the repository root `README.md`
describes the implemented scope and the known gaps.
