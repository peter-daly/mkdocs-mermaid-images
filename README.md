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

This default setup uses the `npx` renderer.

## Requirements

The plugin can render diagrams with one of three backends:

- `npx` (default): runs Mermaid CLI through `npx`
- `docker`: runs Mermaid CLI inside a container
- `api`: fetches PNGs from `mermaid.ink`

All renderers write hashed PNG assets under `assets/mermaid/`. PNG is the only generated output format.

### `npx` renderer

You do not need to install `@mermaid-js/mermaid-cli` globally. The plugin uses Mermaid CLI through `npx` when MkDocs builds the site.

Requirements:

- `node` and `npx` available on `PATH`
- network access on the first run so `npx` can fetch `@mermaid-js/mermaid-cli` if it is not already cached
- any system dependencies required by Mermaid CLI's headless browser on that platform

### `docker` renderer

The Docker renderer uses the official Mermaid CLI container image by default: `ghcr.io/mermaid-js/mermaid-cli/mermaid-cli`.

Requirements:

- `docker` available on `PATH`
- permission to run containers on the build machine
- any container runtime settings needed for Chromium on that platform

### `api` renderer

The API renderer targets `https://mermaid.ink` by default and avoids local Node, Chromium, and Docker requirements.

Requirements:

- outbound network access to the configured API endpoint

For locked-down Linux CI environments, you may also need to disable Chromium sandboxing for the CLI-based renderers. The demo site does this with `no_sandbox: !ENV [MKDOCS_MERMAID_IMAGES_NO_SANDBOX, false]`, and the GitHub Actions workflow sets that environment variable only in CI.

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

## Configuration

### Use the default `npx` renderer

```yaml
plugins:
  - mermaid-images:
      renderer: npx
      theme: default
```

### Use the Docker renderer

```yaml
plugins:
  - mermaid-images:
      renderer: docker
      theme: dark
```

Override the image if needed:

```yaml
plugins:
  - mermaid-images:
      renderer: docker
      docker_image: example/mermaid-cli:test
```

### Use the API renderer

```yaml
plugins:
  - mermaid-images:
      renderer: api
      theme: forest
```

Point it at a compatible endpoint and tune the timeout:

```yaml
plugins:
  - mermaid-images:
      renderer: api
      api_base_url: https://mermaid.ink
      api_timeout: 30
      theme: neutral
```

### Mermaid theme

Choose a site-wide Mermaid theme for all rendered diagrams:

```yaml
plugins:
  - mermaid-images:
      theme: dark
```

Supported values are `default`, `neutral`, `dark`, and `forest`.

This plugin-level theme is site-wide and takes precedence over any `theme` value in a Mermaid config file.

### Mermaid config file

Pass Mermaid-native JSON configuration through to the renderer:

```yaml
plugins:
  - mermaid-images:
      mermaid_config_file: mermaid-config.json
```

Example `mermaid-config.json`:

```json
{
  "themeVariables": {
    "primaryColor": "#f4f7fb",
    "primaryTextColor": "#172033"
  },
  "flowchart": {
    "curve": "basis",
    "useMaxWidth": false
  },
  "sequence": {
    "showSequenceNumbers": true
  }
}
```

`mermaid_config_file` must point to a JSON object. It applies to all diagrams. The plugin's `theme` option still wins over any `theme` value in this file.

### Image size and scale

Wide diagrams can become hard to read if the PNG renderer uses its default viewport. Increase the generated PNG dimensions for those sites:

```yaml
plugins:
  - mermaid-images:
      image_width: 2400
      image_scale: 2
```

`image_width` and `image_height` are optional positive integers. `image_scale` accepts a number from `1` to `3` and defaults to `1`.

You can override these values for an individual diagram by adding short attributes to the Mermaid fence info string:

````md
```mermaid width=2400 scale=2
flowchart LR
    A --> B
```
````

Supported per-diagram attributes are `width`, `height`, and `scale`. They override `image_width`, `image_height`, and `image_scale` for that diagram only. Identical Mermaid source rendered with different image options is written as separate PNG assets.

For `npx` and `docker`, these values are passed to Mermaid CLI as `--width`, `--height`, and `--scale`. For `api`, they are sent as mermaid.ink `width`, `height`, and `scale` query parameters. The API renderer requires a width or height, from either the plugin config or the diagram fence, when the effective scale is above `1`.

### Alt text and captions

Set site-wide image alt text:

```yaml
plugins:
  - mermaid-images:
      alt_text: Mermaid diagram
```

Override alt text or add a caption for an individual diagram:

````md
```mermaid alt="Checkout sequence" caption="Checkout service flow"
sequenceDiagram
    User->>Service: Checkout
```
````

Captions are treated as plain text and escaped before being written to HTML.

### Background color

Set a global generated image background:

```yaml
plugins:
  - mermaid-images:
      background_color: transparent
```

Override it for an individual diagram:

````md
```mermaid background=transparent
flowchart TD
    A --> B
```
````

For `npx` and `docker`, this is passed to Mermaid CLI as `--backgroundColor`. For `api`, it is sent as the mermaid.ink `bgColor` query parameter.

### API retries

The API renderer retries transient failures by default:

```yaml
plugins:
  - mermaid-images:
      renderer: api
      api_retries: 2
      api_retry_backoff: 0.5
```

Retries apply to HTTP `429`, `500`, `502`, `503`, and `504`, plus timeout and network errors. Mermaid syntax errors and other non-transient responses are not retried.

### Chromium sandboxing for CLI renderers

For CI environments that require Chromium sandboxing to be disabled:

```yaml
plugins:
  - mermaid-images:
      no_sandbox: !ENV [MKDOCS_MERMAID_IMAGES_NO_SANDBOX, false]
```

`no_sandbox` applies to the `npx` and `docker` renderers only.

### Puppeteer config for CLI renderers

If you already have a Puppeteer config file, you can pass it through to Mermaid CLI:

```yaml
plugins:
  - mermaid-images:
      puppeteer_config_file: puppeteer-config.json
```

`puppeteer_config_file` applies to the `npx` and `docker` renderers only.

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
