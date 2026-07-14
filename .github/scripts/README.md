# Duplicate Issue Detection

Automatically flags potential duplicate issues with a comment when a new issue
is opened.

## How it works

When an issue is opened, reopened, or edited, the
[`duplicate-issue-detection.yml`](../workflows/duplicate-issue-detection.yml)
workflow runs [`detect_duplicate_issues.py`](detect_duplicate_issues.py). The
script:

1. Loads the target issue's title and body (including any error messages /
   stack traces).
2. Compares it against every other open issue using a weighted similarity
   score:
   - **Title** token overlap (Jaccard).
   - **Body** containment (overlap relative to the smaller body) so reworded
     duplicates still match.
   - **Error lines** — lines that look like errors / exceptions / stack traces
     are matched near-exactly.
3. Applies a penalty when both issues report errors but share none of them
   (different root error ⇒ probably not a duplicate).
4. If one or more candidates clear the confidence threshold, posts a single
   comment on the target issue:

   - 1 match: `This is potentially a duplicate of #123.`
   - 2 matches: `This is potentially a duplicate of #123 and #456.`
   - 3+ matches: `This is potentially a duplicate of #123, #456, and #789.`

If nothing is highly similar, the script stays silent — it errs on the side of
not commenting.

## Requirements

- **Issues must be enabled** on the repository (Settings → General → Features →
  Issues). The workflow triggers on the `issues` event, which never fires while
  issues are disabled.
- The workflow uses the automatically provided `GITHUB_TOKEN` with
  `issues: write` permission. No extra secrets are needed.

## Running manually / locally

The script also works from the command line with the GitHub CLI authenticated
(`gh auth login`):

```bash
# Comment on the issue if duplicates are found
python .github/scripts/detect_duplicate_issues.py --issue 150 --repo owner/name

# Preview without posting a comment
python .github/scripts/detect_duplicate_issues.py --issue 150 --repo owner/name --dry-run
```

## Tuning

Thresholds and weights live at the top of `detect_duplicate_issues.py`:

- `STRONG_MATCH` — minimum combined score to flag a duplicate.
- `TITLE_STRONG` — title-only overlap that flags a duplicate on its own.
- `MAX_DUPLICATES` — maximum number of issues listed in a single comment.
- `STOPWORDS` — common words ignored during matching.

Raise `STRONG_MATCH` for fewer, higher-confidence comments; lower it to catch
more potential duplicates at the cost of more false positives.
