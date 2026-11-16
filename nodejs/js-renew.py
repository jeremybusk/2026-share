#!/usr/bin/env python3
"""Preview and apply JS/TS migrations and npm dependency upgrades. Python 3.10+.

Examples (from the project root):
  python3 modernize.py --list
  python3 modernize.py --profile js --profile react19 --target src --diff
  python3 modernize.py --profile express5 --target packages/api --apply
  python3 modernize.py --upgrade stable --diff
  python3 modernize.py --upgrade lts --lts-fallback stable --apply

Source migrations use pinned upstream revisions and npm tools in a private cache.
Use Node 22.13+ (22.x recommended), npm and Git for source migrations. Upgrades
need only Python and npm, and use the project's configured npm registry. Preview
executes the selected migrations in a temporary source tree. --apply commits
only the resulting edits to existing files. Install with your project's package
manager afterwards to refresh its lockfile, then run typechecks and tests.

--upgrade stable selects the highest published non-prerelease SemVer; major
upgrades are included. lts selects the highest stable release tagged lts,
lts-*, *-lts (or slash variants); --lts-fallback stable handles missing tags.
Exact, caret, tilde, partial, wildcard-component and npm alias specs keep
their shape. Local workspace packages, protocols, unbounded specs, custom
tags and compound ranges are reported and preserved. All four dependency
sections are checked, including peers. No dependency floor is downgraded.
Scans omit symlinks, nested Git checkouts, dependencies, common generated
directories, the tool cache and --exclude-dir entries.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass


# Keep the public inventory and the execution plan together. Each step specifies
# a runner, an upstream repository or npm package, and an exact relative path.
REPOS = {
    'cpojer/js-codemod': 'ae97c9fbbbd03d3319f910340fa2c650b94d9c0c',
    'codemod/react-codemod': '8936057c2a018ad4833a8f95d72b0154092ad19e',
    'nodejs/userland-migrations': 'ddceac8020e28fd4f874dd5c9dc71d5b9a19e219',
    'expressjs/codemod': '2f7f1e5f31ebab6e840957b0cf171a014ad7a3b9',
}
VERSIONS = {'jscodeshift': '17.3.0', 'codemod': '1.18.7', 'types-react-codemod': '3.5.3',
            '@next/codemod': '14.2.35', 'eslint': '9.39.1', 'eslint-plugin-n': '17.23.1',
            'eslint-plugin-unicorn': '61.0.2', '@typescript-eslint/parser': '8.48.0',
            'typescript': '5.9.3', '@babel/parser': '7.28.5', '@babel/traverse': '7.28.5'}
JS = ('js', 'jsx', 'mjs', 'cjs')
TS = ('ts', 'mts', 'cts')
EXTENSIONS = {*JS, *TS, 'tsx'}
SECTIONS = ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')
EXCLUDED = {'.git', '.hg', '.svn', 'node_modules', 'bower_components', '.pnpm', '.pnpm-store',
            '.yarn', '.venv', '.output', '.angular', '.vercel', '.codemod-cache', '.next', '.nuxt', '.svelte-kit', '.turbo', '.cache',
            'dist', 'build', 'out', 'coverage', 'vendor', '__pycache__'}
PRESET19 = ('deprecated-legacy-ref,deprecated-prop-types-types,deprecated-react-child,'
            'deprecated-react-node-array,deprecated-react-fragment,deprecated-react-text,'
            'deprecated-void-function-component,no-implicit-ref-callback-return,'
            'react-element-default-any-props,refobject-defaults,scoped-jsx,useRef-required-initial')


@dataclass(frozen=True)
class Step:
    runner: str
    source: str
    path: str


def transforms(source, names, suffix='.js'):
    return [Step('jscodeshift', source, f'transforms/{name}{suffix}') for name in names.split()]


PROFILES = {
    'js': ('Modernize var, callbacks and object shorthand', transforms('cpojer/js-codemod', 'no-vars arrow-function object-shorthand')),
    'node': ('Fix Buffer constructors and node: imports, report other deprecated APIs', [Step('eslint', 'eslint', 'bin/eslint.js')]),
    'node-url': ('Migrate legacy url.parse/format to WHATWG URL (review relative URLs)',
                 [Step('workflow', 'nodejs/userland-migrations', 'recipes/node-url-to-whatwg-url/workflow.yaml')]),
    'react19': ('Migrate React DOM render, act, string refs, form state and React 19 types',
                [Step('react', 'codemod/react-codemod', f'codemods/{name}/scripts/codemod.ts')
                 for name in ('replace-reactdom-render', 'replace-act-import', 'replace-string-ref', 'replace-use-form-state')]
                + [Step('cleanup', 'local', 'cleanup-render.cjs')]
                + [Step('types', 'types-react-codemod', 'transforms/preset-19.js')]),
    'express5': ('Migrate app.del and deprecated singular request methods', [
        Step('workflow', 'expressjs/codemod', f'codemods/{name}/workflow.yaml')
        for name in ('route-del-to-delete', 'pluralize-method-names')]),
    'next-og': ('Move Next.js ImageResponse imports to next/og', [Step('next', '@next/codemod', 'transforms/next-og-import.js')]),
}
NPM_ORIGINS = {'@next/codemod': 'vercel/next.js', 'types-react-codemod': 'eps1lon/types-react-codemod',
               'eslint': 'eslint/eslint, eslint-community/eslint-plugin-n, sindresorhus/eslint-plugin-unicorn'}
ESLINT_CONFIG = '''import n from 'eslint-plugin-n';
import unicorn from 'eslint-plugin-unicorn';
import tsParser from '@typescript-eslint/parser';
export default [
  { files: ['**/*.{js,mjs,cjs,jsx,ts,mts,cts,tsx}'], plugins: { n, unicorn },
    languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    rules: { 'n/no-deprecated-api': 'warn', 'unicorn/prefer-node-protocol': 'warn', 'unicorn/no-new-buffer': 'warn' } },
  { files: ['**/*.{ts,mts,cts,tsx}'], languageOptions: { parser: tsParser } },
  { files: ['**/*.{cjs,cts}'], languageOptions: { sourceType: 'commonjs' } }
];
'''


# The upstream render migration can leave behind a named import removed in
# React 19. Babel's lexical scope distinguishes actual uses from root.render().
CLEANUP_RENDER = '''const {parse} = require('@babel/parser');
const traverse = require('@babel/traverse').default;
module.exports = function (file, api) {
  if (!file.source.includes('react-dom/client')) return;
  const ts = /[.](?:[cm]?ts|tsx)$/.test(file.path);
  const jsx = /[.](?:[cm]?js|jsx|tsx)$/.test(file.path);
  const ast = parse(file.source, {sourceType: 'unambiguous', allowReturnOutsideFunction: true,
    plugins: ['decorators-legacy', ...(ts ? [['typescript', {dts: /[.]d[.][cm]?ts$/.test(file.path)}]] : []),
      ...(jsx ? ['jsx'] : [])]});
  const unused = new Set();
  traverse(ast, {ImportDeclaration(path) {
    if (path.node.source.value !== 'react-dom') return;
    for (const spec of path.node.specifiers) {
      if (spec.type !== 'ImportSpecifier' || spec.imported.name !== 'render') continue;
      const binding = path.scope.getBinding(spec.local.name);
      if (binding && !binding.referenced) unused.add(spec.local.name);
    }
  }});
  if (!unused.size) return;
  const j = api.jscodeshift;
  const tree = j(file.source);
  tree.find(j.ImportDeclaration, {source: {value: 'react-dom'}}).forEach(path => {
    const specs = path.node.specifiers;
    const keep = specs.filter(s => !unused.has(s.local.name));
    if (keep.length === specs.length) return;
    if (keep.length) path.node.specifiers = keep;
    else j(path).remove();
  });
  return tree.toSource();
};
'''

# The pinned Next transform matches local names and rewrites every next/server
# declaration. Handle aliases, type-only imports and side-effect imports explicitly.
NEXT_OG = """const official = require('@next/codemod/transforms/next-og-import.js').default;
module.exports = function(file, api, options) {
  if (!file.source.includes('next/server')) return;
  const j = api.jscodeshift;
  const tree = j(file.source);
  const declarations = tree.find(j.ImportDeclaration, {source: {value: 'next/server'}}).paths();
  const isImage = s => s.type === 'ImportSpecifier' && s.imported.name === 'ImageResponse';
  const relevant = declarations.filter(p => p.node.specifiers.some(isImage));
  if (!relevant.length) return;
  const plain = declarations.every(p => p.node.importKind !== 'type' && p.node.specifiers.length &&
    p.node.specifiers.every(s => s.importKind !== 'type' && (s.local.name === 'ImageResponse') === isImage(s)));
  if (plain) return official(file, api, options);
  for (const path of relevant) {
    const decl = path.node;
    const keep = decl.specifiers.filter(s => !isImage(s));
    if (keep.length === 0) {
      decl.source.value = 'next/og';
    } else {
      const moved = j.importDeclaration(decl.specifiers.filter(isImage), j.stringLiteral('next/og'));
      moved.importKind = decl.importKind;
      decl.specifiers = keep;
      path.insertBefore(moved);
    }
  }
  return tree.toSource();
};
"""


def react_workflow(jsx_only=False):
    # Use both grammars and include JS test files as well as TS tests.
    steps = []
    for language, exts in [('typescript', TS), ('tsx', (*JS, 'tsx'))]:
        if jsx_only and language == 'typescript':
            continue
        globs = json.dumps(['**/*.' + ext for ext in exts])
        steps.append(f'''      - name: Migrate {language}
        js-ast-grep:
          js_file: scripts/codemod.ts
          language: {language}
          semantic_analysis: file
          include: {globs}''')
    return '''version: "1"
nodes:
  - id: migrate
    name: Migrate React source
    type: automatic
    steps:
''' + '\n'.join(steps) + '\n'


class Problem(Exception):
    pass


def run(args, cwd, *, capture=False):
    args = [str(a) for a in args]
    exe = shutil.which(args[0])
    if not exe:
        raise Problem(f'{args[0]} is required for this operation but was not found on PATH')
    result = subprocess.run([exe, *args[1:]], cwd=cwd, check=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else None, text=True, encoding='utf-8')
    if result.returncode:
        raise Problem(f'{args[0]} {" ".join(args[1:4])} failed (exit {result.returncode})')
    return result.stdout


def cached_tree(dest, populate):
    if not dest.is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            staging = Path(tmp) / 'payload'
            staging.mkdir()
            populate(staging)
            try:
                staging.rename(dest)
            except OSError:
                if not dest.is_dir():  # Another successful builder may have won the race.
                    raise
    return dest


def prepare_steps(steps, cache, base_url):
    packages = set()
    repos = {}
    for step in steps:
        if step.source in REPOS and step.source not in repos:
            repo, revision = step.source, REPOS[step.source]
            def checkout(dst):
                run(['git', 'init', '-q'], dst)
                run(['git', 'fetch', '-q', '--depth=1', f'{base_url.rstrip("/")}/{repo}.git', revision], dst)
                run(['git', 'checkout', '-q', '--detach', revision], dst)
                if repo == 'nodejs/userland-migrations':
                    # The workflow imports this sibling workspace package during execution.
                    shutil.copytree(dst / 'utils', dst / 'node_modules/@nodejs/codemod-utils')
                if repo == 'codemod/react-codemod':
                    # Only the shared AST helpers are needed by the four pinned scripts.
                    with tempfile.TemporaryDirectory() as tmp:
                        prefix = Path(tmp)
                        (prefix / 'package.json').write_text('{"private":true}', encoding='utf-8')
                        run(['npm', 'install', '--ignore-scripts', '--no-fund', '--no-audit', '@jssg/utils@0.0.10'], prefix)
                        shutil.copytree(prefix / 'node_modules', dst / 'node_modules')
                    for item in PROFILES['react19'][1]:
                        if item.runner == 'react':
                            recipe = (dst / item.path).parents[1]
                            workflow = react_workflow(jsx_only=recipe.name == 'replace-string-ref')
                            (recipe / 'modernize.yaml').write_text(workflow, encoding='utf-8')
            entry = revision
            if repo == 'codemod/react-codemod':
                setup = react_workflow() + react_workflow(jsx_only=True) + '@jssg/utils@0.0.10'
                entry += '-' + hashlib.sha256(setup.encode()).hexdigest()[:10]
            repos[repo] = cached_tree(cache / 'repos' / repo / entry, checkout)
        if step.source in VERSIONS:
            packages.add(step.source)
        if step.runner in ('types', 'next', 'jscodeshift'):
            packages.add('jscodeshift')
        elif step.runner in ('workflow', 'react'):
            packages.add('codemod')
        elif step.runner == 'cleanup':
            packages.update(('jscodeshift', '@babel/parser', '@babel/traverse'))
        elif step.runner == 'eslint':
            packages.update(('eslint-plugin-n', 'eslint-plugin-unicorn', '@typescript-eslint/parser', 'typescript'))
    pins = {name: VERSIONS[name] for name in sorted(packages)}
    platform = run(['node', '-p', 'process.platform+"/"+process.arch+"/"+process.versions.modules'], Path.cwd(), capture=True).strip()
    key = hashlib.sha256((json.dumps(pins) + platform + ESLINT_CONFIG + CLEANUP_RENDER + NEXT_OG).encode()).hexdigest()[:20]
    def install(dst):
        (dst / 'package.json').write_text(json.dumps({'private': True, 'dependencies': pins}), encoding='utf-8')
        run(['npm', 'install', '--ignore-scripts', '--no-fund', '--no-audit', '--workspaces=false'], dst)
        (dst / 'next-og.cjs').write_text(NEXT_OG, encoding='utf-8')
        (dst / 'cleanup-render.cjs').write_text(CLEANUP_RENDER, encoding='utf-8')
        (dst / 'eslint.config.mjs').write_text(ESLINT_CONFIG, encoding='utf-8')
    tools = cached_tree(cache / 'tools' / key, install)
    modules = tools / 'node_modules'
    bases = {**repos, 'local': tools}
    paths = [(s, bases.get(s.source, modules / s.source) / s.path) for s in steps]
    for _, path in paths:
        if not path.is_file():
            raise Problem(f'Missing pinned transform: {path}')
    return tools, paths


def file_tree(start, excluded, cache):
    def fail(exc):
        raise exc
    for folder, dirs, names in os.walk(start, onerror=fail):
        parent = Path(folder)
        dirs[:] = sorted(d for d in dirs if d not in excluded and parent / d != cache
                         and not (parent / d).is_symlink()
                         and not (parent / d / '.git').exists())
        for name in sorted(names):
            path = parent / name
            if not path.is_symlink() and path.is_file():
                yield path


@dataclass
class Change:
    path: Path
    before: bytes
    after: bytes


def source_changes(root, target, excluded, steps, cache, base_url):
    originals = {p.relative_to(target): p.read_bytes() for p in file_tree(target, excluded, cache)
                 if p.suffix[1:] in EXTENSIONS}
    if not originals:
        raise Problem(f'No JS/TS source files found in {target}')
    tools, paths = prepare_steps(steps, cache, base_url)
    modules = tools / 'node_modules'
    suffixes = {p.suffix[1:] for p in originals}
    print(f'Staging {len(originals)} source file(s) under {target}', flush=True)
    with tempfile.TemporaryDirectory(prefix='modernize-source-') as tmp:
        stage = Path(tmp) / 'project'
        working = stage / target.relative_to(root)
        for rel, data in originals.items():
            (working / rel).parent.mkdir(parents=True, exist_ok=True)
            (working / rel).write_bytes(data)
        # Preserve package boundaries, module type, and Node engine constraints for the linters.
        parents = {target / rel.parent for rel in originals}
        for parent in list(parents):
            parents.update(p for p in parent.parents if p.is_relative_to(root))
        for parent in parents:
            manifest = parent / 'package.json'
            if manifest.is_file() and not manifest.is_symlink():
                shutil.copyfile(manifest, stage / manifest.relative_to(root))
        for step, path in paths:
            print(f'Running {step.source} / {step.path}', flush=True)
            if step.runner in ('workflow', 'react'):
                if step.runner == 'react':
                    path = path.parents[1] / 'modernize.yaml'
                run(['node', modules / 'codemod/codemod', 'workflow', 'run', '-w', path, '-t', working,
                     '--no-interactive', '--allow-dirty', '--disable-analytics'], working)
            elif step.runner == 'eslint':
                run(['node', path, '--config', tools / 'eslint.config.mjs', '--fix', '.'], working)
            else:
                if step.runner == 'next':
                    path = tools / 'next-og.cjs'
                groups = [('babel', JS), ('ts', TS), ('tsx', ('tsx',))]
                for parser, extensions in groups:
                    if not suffixes.intersection(extensions) or (step.runner == 'types' and parser == 'babel'):
                        continue
                    command = ['node', modules / 'jscodeshift/bin/jscodeshift.js', '--run-in-band', '--fail-on-error',
                               '-t', path, f'--parser={parser}', '--extensions=' + ','.join(extensions)]
                    if step.runner == 'types':
                        command.append('--preset19Transforms=' + PRESET19)
                    run([*command, '.'], working)
        changes = []
        for rel, before in originals.items():
            after = (working / rel).read_bytes()
            if before != after:
                changes.append(Change(target / rel, before, after))
        return changes


PACKAGE = r'(?:@[a-zA-Z0-9._~-]+/)?[a-zA-Z0-9~][a-zA-Z0-9._~-]*'
NUMBER = r'(?:0|[1-9][0-9]*)'
BUILD = r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
STABLE = re.compile(rf'({NUMBER})\.({NUMBER})\.({NUMBER}){BUILD}')
PART = rf'(?:{NUMBER}|[xX*])'
SIMPLE = re.compile(rf'([~^=]?)\s*(v?)({NUMBER})(?:\.({PART})(?:\.({PART}))?)?'
                    rf'(-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?{BUILD}')
DECODER = json.JSONDecoder()
WHITESPACE = re.compile(r'\s*')


def members(text, start):
    """Yield object key, decoded value and exact value span in already-validated JSON."""
    i = WHITESPACE.match(text, start + 1).end()
    while text[i] != '}':
        key, end = DECODER.raw_decode(text, i)
        colon = WHITESPACE.match(text, end).end()
        value_start = WHITESPACE.match(text, colon + 1).end()
        value, end = DECODER.raw_decode(text, value_start)
        yield key, value, value_start, end
        i = WHITESPACE.match(text, end).end()
        if text[i] == ',':
            i = WHITESPACE.match(text, i + 1).end()


def distinct_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'duplicate JSON key {key!r}')
        obj[key] = value
    return obj


@dataclass
class Manifest:
    path: Path
    raw: bytes
    text: str
    name: str | None
    entries: list


def load_manifest(path):
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8-sig')
        obj = json.loads(text, object_pairs_hook=distinct_keys)
        if not isinstance(obj, dict):
            raise ValueError('expected a JSON object')
        entries = []
        for key, value, start, _ in members(text, WHITESPACE.match(text).end()):
            if key in SECTIONS:
                if not isinstance(value, dict):
                    raise ValueError(f'{key} must be an object')
                for name, spec, a, b in members(text, start):
                    if not isinstance(spec, str):
                        raise ValueError(f'{key}.{name} must be a string')
                    entries.append((key, name, spec, a, b))
        return Manifest(path, raw, text, obj.get('name'), entries)
    except (UnicodeError, ValueError) as exc:
        raise Problem(f'{path}: {exc}') from exc


def version_key(version):
    match = STABLE.fullmatch(version)
    return tuple(map(int, match.groups())) if match else None


def registry_info(package, root):
    out = run(['npm', 'view', package, 'dist-tags', 'versions', '--json', '--workspaces=false',
               '--prefer-online', '--fetch-retries=1', '--fetch-timeout=30000'], root, capture=True)
    data = json.loads(out)
    if not isinstance(data, dict) or not isinstance(data.get('dist-tags'), dict):
        raise Problem(f'Unexpected registry metadata for {package}')
    return data


def select_version(info, channel, fallback):
    versions = info.get('versions', [])
    if isinstance(versions, str):
        versions = [versions]
    def highest(candidates):
        eligible = [(version_key(v), v) for v in candidates if isinstance(v, str) and version_key(v) is not None]
        return max(eligible)[1] if eligible else None
    if channel == 'lts':
        tags = info['dist-tags']
        tagged = highest(v for k, v in tags.items() if re.fullmatch(r'(?:lts(?:[-/].+)?|.+[-/]lts)', k, re.I) and v in versions)
        if tagged:
            return tagged, 'lts'
        if fallback != 'stable':
            return None, 'no stable LTS tag'
    return highest(versions), 'stable fallback' if channel == 'lts' else 'stable'


def dependency_source(name, spec, locals_):
    alias_prefix = ''
    if spec.startswith('npm:'):
        alias = re.fullmatch(rf'npm:({PACKAGE})(?:@(.+))?', spec)
        if not alias:
            return None, 'unsupported npm alias'
        name, spec = alias[1], alias[2] or 'latest'
        alias_prefix = f'npm:{name}@'
    if name in locals_:
        return None, 'local package'
    if not re.fullmatch(PACKAGE, name):
        return None, 'non-registry name'
    if spec in ('*', 'x', 'X', ''):
        return None, 'unbounded'
    if not SIMPLE.fullmatch(spec) and spec not in ('stable', 'latest', 'lts'):
        return None, 'protocol, custom tag, or complex range'
    return (name, spec, alias_prefix), None


def replace_spec(spec, version):
    """Keep precision, wildcard style and range operator. Never lower a version floor."""
    match = SIMPLE.fullmatch(spec)
    if not match:  # The accepted publisher tags are materialized as exact versions.
        return version
    operator, v, major, minor, patch, _ = match.groups()
    parts = [p for p in (major, minor, patch) if p is not None]
    old_floor = tuple(int(p) if p.isdigit() else 0 for p in [major, minor or '0', patch or '0'])
    if version_key(version) < old_floor:
        return spec
    numeric = version.split('+')[0].split('.')
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return operator + v + version
    return operator + v + '.'.join(numeric[i] if p.isdigit() else p for i, p in enumerate(parts))


def manifest_changes(root, excluded, channel, fallback, jobs, cache):
    manifests = [load_manifest(p) for p in file_tree(root, excluded, cache) if p.name == 'package.json']
    if not manifests:
        raise Problem(f'No package.json files found under {root}')
    locals_ = {m.name for m in manifests if isinstance(m.name, str)}
    names = set()
    for manifest in manifests:
        for _, name, spec, _, _ in manifest.entries:
            source, _ = dependency_source(name, spec, locals_)
            if source:
                names.add(source[0])
    print(f'Checking {len(names)} registry package(s) across {len(manifests)} manifest(s)', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = {name: pool.submit(registry_info, name, root) for name in sorted(names)}
        versions = {name: select_version(job.result(), channel, fallback) for name, job in pending.items()}
    changes = []
    for m in manifests:
        edits = []
        for section, name, old, a, b in m.entries:
            label = f'{m.path.relative_to(root)} [{section}] {name}'
            source, reason = dependency_source(name, old, locals_)
            if reason:
                print(f'  skip {label}: {old} ({reason})')
                continue
            package, spec, alias = source
            version, policy = versions[package]
            if version is None:
                print(f'  skip {label}: {old} ({policy if channel == "lts" else "no stable release"})')
                continue
            new = alias + replace_spec(spec, version)
            if new != old:
                print(f'  {label}: {old} -> {new} ({policy})')
                edits.append((a, b, json.dumps(new)))
            elif policy == 'lts' and SIMPLE.fullmatch(spec):
                print(f'  keep {label}: {old} (LTS {version})')
        if edits:
            chunks, pos = [], 0
            for a, b, new in sorted(edits):
                chunks.extend((m.text[pos:a], new))
                pos = b
            chunks.append(m.text[pos:])
            codec = 'utf-8-sig' if m.raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
            changes.append(Change(m.path, m.raw, ''.join(chunks).encode(codec)))
    return changes


def replace_file(path, data):
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.modernize-', delete=False) as out:
        tmp = Path(out.name)
    try:
        tmp.write_bytes(data)
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def matches(path, data):
    return path.resolve() == path and path.is_file() and path.read_bytes() == data


def apply_changes(changes):
    # Check the entire plan before the first replacement, then again per file.
    for c in changes:
        if not matches(c.path, c.before):
            raise Problem(f'Changed since preview began: {c.path}. Rerun to re-plan.')
    written = []
    try:
        for c in changes:
            if not matches(c.path, c.before):
                raise Problem(f'Concurrent edit: {c.path}')
            replace_file(c.path, c.after)
            written.append(c)
    except BaseException:
        for c in reversed(written):
            try:
                if matches(c.path, c.after):
                    replace_file(c.path, c.before)
                else:
                    print(f'Concurrent edit retained during rollback: {c.path}', file=sys.stderr)
            except OSError as exc:
                print(f'Rollback failed for {c.path}: {exc}', file=sys.stderr)
        raise


def arguments():
    p = argparse.ArgumentParser(description='Preview or apply pinned source migrations and dependency upgrades.',
                                epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--list', action='store_true', help='list profiles, transforms, source repos and pins')
    p.add_argument('--root', type=Path, default=Path.cwd(), help='project/monorepo root (default: cwd)')
    p.add_argument('--target', type=Path, default=Path('src'), help='source directory relative to root (default: src)')
    p.add_argument('--profile', choices=PROFILES, action='append', default=[], help='source profile; repeat to combine in order')
    p.add_argument('--upgrade', choices=('stable', 'lts'), help='scan every eligible package.json under --root')
    p.add_argument('--lts-fallback', choices=('skip', 'stable'), default='skip', help='when no publisher LTS tag exists (default: skip)')
    p.add_argument('--jobs', type=int, default=4, help='parallel npm metadata lookups (default: 4)')
    p.add_argument('--exclude-dir', action='append', default=[], metavar='NAME', help='extra directory basename to exclude; repeatable')
    p.add_argument('--cache-dir', type=Path, default=Path(os.getenv('XDG_CACHE_HOME') or Path.home() / '.cache') / 'js-modernize',
                   help='dedicated source/tool cache (excluded from scans)')
    p.add_argument('--git-base-url', default=os.getenv('GIT_BASE_URL', 'https://github.com'), help='Git host or mirror base URL')
    p.add_argument('--apply', action='store_true', help='write changes after every selected step succeeds (default: preview)')
    p.add_argument('--diff', action='store_true', help='print unified diffs')
    args = p.parse_args()
    if not (args.list or args.profile or args.upgrade):
        p.error('choose --list, --profile, or --upgrade')
    if args.jobs < 1:
        p.error('--jobs must be positive')
    if args.lts_fallback != 'skip' and args.upgrade != 'lts':
        p.error('--lts-fallback stable requires --upgrade lts')
    return args


def main():
    args = arguments()
    if args.list:
        for name, (label, steps) in PROFILES.items():
            print(f'{name:10} {label}')
            for step in steps:
                origin = NPM_ORIGINS.get(step.source, step.source)
                pin = REPOS.get(step.source) or VERSIONS.get(step.source, 'bundled')
                print(f'  {origin}  {step.path} ({step.source}@{pin})')
        print('Tool pins: ' + ', '.join(f'{k}@{v}' for k, v in VERSIONS.items()))
        return
    root = args.root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise Problem(f'Expected project directory: {root}')
    excluded = EXCLUDED.union(args.exclude_dir)
    changes = []
    cache = args.cache_dir.expanduser().resolve()
    if cache == root:
        raise Problem('Choose a dedicated cache directory, distinct from --root')
    if args.upgrade:
        changes.extend(manifest_changes(root, excluded, args.upgrade, args.lts_fallback, args.jobs, cache))
    if args.profile:
        target = (root / args.target).resolve(strict=True)
        if not target.is_dir() or not target.is_relative_to(root):
            raise Problem('--target must be a directory inside --root')
        if target.is_relative_to(cache):
            raise Problem('--target must be outside the tool cache')
        steps = [s for name in dict.fromkeys(args.profile) for s in PROFILES[name][1]]
        changes.extend(source_changes(root, target, excluded, steps, cache, args.git_base_url))
    for c in changes:
        rel = c.path.relative_to(root).as_posix()
        print(f'  changed  {rel}')
        if args.diff:
            delta = difflib.unified_diff(c.before.decode('utf-8', 'replace').splitlines(), c.after.decode('utf-8', 'replace').splitlines(),
                                         fromfile='a/' + rel, tofile='b/' + rel, lineterm='')
            for line in delta:
                print(line)
    if args.apply:
        apply_changes(changes)
    verb = 'Applied changes to' if args.apply else 'Previewed changes to'
    print(f'{verb} {len(changes)} file(s).')
    if changes and not args.apply:
        print('Rerun with --apply to write these changes.')
    if args.apply and any(c.path.name == 'package.json' for c in changes):
        print("Run your project's package manager install in each affected workspace to refresh lockfiles.")


if __name__ == '__main__':
    try:
        main()
    except (Problem, OSError, ValueError) as exc:
        sys.exit(f'error: {exc}')
    except KeyboardInterrupt:
        sys.exit(130)
