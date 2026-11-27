"""The documentation site's llms.txt (llmstxt.org), written into the built site (make docs).

The pages are zensical.toml's navigation, in its order and sections; each is published as
Markdown beside its HTML (`SPEC.md` beside `SPEC/`), its included files (pymdownx.snippets'
`--8<-- "path"` lines) expanded. llms.txt links them, each with its front matter's
`description`: first the pages that answer a task (their front matter's `task`, what a reader
arrives wanting to do), then every page in the navigation's order. llms-full.txt is every page,
in order, each link in it made absolute: a page's links are relative to the page, and the one
file is somewhere else.

    python tools/llms.py CONFIG SITE_DIR

A build tool, kept out of the relay's package.
"""

import json
import posixpath
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


# an inline link's target, `](target)` or `](target "title")`: an image's too
_LINK = re.compile(r"(\]\()([^)\s]+)")
# a fenced code block, or an inline code span: what looks like a link in one isn't one
_CODE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$|`[^`\n]*`", re.MULTILINE | re.DOTALL)
# a target with a scheme, or a host: it leads the same way from anywhere
_ELSEWHERE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:|//")
# the heading the pages that answer a task are listed under, first
TASKS = "Start here"
# the headings of the pages outside any section: before the first section, and after one
LOOSE, LOOSE_AFTER = "Docs", "Reference"


class DocsError(Exception):
    """A page the navigation names is missing, or lacks a description; or a link in a page
    leads nowhere in the site."""


@dataclass(frozen=True, slots=True)
class Page:
    """A page of the site: its title in the navigation, its source under docs_dir, its
    Markdown with the front matter taken off and its inclusions expanded, and the task it
    answers, if its front matter names one."""

    title: str
    path: str
    description: str
    markdown: str
    task: str | None = None

    def rebased(self, url: str) -> tuple[str, list[str]]:
        """Its Markdown with each link that's relative to the page made absolute, under the
        site at `url`, and the paths in the site those links lead to: read from anywhere else
        (llms-full.txt), a relative link leads somewhere else. DocsError for a link out of the
        site."""
        linked: list[str] = []

        def absolute(link: re.Match[str]) -> str:
            target = link.group(2)
            if _ELSEWHERE.match(target):
                return link.group(0)
            path, hash_, fragment = target.partition("#")
            to = self.path
            if path:
                to = posixpath.normpath(posixpath.join(posixpath.dirname(self.path), path))
            if to.startswith(".."):
                raise DocsError(f"{self.path}: the link {target} leads out of the site")
            linked.append(to)
            return f"{link.group(1)}{url}{to}{hash_}{fragment}"

        out: list[str] = []
        at = 0
        for code in _CODE.finditer(self.markdown):
            out += [_LINK.sub(absolute, self.markdown[at : code.start()]), code.group(0)]
            at = code.end()
        out.append(_LINK.sub(absolute, self.markdown[at:]))
        return "".join(out), linked


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
    task: str | None = json.loads(fields["task"]) if "task" in fields else None
    body = source[front.end() :] if front else source
    expanded = _SNIPPET.sub(lambda m: (root / m.group(1)).read_text().strip("\n"), body)
    return Page(title, path, description, expanded, task)


type Nav = list[dict[str, str | Nav]]


def pages(root: Path, docs: Path, nav: Nav) -> list[Page]:
    """A navigation section's pages, in order, its subsections' flattened into it."""
    result: list[Page] = []
    for entry in nav:
        ((title, target),) = entry.items()
        if not isinstance(target, str):
            result += pages(root, docs, target)
        else:
            result.append(page(root, docs, title, target))
    return result


def sections(root: Path, config: Path) -> tuple[str, str, tuple[Section, ...]]:
    """The site's name, its description, and its navigation read into sections, in its
    order: the pages outside any section are a section of their own where they come, LOOSE
    before the first section and LOOSE_AFTER once there's been one."""
    project = tomllib.loads(config.read_text())["project"]
    docs = root / project["docs_dir"]
    loose: list[Page] = []
    result: list[Section] = []

    def gathered() -> None:
        if loose:
            named = any(s.title not in (LOOSE, LOOSE_AFTER) for s in result)
            result.append(Section(LOOSE_AFTER if named else LOOSE, tuple(loose)))
            loose.clear()

    for entry in project["nav"]:
        ((title, target),) = entry.items()
        if isinstance(target, str):
            loose += pages(root, docs, [entry])
        elif section := pages(root, docs, target):
            gathered()
            result.append(Section(title, tuple(section)))
    gathered()
    return project["site_name"], project["site_description"], tuple(result)


def write(root: Path, config: Path, site: Path) -> None:
    name, description, parts = sections(root, config)
    url = tomllib.loads(config.read_text())["project"]["site_url"]
    index = [f"# {name}", "", f"> {description}"]
    full = [f"# {name}", "", f"> {description}"]
    every = [p for section in parts for p in section.pages]
    if tasks := [p for p in every if p.task is not None]:
        index += ["", f"## {TASKS}", ""]
        index += [f"- [{p.task}]({url}{p.path}): {p.description}" for p in tasks]
    linked: list[tuple[Page, str]] = []
    for section in parts:
        index += ["", f"## {section.title}", ""]
        for p in section.pages:
            (site / p.path).parent.mkdir(parents=True, exist_ok=True)
            (site / p.path).write_text(p.markdown)
            index.append(f"- [{p.title}]({url}{p.path}): {p.description}")
            markdown, to = p.rebased(url)
            linked += [(p, path) for path in to]
            full += ["", markdown.strip("\n")]
    # once every page is in the site: each link in one leads to something there
    missing = sorted({f"{p.path}: {path}" for p, path in linked if not (site / path).exists()})
    if missing:
        raise DocsError("links that lead nowhere in the site:\n  " + "\n  ".join(missing))
    (site / "llms.txt").write_text("\n".join(index) + "\n")
    (site / "llms-full.txt").write_text("\n".join(full) + "\n")


if __name__ == "__main__":
    config = Path(sys.argv[1])
    write(config.parent, config, Path(sys.argv[2]))
