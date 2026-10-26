#!/usr/bin/env bash

scripts=(
  download_mcdonalds.py
  download_burger_king.py
  download_dominos.py
  download_pizza_hut.py
  download_wendys.py
  download_popeyes.py
  download_subway.py
)

cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1

failed=()

for script in "${scripts[@]}"; do
  printf '\nStarting %s\n' "$script"

  python "$script" "$@"
  status=$?

  if (( status == 130 )); then
    echo "Interrupted."
    exit 130
  elif (( status != 0 )); then
    failed+=("$script")
  fi
done

if (( ${#failed[@]} )); then
  printf '\nFailed scripts:\n'
  printf '  %s\n' "${failed[@]}"
  exit 1
fi

echo "All scripts completed successfully."
