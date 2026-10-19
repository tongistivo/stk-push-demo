#!/usr/bin/env bash
# Checks that nothing in your local .env has leaked into Git.
# Prints only the NAMES of leaked variables, never their values.
# Run from the project folder AFTER "git init":   bash check_secrets.sh
set -u

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This folder is not a Git repository yet. Run 'git init' first."; exit 1
fi
if [ ! -f .env ]; then
  echo "No .env file here, so there is nothing to compare against."; exit 0
fi

bad=0
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "PROBLEM: .env is tracked by Git. Run: git rm --cached .env"; bad=1
fi

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue ;; esac
  name="${line%%=*}"
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  [ "${#value}" -ge 8 ] || continue                     # skip short/public values
  if git grep -qIF -- "$value" 2>/dev/null; then
    echo "PROBLEM: the value of $name appears in a tracked file."; bad=1
  fi
  if [ -n "$(git log --all -S"$value" --oneline 2>/dev/null | head -n 1)" ]; then
    echo "PROBLEM: the value of $name appears in Git history."; bad=1
  fi
done < .env

if [ "$bad" -eq 0 ]; then
  echo "OK: no values from .env were found in tracked files or history."
else
  echo "Fix the problems above, then rotate the affected credentials (see README)."
  exit 1
fi
