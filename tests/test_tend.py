import base64
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tend  # noqa: E402

GOOD_README = "# app\n\n" + "This app does a useful thing for the people who need it. " * 3 + "\n"
# Built at runtime so this file never contains a literal secret-shaped string.
FAKE_AWS = "AKIA" + "Q7R2M9KX4TBW8ZPL"
FAKE_GH = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
KEY_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
LONG_VALUE = "Zq8vR2mK" + "4pL9xN3wT7yB5c"


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def fake_jwt(claims):
    return ".".join([b64({"alg": "HS256", "typ": "JWT"}), b64(claims), "s1gnatureAbc123XyZ456"])


def run_git(repo, *args, when=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def make_repo(path, files=None, *, commit=True, when=None):
    """A repo with the given {relative path: content} committed in one commit."""
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "config", "commit.gpgsign", "false")
    for rel, content in (files or {}).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if commit and files:
        run_git(path, "add", "-A")
        run_git(path, "commit", "-q", "-m", "init", when=when)
    return path


HEALTHY = {"README.md": GOOD_README, "LICENSE": "MIT", ".gitignore": "*.pyc\n", "app.py": "print(1)\n"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.today = dt.date.today()

    def tearDown(self):
        self._tmp.cleanup()

    def checks(self, report):
        return {f.check for f in report.findings}

    def messages(self, report):
        return " | ".join(f.message for f in report.findings)


class FindReposTest(Base):
    def test_depth_and_skips(self):
        make_repo(self.root / "a", HEALTHY)
        make_repo(self.root / "grp" / "b", HEALTHY)
        make_repo(self.root / "node_modules" / "c", HEALTHY)
        make_repo(self.root / ".hidden" / "d", HEALTHY)
        make_repo(self.root / "a" / "nested", HEALTHY)
        self.assertEqual([p.name for p in tend.find_repos(self.root)], ["a", "b"])
        self.assertEqual([p.name for p in tend.find_repos(self.root, 1)], ["a"])


class ReadmeTest(unittest.TestCase):
    def test_classification(self):
        self.assertIsNone(tend.readme_problem(GOOD_README))
        self.assertEqual(tend.readme_problem("# Forward\n"), "README is nearly empty")
        self.assertEqual(
            tend.readme_problem("# Forward\n![badge](x)\n<img src=x>\nshort line\n"),
            "README is nearly empty",
        )
        self.assertEqual(
            tend.readme_problem("# Welcome to your Lovable project\n" + GOOD_README),
            "boilerplate README (Lovable)",
        )
        self.assertEqual(
            tend.readme_problem("Welcome to your new dbt project!\n" + GOOD_README),
            "boilerplate README (dbt starter)",
        )


class CheckRepoTest(Base):
    def test_healthy_repo_scores_100(self):
        repo = make_repo(self.root / "good", HEALTHY)
        run_git(repo, "remote", "add", "origin", "https://example.com/good.git")
        report = tend.check_repo(repo)
        self.assertEqual(report.findings, [])
        self.assertEqual((report.score, report.grade), (100, "A"))
        self.assertEqual(report.last_commit, self.today.isoformat())

    def test_bare_repo_collects_every_basic_finding(self):
        repo = make_repo(self.root / "bare", {"notes.txt": "hi"})
        report = tend.check_repo(repo)
        self.assertEqual(self.checks(report), {"readme", "license", "gitignore", "remote"})
        self.assertIn("no README", self.messages(report))
        self.assertEqual(report.score, 100 - 25 - 10 - 5 - 5)
        self.assertEqual(report.grade, "D")

    def test_no_commits(self):
        repo = make_repo(self.root / "empty")
        report = tend.check_repo(repo)
        self.assertIn("no commits yet", self.messages(report))
        self.assertIsNone(report.last_commit)
        self.assertEqual(report.grade, "F")

    def test_stale_activity(self):
        old = (dt.datetime.now() - dt.timedelta(days=400)).isoformat()
        repo = make_repo(self.root / "old", HEALTHY, when=old)
        report = tend.check_repo(repo)
        self.assertIn("no commits in 13 months", self.messages(report))
        mid = make_repo(self.root / "mid", HEALTHY,
                        when=(dt.datetime.now() - dt.timedelta(days=200)).isoformat())
        f = [f for f in tend.check_repo(mid).findings if f.check == "activity"][0]
        self.assertEqual(f.penalty, 5)

    def test_committed_junk(self):
        repo = make_repo(self.root / "junk", {
            **HEALTHY,
            "node_modules/x/index.js": "1",
            "src/__pycache__/cache.dat": "1",
            ".DS_Store": "1",
        })
        msgs = self.messages(tend.check_repo(repo))
        for label in ("node_modules is committed", "__pycache__ is committed", ".DS_Store is committed"):
            self.assertIn(label, msgs)

    def test_tend_toml_skips_checks(self):
        repo = make_repo(self.root / "notes", {
            "notes.md": "x",
            ".tend.toml": '# private notes\nskip = ["license", "gitignore", "nonsense"]\n',
        })
        report = tend.check_repo(repo)
        self.assertNotIn("license", self.checks(report))
        self.assertNotIn("gitignore", self.checks(report))
        self.assertIn(".tend.toml: unknown check 'nonsense'", self.messages(report))
        self.assertEqual(report.skipped, ["gitignore", "license"])
        self.assertIn("(skips: gitignore, license)", tend.render_text([report]))

    def test_dirty_and_unpushed(self):
        upstream = make_repo(self.root / "up", HEALTHY)
        clone = self.root / "clone"
        subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True, capture_output=True)
        run_git(clone, "config", "commit.gpgsign", "false")
        (clone / "new.txt").write_text("x")
        run_git(clone, "add", "-A")
        run_git(clone, "commit", "-q", "-m", "local")
        (clone / "scratch.txt").write_text("y")
        msgs = self.messages(tend.check_repo(clone))
        self.assertIn("1 unpushed commit", msgs)
        self.assertIn("1 uncommitted change", msgs)


class SecretsTest(Base):
    def scan(self, files):
        repo = make_repo(self.root / "s", {**HEALTHY, **files})
        return [f.message for f in tend.check_repo(repo).findings if f.check == "secrets"]

    def test_real_looking_tokens_are_flagged_without_echoing_them(self):
        found = self.scan({"config.py": f'AWS = "{FAKE_AWS}"\ntoken = "{FAKE_GH}"\n'})
        self.assertEqual(found, ["AWS access key in config.py:1", "GitHub token in config.py:2"])
        self.assertNotIn(FAKE_AWS[:6], " ".join(found))

    def test_private_key_header(self):
        found = self.scan({"notes.md": f"x\n{KEY_HEADER}\nabc\n"})
        self.assertEqual(found, ["Private key in notes.md:2"])

    def test_placeholders_and_lookalikes_are_ignored(self):
        found = self.scan({
            "docs.md": "export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx\n"
                       "OPENAI_API_KEY=sk-your-key-goes-here-and-more-chars-here\n",
            "page.html": '<li class="task-list_item_with_a_long_class_name_abc">\n',
            "tests/conftest.py": 'SLACK = "xoxb-test-token-1234567890"\n',
        })
        self.assertEqual(found, [])

    def test_secret_files(self):
        found = self.scan({
            ".env": "A=1",
            "deploy/key.pem": "x",
            ".env.example": "A=",
            ".env.production.example": "A=",
            ".venv/lib/site-packages/certifi/cacert.pem": "x",
        })
        self.assertEqual(sorted(found), [
            "secret-looking file tracked: .env",
            "secret-looking file tracked: deploy/key.pem",
        ])

    def test_jwts_flagged_except_supabase_anon_keys(self):
        found = self.scan({
            "notes.md": f"Gong secret: {fake_jwt({'sub': 'svc-1234', 'exp': 1999999999})}\n",
            "client.ts": f'const key = "{fake_jwt({"iss": "supabase", "role": "anon"})}"\n',
        })
        self.assertEqual(found, ["JWT / bearer token in notes.md:1"])

    def test_values_after_labels(self):
        found = self.scan({
            "notes.md": f"## API creds\nSecret: {LONG_VALUE}\npassword = '{LONG_VALUE}'\n",
            "app.py": "api_key = process.env.OPENAI_API_KEY_2024\n"
                      "token = 'abc'\n"
                      "TOKEN=MY_SERVICE_TOKEN_V2_NAME\n",
            "page.html": f"<script data-cf-beacon='{{\"token\":\"{LONG_VALUE}\"}}'></script>\n",
        })
        self.assertEqual(found, [
            "value after 'Secret' label in notes.md:2",
            "value after 'password' label in notes.md:3",
        ])

    def test_env_with_only_public_vars_is_not_a_secret(self):
        found = self.scan({
            "web/.env": "VITE_SUPABASE_URL=https://x.supabase.co\nVITE_SUPABASE_PUBLISHABLE_KEY=abc\n",
            "api/.env": "VITE_URL=x\nDATABASE_URL=postgres://u:p@h/db\n",
        })
        self.assertEqual(found, ["secret-looking file tracked: api/.env"])

    def test_vendored_matches_skipped(self):
        found = self.scan({"node_modules/pkg/test.js": f'k = "{FAKE_AWS}"\n'})
        self.assertEqual(found, [])


class DraftTest(Base):
    def test_node_app_draft(self):
        repo = make_repo(self.root / "web", {
            "package.json": json.dumps({
                "description": "Track movies you've watched.",
                "scripts": {"dev": "vite", "build": "vite build", "lint": "eslint"},
                "dependencies": {"react": "18", "@supabase/supabase-js": "2"},
                "devDependencies": {"vite": "5"},
            }),
            "src/App.jsx": "x", "src/main.jsx": "x", "index.html": "x",
        })
        draft = tend.draft_readme(repo)
        self.assertTrue(draft.startswith(tend.DRAFT_MARKER + "\n# web\n\nTrack movies you've watched."))
        self.assertIn("- Built with: React, Vite, Supabase", draft)
        self.assertIn("- Languages: JavaScript, HTML", draft)
        self.assertIn("```bash\nnpm install\nnpm run dev\nnpm run build\n```", draft)
        self.assertNotIn("npm run lint", draft)
        self.assertIn("- `src/`", draft)
        self.assertIn("1 commit, ", draft)

    def test_python_imports_and_entrypoint(self):
        repo = make_repo(self.root / "game", {"main.py": "import pygame\nfrom os import path\n"})
        draft = tend.draft_readme(repo)
        self.assertIn("- Built with: Pygame", draft)
        self.assertIn("python main.py", draft)

    def test_streamlit_and_requirements(self):
        repo = make_repo(self.root / "dash", {
            "requirements.txt": "streamlit==1.0\nanthropic>=0.30\n", "app.py": "x"})
        draft = tend.draft_readme(repo)
        self.assertIn("Streamlit, Anthropic SDK", draft)
        self.assertIn("pip install -r requirements.txt\nstreamlit run app.py", draft)

    def test_empty_repo_draft_is_all_todos(self):
        repo = make_repo(self.root / "blank")
        draft = tend.draft_readme(repo)
        self.assertIn("TODO: one sentence", draft)
        self.assertIn("TODO: how to run", draft)
        self.assertNotIn("## Status", draft)

    def test_write_draft_never_clobbers_user_files(self):
        repo = make_repo(self.root / "r", {"a.py": "x"})
        path = tend.write_draft(repo)
        self.assertTrue(path.read_text().startswith(tend.DRAFT_MARKER))
        self.assertEqual(tend.write_draft(repo), path)  # own draft: replaced
        path.write_text("my own notes")
        self.assertIsNone(tend.write_draft(repo))  # someone else's file: kept
        self.assertEqual(path.read_text(), "my own notes")


class CliTest(Base):
    def setUp(self):
        super().setUp()
        self.good = make_repo(self.root / "good", HEALTHY)
        run_git(self.good, "remote", "add", "origin", "https://example.com/g.git")
        self.bare = make_repo(self.root / "bare", {"x.txt": "x"})

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tend.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_text_report_sorted_worst_first_with_summary(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("D  55  bare "))
        self.assertTrue(lines[1].startswith("A 100  good "))
        self.assertIn("2 repos scanned, 1 need attention.", out)
        self.assertIn("1  no license", out)

    def test_issues_only_and_json(self):
        _, out, _ = self.run_main("--issues-only", "--format", "json")
        data = json.loads(out)
        self.assertEqual([r["name"] for r in data], ["bare"])
        self.assertEqual(data[0]["grade"], "D")
        self.assertEqual(data[0]["findings"][0]["check"], "readme")

    def test_markdown(self):
        _, out, _ = self.run_main("--format", "markdown")
        self.assertIn("| Grade | Score | Repo | Last commit | Issues |", out)
        self.assertIn("| D | 55 | bare |", out)
        self.assertIn("no README<br>no license", out)

    def test_fail_under_and_secret_exit_codes(self):
        self.assertEqual(self.run_main("--fail-under", "50")[0], 0)
        self.assertEqual(self.run_main("--fail-under", "60")[0], 1)
        make_repo(self.root / "leaky", {**HEALTHY, "k.py": f'k = "{FAKE_AWS}"\n'})
        self.assertEqual(self.run_main()[0], 1)

    def test_fix_writes_drafts_only_where_needed(self):
        code, out, _ = self.run_main("--fix")
        self.assertEqual(code, 0)
        self.assertIn("Drafted 1 README:", out)
        self.assertTrue((self.bare / "README.draft.md").exists())
        self.assertFalse((self.good / "README.draft.md").exists())
        # The draft itself must not count as a README on the next run.
        _, out, _ = self.run_main()
        self.assertIn("no README", out)

    def test_errors(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stderr(err):
            self.assertEqual(tend.main(["--root", empty]), 2)
        self.assertIn("no git repositories", err.getvalue())
        with contextlib.redirect_stderr(err):
            self.assertEqual(tend.main(["--root", str(self.root / "nope")]), 2)
        self.assertIn("not a directory", err.getvalue())

    def test_cli_entrypoint(self):
        result = subprocess.run([sys.executable, tend.__file__, "--root", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 repos scanned", result.stdout)


if __name__ == "__main__":
    unittest.main()
