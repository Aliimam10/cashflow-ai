# Synthetic demo data

CashFlow AI generates demonstration data locally rather than committing large
derived CSV files. The generated records are fictional and reproducible.

From the repository root:

```bash
make demo-data
```

The default command creates canonical and bank-like layouts for all supported
profiles under `data/demo/generated/`. Delete that directory at any time; the
same seed and arguments recreate the same files.

Do not place real statements in this directory. Real local imports belong in an
ignored private location and must never be committed.

## Fully labelled dashboard demo

To exercise the real CSV importer and then approve every generated category and
financial role without hundreds of manual clicks, run:

```bash
make demo-dashboard
```

This regenerates the one-year student canonical CSV and creates the separate,
ignored `data/cashflow-demo.db`. The seeding step refuses to write to the normal
`data/cashflow.db` or to add a profile to a demo database that already has one; the
preceding migration step may still bring an existing disposable demo schema up to
date. Start the API for that isolated database with `make demo-dashboard-api`, start
the normal UI with `make ui` in another terminal, and open
`http://127.0.0.1:8501`.

The automatic approvals are permitted only because every row and label is generated
fictional ground truth. Normal CSV and PDF uploads remain review-gated.

To rebuild the demo later, stop its API first and preserve the old ignored database:

```bash
mv -i data/cashflow-demo.db data/cashflow-demo.previous.db
make demo-dashboard
```

The `-i` flag asks before replacing an older backup.
