from __future__ import annotations

import io
import json
import os
import subprocess
import urllib.error
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
