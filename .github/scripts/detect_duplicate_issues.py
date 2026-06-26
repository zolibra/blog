#!/usr/bin/env python3
"""Detect duplicate GitHub issues using keyword / token-overlap matching.

This implements the multi-strategy duplicate detection workflow:

1. Gather context from the target issue (title + body, including any
   error messages / stack traces).
2. Compare against every other open issue using token-overlap similarity
   that gives extra weight to title matches and to shared error-message
   lines.
3. Only flag candidates we are highly confident about, then post a single
   comment on the target issue following the skill's formatting rules:

       1 dup  -> "This is potentially a duplicate of #123."
       2 dups -> "This is potentially a duplicate of #123 and #456."
       3+     -> "This is potentially a duplicate of #123, #456, and #789."

The script shells out to the GitHub CLI (`gh`), which is pre-installed on
GitHub-hosted runners. Set GH_TOKEN (or GITHUB_TOKEN) with `issues: write`.

Usage:
    detect_duplicate_issues.py --issue <number> [--repo owner/name] [--dry-run]

If --issue is omitted the script reads the issue number from the GitHub
Actions event payload at $GITHUB_EVENT_PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Iterable


# Tokens shorter than this (after normalisation) are ignored as noise.
MIN_TOKEN_LEN = 3

# Very common words that add noise rather than signal.
STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "have", "has", "are",
    "was", "were", "not", "but", "you", "your", "out", "get", "got", "can",
    "cant", "cannot", "does", "doesnt", "did", "when", "what", "why", "how",
    "after", "before", "into", "its", "it's", "they", "them", "then", "than",
    "there", "here", "some", "any", "all", "also", "should", "would", "could",
    "will", "i", "im", "i'm", "ive", "i've", "a", "an", "to", "of", "in", "on",
    "is", "be", "as", "at", "or", "if", "by", "we", "my", "me", "do", "so",
    "issue", "bug", "error", "problem", "expected", "actual", "steps",
    "reproduce", "behavior", "behaviour", "version", "describe",
}

# Decision thresholds. These intentionally err on the side of caution: we
# only comment when we are highly confident, otherwise we stay silent.
STRONG_MATCH = 0.35   # combined similarity score considered a duplicate
TITLE_STRONG = 0.70   # OR a very high title-only overlap
MAX_DUPLICATES = 5    # never list more than this many in a comment


def run_gh(args: list[str]) -> str:
    """Run a gh command and return stdout, raising on failure."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords / short tokens."""
    if not text:
        return set()
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    return {
        w for w in words
        if len(w) >= MIN_TOKEN_LEN and w not in STOPWORDS
    }


def extract_error_lines(text: str) -> set[str]:
    """Pull out lines that look like errors / stack traces for exact-ish match."""
    if not text:
        return set()
    error_re = re.compile(
        r"(error|exception|traceback|cannot find|cannot read|undefined|"
        r"not found|failed|fatal|panic|segfault|stack trace)",
        re.IGNORECASE,
    )
    lines = set()
    for raw in text.splitlines():
        line = raw.strip().strip("`").strip()
        if line and error_re.search(line):
            # Normalise whitespace so minor formatting diffs still match.
            lines.add(re.sub(r"\s+", " ", line.lower()))
    return lines


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def overlap_ratio(a: set[str], b: set[str]) -> float:
    """Shared lines relative to the smaller set (good for error matches)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def similarity(target: dict, candidate: dict) -> float:
    """Combined weighted similarity score in [0, 1]."""
    title_sim = jaccard(target["title_tokens"], candidate["title_tokens"])
    # Use containment (overlap relative to the smaller doc) for the body:
    # duplicates are often reworded or one body is a subset of the other,
    # so plain Jaccard under-scores genuine matches.
    body_sim = overlap_ratio(target["body_tokens"], candidate["body_tokens"])
    error_sim = overlap_ratio(target["error_lines"], candidate["error_lines"])

    # Weighted blend: title, body containment, and shared error lines.
    score = (0.40 * title_sim) + (0.40 * body_sim) + (0.20 * error_sim)

    # Strong negative signal: if both issues report errors but share none of
    # them, they are likely different bugs in the same area (e.g. "module not
    # found: ./config" vs "./database"). Dampen the score so near-identical
    # wording with a different root error is not flagged as a duplicate.
    if target["error_lines"] and candidate["error_lines"] and error_sim == 0.0:
        score *= 0.6

    # Boost when titles alone are extremely similar.
    if title_sim >= TITLE_STRONG:
        score = max(score, title_sim)
    return score


def build_doc(issue: dict) -> dict:
    title = issue.get("title", "") or ""
    body = issue.get("body", "") or ""
    return {
        "number": issue["number"],
        "title": title,
        "title_tokens": tokenize(title),
        "body_tokens": tokenize(f"{title}\n{body}"),
        "error_lines": extract_error_lines(body),
    }


def fetch_issue(repo: str | None, number: int) -> dict:
    args = ["issue", "view", str(number), "--json", "number,title,body"]
    if repo:
        args += ["--repo", repo]
    return json.loads(run_gh(args))


def fetch_open_issues(repo: str | None) -> list[dict]:
    args = [
        "issue", "list",
        "--state", "open",
        "--limit", "500",
        "--json", "number,title,body",
    ]
    if repo:
        args += ["--repo", repo]
    return json.loads(run_gh(args))


def format_comment(numbers: list[int]) -> str:
    refs = [f"#{n}" for n in numbers]
    if len(refs) == 1:
        joined = refs[0]
    elif len(refs) == 2:
        joined = f"{refs[0]} and {refs[1]}"
    else:
        joined = ", ".join(refs[:-1]) + f", and {refs[-1]}"
    return f"This is potentially a duplicate of {joined}."


def post_comment(repo: str | None, number: int, body: str) -> None:
    args = ["issue", "comment", str(number), "--body", body]
    if repo:
        args += ["--repo", repo]
    run_gh(args)


def resolve_issue_number(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        issue = payload.get("issue")
        if issue and "number" in issue:
            return int(issue["number"])
    raise SystemExit(
        "No issue number provided and none found in GITHUB_EVENT_PATH."
    )


def find_duplicates(target: dict, candidates: Iterable[dict]) -> list[tuple[int, float]]:
    matches: list[tuple[int, float]] = []
    for cand in candidates:
        if cand["number"] == target["number"]:
            continue
        score = similarity(target, cand)
        title_sim = jaccard(target["title_tokens"], cand["title_tokens"])
        if score >= STRONG_MATCH or title_sim >= TITLE_STRONG:
            matches.append((cand["number"], score))
    # Highest confidence first, then lowest issue number for stability.
    matches.sort(key=lambda m: (-m[1], m[0]))
    return matches[:MAX_DUPLICATES]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, default=None,
                        help="Target issue number (defaults to Actions event).")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                        help="owner/name (defaults to $GITHUB_REPOSITORY).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the comment instead of posting it.")
    args = parser.parse_args()

    number = resolve_issue_number(args.issue)
    repo = args.repo or None

    target = build_doc(fetch_issue(repo, number))
    candidates = [build_doc(i) for i in fetch_open_issues(repo)]

    matches = find_duplicates(target, candidates)
    if not matches:
        print(f"No high-confidence duplicates found for #{number}.")
        return 0

    numbers = [n for n, _ in matches]
    comment = format_comment(numbers)
    detail = ", ".join(f"#{n} (score={s:.2f})" for n, s in matches)
    print(f"Candidate duplicates for #{number}: {detail}")

    if args.dry_run:
        print(f"[dry-run] Would comment on #{number}: {comment}")
        return 0

    post_comment(repo, number, comment)
    print(f"Commented on #{number}: {comment}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
