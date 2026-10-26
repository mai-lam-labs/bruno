"""The documentation site's llms.txt (llmstxt.org), written into the built site (make docs).

The pages are zensical.toml's navigation, in its order and sections; each is published as
Markdown beside its HTML (`SPEC.md` beside `SPEC/`), its included files (pymdownx.snippets'
`--8<-- "path"` lines) expanded. llms.txt links them, each with its front matter's
`description`; llms-full.txt is every page, in order.

    python tools/llms.py CONFIG SITE_DIR

A build tool, kept out of the relay's package.
"""

import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# a pymdownx.snippets inclusion: the whole line, the path relative to the repository's root
_SNIPPET = re.compile(r'^--8<-- "([^"]+)"$', re.MULTILINE)
# a page's front matter: `key: "value"` lines between two `---` lines, at its start, each value
# double-quoted (YAML's double-quoted strings, as JSON's for what these hold)
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n+", re.DOTALL)


class DocsError(Exception):
    """A page the navigation names is missing, or lacks a description."""


@dataclass(frozen=True, slots=True)
class Page:
    """A page of the site: its title in the navigation, its source under docs_dir, and its
    Markdown with the front matter taken off and its inclusions expanded."""

    title: str
    path: str
    description: str
    markdown: str


@dataclass(frozen=True, slots=True)
class Section:
    """A heading of llms.txt: a top-level navigation section, or the pages outside any."""

    title: str
    pages: tuple[Page, ...]


def page(root: Path, docs: Path, title: str, path: str) -> Page:
    source = (docs / path).read_text()
    front = _FRONT_MATTER.match(source)
    fields = dict(
        line.split(": ", 1) for line in (front.group(1).splitlines() if front else ()) if line
    )
    if "description" not in fields:
        raise DocsError(f"{path}: no description in its front matter")
    description: str = json.loads(fields["description"])
    body = source[front.end() :] if front else source
    expanded = _SNIPPET.sub(lambda m: (root / m.group(1)).read_text().strip("\n"), body)
    return Page(title, path, description, expanded)


type Nav = list[dict[str, str | Nav]]


def pages(root: Path, docs: Path, nav: Nav) -> list[Page]:
    """A navigation section's pages, in order, its subsections' flattened into it."""
    result: list[Page] = []
    for entry in nav:
        ((title, target),) = entry.items()
        result += (
            [page(root, docs, title, target)]
            if isinstance(target, str)
            else pages(root, docs, target)
        )
    return result


def sections(root: Path, config: Path) -> tuple[str, str, tuple[Section, ...]]:
    """The site's name, its description, and its navigation read into sections."""
    project = tomllib.loads(config.read_text())["project"]
    docs = root / project["docs_dir"]
    loose: list[Page] = []
    result: list[Section] = []
    for entry in project["nav"]:
        ((title, target),) = entry.items()
        if isinstance(target, str):
            loose.append(page(root, docs, title, target))
        else:
            result.append(Section(title, tuple(pages(root, docs, target))))
    return (
        project["site_name"],
        project["site_description"],
        (Section("Docs", tuple(loose)), *result),
    )


def write(root: Path, config: Path, site: Path) -> None:
    name, description, parts = sections(root, config)
    url = tomllib.loads(config.read_text())["project"]["site_url"]
    index = [f"# {name}", "", f"> {description}"]
    full = [f"# {name}", "", f"> {description}"]
    for section in parts:
        index += ["", f"## {section.title}", ""]
        for p in section.pages:
            (site / p.path).parent.mkdir(parents=True, exist_ok=True)
            (site / p.path).write_text(p.markdown)
            index.append(f"- [{p.title}]({url}{p.path}): {p.description}")
            full += ["", p.markdown.strip("\n")]
    (site / "llms.txt").write_text("\n".join(index) + "\n")
    (site / "llms-full.txt").write_text("\n".join(full) + "\n")


if __name__ == "__main__":
    config = Path(sys.argv[1])
    write(config.parent, config, Path(sys.argv[2]))
