"""A workspace package's npm tarball, as `deno pack` writes it, made publishable on npm: its
package.json given what npm shows and provenance checks (`description`, the first sentence of
its README; `homepage`; `repository`, this one, the package's directory in it), and a dependency
on each workspace package it imports, at that package's version, which `deno pack` leaves out
(`deno publish` writes them for JSR). Then every package its code imports must be declared:
one that isn't fails, rather than install where something else happens to bring it. A package
the host application must share with it is a peer (PEERS), at the range its deno.json gives.

    python tools/npm_package.py TARBALL PACKAGE_DIR

A build tool, kept out of the relay's package.
"""

import gzip
import io
import json
import re
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/mai-lam-labs/bruno"
HOMEPAGE = "https://docs.mailam.app/bruno/"
# each package's peers: what the application installs, of which it must use the application's
# copy (the AgentKit provider extends AgentKit's ActionProvider: a copy of its own wouldn't be
# the class AgentKit checks for)
PEERS = {"@mailam/bruno-agentkit": ("@coinbase/agentkit",)}
# an import's specifier in a module: `from "x"`, `import "x"`, `import("x")`, `export … from "x"`
_IMPORT = re.compile(r"""(?:\bfrom|\bimport)\s*\(?\s*["']([^"']+)["']""")
# a Markdown link, for its text
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


class PackageError(Exception):
    """The tarball imports a package it doesn't declare, or isn't what `deno pack` writes."""


@dataclass(frozen=True, slots=True)
class Member:
    """A workspace package: its name, version and directory, relative to the repository."""

    name: str
    version: str
    directory: str


def members() -> dict[str, Member]:
    """The workspace's packages that have a name, by name."""
    workspace = json.loads((ROOT / "deno.json").read_text())["workspace"]
    found: dict[str, Member] = {}
    for path in workspace:
        config = json.loads((ROOT / path / "deno.json").read_text())
        if "name" in config:
            directory = str(Path(path))
            found[config["name"]] = Member(config["name"], config["version"], directory)
    return found


def package_of(specifier: str) -> str | None:
    """The npm package a bare `specifier` names (`@scope/name/sub` is `@scope/name`); None for
    a relative or absolute path, or a runtime's own module (`node:`)."""
    if specifier.startswith((".", "/")) or ":" in specifier:
        return None
    parts = specifier.split("/")
    return "/".join(parts[:2]) if specifier.startswith("@") else parts[0]


def description(readme: str) -> str:
    """The README's first sentence after its title, its links as their text."""
    paragraph = readme.split("\n\n")[1].replace("\n", " ")
    sentence = _LINK.sub(r"\1", paragraph).split(". ")[0].rstrip(".")
    return sentence + "."


def made_publishable(tarball: Path, directory: Path) -> None:
    """`tarball` rewritten in place, its package.json completed and its imports checked."""
    with tarfile.open(tarball, "r:gz") as archive:
        entries = [(info, archive.extractfile(info)) for info in archive.getmembers()]
        files = {info.name: data.read() for info, data in entries if data is not None}
        infos = [info for info, _ in entries]
    if "package/package.json" not in files:
        raise PackageError(f"{tarball}: no package/package.json")
    package = json.loads(files["package/package.json"])
    workspace = members()
    member = workspace.get(package["name"])
    if member is None or Path(member.directory) != directory:
        raise PackageError(f"{tarball}: {package['name']} isn't {directory}'s package")
    imported: set[str] = set()
    for name, data in files.items():
        if name.endswith(".js"):
            for specifier in _IMPORT.findall(data.decode()):
                found = package_of(specifier)
                if found is not None and found != package["name"]:
                    imported.add(found)
    dependencies: dict[str, str] = dict(package.get("dependencies", {}))
    for name in sorted(imported):
        if name in workspace and name not in dependencies:
            dependencies[name] = workspace[name].version
    peers: dict[str, str] = dict(package.get("peerDependencies", {}))
    for name in PEERS.get(package["name"], ()):
        if name not in dependencies:
            raise PackageError(f"{package['name']}: its peer {name} isn't among its dependencies")
        peers[name] = dependencies.pop(name)
    declared = dependencies.keys() | peers.keys()
    undeclared = sorted(imported - declared)
    if undeclared:
        raise PackageError(f"{package['name']} imports what it doesn't declare: {undeclared}")
    readme = files.get("package/README.md")
    if readme is None:
        raise PackageError(f"{package['name']}: no README.md")
    completed = {
        "name": package["name"],
        "version": package["version"],
        "description": description(readme.decode()),
        "homepage": HOMEPAGE,
        "repository": {
            "type": "git",
            "url": f"git+{REPOSITORY}.git",
            "directory": member.directory,
        },
        **{
            k: v
            for k, v in package.items()
            if k not in ("name", "version", "dependencies", "peerDependencies")
        },
        **({"dependencies": dict(sorted(dependencies.items()))} if dependencies else {}),
        **({"peerDependencies": dict(sorted(peers.items()))} if peers else {}),
    }
    files["package/package.json"] = (json.dumps(completed, indent=2) + "\n").encode()
    out = io.BytesIO()
    # gzip's header given no time, so the same sources make the same bytes: what's published
    # can be checked against what was tested
    with (
        gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for info in infos:
            data = files.get(info.name)
            if data is None:
                archive.addfile(info)
                continue
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    tarball.write_bytes(out.getvalue())
    print(f"{package['name']}@{package['version']}: dependencies {dependencies}, peers {peers}")


if __name__ == "__main__":
    try:
        made_publishable(Path(sys.argv[1]), Path(sys.argv[2]))
    except PackageError as e:
        raise SystemExit(f"error: {e}") from e
