#!/usr/bin/env python3
"""tend: a health report for all your git repos.

Scans a folder of git repositories, checks each one for the things that make a
repo easy to pick up again (a real README, a license, a .gitignore, recent
commits, no committed secrets), and prints a ranked to-do list. `--fix` drafts
missing READMEs from each repo's own files. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

__version__ = "0.1.0"

SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target"}

CRITICAL, WARN, INFO = "critical", "warn", "info"

# README text that means "the scaffold's README was never replaced".
BOILERPLATE = {
    "Lovable": "Welcome to your Lovable project",
    "dbt starter": "Welcome to your new dbt project",
    "Astro starter": "Astro Starter Kit",
    "Create React App": "Getting Started with Create React App",
    "Vite template": "This template provides a minimal setup to get React working in Vite",
    "Next.js starter": "project bootstrapped with [`create-next-app`]",
}
MIN_README_CHARS = 80

SECRET_PATTERNS = {
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{36}",
    "GitHub fine-grained token": r"github_pat_[A-Za-z0-9_]{50,}",
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "OpenAI API key": r"sk-(proj-)?[A-Za-z0-9_-]{32,}",
    "Slack token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "Stripe live key": r"sk_live_[0-9A-Za-z]{24,}",
    "Private key": r"-----BEGIN ([A-Z]+ )?PRIVATE KEY-----",
}
PLACEHOLDER = re.compile(r"x{4,}|X{4,}|your|example|placeholder|dummy|fake|redacted|test|\.\.\.|<|\*{3,}", re.I)
SECRET_FILE = re.compile(
    r"(^|/)(\.env(\.[\w.-]+)?|id_rsa|id_ed25519|id_ecdsa"
    r"|credentials\.json|service[-_]account[\w-]*\.json|[^/]+\.pem|[^/]+\.p12|[^/]+\.pfx)$"
)
SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist")
# Committed dependency folders are reported as junk; their vendored files
# (CA bundles, test keys) aren't the repo owner's secrets.
VENDORED = re.compile(r"(^|/)(node_modules|\.venv|venv|site-packages|vendor)/")
JUNK = {
    "node_modules/": "node_modules",
    "__pycache__/": "__pycache__",
    ".venv/": ".venv",
    ".DS_Store": ".DS_Store",
}


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    penalty: int


@dataclass
class Report:
    name: str
    path: str
    last_commit: str | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def score(self) -> int:
        return max(0, 100 - sum(f.penalty for f in self.findings))

    @property
    def grade(self) -> str:
        s = self.score
        return "A" if s >= 90 else "B" if s >= 75 else "C" if s >= 60 else "D" if s >= 40 else "F"

    def to_dict(self) -> dict:
        return {**asdict(self), "score": self.score, "grade": self.grade}


def find_repos(root: Path, max_depth: int = 2) -> list[Path]:
    """Git repositories at or below root, up to max_depth levels down.

    Does not descend into a repository once found, and skips hidden and
    dependency directories.
    """
    repos: list[Path] = []

    def walk(path: Path, depth: int) -> None:
        if (path / ".git").exists():
            repos.append(path)
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in path.iterdir() if p.is_dir())
        except (PermissionError, FileNotFoundError):
            return
        for child in children:
            if child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            walk(child, depth + 1)

    walk(root.resolve(), 0)
    return repos


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def tracked_files(repo: Path) -> list[str]:
    return [f for f in git(repo, "ls-files", "-z", check=False).split("\0") if f]


def find_readme(repo: Path) -> Path | None:
    for p in sorted(repo.iterdir()):
        if p.is_file() and p.name.lower().startswith("readme") and p.name.lower() != "readme.draft.md":
            return p
    return None


def readme_problem(text: str) -> str | None:
    """Why this README doesn't do its job, or None if it does."""
    for label, signature in BOILERPLATE.items():
        if signature in text:
            return f"boilerplate README ({label})"
    body = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(("#", "<", "![", "[!["))
    )
    if len(re.sub(r"\s+", "", body)) < MIN_README_CHARS:
        return "README is nearly empty"
    return None


def is_placeholder(token: str) -> bool:
    return bool(PLACEHOLDER.search(token)) or len(set(token)) < 8


# Frontend build tools inline these into the shipped JavaScript, so they are
# public by design (e.g. a Supabase publishable key in a Vite app's .env).
PUBLIC_ENV_PREFIXES = ("VITE_", "NEXT_PUBLIC_", "EXPO_PUBLIC_", "PUBLIC_", "REACT_APP_")


def is_public_env(path: Path) -> bool:
    """True for a .env file whose every variable is a browser-public one."""
    if not re.search(r"(^|/)\.env(\.[\w.-]+)?$", str(path)):
        return False
    names = [
        line.split("=", 1)[0].strip().removeprefix("export ").strip()
        for line in _read(path).splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    return bool(names) and all(n.startswith(PUBLIC_ENV_PREFIXES) for n in names)


def scan_secrets(repo: Path, files: list[str]) -> list[Finding]:
    findings = []
    for f in files:
        if (SECRET_FILE.search(f) and not f.endswith(SAFE_SUFFIXES) and not VENDORED.search(f)
                and not is_public_env(repo / f)):
            findings.append(Finding("secrets", CRITICAL, f"secret-looking file tracked: {f}", 40))

    args = ["grep", "-I", "-n", "-E", "--no-color"]
    for pattern in SECRET_PATTERNS.values():
        args += ["-e", pattern]
    out = git(repo, *args, check=False)  # exit 1 just means no matches
    # Token must start at a word boundary, so "task-list_x" isn't read as "sk-list_x".
    compiled = {label: re.compile(r"(?<![\w-])" + p) for label, p in SECRET_PATTERNS.items()}
    seen = set()
    for line in out.splitlines():
        location, _, content = line.partition(":")
        lineno, _, content = content.partition(":")
        if VENDORED.search(location):
            continue
        for label, rx in compiled.items():
            m = rx.search(content)
            if not m:
                continue
            token = m.group(0)
            if label != "Private key" and is_placeholder(token):
                break
            key = (location, label)
            if key not in seen:
                seen.add(key)
                # Location only: never echo any part of a secret.
                findings.append(Finding("secrets", CRITICAL, f"{label} in {location}:{lineno}", 40))
            break
    return findings


def check_repo(repo: Path, today: dt.date | None = None) -> Report:
    today = today or dt.date.today()
    report = Report(name=repo.name, path=str(repo), last_commit=None)
    add = report.findings.append

    has_commits = bool(git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).strip())
    if not has_commits:
        add(Finding("commits", WARN, "no commits yet", 40))
    else:
        last = git(repo, "log", "-1", "--format=%cs").strip()
        report.last_commit = last
        age = (today - dt.date.fromisoformat(last)).days
        if age > 365:
            add(Finding("activity", INFO, f"no commits in {age // 30} months", 10))
        elif age > 180:
            add(Finding("activity", INFO, f"no commits in {age // 30} months", 5))

    readme = find_readme(repo)
    if readme is None:
        add(Finding("readme", WARN, "no README", 25))
    else:
        problem = readme_problem(readme.read_text(encoding="utf-8", errors="replace"))
        if problem:
            add(Finding("readme", WARN, problem, 20))

    names = {p.name.lower() for p in repo.iterdir()}
    if not any(n.startswith(("license", "licence", "copying")) for n in names):
        add(Finding("license", WARN, "no license", 10))
    if ".gitignore" not in names:
        add(Finding("gitignore", WARN, "no .gitignore", 5))

    files = tracked_files(repo)
    for marker, label in JUNK.items():
        if any(f == marker.rstrip("/") or f.startswith(marker) or f"/{marker}" in f"/{f}" for f in files):
            add(Finding("junk", WARN, f"{label} is committed", 5))

    if not git(repo, "remote", check=False).strip():
        add(Finding("remote", INFO, "no remote (only on this machine)", 5))
    elif has_commits:
        ahead = git(repo, "rev-list", "--count", "@{u}..HEAD", check=False).strip()
        if ahead.isdigit() and int(ahead) > 0:
            add(Finding("unpushed", WARN, f"{ahead} unpushed commit{'s' if ahead != '1' else ''}", 5))

    dirty = [l for l in git(repo, "status", "--porcelain", check=False).splitlines() if l]
    if dirty:
        add(Finding("dirty", INFO, f"{len(dirty)} uncommitted change{'s' if len(dirty) != 1 else ''}", 5))

    report.findings.extend(scan_secrets(repo, files))
    return report


# --- README drafting -------------------------------------------------------

LANGUAGES = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".java": "Java",
    ".scala": "Scala", ".gd": "GDScript", ".cs": "C#", ".swift": "Swift", ".kt": "Kotlin",
    ".sql": "SQL", ".sh": "Shell", ".html": "HTML", ".css": "CSS", ".md": "Markdown",
    ".ipynb": "Jupyter", ".astro": "Astro", ".vue": "Vue", ".svelte": "Svelte",
}
JS_FRAMEWORKS = {
    "next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte", "astro": "Astro",
    "vite": "Vite", "express": "Express", "tailwindcss": "Tailwind CSS",
    "@supabase/supabase-js": "Supabase", "@anthropic-ai/sdk": "Anthropic SDK", "openai": "OpenAI SDK",
}
PY_FRAMEWORKS = {
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI", "streamlit": "Streamlit",
    "pygame": "Pygame", "anthropic": "Anthropic SDK", "openai": "OpenAI SDK",
    "langchain": "LangChain", "pandas": "pandas",
}
DRAFT_MARKER = "<!-- drafted by tend: review and edit, then rename to README.md -->"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return ""


def _python_requirements(repo: Path, files: list[str]) -> set[str]:
    """Package names from requirements.txt / pyproject.toml, plus top-level imports."""
    text = _read(repo / "requirements.txt") + "\n" + _read(repo / "pyproject.toml")
    names = {m.group(1).lower() for m in re.finditer(r"^[\s\"']*([A-Za-z][\w.-]*)", text, re.M)}
    for f in files[:500]:
        if f.endswith(".py") and not VENDORED.search(f):
            src = _read(repo / f)
            names |= {m.group(1).lower() for m in re.finditer(r"^\s*(?:from|import)\s+(\w+)", src, re.M)}
    return names


def draft_readme(repo: Path) -> str:
    files = tracked_files(repo) or [
        str(p.relative_to(repo)) for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.parts
    ]
    pkg: dict = {}
    if (repo / "package.json").exists():
        try:
            pkg = json.loads(_read(repo / "package.json"))
        except json.JSONDecodeError:
            pkg = {}
    pyproject = _read(repo / "pyproject.toml")
    m = re.search(r'^description\s*=\s*"([^"]+)"', pyproject, re.M)
    description = pkg.get("description") or (m.group(1) if m else "")

    langs = Counter(LANGUAGES[Path(f).suffix.lower()] for f in files if Path(f).suffix.lower() in LANGUAGES)
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    pyreqs = _python_requirements(repo, files)
    stack = [name for dep, name in JS_FRAMEWORKS.items() if dep in deps]
    stack += [name for dep, name in PY_FRAMEWORKS.items() if dep in pyreqs and name not in stack]
    if (repo / "project.godot").exists():
        stack.append("Godot")
    if (repo / "dbt_project.yml").exists():
        stack.append("dbt")
    if (repo / "Dockerfile").exists() or (repo / "docker-compose.yml").exists():
        stack.append("Docker")

    run: list[str] = []
    if pkg:
        run.append("npm install")
        scripts = pkg.get("scripts", {})
        run += [f"npm run {s}" for s in ("dev", "start", "build", "test") if s in scripts]
    if (repo / "requirements.txt").exists():
        run.append("pip install -r requirements.txt")
    elif pyproject:
        run.append("pip install -e .")
    entry = next((e for e in ("main.py", "app.py", "run.py", "manage.py") if (repo / e).exists()), None)
    if entry:
        run.append(f"streamlit run {entry}" if "streamlit" in pyreqs else f"python {entry}")
    if (repo / "project.godot").exists():
        run.append("# open project.godot in the Godot editor")
    if (repo / "dbt_project.yml").exists():
        run += ["dbt deps", "dbt run"]

    top = sorted({f.split("/")[0] + ("/" if "/" in f else "") for f in files})
    top.sort(key=lambda p: (not p.endswith("/"), p.lower()))

    lines = [DRAFT_MARKER, f"# {repo.name}", ""]
    lines.append(description or "TODO: one sentence on what this is and who it's for.")
    lines.append("")
    if stack or langs:
        lines += ["## Stack", ""]
        if stack:
            lines.append(f"- Built with: {', '.join(stack)}")
        if langs:
            lines.append(f"- Languages: {', '.join(l for l, _ in langs.most_common(4))}")
        lines.append("")
    lines += ["## Getting started", ""]
    if run:
        lines += ["```bash", *run, "```"]
    else:
        lines.append("TODO: how to run or use this.")
    lines.append("")
    if top:
        lines += ["## Layout", ""]
        lines += [f"- `{p}`" for p in top[:15]]
        if len(top) > 15:
            lines.append(f"- … and {len(top) - 15} more")
        lines.append("")
    count = git(repo, "rev-list", "--count", "HEAD", check=False).strip()
    if count.isdigit():
        first = git(repo, "log", "--reverse", "--format=%cs", check=False).split("\n", 1)[0]
        last = git(repo, "log", "-1", "--format=%cs", check=False).strip()
        lines += ["## Status", "", f"{count} commit{'s' if count != '1' else ''}, {first} to {last}. TODO: active, paused, or done?", ""]
    return "\n".join(lines)


def write_draft(repo: Path) -> Path | None:
    """Write README.draft.md. Never touches README.md, and only replaces a draft tend wrote."""
    path = repo / "README.draft.md"
    if path.exists() and DRAFT_MARKER not in _read(path):
        return None
    path.write_text(draft_readme(repo), encoding="utf-8")
    return path


# --- Output ----------------------------------------------------------------

def render_text(reports: list[Report]) -> str:
    width = max((len(r.name) for r in reports), default=4)
    lines = []
    for r in reports:
        issues = " · ".join(f.message for f in r.findings) or "all good"
        lines.append(f"{r.grade} {r.score:>3}  {r.name:<{width}}  {issues}")
    return "\n".join(lines)


def render_markdown(reports: list[Report]) -> str:
    lines = ["| Grade | Score | Repo | Last commit | Issues |", "|---|---|---|---|---|"]
    for r in reports:
        issues = "<br>".join(f.message.replace("|", "\\|") for f in r.findings) or "all good"
        lines.append(f"| {r.grade} | {r.score} | {r.name} | {r.last_commit or '—'} | {issues} |")
    return "\n".join(lines)


def summary(reports: list[Report]) -> str:
    """Fixes ranked by how many repos need them."""
    counts: Counter = Counter()
    for r in reports:
        for check in {f.check for f in r.findings}:
            counts[check] += 1
    labels = {
        "secrets": "possible secrets committed (rotate them first)",
        "readme": "missing, empty, or boilerplate README (tend --fix drafts one)",
        "commits": "no commits yet",
        "license": "no license",
        "gitignore": "no .gitignore",
        "junk": "dependency or OS junk committed",
        "unpushed": "unpushed commits",
        "remote": "no remote",
        "dirty": "uncommitted changes",
        "activity": "no commits in 6+ months",
    }
    order = list(labels)
    needs = sum(1 for r in reports if r.findings)
    lines = [f"{len(reports)} repos scanned, {needs} need attention."]
    for check in sorted(counts, key=lambda c: (order.index(c), -counts[c])):
        lines.append(f"  {counts[check]:>3}  {labels[check]}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tend", description="A health report for all your git repos.")
    p.add_argument("--root", type=Path, default=Path.cwd(),
                   help="folder to scan for repos (default: current directory)")
    p.add_argument("--depth", type=int, default=2,
                   help="how many folder levels below --root to look for repos (default: 2)")
    p.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    p.add_argument("--issues-only", action="store_true", help="hide repos with nothing to fix")
    p.add_argument("--fail-under", type=int, metavar="SCORE",
                   help="exit 1 if any repo scores below SCORE")
    p.add_argument("--fix", action="store_true",
                   help="write README.draft.md in each repo whose README needs work")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root.expanduser()
    if not root.is_dir():
        print(f"tend: --root {root} is not a directory", file=sys.stderr)
        return 2
    repos = find_repos(root, args.depth)
    if not repos:
        print(f"tend: no git repositories found under {root} (depth {args.depth})", file=sys.stderr)
        return 2

    reports = [check_repo(r) for r in repos]
    reports.sort(key=lambda r: (r.score, r.name.lower()))
    shown = [r for r in reports if r.findings] if args.issues_only else reports

    if args.format == "json":
        print(json.dumps([r.to_dict() for r in shown], indent=2))
    elif args.format == "markdown":
        print(render_markdown(shown))
        print()
        print("```\n" + summary(reports) + "\n```")
    else:
        print(render_text(shown))
        print()
        print(summary(reports))

    if args.fix:
        drafted, skipped = [], []
        for r in reports:
            if any(f.check == "readme" for f in r.findings):
                path = write_draft(Path(r.path))
                (drafted if path else skipped).append(path or Path(r.path) / "README.draft.md")
        out = sys.stderr if args.format == "json" else sys.stdout
        print(f"\nDrafted {len(drafted)} README{'s' if len(drafted) != 1 else ''}:", file=out)
        for p in drafted:
            print(f"  {p}", file=out)
        for p in skipped:
            print(f"  skipped {p} (exists and wasn't written by tend)", file=out)

    critical = any(f.severity == CRITICAL for r in reports for f in r.findings)
    below = args.fail_under is not None and any(r.score < args.fail_under for r in reports)
    return 1 if critical or below else 0


if __name__ == "__main__":
    sys.exit(main())
