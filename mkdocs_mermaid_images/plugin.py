from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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


class MermaidImagesPlugin(BasePlugin):
    def __init__(self) -> None:
        self._generated_files: dict[str, File] = {}
        self._page_replacements: dict[str, list[Replacement]] = {}
        self._temp_dir: Path | None = None

    def on_config(self, config: MkDocsConfig) -> MkDocsConfig:
        self._cleanup_temp_dir()
        self._generated_files = {}
        self._page_replacements = {}
        self._temp_dir = Path(tempfile.mkdtemp(prefix="mkdocs-mermaid-images-"))
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

        command = [
            "npx",
            "-y",
            "@mermaid-js/mermaid-cli",
            "-i",
            str(input_path),
            "-o",
            str(output_path),
        ]

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            detail = f": {stderr}" if stderr else "."
            raise PluginError(f"Failed to render Mermaid diagram in '{source_path}'{detail}") from exc

        asset_src_uri = f"{MERMAID_ASSET_DIR}/{content_hash}.png"
        return File.generated(config, asset_src_uri, abs_src_path=str(output_path))

    def _ensure_temp_dir(self) -> Path:
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="mkdocs-mermaid-images-"))
        return self._temp_dir

    def _cleanup_temp_dir(self) -> None:
        if self._temp_dir is None:
            return
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None


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
