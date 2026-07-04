#!/usr/bin/env python3
"""Detect potential duplicate GitHub issues for a newly opened issue.

This script implements the multi-strategy matching described in the
`github-issue-dedupe` skill:

  * Extract keywords from the target issue's title and body.
  * Pull error-message / stack-trace style lines from the body.
  * Compare against every other issue in the repository, scoring on title
    keyword overlap plus shared error lines.
  * If one or more high-confidence candidates are found, post a single
    comment on the target issue using the skill's exact wording rules.

It is intended to run inside GitHub Actions on the `issues: opened` event,
but it can also be exercised locally by setting the same environment
variables (see `main`). Only the GitHub CLI (`gh`) and the standard library
are required.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any


# Common English + GitHub-boilerplate words that add noise to keyword matching.
STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
    "how", "man", "new", "now", "old", "see", "two", "way", "who", "boy",
    "did", "its", "let", "put", "say", "she", "too", "use", "with", "that",
    "this", "have", "from", "they", "will", "would", "there", "their", "what",
    "about", "which", "when", "make", "like", "time", "just", "know", "take",
    "into", "your", "some", "them", "than", "then", "look", "only", "come",
    "over", "also", "back", "after", "work", "first", "well", "even", "want",
    "because", "these", "give", "most", "issue", "bug", "error", "does",
    "when", "while", "using", "used", "still", "should", "could", "been",
    "were", "http", "https", "com", "www", "org",
}

# Minimum scores required before we will comment. Deliberately conservative to
# avoid noisy false positives, per the skill's "only comment if high
# confidence" guidance.
#
# A candidate is only considered when its titles clearly overlap OR it shares
# an identical error line; the combined score then blends title and full-text
# similarity. These thresholds flag reworded duplicates (skill Examples 1 & 3)
# while rejecting "similar but different" issues (skill Example 2).
MIN_TITLE_OVERLAP = 0.4
MIN_COMBINED_SCORE = 0.5
MAX_REPORTED = 5


def run_gh(args: list[str]) -> str:
    """Run a `gh` command and return stdout, raising on failure."""
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def tokenize(text: str) -> set[str]:
    """Return the set of meaningful lowercase word tokens in `text`."""
    words = re.findall(r"[a-zA-Z0-9_]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def extract_error_lines(text: str) -> set[str]:
    """Return normalized lines that look like errors or stack traces."""
    error_lines: set[str] = set()
    error_markers = (
        "error", "exception", "cannot", "undefined", "null", "failed",
        "traceback", "not found", "unexpected", "fatal", "panic",
    )
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if len(line) < 8:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in error_markers):
            # Collapse whitespace and drop noisy code-fence markers.
            normalized = re.sub(r"\s+", " ", line.strip("`> ")).lower()
            if normalized:
                error_lines.add(normalized)
    return error_lines


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_coeff(a: set[str], b: set[str]) -> float:
    """Szymkiewicz-Simpson overlap: |A n B| / min(|A|, |B|).

    More forgiving than Jaccard when a duplicate reuses only a subset of the
    other title's words (e.g. a shorter, reworded title).
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def score_candidate(
    target_title_tokens: set[str],
    target_body_tokens: set[str],
    target_error_lines: set[str],
    candidate: dict[str, Any],
) -> float:
    """Combine title, body, and error-line similarity into a single score."""
    cand_title_tokens = tokenize(candidate.get("title", ""))
    cand_body_tokens = tokenize(candidate.get("body", ""))
    cand_error_lines = extract_error_lines(candidate.get("body", ""))

    # Title overlap is the primary gate; full-text (title + body) Jaccard adds
    # supporting context.
    title_overlap = overlap_coeff(target_title_tokens, cand_title_tokens)
    target_all = target_title_tokens | target_body_tokens
    cand_all = cand_title_tokens | cand_body_tokens
    content_score = jaccard(target_all, cand_all)

    # Sharing an identical (or near-identical) error line is a very strong
    # signal, so give it a large boost.
    shared_errors = target_error_lines & cand_error_lines
    error_boost = 0.5 if shared_errors else 0.0

    # Require a solid title overlap OR a shared error line before considering
    # it a candidate at all. This rejects issues that merely touch the same
    # component but describe different bugs.
    if title_overlap < MIN_TITLE_OVERLAP and not shared_errors:
        return 0.0

    return (0.5 * title_overlap) + (0.5 * content_score) + error_boost


def format_comment(numbers: list[int]) -> str:
    """Format the duplicate comment per the skill's wording rules."""
    refs = [f"#{n}" for n in numbers]
    if len(refs) == 1:
        joined = refs[0]
    elif len(refs) == 2:
        joined = f"{refs[0]} and {refs[1]}"
    else:
        joined = ", ".join(refs[:-1]) + f", and {refs[-1]}"
    return f"This is potentially a duplicate of {joined}."


def load_event_issue() -> dict[str, Any]:
    """Load the target issue from the GitHub Actions event payload."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        issue = payload.get("issue")
        if issue:
            return issue

    # Fallback for local runs: fetch by ISSUE_NUMBER via the API.
    number = os.environ.get("ISSUE_NUMBER")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if number and repo:
        raw = run_gh(["api", f"repos/{repo}/issues/{number}"])
        return json.loads(raw)

    raise SystemExit("Could not determine the target issue (no event payload).")


def fetch_all_issues(repo: str) -> list[dict[str, Any]]:
    """Return all issues (open and closed) for the repo, excluding PRs."""
    raw = run_gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/issues?state=all&per_page=100",
        ]
    )
    # `--paginate` concatenates JSON arrays; join them into one list.
    issues: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    text = raw.strip()
    while idx < len(text):
        # Skip whitespace between concatenated arrays.
        while idx < len(text) and text[idx] in " \t\r\n":
            idx += 1
        if idx >= len(text):
            break
        chunk, end = decoder.raw_decode(text, idx)
        if isinstance(chunk, list):
            issues.extend(chunk)
        idx = end
    # Exclude pull requests, which the issues endpoint also returns.
    return [i for i in issues if "pull_request" not in i]


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is not set.")

    target = load_event_issue()
    target_number = target["number"]
    target_title = target.get("title", "")
    target_body = target.get("body", "") or ""

    target_title_tokens = tokenize(target_title)
    target_body_tokens = tokenize(target_body)
    target_error_lines = extract_error_lines(target_body)

    print(f"Target issue #{target_number}: {target_title!r}")
    print(f"Title tokens: {sorted(target_title_tokens)}")

    candidates = fetch_all_issues(repo)
    scored: list[tuple[float, int, str]] = []
    for cand in candidates:
        if cand.get("number") == target_number:
            continue
        score = score_candidate(
            target_title_tokens,
            target_body_tokens,
            target_error_lines,
            cand,
        )
        if score >= MIN_COMBINED_SCORE:
            scored.append((score, cand["number"], cand.get("title", "")))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:MAX_REPORTED]

    if not top:
        print("No high-confidence duplicates found. Doing nothing.")
        return 0

    for score, number, title in top:
        print(f"  candidate #{number} (score={score:.2f}): {title!r}")

    numbers = [number for _, number, _ in top]
    comment = format_comment(numbers)
    print(f"Posting comment on #{target_number}: {comment}")

    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1 set; skipping comment.")
        return 0

    run_gh(
        [
            "issue",
            "comment",
            str(target_number),
            "--repo",
            repo,
            "--body",
            comment,
        ]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
