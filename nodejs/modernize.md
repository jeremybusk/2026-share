### See the transforms and their source repositories
```
python3 modernize.py --list
```

### Preview source migrations and dependency upgrades together
```
python3 modernize.py --profile js --profile react19 --upgrade stable --diff
```

### Apply the same choices
```
python3 modernize.py --profile js --profile react19 --upgrade stable --apply
```

### Upgrade dependencies only, preferring LTS and falling back to stable
```
python3 modernize.py --upgrade lts --lts-fallback stable --apply
```

### set npm registry

```
python3 modernize.py --upgrade stable --diff \
  --npm-registry https://npm.corp.example/repository/npm/
```

```
python3 modernize.py --profile react19 --diff \
  --npm-registry https://npm.corp.example/repository/npm/ \
  --git-base-url https://git.corp.example/mirrors
```

If the registry requires authentication, put the following in ~/.npmrc and provide NPM_TOKEN through your shell or CI secret store. The registry line also sets the default for a later pnpm install. The host and path on the token line should match your actual registry. npm Docs https://docs.npmjs.com/using-private-packages-in-a-ci-cd-workflow/?utm_source=chatgpt.com

```
registry=https://npm.corp.example/repository/npm/
//npm.corp.example/repository/npm/:_authToken=${NPM_TOKEN}
```
