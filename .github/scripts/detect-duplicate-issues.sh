#!/usr/bin/env bash
#
# Detect likely duplicate GitHub issues for a single target issue and, when the
# match is strong, post one comment linking the candidate issue(s).
#
# Required environment variables:
#   GH_TOKEN      - token with `issues: write` (provided by Actions)
#   GH_REPO       - owner/repo (e.g. zolibra/blog)
#   ISSUE_NUMBER  - the number of the issue to check
#
# Optional environment variables:
#   MIN_SHARED_KEYWORDS - minimum overlapping keywords to consider (default 2)
#   MIN_SCORE           - minimum similarity score 0..1 to flag (default 0.5)
#   MAX_CANDIDATES      - max duplicates to list in the comment (default 3)
#   DRY_RUN             - if "true", print the comment instead of posting it
#
# The script is intentionally conservative: it only comments when at least one
# candidate clears both the shared-keyword and similarity-score thresholds.

set -euo pipefail

: "${GH_REPO:?GH_REPO is required}"
: "${ISSUE_NUMBER:?ISSUE_NUMBER is required}"

MIN_SHARED_KEYWORDS="${MIN_SHARED_KEYWORDS:-2}"
MIN_SCORE="${MIN_SCORE:-0.5}"
MAX_CANDIDATES="${MAX_CANDIDATES:-3}"
DRY_RUN="${DRY_RUN:-false}"

# Common English + GitHub-issue noise words that should not drive matching.
STOPWORDS=" the a an and or but if then else when while for to of in on at by with \
without from into out over under again further is are was were be been being do does \
did doing have has had having this that these those i you he she it we they them my \
your our their not no nor so than too very can will just dont cant wont issue bug \
error problem feature request please help app page site not working doesnt does using \
use used after before about would could should also get got via "

# Lowercase, strip non-alphanumerics, drop stopwords and short tokens, dedupe.
normalize_keywords() {
  tr '[:upper:]' '[:lower:]' \
    | tr -c '[:alnum:]' ' ' \
    | tr -s ' ' '\n' \
    | awk 'length($0) >= 3' \
    | grep -vxF -f <(printf '%s\n' $STOPWORDS) \
    | sort -u
}

echo "Fetching target issue #${ISSUE_NUMBER} from ${GH_REPO}..."
target_json="$(gh issue view "$ISSUE_NUMBER" --repo "$GH_REPO" --json number,title,body)"
target_title="$(jq -r '.title // ""' <<<"$target_json")"
target_body="$(jq -r '.body // ""' <<<"$target_json")"

# Title keywords are weighted heavily; body keywords add recall.
target_title_kw="$(printf '%s' "$target_title" | normalize_keywords || true)"
target_all_kw="$(printf '%s\n%s' "$target_title" "$target_body" | normalize_keywords || true)"

if [[ -z "$target_all_kw" ]]; then
  echo "Target issue has no usable keywords; nothing to do."
  exit 0
fi

target_kw_count="$(printf '%s\n' "$target_all_kw" | grep -c . || true)"

echo "Listing other open issues..."
candidates_json="$(gh issue list --repo "$GH_REPO" --state open --limit 200 \
  --json number,title,body)"

best_matches=()  # entries: "score<TAB>shared<TAB>number"

while IFS= read -r row; do
  [[ -z "$row" ]] && continue
  num="$(jq -r '.number' <<<"$row")"
  [[ "$num" == "$ISSUE_NUMBER" ]] && continue

  cand_title="$(jq -r '.title // ""' <<<"$row")"
  cand_body="$(jq -r '.body // ""' <<<"$row")"
  cand_all_kw="$(printf '%s\n%s' "$cand_title" "$cand_body" | normalize_keywords || true)"
  [[ -z "$cand_all_kw" ]] && continue

  shared_count="$(comm -12 <(printf '%s\n' "$target_all_kw") \
    <(printf '%s\n' "$cand_all_kw") | grep -c . || true)"
  (( shared_count < MIN_SHARED_KEYWORDS )) && continue

  cand_kw_count="$(printf '%s\n' "$cand_all_kw" | grep -c . || true)"

  # Title-keyword overlap as a strong duplicate signal.
  title_shared="$(comm -12 <(printf '%s\n' "$target_title_kw") \
    <(printf '%s\n' "$cand_all_kw") | grep -c . || true)"
  target_title_count="$(printf '%s\n' "$target_title_kw" | grep -c . || true)"

  # Combined score: overlap coefficient over the smaller keyword set, nudged up
  # by how much of the target's title is reflected in the candidate.
  smaller=$(( target_kw_count < cand_kw_count ? target_kw_count : cand_kw_count ))
  (( smaller == 0 )) && continue

  score="$(awk -v s="$shared_count" -v sm="$smaller" \
    -v ts="$title_shared" -v tt="$target_title_count" 'BEGIN {
      overlap = s / sm;
      title_ratio = (tt > 0) ? ts / tt : 0;
      score = 0.7 * overlap + 0.3 * title_ratio;
      printf "%.4f", score;
    }')"

  keep="$(awk -v sc="$score" -v min="$MIN_SCORE" 'BEGIN { print (sc >= min) ? 1 : 0 }')"
  if [[ "$keep" == "1" ]]; then
    best_matches+=("${score}	${shared_count}	${num}")
    echo "  candidate #${num}: score=${score} shared=${shared_count} (\"${cand_title}\")"
  fi
done < <(jq -c '.[]' <<<"$candidates_json")

if (( ${#best_matches[@]} == 0 )); then
  echo "No high-confidence duplicates found. Doing nothing."
  exit 0
fi

# Sort by score desc, keep up to MAX_CANDIDATES, collect issue numbers.
mapfile -t sorted < <(printf '%s\n' "${best_matches[@]}" | sort -t$'\t' -k1,1nr)

numbers=()
for entry in "${sorted[@]}"; do
  (( ${#numbers[@]} >= MAX_CANDIDATES )) && break
  numbers+=("#$(cut -f3 <<<"$entry")")
done

# Format per the dedupe phrasing rules.
count=${#numbers[@]}
if (( count == 1 )); then
  refs="${numbers[0]}"
elif (( count == 2 )); then
  refs="${numbers[0]} and ${numbers[1]}"
else
  last="${numbers[$((count - 1))]}"
  head_refs="$(printf '%s, ' "${numbers[@]:0:count-1}")"
  refs="${head_refs}and ${last}"
fi

comment="This is potentially a duplicate of ${refs}."

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] Would comment on #${ISSUE_NUMBER}: ${comment}"
  exit 0
fi

echo "Commenting on #${ISSUE_NUMBER}: ${comment}"
gh issue comment "$ISSUE_NUMBER" --repo "$GH_REPO" --body "$comment"
