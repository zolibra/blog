# Duplicate issue detection

Automated, conservative duplicate-issue detection for this repository.

## How it works

When a new issue is opened, the
[`Duplicate Issue Detection`](../workflows/duplicate-issue-detection.yml)
workflow runs [`detect-duplicate-issues.sh`](./detect-duplicate-issues.sh). The
script:

1. Reads the new issue's title and body.
2. Normalizes them into keywords (lowercased, punctuation stripped, stopwords
   and very short tokens removed).
3. Compares those keywords against every other open issue, computing a
   similarity score that weights overall keyword overlap and title overlap.
4. If at least one candidate clears both thresholds, posts a single comment:
   `This is potentially a duplicate of #123 and #456.`

It is tuned for precision: when nothing clears the thresholds, it stays silent.

## Requirements

GitHub Issues must be enabled on the repository for the `issues: opened`
trigger to fire. Enable them under **Settings -> General -> Features -> Issues**
or run:

```bash
gh repo edit --enable-issues
```

## Tuning

The script reads optional environment variables (set them in the workflow's
`env:` block to override the defaults):

- `MIN_SHARED_KEYWORDS` (default `2`) - minimum overlapping keywords to consider.
- `MIN_SCORE` (default `0.5`) - minimum similarity score (0..1) required to flag.
- `MAX_CANDIDATES` (default `3`) - max duplicates listed in a single comment.
- `DRY_RUN` (default `false`) - when `true`, prints the comment instead of posting.

## Running manually

You can run the detector locally against any issue:

```bash
GH_REPO="zolibra/blog" ISSUE_NUMBER="123" DRY_RUN="true" \
  bash .github/scripts/detect-duplicate-issues.sh
```

Drop `DRY_RUN` (or set it to `false`) to actually post the comment.
