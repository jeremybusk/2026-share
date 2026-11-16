Save the following script as run-local-codemods.sh and execute it with bash run-local-codemods.sh ./src. It clones the repositories into a local cache directory (.codemod-cache), installs the runners locally, and iterates through the arrays of Node.js and React AST transforms without reaching out to app.codemod.com.
#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="${1:-./src}"
CACHE_DIR="${2:-./.codemod-cache}"
GIT_BASE_URL="${GIT_BASE_URL:-https://github.com}"

REPOS=(
  "nodejs/userland-migrations"
  "joyeecheung/node-dep-codemod"
  "reactjs/react-codemod"
  "codemod/react-codemod"
  "eps1lon/types-react-codemod"
  "cpojer/js-codemod"
)

REACT_JSCODESHIFT_TRANSFORMS=(
  "replace-reactdom-render"
  "replace-act-import"
  "replace-string-ref"
  "replace-use-form-state"
)

NODE_DEP_JSCODESHIFT_TRANSFORMS=(
  "buffer"
  "url-to-whatwg-url"
)

JS_MODERNIZATION_TRANSFORMS=(
  "no-vars"
  "arrow-function"
  "object-shorthand"
)

mkdir -p "$CACHE_DIR"

for repo in "${REPOS[@]}"; do
  repo_name="${repo##*/}"
  owner_name="${repo%/*}"
  dest="$CACHE_DIR/${owner_name}_${repo_name}"
  if [ ! -d "$dest/.git" ]; then
    git clone --depth 1 "${GIT_BASE_URL}/${repo}.git" "$dest"
  else
    git -C "$dest" pull --ff-only
  fi
done

pnpm add -D jscodeshift @codemod.com/cli types-react-codemod eslint eslint-plugin-n eslint-plugin-unicorn

for workflow in "$CACHE_DIR/nodejs_userland-migrations/recipes"/*/workflow.yaml; do
  if [ -f "$workflow" ]; then
    pnpm exec codemod workflow run -w "$workflow" -t "$TARGET_DIR" --no-interactive || true
  fi
done

for transform in "${NODE_DEP_JSCODESHIFT_TRANSFORMS[@]}"; do
  script_path="$CACHE_DIR/joyeecheung_node-dep-codemod/${transform}.js"
  if [ -f "$script_path" ]; then
    pnpm exec jscodeshift \
      -t "$script_path" \
      --parser=tsx \
      --extensions=ts,tsx,js,jsx \
      --ignore-pattern="**/node_modules/**" \
      "$TARGET_DIR"
  fi
done

for transform in "${REACT_JSCODESHIFT_TRANSFORMS[@]}"; do
  script_path="$CACHE_DIR/reactjs_react-codemod/transforms/${transform}.js"
  if [ -f "$script_path" ]; then
    pnpm exec jscodeshift \
      -t "$script_path" \
      --parser=tsx \
      --extensions=ts,tsx,js,jsx \
      --ignore-pattern="**/node_modules/**" \
      "$TARGET_DIR"
  fi
done

for workflow in "$CACHE_DIR/codemod_react-codemod/crates"/*/workflow.yaml "$CACHE_DIR/codemod_react-codemod/packages"/*/workflow.yaml; do
  if [ -f "$workflow" ]; then
    pnpm exec codemod workflow run -w "$workflow" -t "$TARGET_DIR" --no-interactive || true
  fi
done

for transform in "${JS_MODERNIZATION_TRANSFORMS[@]}"; do
  script_path="$CACHE_DIR/cpojer_js-codemod/transforms/${transform}.js"
  if [ -f "$script_path" ]; then
    pnpm exec jscodeshift \
      -t "$script_path" \
      --parser=tsx \
      --extensions=ts,tsx,js,jsx \
      --ignore-pattern="**/node_modules/**" \
      "$TARGET_DIR"
  fi
done

pnpm exec types-react-codemod preset-19 "$TARGET_DIR" --yes

pnpm exec eslint --fix \
  --no-config-lookup \
  --plugin n \
  --plugin unicorn \
  --rule 'n/no-deprecated-api: error' \
  --rule 'unicorn/prefer-node-protocol: error' \
  --rule 'unicorn/no-new-buffer: error' \
  "$TARGET_DIR" || true

Usage Options
 * Custom Internal Git Mirror: If github.com is blocked and you mirror these repositories internally, override GIT_BASE_URL when running the script:
   GIT_BASE_URL="https://git.internal.domain/mirrors" bash run-local-codemods.sh ./src

 * Air-Gapped Pre-Populated Cache: If you pre-clone the folders onto a drive or artifact share, pass the directory path as the second argument and the script will skip cloning and run directly from disk:
   bash run-local-codemods.sh ./src /path/to/precloned-codemods

