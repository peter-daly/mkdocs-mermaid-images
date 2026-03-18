from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mkdocs.config import base, config_options as c
from mkdocs.exceptions import PluginError
from mkdocs.plugins import BasePlugin
from mkdocs.structure.files import File, Files
from mkdocs.utils import meta

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.pages import Page


MERMAID_INFO_STRINGS = {"mermaid", "mermaidjs"}
MERMAID_ASSET_DIR = "assets/mermaid"
MERMAID_ALT_TEXT = "Mermaid diagram"
MERMAID_API_USER_AGENT = "mkdocs-mermaid-images"
MERMAID_DOCKER_IMAGE = "ghcr.io/mermaid-js/mermaid-cli/mermaid-cli"
FENCE_START_RE = re.compile(r"^(?P<indent>[ ]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*)$")


@dataclass(frozen=True)
class MermaidBlock:
    raw_block: str
    content: str
    indent: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Replacement:
    raw_block: str
    indent: str
    asset_src_uri: str


class MermaidImagesConfig(base.Config):
    renderer = c.Choice(("npx", "docker", "api"), default="npx")
    theme = c.Choice(("default", "neutral", "dark", "forest"), default="default")
    docker_image = c.Type(str, default=MERMAID_DOCKER_IMAGE)
    api_base_url = c.Type(str, default="https://mermaid.ink")
    api_timeout = c.Type(int, default=30)
    puppeteer_config_file = c.Optional(c.File(exists=True))
    no_sandbox = c.Type(bool, default=False)


class MermaidImagesPlugin(BasePlugin[MermaidImagesConfig]):
    def __init__(self) -> None:
        self.config = MermaidImagesConfig()
        self._generated_files: dict[str, File] = {}
        self._page_replacements: dict[str, list[Replacement]] = {}
        self._temp_dir: Path | None = None
        self._puppeteer_config_path: Path | None = None

    def on_config(self, config: MkDocsConfig) -> MkDocsConfig:
        self._cleanup_temp_dir()
        self._generated_files = {}
        self._page_replacements = {}
        self._temp_dir = Path(tempfile.mkdtemp(prefix="mkdocs-mermaid-images-"))
        self._puppeteer_config_path = None
        return config

    def on_files(self, files: Files, *, config: MkDocsConfig) -> Files:
        for file in files.documentation_pages():
            markdown, _ = meta.get_data(file.content_string)
            blocks = list(_extract_mermaid_blocks(markdown))
            if not blocks:
                continue

            replacements: list[Replacement] = []
            for block in blocks:
                asset_file = self._generated_files.get(block.content_hash)
                if asset_file is None:
                    asset_file = self._render_mermaid_block(
                        block_content=block.content,
                        content_hash=block.content_hash,
                        source_path=file.src_uri,
                        config=config,
                    )
                    self._generated_files[block.content_hash] = asset_file
                    files.append(asset_file)

                replacements.append(
                    Replacement(
                        raw_block=block.raw_block,
                        indent=block.indent,
                        asset_src_uri=asset_file.src_uri,
                    )
                )

            self._page_replacements[file.src_uri] = replacements

        return files

    def on_page_markdown(
        self,
        markdown: str,
        *,
        page: Page,
        config: MkDocsConfig,
        files: Files,
    ) -> str:
        replacements = self._page_replacements.get(page.file.src_uri)
        if not replacements:
            return markdown

        updated_markdown = markdown
        for replacement in replacements:
            asset_file = files.get_file_from_path(replacement.asset_src_uri)
            if asset_file is None:
                raise PluginError(
                    f"Generated Mermaid asset '{replacement.asset_src_uri}' was not found "
                    f"while rendering '{page.file.src_uri}'."
                )

            image_url = asset_file.url_relative_to(page.file)
            replacement_markdown = f"{replacement.indent}![{MERMAID_ALT_TEXT}]({image_url})"
            if replacement.raw_block.endswith("\n"):
                replacement_markdown += "\n"
            updated_markdown = updated_markdown.replace(
                replacement.raw_block,
                replacement_markdown,
                1,
            )

        return updated_markdown

    def on_post_build(self, *, config: MkDocsConfig) -> None:
        self._cleanup_temp_dir()

    def on_shutdown(self) -> None:
        self._cleanup_temp_dir()

    def _render_mermaid_block(
        self,
        *,
        block_content: str,
        content_hash: str,
        source_path: str,
        config: MkDocsConfig,
    ) -> File:
        temp_dir = self._ensure_temp_dir()
        input_path = temp_dir / f"{content_hash}.mmd"
        output_path = temp_dir / f"{content_hash}.png"
        input_path.write_text(block_content, encoding="utf-8")

        if self.config.renderer == "npx":
            self._render_with_npx(input_path=input_path, output_path=output_path, source_path=source_path)
        elif self.config.renderer == "docker":
            self._render_with_docker(input_path=input_path, output_path=output_path, source_path=source_path)
        else:
            self._render_with_api(block_content=block_content, output_path=output_path, source_path=source_path)

        asset_src_uri = f"{MERMAID_ASSET_DIR}/{content_hash}.png"
        return File.generated(config, asset_src_uri, abs_src_path=str(output_path))

    def _render_with_npx(self, *, input_path: Path, output_path: Path, source_path: str) -> None:
        command = [
            "npx",
            "-y",
            "-p",
            "@mermaid-js/mermaid-cli",
            "mmdc",
        ]
        puppeteer_config_path = self._resolve_cli_puppeteer_config_path()
        if puppeteer_config_path is not None:
            command.extend(["-p", str(puppeteer_config_path)])
        command.extend(["-t", self.config.theme, "-i", str(input_path), "-o", str(output_path)])

        self._run_render_command(command, renderer="npx", source_path=source_path)

    def _render_with_docker(self, *, input_path: Path, output_path: Path, source_path: str) -> None:
        temp_dir = self._ensure_temp_dir()
        container_input_path = f"/data/{input_path.name}"
        container_output_path = f"/data/{output_path.name}"

        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{temp_dir}:/data",
            "-w",
            "/data",
        ]
        if os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        command.append(self.config.docker_image)
        command.append("mmdc")

        puppeteer_config_path = self._resolve_cli_puppeteer_config_path()
        if puppeteer_config_path is not None:
            command.extend(["-p", f"/data/{puppeteer_config_path.name}"])

        command.extend(["-t", self.config.theme, "-i", container_input_path, "-o", container_output_path])
        self._run_render_command(command, renderer="docker", source_path=source_path)

    def _render_with_api(self, *, block_content: str, output_path: Path, source_path: str) -> None:
        state = json.dumps(
            {
                "code": block_content,
                "mermaid": {},
            },
            separators=(",", ":"),
        )
        compressed_state = zlib.compress(state.encode("utf-8"), level=9)
        encoded_state = base64.urlsafe_b64encode(compressed_state).decode("ascii").rstrip("=")
        api_url = (
            f"{self.config.api_base_url.rstrip('/')}/img/pako:{encoded_state}"
            f"?type=png&theme={self.config.theme}"
        )
        request = urllib.request.Request(
            api_url,
            headers={"User-Agent": MERMAID_API_USER_AGENT},
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.api_timeout) as response:
                content_type = response.headers.get_content_type()
                if content_type != "image/png":
                    raise PluginError(
                        f"Failed to render Mermaid diagram in '{source_path}' via api renderer: "
                        f"expected image/png response but received '{content_type}'."
                    )
                output_path.write_bytes(response.read())
        except PluginError:
            raise
        except urllib.error.HTTPError as exc:
            response_body = b""
            if exc.fp is not None:
                response_body = exc.read()
            detail = (response_body.decode("utf-8", errors="replace") or str(exc)).strip()
            self._raise_render_error(renderer="api", source_path=source_path, detail=detail, exc=exc)
        except urllib.error.URLError as exc:
            self._raise_render_error(renderer="api", source_path=source_path, detail=str(exc.reason), exc=exc)
        except TimeoutError as exc:
            self._raise_render_error(renderer="api", source_path=source_path, detail="request timed out", exc=exc)

    def _run_render_command(self, command: list[str], *, renderer: str, source_path: str) -> None:
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip() or "renderer command failed"
            self._raise_render_error(renderer=renderer, source_path=source_path, detail=stderr, exc=exc)

    def _ensure_temp_dir(self) -> Path:
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="mkdocs-mermaid-images-"))
        return self._temp_dir

    def _resolve_cli_puppeteer_config_path(self) -> Path | None:
        configured_path = self.config.puppeteer_config_file
        if configured_path is None and not self.config.no_sandbox:
            return None

        if self._puppeteer_config_path is not None:
            return self._puppeteer_config_path

        launch_config: dict[str, object] = {}
        if configured_path is not None:
            with Path(configured_path).open(encoding="utf-8") as config_file:
                loaded_config = json.load(config_file)
            if not isinstance(loaded_config, dict):
                raise PluginError("Mermaid puppeteer config file must contain a JSON object.")
            launch_config = loaded_config

        args = launch_config.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise PluginError("Mermaid puppeteer config 'args' must be a list of strings.")

        merged_args = list(args)
        if self.config.no_sandbox:
            for arg in ("--no-sandbox", "--disable-setuid-sandbox"):
                if arg not in merged_args:
                    merged_args.append(arg)
        if merged_args or "args" in launch_config:
            launch_config["args"] = merged_args

        config_path = self._ensure_temp_dir() / "puppeteer-config.json"
        config_path.write_text(json.dumps(launch_config), encoding="utf-8")
        self._puppeteer_config_path = config_path
        return config_path

    def _raise_render_error(self, *, renderer: str, source_path: str, detail: str, exc: Exception) -> None:
        suffix = f": {detail}" if detail else "."
        raise PluginError(
            f"Failed to render Mermaid diagram in '{source_path}' via {renderer} renderer{suffix}"
        ) from exc

    def _cleanup_temp_dir(self) -> None:
        if self._temp_dir is None:
            return
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None
        self._puppeteer_config_path = None


def _extract_mermaid_blocks(markdown: str) -> list[MermaidBlock]:
    lines = markdown.splitlines(keepends=True)
    blocks: list[MermaidBlock] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        start_match = FENCE_START_RE.match(line)
        if start_match is None:
            index += 1
            continue

        info_string = start_match.group("info").strip()
        info_name = info_string.split(None, 1)[0].lower() if info_string else ""
        if info_name not in MERMAID_INFO_STRINGS:
            index += 1
            continue

        fence = start_match.group("fence")
        indent = start_match.group("indent")
        fence_char = fence[0]
        closing_re = re.compile(rf"^[ ]{{0,3}}{re.escape(fence_char)}{{{len(fence)},}}[ \t]*$")

        end_index = index + 1
        while end_index < len(lines) and closing_re.match(lines[end_index]) is None:
            end_index += 1

        if end_index < len(lines):
            raw_lines = lines[index : end_index + 1]
            content_lines = lines[index + 1 : end_index]
            index = end_index + 1
        else:
            raw_lines = lines[index:]
            content_lines = lines[index + 1 :]
            index = len(lines)

        blocks.append(
            MermaidBlock(
                raw_block="".join(raw_lines),
                content="".join(_strip_container_indent(content_lines, len(indent))),
                indent=indent,
            )
        )

    return blocks


def _strip_container_indent(lines: list[str], indent_width: int) -> list[str]:
    if indent_width == 0:
        return lines

    stripped_lines: list[str] = []
    for line in lines:
        removable = min(indent_width, len(line) - len(line.lstrip(" ")))
        stripped_lines.append(line[removable:])
    return stripped_lines
