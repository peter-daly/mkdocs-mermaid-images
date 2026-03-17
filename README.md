# mkdocs-mermaid-images

MkDocs plugin that replaces Mermaid code fences with generated PNG images.

## Basic setup

Add the plugin to your `mkdocs.yml`:

```yaml
site_name: My Docs

plugins:
  - search
  - mermaid-images
```

## Requirements

You do not need to install `@mermaid-js/mermaid-cli` globally. The plugin runs it through `npx` when MkDocs builds the site.

That means the machine running the build needs:

- `node` and `npx` available on `PATH`
- network access on the first run so `npx` can fetch `@mermaid-js/mermaid-cli` if it is not already cached
- any system dependencies required by Mermaid CLI's headless browser on that platform

For locked-down Linux CI environments, you may also need to disable Chromium sandboxing. The demo site does this with `no_sandbox: !ENV [MKDOCS_MERMAID_IMAGES_NO_SANDBOX, false]`, and the GitHub Actions workflow sets that environment variable only in CI.

## Examples

Use either `mermaid` or `mermaidjs` fenced code blocks in Markdown:

````md
# Architecture

```mermaid
flowchart TD
    User --> MkDocs
    MkDocs --> Plugin
    Plugin --> PNG[Generated PNG]
```
````

The built page will contain a normal Markdown image link instead of the code fence, and the generated file will be written under `assets/mermaid/`.

Repeated diagrams are rendered once and reused by content hash:

````md
```mermaid
graph TD
A --> B
```

```mermaid
graph TD
A --> B
```
````

Both fences will point at the same generated PNG.

You can also use `mermaidjs` fences:

```md
~~~mermaidjs
sequenceDiagram
    participant User
    participant Site
    User->>Site: Open docs
~~~
```

For CI environments that require Chromium sandboxing to be disabled:

```yaml
plugins:
  - mermaid-images:
      no_sandbox: !ENV [MKDOCS_MERMAID_IMAGES_NO_SANDBOX, false]
```

If you already have a Puppeteer config file, you can pass it through to Mermaid CLI:

```yaml
plugins:
  - mermaid-images:
      puppeteer_config_file: puppeteer-config.json
```

## Demo site

A minimal demo site lives in [examples/demo](https://github.com/peter-daly/mkdocs-mermaid-images/tree/main/examples/demo). It has a single page with a few Mermaid diagrams so you can verify the plugin renders them into image assets during the build.

Run it from the repository root:

```bash
uv sync
uv run mkdocs serve -f examples/demo/mkdocs.yml
```

Or use the `Makefile` targets:

```bash
make demo-serve
```

Or build the static site:

```bash
uv run mkdocs build -f examples/demo/mkdocs.yml
```

You can also run the repository checks from the root:

```bash
make ty
make test
make check
```

The generated files will be written to `examples/demo/site/`, and the rendered diagram images will appear under `examples/demo/site/assets/mermaid/`.
