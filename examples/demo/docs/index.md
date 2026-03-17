# Mermaid Images Demo

This page includes a few Mermaid fences. When MkDocs builds the site, the plugin replaces them with generated PNG images.

## Flowchart

```mermaid
flowchart TD
    Start --> Parse[Parse Markdown]
    Parse --> Render[Render Mermaid]
    Render --> Done[Write PNG Asset]
```

## Sequence Diagram

```mermaid
sequenceDiagram
    participant User
    participant MkDocs
    participant Plugin
    User->>MkDocs: Build site
    MkDocs->>Plugin: Process markdown page
    Plugin->>Plugin: Render Mermaid diagram
    Plugin-->>MkDocs: Return image link
```

## State Diagram

```mermaid
stateDiagram-v2
    [*] --> FoundFence
    FoundFence --> Rendering
    Rendering --> Cached: same diagram hash
    Rendering --> Generated: new diagram hash
    Cached --> [*]
    Generated --> [*]
```


# Same Flowchart

```mermaid
flowchart TD
    Start --> Parse[Parse Markdown]
    Parse --> Render[Render Mermaid]
    Render --> Done[Write PNG Asset]
```