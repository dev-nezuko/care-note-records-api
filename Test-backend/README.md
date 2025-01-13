# Backend quick reference

The application is `main:app`. Run commands from this directory so the default
SQLite file and imports have a predictable location.

```sh
python -m pip install -r requirements.txt
python seed_demo.py --notes 1000
python -m uvicorn main:app --reload
```

Startup creates missing tables and preserves existing notes. Seeding is explicit;
only the seeder's `--reset` option drops existing tables. `CARE_NOTES_DB_PATH`
selects a different SQLite database file for both sync and async connections.

The actual endpoints are documented at `/docs`: list/create care notes, care
statistics and an illustrative benchmark trigger. Tenant/facility parameters are
filters rather than an authorization boundary.

For an isolated startup/input regression check:

```sh
python -m pip install -r requirements-test.txt
python -m unittest -v test_startup
```

See the [project README](../README.md) for API examples, architecture, reset
semantics and limitations. Keep demonstration records synthetic.
