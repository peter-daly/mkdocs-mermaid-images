from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.exceptions import PluginError
from mkdocs.structure.files import File, Files
from mkdocs.structure.pages import Page

from mkdocs_mermaid_images.plugin import (
    MERMAID_ASSET_DIR,
    MermaidImagesPlugin,
    _extract_mermaid_blocks,
)


def build_config(tmp_path: Path) -> MkDocsConfig:
    docs_dir = tmp_path / "docs"
    site_dir = tmp_path / "site"
    docs_dir.mkdir()
    site_dir.mkdir()

    config = MkDocsConfig()
    config.load_dict(
        {
            "site_name": "Test Docs",
            "docs_dir": str(docs_dir),
            "site_dir": str(site_dir),
            "use_directory_urls": True,
            "plugins": [],
        }
    )
    errors, warnings = config.validate()
    assert errors == []
    assert warnings == []
    config.plugins._current_plugin = "mermaid-images"
    return config


def build_doc_file(config: MkDocsConfig, src_uri: str, content: str) -> File:
    path = Path(config.docs_dir) / src_uri
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return File(src_uri, config.docs_dir, config.site_dir, config.use_directory_urls)


def test_extracts_mermaid_and_mermaidjs_fences() -> None:
    markdown = """```mermaid
graph TD
A --> B
```

~~~mermaidjs
flowchart LR
X --> Y
~~~

```python
print("ignore")
```
"""

    blocks = _extract_mermaid_blocks(markdown)

    assert [block.content for block in blocks] == [
        "graph TD\nA --> B\n",
        "flowchart LR\nX --> Y\n",
    ]


def test_extracts_multiple_blocks_and_supports_tilde_fences() -> None:
    markdown = """~~~mermaid
graph TD
A --> B
~~~

~~~mermaid
graph TD
B --> C
~~~"""

    blocks = _extract_mermaid_blocks(markdown)

    assert len(blocks) == 2
    assert blocks[0].raw_block.startswith("~~~mermaid")
    assert blocks[1].content == "graph TD\nB --> C\n"


def test_on_files_renders_unique_hashes_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = build_config(tmp_path)
    plugin = MermaidImagesPlugin()
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```

```mermaid
graph TD
A --> B
```

```mermaid
graph TD
B --> C
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_bytes(b"png")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    generated_paths = sorted(file.src_uri for file in files if file.src_uri.startswith(MERMAID_ASSET_DIR))
    assert len(calls) == 2
    assert len(generated_paths) == 2
    assert len(plugin._page_replacements["index.md"]) == 3


def test_on_page_markdown_replaces_blocks_with_relative_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = MermaidImagesPlugin()
    plugin.on_config(config)

    markdown = """# Title

```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "guide/page.md", markdown)
    files = Files([doc_file])

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_bytes(b"png")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)
    plugin.on_files(files, config=config)

    page = Page(title=None, file=doc_file, config=config)
    rendered = plugin.on_page_markdown(markdown, page=page, config=config, files=files)

    assert "```mermaid" not in rendered
    assert "![Mermaid diagram](../../assets/mermaid/" in rendered
    assert rendered.endswith("\n")


def test_on_page_markdown_preserves_list_indentation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = MermaidImagesPlugin()
    plugin.on_config(config)

    markdown = """- item

  ```mermaid
  graph TD
  A --> B
  ```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_bytes(b"png")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)
    plugin.on_files(files, config=config)

    page = Page(title=None, file=doc_file, config=config)
    rendered = plugin.on_page_markdown(markdown, page=page, config=config, files=files)

    assert "  ![Mermaid diagram](assets/mermaid/" in rendered


def test_render_failure_raises_plugin_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = MermaidImagesPlugin()
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "broken.md", markdown)
    files = Files([doc_file])

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="parse error")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    with pytest.raises(PluginError, match="broken.md"):
        plugin.on_files(files, config=config)

    with pytest.raises(PluginError, match="parse error"):
        plugin.on_files(files, config=config)
