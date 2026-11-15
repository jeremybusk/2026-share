Upgrading from Node 18 and React 17 to Node 24 and React 19 with pnpm can be automated with a shell script that handles version pinning, lockfile migration, dependency updates, and official React codemods.
Automated Upgrade Script
Save the following script as upgrade.sh in your project root and run bash upgrade.sh:
#!/usr/bin/env bash
set -euo pipefail

git checkout -b chore/upgrade-node24-react19

echo "24" > .nvmrc
echo "24" > .node-version

if command -v fnm >/dev/null 2>&1; then
  fnm install 24
  fnm use 24
elif [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
  . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
  nvm install 24
  nvm use 24
fi

corepack enable
corepack prepare pnpm@latest --activate

if [ -f package-lock.json ] || [ -f yarn.lock ]; then
  pnpm import
  rm -f package-lock.json yarn.lock
fi

rm -rf node_modules

pnpm pkg set engines.node=">=24.0.0"

pnpm update --latest

pnpm add react@^19.0.0 react-dom@^19.0.0
pnpm add -D @types/node@^24.0.0 @types/react@^19.0.0 @types/react-dom@^19.0.0

pnpm dlx codemod@latest react/19/migration-recipe
pnpm dlx types-react-codemod@latest preset-19 ./src

pnpm rebuild
pnpm install

Private Repository & Registry Configuration
If your private repository pulls internal scoped packages (e.g., @company/*) from a private registry like GitHub Packages, GitLab, AWS CodeArtifact, or Artifactory, ensure your .npmrc is present in the project root before running the script:
@your-org:registry=https://npm.pkg.github.com/
//npm.pkg.github.com/:_authToken=${NPM_TOKEN}
auto-install-peers=true
strict-peer-dependencies=false

Setting strict-peer-dependencies=false in .npmrc prevents pnpm update --latest from halting if third-party libraries still declare react@^18.0.0 in their peer dependency ranges.
Critical Breaking Changes to Check Post-Script
Jumping across multiple major versions introduces runtime and API removals that scripts and codemods cannot always resolve automatically:
 * ReactDOM.render to createRoot: React 19 completely removes ReactDOM.render and ReactDOM.hydrate. Your application entry point (src/index.tsx or src/main.tsx) must use createRoot from react-dom/client:
   import { createRoot } from 'react-dom/client';
import App from './App';

const container = document.getElementById('root');
const root = createRoot(container!);
root.render(<App />);

 * Build Tool Compatibility (react-scripts): If your app uses Create React App (react-scripts), it will fail to build or run cleanly on Node 24 and React 19 because react-scripts is unmaintained. Replacing react-scripts with Vite or Rsbuild is strongly recommended if you are currently on CRA.
 * Removed React 17 Patterns: React 19 removes propTypes, defaultProps on function components, string refs, findDOMNode, and Legacy Context (contextTypes). The react/19/migration-recipe codemod in the script converts most of these automatically.
 * Node 24 Native Add-ons & OpenSSL 3.5: Node 24 uses NODE_MODULE_VERSION 137 and OpenSSL 3.5. Deleting node_modules and running pnpm update --latest ensures native C++ add-ons (sharp, bcrypt, canvas, node-sass) fetch prebuilt binaries compatible with Node 24. If your project still uses node-sass, replace it with sass (pnpm remove node-sass && pnpm add -D sass).


# codemod local run


If app.codemod.com (the Codemod Registry) is blocked on your network, running pnpm dlx codemod@latest react/19/migration-recipe will fail because the codemod CLI fetches named recipes (react/19/...) from codemod.com at runtime rather than bundling them inside its npm package.
However, this does not break your ability to run the React 19 AST migrations. You can bypass codemod.com entirely and run everything 100% locally through standard npm packages mirrored on your internal registry.
How the Two Tools Differ on Restricted Networks
 * types-react-codemod (100% Self-Contained in npm):
   types-react-codemod is a standalone npm package with all jscodeshift transforms bundled inside the tarball. It never contacts app.codemod.com. As long as the package is in your private npm registry or local cache, it runs completely offline.
 * codemod CLI (Requires Registry or Local Source Path):
   The codemod binary is just an execution engine (built on jscodeshift and ast-grep). When you pass a registry slug like react/19/migration-recipe, it reaches out to app.codemod.com to download the transform tarball. To run it without external network access, you either pass --source pointing to a local directory or run the underlying transforms directly with jscodeshift.
Option 1: Bypass codemod CLI and Use types-react-codemod + jscodeshift (Recommended)
If your private registry mirrors standard npm packages, install the runners as devDependencies (or run them from your internal registry) so zero outbound calls are made outside your network:
pnpm add -D types-react-codemod jscodeshift

pnpm exec types-react-codemod preset-19 ./src

For the runtime React 19 transforms that react/19/migration-recipe normally runs (replace-reactdom-render, replace-string-ref, replace-act-import, replace-use-form-state), if you have an internal Git mirror of [https://github.com/reactjs/react-codemod](https://github.com/reactjs/react-codemod) or a pre-downloaded copy of the transforms directory on disk, you can execute them directly with jscodeshift or local codemod --source:
pnpm exec codemod --no-telemetry --source /path/to/local/codemod-dir --target ./src

Option 2: Pre-warm the .codemod Cache on an Connected Machine
When codemod downloads a recipe from app.codemod.com, it stores the extracted AST scripts in ~/.codemod. If you have a machine or build agent that does have access to codemod.com:
 * Run the recipe once on a dummy directory to populate ~/.codemod:
   npx codemod@latest react/19/migration-recipe --no-interactive

 * Copy the downloaded package directory from ~/.codemod/ into your repository (e.g., ./tools/react-19-codemods/).
 * On your restricted network, execute the local scripts directly with --source and --no-telemetry:
   pnpm exec codemod --no-telemetry --source ./tools/react-19-codemods/replace-reactdom-render --target ./src

Option 3: Skip react/19/migration-recipe and Only Run types-react-codemod
In most React 17/18 TypeScript codebases, react/19/migration-recipe touches very few files because it only targets 5 specific runtime patterns:
 * ReactDOM.render -> createRoot (usually 1 file: src/index.tsx)
 * react-dom/test-utils act -> react act (easily replaced with a one-line sed or IDE find-and-replace)
 * useFormState -> useActionState (only applies if you used experimental React 18 canary hooks)
 * String refs (ref="myRef") and propTypes
Meanwhile, types-react-codemod (preset-19) does the heavy lifting across dozens or hundreds of files (ReactElement generic defaults, useRef initial arguments, RefCallback return types, JSX namespace migration) and runs 100% locally from your npm registry without touching app.codemod.com.
Here is the updated offline/internal-registry-safe block for your upgrade.sh script:
pnpm add -D types-react-codemod

pnpm exec types-react-codemod preset-19 ./src --yes

find ./src -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
  -exec sed -i "s/from ['\"]react-dom\/test-utils['\"]/from 'react'/g" {} +

pnpm remove -D types-react-codemod

