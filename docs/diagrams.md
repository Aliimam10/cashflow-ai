# Architecture and data-flow diagrams

These diagrams describe the Version 1 local modular monolith. Arrows show data or
control flow, not separate deployable microservices.

## Component architecture

```mermaid
flowchart TB
    User[Local user] --> UI[Streamlit interface]
    UI -->|typed loopback HTTP| API[FastAPI routes]
    API --> Services[Application and domain services]

    subgraph Sources[Statement source adapters]
        CSV[CSV parser]
        PDF[Embedded-text PDF extractor]
        OCR[Local Tesseract OCR adapter]
    end

    API --> Sources
    Sources --> Review[Preview, reconciliation, and explicit review]
    Review --> Services
    Services --> Repos[SQLAlchemy repositories]
    Repos --> DB[(Local SQLite)]

    Services --> Category[Categorisation]
    Services --> Analytics[Coverage-aware analytics]
    Services --> Recurrence[Recurrence detection]
    Services --> Forecast[Forecasting and uncertainty]
    Services --> Anomaly[Unusual-activity review]
    Services --> Planning[Budgets, goals, and scenarios]

    Category --> Models[(Private local model artefacts)]
    Forecast --> Models
    Anomaly --> Models
```

Routes and Streamlit pages coordinate typed requests only. Financial rules stay in
services, source adapters do not write trusted transactions, and SQLAlchemy
repositories do not contain user-interface decisions.

## Statement trust flow

```mermaid
flowchart LR
    Upload[Ephemeral local upload] --> Validate[Type, signature, size, and hash]
    Validate --> Extract{Source type}
    Extract -->|CSV| CsvPreview[Structured preview and mapping]
    Extract -->|digital PDF| TextPreview[Text/table preview]
    Extract -->|scanned PDF| OcrPreview[Local OCR preview and confidence]
    CsvPreview --> Review[User review]
    TextPreview --> Review
    OcrPreview --> Review
    Review -->|reject or correct| Review
    Review -->|explicitly confirm exact file| Canonical[Canonical validation]
    Canonical --> Duplicate[Duplicate and overlap checks]
    Duplicate -->|confirmed CSV only| Persist[Atomic persistence]
    Duplicate -->|approved PDF| Memory[Trusted in-memory result]
    Persist --> Raw[(Preserved raw row)]
    Persist --> Verified[(Verified transaction)]
    Persist --> Coverage[(Coverage and gaps)]
    Persist --> Balance[(Balance snapshots)]
    Memory -. no Version 1 PDF database write .-> Persist
```

Every row remains represented. Malformed rows are quarantined, exact duplicates are
the only automatic exclusions, and probable duplicates wait for a decision. PDF
approval deliberately stops before persistence because the approved rows, rejected
evidence, balances, and coverage must eventually be saved as one transaction.

## Calculation and model flow

```mermaid
flowchart LR
    Evidence[(Verified transactions,
    coverage, balances,
    user decisions)] --> Cutoff[Point-in-time evidence gate]
    Cutoff --> Roles[Financial roles]
    Cutoff --> Categories[Categories]
    Roles --> Analytics[Observed analytics]
    Categories --> Analytics
    Roles --> Recurrence[Confirmed recurrence]
    Categories --> Recurrence
    Analytics --> ForecastData[Covered forecast dataset]
    Recurrence --> ForecastData
    ForecastData --> Baselines[Simple baselines]
    ForecastData --> Candidate[Gradient-boosting candidate]
    Baselines --> Select{Explicit selection gates}
    Candidate --> Select
    Select --> Path[Balance path and uncertainty]
    Path --> Planning[Budgets, goals, and scenarios]
    Cutoff --> Anomaly[Rules and gated Isolation Forest]
```

Information created after a historical cutoff cannot enter its training features,
labels, recurrence state, or evaluation. Missing statement coverage stays unknown.
An advanced model runs only when it beats an information-equivalent baseline under
the documented safeguards; otherwise an executable baseline remains in control.

## Local container topology

```mermaid
flowchart LR
    Browser[Local browser] -->|127.0.0.1:8501| UIProcess[Streamlit process]
    Browser -->|127.0.0.1:8000| APIProcess[FastAPI process]
    UIProcess -->|shared loopback namespace| APIProcess
    APIProcess --> SQLite[(cashflow_data volume)]
    APIProcess --> Artefacts[(cashflow_models volume)]
    APIProcess --> Tesseract[Local Tesseract]
```

Both ports bind to loopback. The containers do not introduce a cloud database,
external OCR, Redis, Kafka, background worker, or deployment target.
