# Architecture

CashFlow AI will use a local-first modular-monolith architecture. This document
will describe component boundaries, dependency direction, deployment topology,
and important architectural decisions as those components are implemented.

The approved high-level flow is:

```text
data sources -> ingestion -> relational storage -> analytics and ML
             -> FastAPI -> Streamlit
```

Business logic will remain outside API routes and Streamlit pages.

