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
