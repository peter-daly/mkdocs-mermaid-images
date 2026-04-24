from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
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
TRANSIENT_API_STATUS_CODES = {429, 500, 502, 503, 504}
FENCE_START_RE = re.compile(r"^(?P<indent>[ ]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*)$")


@dataclass(frozen=True)
class MermaidBlock:
    raw_block: str
    content: str
    indent: str
    image_width: int | None = None
    image_height: int | None = None
    image_scale: int | float | None = None
    alt_text: str | None = None
    caption: str | None = None
    background_color: str | None = None


@dataclass(frozen=True)
class ImageOptions:
    width: int | None = None
    height: int | None = None
    scale: int | float = 1
    background_color: str | None = None
    mermaid_config: dict[str, object] | None = None
    theme: str = "default"


@dataclass(frozen=True)
class ImageOptionOverrides:
    width: int | None = None
    height: int | None = None
    scale: int | float | None = None
    alt_text: str | None = None
    caption: str | None = None
    background_color: str | None = None


@dataclass(frozen=True)
class Replacement:
    raw_block: str
    indent: str
    asset_src_uri: str
    alt_text: str
    caption: str | None = None


class _PositiveInteger(c.Type):
    def __init__(self) -> None:
        super().__init__(int)

    def run_validation(self, value: object) -> int:
        if isinstance(value, bool):
            raise base.ValidationError("Expected a positive integer, not a boolean.")

        validated = super().run_validation(value)
        if validated <= 0:
            raise base.ValidationError("Expected a positive integer.")
        return validated


class _ImageScale(c.Type):
    def __init__(self) -> None:
        super().__init__((int, float), default=1)

    def run_validation(self, value: object) -> int | float:
        if isinstance(value, bool):
            raise base.ValidationError("Expected a number between 1 and 3, not a boolean.")

        validated = super().run_validation(value)
        if validated < 1 or validated > 3:
            raise base.ValidationError("Expected a number between 1 and 3.")
        return validated


class _NonNegativeInteger(c.Type):
    def __init__(self, *, default: int) -> None:
        super().__init__(int, default=default)

    def run_validation(self, value: object) -> int:
        if isinstance(value, bool):
            raise base.ValidationError("Expected a non-negative integer, not a boolean.")

        validated = super().run_validation(value)
        if validated < 0:
            raise base.ValidationError("Expected a non-negative integer.")
        return validated


class _NonNegativeNumber(c.Type):
    def __init__(self, *, default: int | float) -> None:
        super().__init__((int, float), default=default)

    def run_validation(self, value: object) -> int | float:
        if isinstance(value, bool):
            raise base.ValidationError("Expected a non-negative number, not a boolean.")

        validated = super().run_validation(value)
        if validated < 0:
            raise base.ValidationError("Expected a non-negative number.")
        return validated


class MermaidImagesConfig(base.Config):
    renderer = c.Choice(("npx", "docker", "api"), default="npx")
    theme = c.Choice(("default", "neutral", "dark", "forest"), default="default")
    alt_text = c.Type(str, default=MERMAID_ALT_TEXT)
    background_color = c.Optional(c.Type(str))
    mermaid_config_file = c.Optional(c.File(exists=True))
    image_width = c.Optional(_PositiveInteger())
    image_height = c.Optional(_PositiveInteger())
    image_scale = _ImageScale()
    docker_image = c.Type(str, default=MERMAID_DOCKER_IMAGE)
    api_base_url = c.Type(str, default="https://mermaid.ink")
    api_timeout = c.Type(int, default=30)
    api_retries = _NonNegativeInteger(default=2)
    api_retry_backoff = _NonNegativeNumber(default=0.5)
    puppeteer_config_file = c.Optional(c.File(exists=True))
    no_sandbox = c.Type(bool, default=False)


class MermaidImagesPlugin(BasePlugin[MermaidImagesConfig]):
    def __init__(self) -> None:
        self.config = MermaidImagesConfig()
        self._generated_files: dict[str, File] = {}
        self._page_replacements: dict[str, list[Replacement]] = {}
        self._temp_dir: Path | None = None
        self._puppeteer_config_path: Path | None = None
        self._mermaid_config_path: Path | None = None
        self._mermaid_config: dict[str, object] | None = None

    def on_config(self, config: MkDocsConfig) -> MkDocsConfig:
        self._cleanup_temp_dir()
        self._generated_files = {}
        self._page_replacements = {}
        self._temp_dir = Path(tempfile.mkdtemp(prefix="mkdocs-mermaid-images-"))
        self._puppeteer_config_path = None
        self._mermaid_config_path = None
        self._mermaid_config = self._load_mermaid_config()
        return config

    def on_files(self, files: Files, *, config: MkDocsConfig) -> Files:
        for file in files.documentation_pages():
            markdown, _ = meta.get_data(file.content_string)
            try:
                blocks = list(_extract_mermaid_blocks(markdown))
            except PluginError as exc:
                raise PluginError(f"Invalid Mermaid fence in '{file.src_uri}': {exc}") from exc
            if not blocks:
                continue

            replacements: list[Replacement] = []
            for block in blocks:
                image_options = self._resolve_image_options(block)
                content_hash = _content_hash(block.content, image_options)
                asset_file = self._generated_files.get(content_hash)
                if asset_file is None:
                    asset_file = self._render_mermaid_block(
                        block_content=block.content,
                        content_hash=content_hash,
                        image_options=image_options,
                        source_path=file.src_uri,
                        config=config,
                    )
                    self._generated_files[content_hash] = asset_file
                    files.append(asset_file)

                replacements.append(
                    Replacement(
                        raw_block=block.raw_block,
                        indent=block.indent,
                        asset_src_uri=asset_file.src_uri,
                        alt_text=block.alt_text if block.alt_text is not None else self.config.alt_text,
                        caption=block.caption,
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
            replacement_markdown = _replacement_markdown(replacement, image_url)
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
        image_options: ImageOptions,
        source_path: str,
        config: MkDocsConfig,
    ) -> File:
        temp_dir = self._ensure_temp_dir()
        input_path = temp_dir / f"{content_hash}.mmd"
        output_path = temp_dir / f"{content_hash}.png"
        input_path.write_text(block_content, encoding="utf-8")

        if self.config.renderer == "npx":
            self._render_with_npx(
                input_path=input_path,
                output_path=output_path,
                image_options=image_options,
                source_path=source_path,
            )
        elif self.config.renderer == "docker":
            self._render_with_docker(
                input_path=input_path,
                output_path=output_path,
                image_options=image_options,
                source_path=source_path,
            )
        else:
            self._render_with_api(
                block_content=block_content,
                output_path=output_path,
                image_options=image_options,
                source_path=source_path,
            )

        asset_src_uri = f"{MERMAID_ASSET_DIR}/{content_hash}.png"
        return File.generated(config, asset_src_uri, abs_src_path=str(output_path))

    def _render_with_npx(
        self,
        *,
        input_path: Path,
        output_path: Path,
        image_options: ImageOptions,
        source_path: str,
    ) -> None:
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
        mermaid_config_path = self._resolve_cli_mermaid_config_path()
        if mermaid_config_path is not None:
            command.extend(["-c", str(mermaid_config_path)])
        command.extend(["-t", self.config.theme])
        command.extend(_render_option_cli_args(image_options))
        command.extend(["-i", str(input_path), "-o", str(output_path)])

        self._run_render_command(command, renderer="npx", source_path=source_path)

    def _render_with_docker(
        self,
        *,
        input_path: Path,
        output_path: Path,
        image_options: ImageOptions,
        source_path: str,
    ) -> None:
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

        mermaid_config_path = self._resolve_cli_mermaid_config_path()
        if mermaid_config_path is not None:
            command.extend(["-c", f"/data/{mermaid_config_path.name}"])

        command.extend(["-t", self.config.theme])
        command.extend(_render_option_cli_args(image_options))
        command.extend(["-i", container_input_path, "-o", container_output_path])
        self._run_render_command(command, renderer="docker", source_path=source_path)

    def _render_with_api(
        self,
        *,
        block_content: str,
        output_path: Path,
        image_options: ImageOptions,
        source_path: str,
    ) -> None:
        if image_options.scale != 1 and image_options.width is None and image_options.height is None:
            raise PluginError(
                "The api renderer requires a width or height when the effective image scale is above 1."
            )

        state = json.dumps(
            {
                "code": block_content,
                "mermaid": image_options.mermaid_config or {},
            },
            separators=(",", ":"),
        )
        compressed_state = zlib.compress(state.encode("utf-8"), level=9)
        encoded_state = base64.urlsafe_b64encode(compressed_state).decode("ascii").rstrip("=")
        query_params: list[tuple[str, str | int | float]] = [
            ("type", "png"),
            ("theme", self.config.theme),
        ]
        if image_options.width is not None:
            query_params.append(("width", image_options.width))
        if image_options.height is not None:
            query_params.append(("height", image_options.height))
        if image_options.scale != 1:
            query_params.append(("scale", image_options.scale))
        if image_options.background_color is not None:
            query_params.append(("bgColor", _api_background_color(image_options.background_color)))

        api_url = (
            f"{self.config.api_base_url.rstrip('/')}/img/pako:{encoded_state}"
            f"?{urllib.parse.urlencode(query_params)}"
        )
        request = urllib.request.Request(
            api_url,
            headers={"User-Agent": MERMAID_API_USER_AGENT},
        )

        for attempt in range(self.config.api_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.config.api_timeout) as response:
                    content_type = response.headers.get_content_type()
                    if content_type != "image/png":
                        raise PluginError(
                            f"Failed to render Mermaid diagram in '{source_path}' via api renderer: "
                            f"expected image/png response but received '{content_type}'."
                        )
                    output_path.write_bytes(response.read())
                    return
            except PluginError:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code in TRANSIENT_API_STATUS_CODES and attempt < self.config.api_retries:
                    self._sleep_before_api_retry(attempt)
                    continue

                detail = _http_error_detail(exc)
                self._raise_render_error(renderer="api", source_path=source_path, detail=detail, exc=exc)
            except urllib.error.URLError as exc:
                if attempt < self.config.api_retries:
                    self._sleep_before_api_retry(attempt)
                    continue

                self._raise_render_error(renderer="api", source_path=source_path, detail=str(exc.reason), exc=exc)
            except TimeoutError as exc:
                if attempt < self.config.api_retries:
                    self._sleep_before_api_retry(attempt)
                    continue

                self._raise_render_error(renderer="api", source_path=source_path, detail="request timed out", exc=exc)

    def _run_render_command(self, command: list[str], *, renderer: str, source_path: str) -> None:
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip() or "renderer command failed"
            self._raise_render_error(renderer=renderer, source_path=source_path, detail=stderr, exc=exc)

    def _resolve_image_options(self, block: MermaidBlock) -> ImageOptions:
        return ImageOptions(
            width=block.image_width if block.image_width is not None else self.config.image_width,
            height=block.image_height if block.image_height is not None else self.config.image_height,
            scale=block.image_scale if block.image_scale is not None else self.config.image_scale,
            background_color=(
                block.background_color if block.background_color is not None else self.config.background_color
            ),
            mermaid_config=self._mermaid_config,
            theme=self.config.theme,
        )

    def _sleep_before_api_retry(self, attempt: int) -> None:
        delay = self.config.api_retry_backoff * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def _load_mermaid_config(self) -> dict[str, object] | None:
        configured_path = self.config.mermaid_config_file
        if configured_path is None:
            return None

        with Path(configured_path).open(encoding="utf-8") as config_file:
            loaded_config = json.load(config_file)
        if not isinstance(loaded_config, dict):
            raise PluginError("Mermaid config file must contain a JSON object.")
        return loaded_config

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

    def _resolve_cli_mermaid_config_path(self) -> Path | None:
        if self._mermaid_config is None:
            return None

        if self._mermaid_config_path is not None:
            return self._mermaid_config_path

        config_path = self._ensure_temp_dir() / "mermaid-config.json"
        config_path.write_text(json.dumps(self._mermaid_config), encoding="utf-8")
        self._mermaid_config_path = config_path
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
        self._mermaid_config_path = None
        self._mermaid_config = None


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
        try:
            info_parts = shlex.split(info_string)
        except ValueError as exc:
            raise PluginError(f"Invalid Mermaid fence options: {exc}") from exc
        info_name = info_parts[0].lower() if info_parts else ""
        if info_name not in MERMAID_INFO_STRINGS:
            index += 1
            continue

        image_options = _parse_info_string_image_options(info_parts[1:])
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
                image_width=image_options.width,
                image_height=image_options.height,
                image_scale=image_options.scale,
                alt_text=image_options.alt_text,
                caption=image_options.caption,
                background_color=image_options.background_color,
            )
        )

    return blocks


def _parse_info_string_image_options(tokens: list[str]) -> ImageOptionOverrides:
    width: int | None = None
    height: int | None = None
    scale: int | float | None = None
    alt_text: str | None = None
    caption: str | None = None
    background_color: str | None = None

    for token in tokens:
        key, separator, value = token.partition("=")
        if separator != "=":
            continue

        if key == "width":
            width = _parse_positive_integer(value, "width")
        elif key == "height":
            height = _parse_positive_integer(value, "height")
        elif key == "scale":
            scale = _parse_image_scale(value)
        elif key == "alt":
            alt_text = value
        elif key == "caption":
            caption = value
        elif key == "background":
            background_color = value

    return ImageOptionOverrides(
        width=width,
        height=height,
        scale=scale,
        alt_text=alt_text,
        caption=caption,
        background_color=background_color,
    )


def _parse_positive_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PluginError(f"Invalid Mermaid image {name}: expected a positive integer.") from exc

    if parsed <= 0:
        raise PluginError(f"Invalid Mermaid image {name}: expected a positive integer.")
    return parsed


def _parse_image_scale(value: str) -> int | float:
    try:
        parsed: int | float
        parsed = float(value) if "." in value else int(value)
    except ValueError as exc:
        raise PluginError("Invalid Mermaid image scale: expected a number between 1 and 3.") from exc

    if parsed < 1 or parsed > 3:
        raise PluginError("Invalid Mermaid image scale: expected a number between 1 and 3.")
    return parsed


def _content_hash(block_content: str, image_options: ImageOptions) -> str:
    payload = json.dumps(
        {
            "content": block_content,
            "image_options": {
                "width": image_options.width,
                "height": image_options.height,
                "scale": image_options.scale,
                "background_color": image_options.background_color,
                "mermaid_config": image_options.mermaid_config,
                "theme": image_options.theme,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render_option_cli_args(image_options: ImageOptions) -> list[str]:
    args: list[str] = []
    if image_options.width is not None:
        args.extend(["-w", str(image_options.width)])
    if image_options.height is not None:
        args.extend(["-H", str(image_options.height)])
    if image_options.scale != 1:
        args.extend(["-s", str(image_options.scale)])
    if image_options.background_color is not None:
        args.extend(["-b", image_options.background_color])
    return args


def _replacement_markdown(replacement: Replacement, image_url: str) -> str:
    if replacement.caption is None:
        return f"{replacement.indent}![{_escape_markdown_alt(replacement.alt_text)}]({image_url})"

    escaped_url = html.escape(image_url, quote=True)
    escaped_alt = html.escape(replacement.alt_text, quote=True)
    escaped_caption = html.escape(replacement.caption, quote=False)
    return "\n".join(
        [
            f"{replacement.indent}<figure>",
            f'{replacement.indent}  <img src="{escaped_url}" alt="{escaped_alt}">',
            f"{replacement.indent}  <figcaption>{escaped_caption}</figcaption>",
            f"{replacement.indent}</figure>",
        ]
    )


def _escape_markdown_alt(alt_text: str) -> str:
    return alt_text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def _api_background_color(background_color: str) -> str:
    if background_color.startswith("#"):
        return background_color[1:]
    if re.fullmatch(r"[\da-fA-F]{3,8}", background_color):
        return background_color
    if background_color.startswith("!"):
        return background_color
    return f"!{background_color}"


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    response_body = b""
    if exc.fp is not None:
        response_body = exc.read()
    return (response_body.decode("utf-8", errors="replace") or str(exc)).strip()


def _strip_container_indent(lines: list[str], indent_width: int) -> list[str]:
    if indent_width == 0:
        return lines

    stripped_lines: list[str] = []
    for line in lines:
        removable = min(indent_width, len(line) - len(line.lstrip(" ")))
        stripped_lines.append(line[removable:])
    return stripped_lines
