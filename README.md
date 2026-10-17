# tend

A health report for all your git repos.

`tend` scans a folder of repositories and checks each one for the things that
make a repo easy to come back to: a real README, a license, a `.gitignore`,
recent activity, nothing committed that shouldn't be (dependency folders, OS junk,
**secrets**). It prints a ranked to-do list, worst repos first, and `--fix` drafts
the missing READMEs from each repo's own files.

```
$ tend --root ~/code
F  10  old-notes         no README · no license · .DS_Store is committed · OpenAI API key in notes.md:212
F  25  weekend-app       boilerplate README (Lovable) · no license · secret-looking file tracked: .env
D  55  arcade-game       no commits in 20 months · no README · no license
C  60  recipe-box        no README · no license
A 100  daily-log         all good

5 repos scanned, 4 need attention.
    2  possible secrets committed (rotate them first)
    3  missing, empty, or boilerplate README (tend --fix drafts one)
    4  no license
   ...
```

One Python file, standard library only, Python 3.9+.

## Install

```bash
pipx install git+https://github.com/laufferw/tend   # or: pip install .
```

Or run the file directly: `python3 tend.py --root ~/code`

## Usage

```bash
tend                          # repos under the current directory
tend --root ~/code --depth 3  # look deeper for repos
tend --issues-only            # hide repos with nothing to fix
tend --format markdown        # a table to paste into an issue or note
tend --format json            # for scripts
tend --fail-under 75          # exit 1 if any repo scores below 75 (for CI)
tend --fix                    # write README.draft.md wherever the README needs work
```

## What it checks

Every repo starts at 100. Each finding takes points off, and the grade is A (90+),
B (75+), C (60+), D (40+), or F.

| Check | Finding | Points |
|-------|---------|-------:|
| Secrets | A tracked file that looks like a credential (`.env`, `*.pem`, `id_rsa`, `credentials.json`, ...) | −40 each |
| Secrets | A token pattern in tracked text (AWS, GitHub, Anthropic, OpenAI, Slack, Google, Stripe, private-key headers) | −40 each |
| Commits | No commits yet | −40 |
| README | Missing | −25 |
| README | Nearly empty (under 80 characters of real text), or unchanged scaffold boilerplate (Lovable, Vite, Create React App, Next.js, Astro, dbt) | −20 |
| License | No `LICENSE`, `LICENCE` or `COPYING` file | −10 |
| Activity | No commits in over a year (−10) or over six months (−5) | −5 / −10 |
| `.gitignore` | Missing | −5 |
| Junk | `node_modules`, `.venv`, `__pycache__` or `.DS_Store` is committed | −5 each |
| Remote | No remote, so the repo exists only on this machine | −5 |
| Unpushed | Commits ahead of the upstream branch | −5 |
| Dirty | Uncommitted changes | −5 |

The summary under the table ranks fixes by how many repos need them, so you can
work through one kind of fix at a time.

## `--fix`: README drafts

For each repo whose README is missing, empty, or boilerplate, `tend --fix` writes
`README.draft.md` next to it. The draft is built only from what's in the repo:

- **Description:** taken from `package.json` or `pyproject.toml`, if either has one.
- **Stack:** frameworks from dependencies and Python imports (React, Vite, Supabase,
  Streamlit, Pygame, the Anthropic SDK, ...), marker files (Godot, dbt, Docker),
  and the top languages by file count.
- **Getting started:** commands from `package.json` scripts, `requirements.txt`, the
  Python entry point, Godot, or dbt.
- **Layout:** the repo's top-level files and folders.
- **Status:** commit count and date range.

Anything it can't infer is left as a `TODO:`. Review the draft, then rename it to
`README.md` and commit it yourself.

`--fix` never edits `README.md`, never runs git commands that change anything, and
replaces a `README.draft.md` only if `tend` wrote it.

## Inputs, outputs, and failure modes

- **Inputs:** the local git repos under `--root`. `tend` makes no network calls. It reads the
  working tree and runs read-only git commands (`ls-files`, `grep`, `log`, `status`,
  `rev-list`).
- **Outputs:** the report on stdout. Exit codes: `0` if no secrets were found and the
  `--fail-under` bar (if set) was met. `1` if any secret was found or a repo scored
  below `--fail-under`. `2` for a bad `--root` or no repos found.
- **Secrets are only checked in the current tree.** A key that was committed and then
  deleted is still in git history. Use a history scanner such as gitleaks or
  trufflehog for that. In any case, if `tend` flags a real key, **rotate it first**:
  in a public repo, assume it has already been copied.
- **Secret detection is pattern-based.** It skips obvious placeholders (`xxxx`,
  `your-key`, `example`, `test`) and anything inside vendored dependency folders.
  A committed `.env` isn't flagged when every variable in it is browser-public by
  design (`VITE_`, `NEXT_PUBLIC_`, `EXPO_PUBLIC_`, `PUBLIC_`, `REACT_APP_`), since
  those values ship in the app's JavaScript anyway.
  It can still miss custom token formats, or flag an unusual-but-harmless string.
  Findings show the file and line, never the secret itself.
- **"Stale" isn't "bad".** A finished project with no commits in a year is fine. The
  activity penalty is small so it mostly affects ordering.
- **Local view only.** GitHub-side settings (repo description, topics, visibility,
  branch protection) aren't checked.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests build throwaway repos for each case. Secret-shaped test strings are
assembled at runtime so the repo passes its own scan. CI runs the tests on Linux and
macOS with Python 3.9 and 3.13, then runs `tend --fail-under 90` on this repo.

## License

MIT
