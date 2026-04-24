from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import urllib.error
import urllib.parse
import zlib
from email.message import Message
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


def build_plugin(config_dict: dict[str, object] | None = None) -> MermaidImagesPlugin:
    plugin = MermaidImagesPlugin()
    errors, warnings = plugin.load_config(config_dict or {})
    assert errors == []
    assert warnings == []
    return plugin


def test_theme_config_accepts_supported_values() -> None:
    for theme in ("default", "neutral", "dark", "forest"):
        plugin = MermaidImagesPlugin()
        errors, warnings = plugin.load_config({"theme": theme})
        assert errors == []
        assert warnings == []
        assert plugin.config.theme == theme


def test_theme_config_rejects_unsupported_values() -> None:
    plugin = MermaidImagesPlugin()
    errors, _ = plugin.load_config({"theme": "base"})
    assert errors


def test_image_size_config_accepts_positive_values() -> None:
    plugin = build_plugin({"image_width": 2400, "image_height": 1200, "image_scale": 2})

    assert plugin.config.image_width == 2400
    assert plugin.config.image_height == 1200
    assert plugin.config.image_scale == 2


@pytest.mark.parametrize(
    ("config_key", "config_value"),
    [
        ("image_width", 0),
        ("image_height", -1),
        ("image_scale", 0),
        ("image_scale", 4),
        ("image_width", True),
    ],
)
def test_image_size_config_rejects_invalid_values(config_key: str, config_value: object) -> None:
    plugin = MermaidImagesPlugin()
    errors, _ = plugin.load_config({config_key: config_value})

    assert errors


def test_render_polish_config_accepts_supported_values(tmp_path: Path) -> None:
    mermaid_config = tmp_path / "mermaid-config.json"
    mermaid_config.write_text(json.dumps({"flowchart": {"curve": "basis"}}), encoding="utf-8")

    plugin = build_plugin(
        {
            "alt_text": "Architecture diagram",
            "background_color": "transparent",
            "mermaid_config_file": str(mermaid_config),
            "api_retries": 2,
            "api_retry_backoff": 0.5,
        }
    )

    assert plugin.config.alt_text == "Architecture diagram"
    assert plugin.config.background_color == "transparent"
    assert plugin.config.mermaid_config_file == str(mermaid_config)
    assert plugin.config.api_retries == 2
    assert plugin.config.api_retry_backoff == 0.5


@pytest.mark.parametrize(
    ("config_key", "config_value"),
    [
        ("api_retries", -1),
        ("api_retries", True),
        ("api_retry_backoff", -0.1),
        ("api_retry_backoff", True),
    ],
)
def test_render_polish_config_rejects_invalid_values(config_key: str, config_value: object) -> None:
    plugin = MermaidImagesPlugin()
    errors, _ = plugin.load_config({config_key: config_value})

    assert errors


def test_mermaid_config_file_must_contain_json_object(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    mermaid_config = tmp_path / "mermaid-config.json"
    mermaid_config.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    plugin = build_plugin({"mermaid_config_file": str(mermaid_config)})

    with pytest.raises(PluginError, match="JSON object"):
        plugin.on_config(config)


def write_cli_output(command: list[str]) -> None:
    output_arg = command[command.index("-o") + 1]
    output_path = Path(output_arg)
    if output_path.is_absolute() and output_path.parts[:2] == ("/", "data"):
        mount_spec = command[command.index("-v") + 1]
        host_dir, _ = mount_spec.rsplit(":", 1)
        output_path = Path(host_dir) / output_path.name
    output_path.write_bytes(b"png")


class FakeHTTPResponse:
    def __init__(self, body: bytes, content_type: str = "image/png") -> None:
        self._body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


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


def test_extracts_mermaid_fence_image_size_options() -> None:
    markdown = """```mermaid width=2400 height=1200 scale=2
graph TD
A --> B
```
"""

    blocks = _extract_mermaid_blocks(markdown)

    assert len(blocks) == 1
    assert blocks[0].image_width == 2400
    assert blocks[0].image_height == 1200
    assert blocks[0].image_scale == 2


def test_extracts_mermaid_fence_render_polish_options() -> None:
    markdown = '''```mermaid alt="Checkout flow" caption="Checkout <service>" background=transparent
graph TD
A --> B
```
'''

    blocks = _extract_mermaid_blocks(markdown)

    assert len(blocks) == 1
    assert blocks[0].alt_text == "Checkout flow"
    assert blocks[0].caption == "Checkout <service>"
    assert blocks[0].background_color == "transparent"


def test_extracts_mermaid_fence_rejects_invalid_quoted_options() -> None:
    markdown = '''```mermaid alt="Checkout flow
graph TD
A --> B
```
'''

    with pytest.raises(PluginError, match="Invalid Mermaid fence options"):
        _extract_mermaid_blocks(markdown)


@pytest.mark.parametrize(
    "info_string",
    [
        "mermaid width=0",
        "mermaid height=-1",
        "mermaid scale=0",
        "mermaid scale=4",
        "mermaid width=wide",
    ],
)
def test_extracts_mermaid_fence_rejects_invalid_image_size_options(info_string: str) -> None:
    markdown = f"""```{info_string}
graph TD
A --> B
```
"""

    with pytest.raises(PluginError):
        _extract_mermaid_blocks(markdown)


def test_on_files_reports_source_for_invalid_fence_image_size_options(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin()
    plugin.on_config(config)

    markdown = """```mermaid scale=4
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "broken.md", markdown)
    files = Files([doc_file])

    with pytest.raises(PluginError, match="broken.md"):
        plugin.on_files(files, config=config)


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
    plugin = build_plugin({"theme": "dark"})
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
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    generated_paths = sorted(file.src_uri for file in files if file.src_uri.startswith(MERMAID_ASSET_DIR))
    assert len(calls) == 2
    assert len(generated_paths) == 2
    assert len(plugin._page_replacements["index.md"]) == 3
    assert calls[0][:5] == ["npx", "-y", "-p", "@mermaid-js/mermaid-cli", "mmdc"]
    assert calls[0][calls[0].index("-t") + 1] == "dark"


def test_on_files_renders_same_content_with_different_image_options_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin()
    plugin.on_config(config)

    markdown = """```mermaid width=1200
graph TD
A --> B
```

```mermaid width=2400
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    generated_paths = sorted(file.src_uri for file in files if file.src_uri.startswith(MERMAID_ASSET_DIR))
    widths = [command[command.index("-w") + 1] for command in calls]
    assert len(calls) == 2
    assert len(generated_paths) == 2
    assert widths == ["1200", "2400"]


def test_on_files_reuses_asset_when_only_alt_or_caption_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin()
    plugin.on_config(config)

    markdown = '''```mermaid alt="First alt" caption="First caption"
graph TD
A --> B
```

```mermaid alt="Second alt" caption="Second caption"
graph TD
A --> B
```
'''
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    generated_paths = sorted(file.src_uri for file in files if file.src_uri.startswith(MERMAID_ASSET_DIR))
    assert len(calls) == 1
    assert len(generated_paths) == 1


def test_on_files_renders_same_content_with_different_backgrounds_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin()
    plugin.on_config(config)

    markdown = """```mermaid background=white
graph TD
A --> B
```

```mermaid background=transparent
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    generated_paths = sorted(file.src_uri for file in files if file.src_uri.startswith(MERMAID_ASSET_DIR))
    assert len(calls) == 2
    assert len(generated_paths) == 2


def test_npx_renderer_passes_image_size_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"image_width": 2400, "image_height": 1200, "image_scale": 2})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    command = calls[0]
    mmdc_args = command[command.index("mmdc") + 1 :]
    assert mmdc_args[mmdc_args.index("-w") + 1] == "2400"
    assert mmdc_args[mmdc_args.index("-H") + 1] == "1200"
    assert mmdc_args[mmdc_args.index("-s") + 1] == "2"


def test_npx_renderer_uses_fence_image_options_over_global_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"image_width": 1200, "image_height": 600, "image_scale": 1.5})
    plugin.on_config(config)

    markdown = """```mermaid width=2400 height=1200 scale=2
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    command = calls[0]
    mmdc_args = command[command.index("mmdc") + 1 :]
    assert mmdc_args[mmdc_args.index("-w") + 1] == "2400"
    assert mmdc_args[mmdc_args.index("-H") + 1] == "1200"
    assert mmdc_args[mmdc_args.index("-s") + 1] == "2"


def test_on_files_passes_no_sandbox_puppeteer_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"no_sandbox": True, "theme": "forest"})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        mmdc_index = command.index("mmdc")
        config_path = Path(command[mmdc_index + 2])
        config_data = config_path.read_text(encoding="utf-8")
        assert "--no-sandbox" in config_data
        assert "--disable-setuid-sandbox" in config_data
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    assert len(calls) == 1
    assert "-p" in calls[0]
    assert calls[0][calls[0].index("-t") + 1] == "forest"


def test_on_page_markdown_replaces_blocks_with_relative_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin()
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
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)
    plugin.on_files(files, config=config)

    page = Page(title=None, file=doc_file, config=config)
    rendered = plugin.on_page_markdown(markdown, page=page, config=config, files=files)

    assert "```mermaid" not in rendered
    assert "![Mermaid diagram](../../assets/mermaid/" in rendered
    assert rendered.endswith("\n")


def test_on_page_markdown_uses_alt_text_and_escaped_plain_caption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"alt_text": "Default alt"})
    plugin.on_config(config)

    markdown = '''# Title

```mermaid alt="Checkout <flow>" caption="Use <service> & pay"
graph TD
A --> B
```
'''
    doc_file = build_doc_file(config, "guide/page.md", markdown)
    files = Files([doc_file])

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)
    plugin.on_files(files, config=config)

    page = Page(title=None, file=doc_file, config=config)
    rendered = plugin.on_page_markdown(markdown, page=page, config=config, files=files)

    assert "<figure>" in rendered
    assert 'alt="Checkout &lt;flow&gt;"' in rendered
    assert "<figcaption>Use &lt;service&gt; &amp; pay</figcaption>" in rendered
    assert "![Default alt]" not in rendered


def test_on_page_markdown_uses_global_alt_text_without_caption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"alt_text": "System flow"})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)
    plugin.on_files(files, config=config)

    page = Page(title=None, file=doc_file, config=config)
    rendered = plugin.on_page_markdown(markdown, page=page, config=config, files=files)

    assert "![System flow](assets/mermaid/" in rendered


def test_on_page_markdown_preserves_list_indentation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin()
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
        write_cli_output(command)
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
    plugin = build_plugin()
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


def test_docker_renderer_uses_default_image_and_data_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "docker", "theme": "neutral"})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    assert len(calls) == 1
    command = calls[0]
    assert command[:4] == ["docker", "run", "--rm", "-v"]
    assert command[4].endswith(":/data")
    assert command[5:7] == ["-w", "/data"]
    if os.name == "nt":
        assert "--user" not in command
    else:
        assert command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "ghcr.io/mermaid-js/mermaid-cli/mermaid-cli" in command
    assert command[command.index("-t") + 1] == "neutral"
    mmdc_index = command.index("mmdc")
    assert command[mmdc_index + 1 : mmdc_index + 7] == [
        "-t",
        "neutral",
        "-i",
        f"/data/{Path(command[-1]).name.replace('.png', '.mmd')}",
        "-o",
        command[-1],
    ]
    assert command[-1].startswith("/data/")


def test_docker_renderer_supports_image_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "docker", "docker_image": "example/mermaid-cli:test"})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    assert calls[0][calls[0].index("mmdc") - 1] == "example/mermaid-cli:test"


def test_docker_renderer_passes_puppeteer_config_and_no_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    puppeteer_config = tmp_path / "puppeteer.json"
    puppeteer_config.write_text(json.dumps({"args": ["--window-size=1280,720"]}), encoding="utf-8")
    plugin = build_plugin(
        {
            "renderer": "docker",
            "puppeteer_config_file": str(puppeteer_config),
            "no_sandbox": True,
        }
    )
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        config_path = Path(command[command.index("-v") + 1].rsplit(":", 1)[0]) / "puppeteer-config.json"
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        assert config_data["args"] == [
            "--window-size=1280,720",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    assert calls[0][calls[0].index("mmdc") + 1 : calls[0].index("mmdc") + 3] == ["-p", "/data/puppeteer-config.json"]


def test_docker_renderer_passes_image_size_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin(
        {
            "renderer": "docker",
            "image_width": 2400,
            "image_height": 1200,
            "image_scale": 2,
        }
    )
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    command = calls[0]
    mmdc_args = command[command.index("mmdc") + 1 :]
    assert mmdc_args[mmdc_args.index("-w") + 1] == "2400"
    assert mmdc_args[mmdc_args.index("-H") + 1] == "1200"
    assert mmdc_args[mmdc_args.index("-s") + 1] == "2"


def test_npx_renderer_passes_background_and_mermaid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    mermaid_config = tmp_path / "mermaid-config.json"
    mermaid_config.write_text(json.dumps({"flowchart": {"curve": "basis"}}), encoding="utf-8")
    plugin = build_plugin({"background_color": "transparent", "mermaid_config_file": str(mermaid_config)})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    command = calls[0]
    mmdc_args = command[command.index("mmdc") + 1 :]
    assert mmdc_args[mmdc_args.index("-b") + 1] == "transparent"
    assert json.loads(Path(mmdc_args[mmdc_args.index("-c") + 1]).read_text(encoding="utf-8")) == {
        "flowchart": {"curve": "basis"}
    }


def test_docker_renderer_passes_background_and_mermaid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    mermaid_config = tmp_path / "mermaid-config.json"
    mermaid_config.write_text(json.dumps({"flowchart": {"curve": "basis"}}), encoding="utf-8")
    plugin = build_plugin(
        {
            "renderer": "docker",
            "background_color": "transparent",
            "mermaid_config_file": str(mermaid_config),
        }
    )
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        write_cli_output(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    plugin.on_files(files, config=config)

    command = calls[0]
    mmdc_args = command[command.index("mmdc") + 1 :]
    assert mmdc_args[mmdc_args.index("-b") + 1] == "transparent"
    assert mmdc_args[mmdc_args.index("-c") + 1] == "/data/mermaid-config.json"


def test_docker_render_failure_raises_plugin_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "docker"})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "broken.md", markdown)
    files = Files([doc_file])

    def fake_run(command: list[str], check: bool, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="docker parse error")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.subprocess.run", fake_run)

    with pytest.raises(PluginError, match="via docker renderer"):
        plugin.on_files(files, config=config)

    with pytest.raises(PluginError, match="docker parse error"):
        plugin.on_files(files, config=config)


def test_api_renderer_fetches_png_and_writes_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "api", "api_timeout": 12, "theme": "dark"})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    requests: list[tuple[str, str | None, int]] = []

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        requests.append((request.full_url, request.get_header("User-agent"), timeout))
        return FakeHTTPResponse(b"\x89PNG\r\n\x1a\npng")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    plugin.on_files(files, config=config)

    generated_paths = [file for file in files if file.src_uri.startswith(MERMAID_ASSET_DIR)]
    assert len(generated_paths) == 1
    assert generated_paths[0].abs_src_path is not None
    assert Path(generated_paths[0].abs_src_path).read_bytes().startswith(b"\x89PNG")
    assert len(requests) == 1
    request_url, user_agent, timeout = requests[0]
    assert request_url.startswith("https://mermaid.ink/img/pako:")
    assert request_url.endswith("?type=png&theme=dark")
    assert '"theme":"default"' not in request_url
    assert user_agent == "mkdocs-mermaid-images"
    assert timeout == 12


def test_api_renderer_passes_image_size_query_params(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin(
        {
            "renderer": "api",
            "theme": "dark",
            "image_width": 2400,
            "image_height": 1200,
            "image_scale": 2,
        }
    )
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    requests: list[str] = []

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        requests.append(request.full_url)
        return FakeHTTPResponse(b"\x89PNG\r\n\x1a\npng")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    plugin.on_files(files, config=config)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0]).query)
    assert query["type"] == ["png"]
    assert query["theme"] == ["dark"]
    assert query["width"] == ["2400"]
    assert query["height"] == ["1200"]
    assert query["scale"] == ["2"]


def test_api_renderer_passes_background_and_mermaid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    mermaid_config = tmp_path / "mermaid-config.json"
    mermaid_config.write_text(json.dumps({"theme": "forest", "flowchart": {"curve": "basis"}}), encoding="utf-8")
    plugin = build_plugin(
        {
            "renderer": "api",
            "theme": "dark",
            "background_color": "transparent",
            "mermaid_config_file": str(mermaid_config),
        }
    )
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    requests: list[str] = []

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        requests.append(request.full_url)
        return FakeHTTPResponse(b"\x89PNG\r\n\x1a\npng")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    plugin.on_files(files, config=config)

    parsed_url = urllib.parse.urlparse(requests[0])
    query = urllib.parse.parse_qs(parsed_url.query)
    encoded_state = parsed_url.path.rsplit("pako:", 1)[1]
    padded_state = encoded_state + "=" * (-len(encoded_state) % 4)
    state = json.loads(zlib.decompress(base64.urlsafe_b64decode(padded_state)).decode("utf-8"))
    assert query["theme"] == ["dark"]
    assert query["bgColor"] == ["!transparent"]
    assert state["mermaid"] == {"theme": "forest", "flowchart": {"curve": "basis"}}


def test_api_renderer_uses_fence_image_options_over_global_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin(
        {
            "renderer": "api",
            "image_width": 1200,
            "image_height": 600,
            "image_scale": 1.5,
        }
    )
    plugin.on_config(config)

    markdown = """```mermaid width=2400 height=1200 scale=2
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    requests: list[str] = []

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        requests.append(request.full_url)
        return FakeHTTPResponse(b"\x89PNG\r\n\x1a\npng")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    plugin.on_files(files, config=config)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0]).query)
    assert query["width"] == ["2400"]
    assert query["height"] == ["1200"]
    assert query["scale"] == ["2"]


def test_api_renderer_requires_size_when_scale_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "api", "image_scale": 2})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        raise AssertionError("api renderer should reject scale before making a request")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(PluginError, match="width or height"):
        plugin.on_files(files, config=config)


def test_api_renderer_retries_transient_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "api", "api_retries": 2, "api_retry_backoff": 0})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    attempts = 0

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=503,
                msg="Service Unavailable",
                hdrs=Message(),
                fp=io.BytesIO(b"Service Unavailable"),
            )
        return FakeHTTPResponse(b"\x89PNG\r\n\x1a\npng")

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    plugin.on_files(files, config=config)

    assert attempts == 3


def test_api_renderer_does_not_retry_non_transient_http_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "api", "api_retries": 2, "api_retry_backoff": 0})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "index.md", markdown)
    files = Files([doc_file])
    attempts = 0

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=400,
            msg="Bad Request",
            hdrs=Message(),
            fp=io.BytesIO(b"Bad Request"),
        )

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(PluginError, match="Bad Request"):
        plugin.on_files(files, config=config)

    assert attempts == 1


@pytest.mark.parametrize(
    ("urlopen_error", "match"),
    [
        (
            urllib.error.HTTPError(
                url="https://mermaid.ink/img/test",
                code=503,
                msg="Service Unavailable",
                hdrs=Message(),
                fp=io.BytesIO(b"Service Unavailable"),
            ),
            "Service Unavailable",
        ),
        (
            urllib.error.URLError(TimeoutError("timed out")),
            "timed out",
        ),
    ],
)
def test_api_render_failure_raises_plugin_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    urlopen_error: Exception,
    match: str,
) -> None:
    config = build_config(tmp_path)
    plugin = build_plugin({"renderer": "api"})
    plugin.on_config(config)

    markdown = """```mermaid
graph TD
A --> B
```
"""
    doc_file = build_doc_file(config, "broken.md", markdown)
    files = Files([doc_file])

    def fake_urlopen(request, timeout: int) -> FakeHTTPResponse:
        raise urlopen_error

    monkeypatch.setattr("mkdocs_mermaid_images.plugin.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(PluginError, match="via api renderer"):
        plugin.on_files(files, config=config)

    with pytest.raises(PluginError, match=match):
        plugin.on_files(files, config=config)
