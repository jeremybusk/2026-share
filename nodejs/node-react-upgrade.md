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
