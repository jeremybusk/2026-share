# See the transforms and their source repositories
python3 js-renew.py --list

# Preview source migrations and dependency upgrades together
python3 js-renew.py --profile js --profile react19 --upgrade stable --diff

# Apply the same choices
python3 js-renew.py --profile js --profile react19 --upgrade stable --apply

# Upgrade dependencies only, preferring LTS and falling back to stable
python3 js-renew.py --upgrade lts --lts-fallback stable --apply
