# Backend quick reference

The application is `main:app`. Run commands from this directory so the default
SQLite file and imports resolve predictably.

```sh
docker compose up -d --build --wait
docker compose run --rm api python seed_demo.py --notes 1000
```

or, without Docker (Python 3.12):

```sh
python -m pip install -r requirements.txt
python seed_demo.py --notes 1000
python -m uvicorn main:app --reload
```

Tests and lint:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -v
ruff check . && ruff format --check .
```

| File | Contents |
| --- | --- |
| `main.py` | Model, request/response schemas, routes, seeder and benchmark |
| `seed_demo.py` | CLI that appends synthetic notes (`--reset` drops the table first) |
| `test_api.py`, `test_startup.py` | API, seeding and startup tests |
| `testing_db.py` | Points the tests at a temporary SQLite file |

See the [project README](../README.md) for API examples, settings, design
notes and limitations. Keep demonstration records synthetic.
