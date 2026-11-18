# Workflow

How a team uses Kaizeval from the first connection to a release decision, and what the system
does at each step. The component view is in [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. The whole journey

```mermaid
flowchart TD
    A(["Install and sign in"]) --> B["Connect the agent<br/>verify its capabilities"]
    B --> C["Configure a model<br/>run preflight"]
    C --> D["Upload knowledge documents"]
    D --> E["Build the specification<br/>Review and Edit, Approve, or Skip"]
    E --> F["Generate tests from evidence<br/>or import approved Q&A"]
    F --> G[("Regression set")]
    F --> H[("Sealed holdout")]
    G --> I["Evaluate Baseline A"]
    I --> J["Findings and recommendations"]
    J --> K{"Can the agent be<br/>patched safely?"}
    K -->|No| L(["Report: recommendations only"])
    K -->|Yes| M["Create isolated Candidate B"]
    M --> N["Propose one allowlisted change"]
    N --> O["Re-run the full regression set on B<br/>paired comparison with A"]
    O --> P{"Passes every gate?"}
    P -->|No| Q["Roll the change back"]
    P -->|Yes| R["Keep the change"]
    Q --> S{"Stop rule reached?"}
    R --> S
    S -->|No| N
    S -->|Yes| T["Independent holdout validation<br/>aggregate result only"]
    H -.->|"never shown to the optimizer"| T
    T --> U["Final report"]
    U --> V{"Owner approves<br/>the release?"}
    V -->|Yes| W(["Promote Candidate B"])
    V -->|No| X(["Keep Baseline A"])
```

| Step | Who | What Kaizeval guarantees |
|---|---|---|
| Connect the agent | Editor (owner for write access) | Only capabilities that the connector implements **and** the customer granted are offered |
| Configure a model | Owner / editor | No AI-assisted step runs on a model that has not passed preflight; no silent fallback |
| Upload knowledge | Editor | Every passage keeps file, version, SHA-256, page or section and offsets |
| Specification | Reviewer | The choice (approve or skip) is recorded with the person and time; edits create new versions |
| Tests | Editor / reviewer | The split into regression and holdout happens before any optimization |
| Evaluate | Editor | Deterministic checks and approved references outrank model judges |
| Optimize | Editor (owner for autonomous) | Only Candidate B is written; the whole regression set is re-run every round |
| Validate | Automatic | The optimizer never sees holdout questions, references or per-case results |
| Release | Owner | Nothing is promoted without an explicit, recorded approval |

The number of tester agents (1 to 8) and reviewer agents (0 to 7) is yours to choose, per dataset
and per run. Testers write and run the tests independently; reviewers start only after the
testers' answers are in. The quickest way to run this whole journey is one file and one command:
see [PROJECT_MODE.md](PROJECT_MODE.md).

## 2. How one answer is scored

```mermaid
flowchart LR
    A["Test case<br/>+ reference evidence"] --> B["Invoke the agent"]
    B --> C["Deterministic checks<br/>facts present, source cited, refusal"]
    C --> D{"Decisive for<br/>every dimension?"}
    D -->|Yes| H["Final scores"]
    D -->|No| E{"Evaluation mode"}
    E -->|deterministic| N["Remaining dimensions:<br/>not scored"]
    E -->|single| J1["Judge 1"]
    E -->|dual| J2["Judge 1 and Judge 2<br/>independent contexts"]
    J2 --> G{"Agree, and no doubt<br/>on a critical case?"}
    G -->|Yes| M["Average the two"]
    G -->|No| R["Arbiter<br/>may ask for human review"]
    J1 --> H
    M --> H
    R --> H
    N --> H
```

Precedence for each dimension: a decisive deterministic result, then a model judgment (labelled
as an estimate), otherwise "not scored". A case whose main dimension cannot be observed is
reported as `n/a` instead of receiving a guessed score.

## 3. One optimization round

```mermaid
sequenceDiagram
    autonumber
    participant O as Optimization service
    participant G as LLM Gateway (optimizer role)
    participant R as Reviewer
    participant C as Connector
    participant A as Agent (A and B)
    participant E as Evaluation service

    O->>G: failure summary from regression runs only
    G-->>O: one proposed change
    O->>O: validate against the connector's allowlist
    opt human-guided mode
        O->>R: show diff and rationale
        R-->>O: approve or reject (with a reason)
    end
    O->>C: apply the patch to Candidate B
    C->>A: change B only
    O->>C: re-read the baseline digest
    C-->>O: unchanged (otherwise abort: NO_SAFE_ISOLATION)
    O->>E: run the full regression set on B
    E->>A: invoke B for every case
    E-->>O: scores
    O->>O: paired comparison with A, apply the gates
    alt every gate passes
        O->>O: keep the change
    else a gate fails
        O->>C: restore Candidate B
    end
```

Gates: minimum improvement over the best version so far, no new critical failure, no category
dropping more than the tolerance, optional per-category minimums and a latency limit. The loop stops when
the target score is reached or when two cycles in a row leave the score unchanged; an optional round limit, model and agent budgets and cancellation can end it earlier. It always exits with the recommendations for what still fails.

## 4. Roles and decisions

```mermaid
flowchart LR
    V["Viewer<br/>read results"] --> RV["Reviewer<br/>approve specs, references,<br/>recommendations and patches<br/>open the holdout (audited)"]
    RV --> ED["Editor<br/>register agents and data<br/>start runs"]
    ED --> OW["Owner<br/>credentials and egress<br/>grant write access<br/>autonomous mode<br/>approve and promote releases"]
```

Each role includes the permissions of the roles to its left.
