# GitHub

- **Access:** the `gh` CLI (https://cli.github.com). An enterprise host needs `GH_HOST=<host>` or `--hostname`; `gh` defaults to github.com and fails with "could not resolve to a Repository" otherwise.
- **Install:** `brew install gh` (macOS) or the package for the platform.
- **Sign-in:** `gh auth login` (add `--hostname <host>` for enterprise).

```yaml
- id: github
  name: GitHub
  check: gh auth status
  login: gh auth login
  deny: [gh pr create, gh pr merge, gh pr comment, gh pr review, gh issue create, gh issue comment, gh issue close, gh workflow run, gh release create, gh repo create, gh api -X]
```

- **Quirks:** `gh api` defaults to GET; any `-X`/`--method` other than GET is a write. Record local checkout paths in `TOOLS.md` so skills can read code for verification.
