#!/usr/bin/env bash
#
# Check that settings.yaml covers all MISP settings.
#
# Queries the MISP API for all known settings and compares against
# settings.yaml. Reports any settings not tracked in our config.
# Intended to run during integration testing to catch new settings
# introduced by MISP upgrades.
#
# Usage:
#   tests/check-settings-coverage.sh <base_url> <authkey>
#
# Exit codes:
#   0 - all settings covered (or only known-skipped ones missing)
#   1 - new unknown settings found with non-empty values
#
set -euo pipefail

BASE_URL="${1:?Usage: check-settings-coverage.sh <base_url> <authkey>}"
API_KEY="${2:?Usage: check-settings-coverage.sh <base_url> <authkey>}"
BASE_URL="${BASE_URL%/}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS_YAML="${SCRIPT_DIR}/../files/misp-config/settings.yaml"

if [ ! -f "$SETTINGS_YAML" ]; then
    echo "ERROR: settings.yaml not found at $SETTINGS_YAML" >&2
    exit 1
fi

echo "Checking settings coverage against ${BASE_URL}"
echo ""

API_JSON=$(mktemp)
trap 'rm -f "$API_JSON"' EXIT

HTTP_CODE=$(curl -sS -w "%{http_code}" -o "$API_JSON" --max-time 60 \
    -H "Authorization: ${API_KEY}" \
    -H "Accept: application/json" \
    "${BASE_URL}/servers/serverSettings")

if [ "$HTTP_CODE" != "200" ]; then
    echo "ERROR: /servers/serverSettings returned HTTP ${HTTP_CODE}" >&2
    head -c 200 "$API_JSON" >&2
    exit 1
fi

# Verify we got JSON, not an HTML error page
if ! python3 -c "import json, sys; json.load(open(sys.argv[1]))" "$API_JSON" 2>/dev/null; then
    echo "ERROR: /servers/serverSettings did not return valid JSON" >&2
    head -c 300 "$API_JSON" >&2
    echo >&2
    exit 1
fi

python3 -c "
import json, sys, yaml

with open(sys.argv[1]) as f:
    data = json.load(f)
with open(sys.argv[2]) as f:
    our_yaml = yaml.safe_load(f)

our_keys = set()
for group in our_yaml.get('settings', {}).values():
    if isinstance(group, dict):
        our_keys.update(group.keys())

# Settings we intentionally skip (email templates, dynamic values)
KNOWN_SKIPPED = {
    'MISP.live',                           # set dynamically by entrypoint
    'MISP.forgotPasswordText',             # long email template with \$variables
    'MISP.forgotPasswordTextNoEnc',        # long email template
    'MISP.newUserText',                    # long email template
    'MISP.passwordResetText',              # long email template
    'MISP.maintenance_message',            # long text
    'Security.email_otp_text',             # long email template
    'Security.self_registration_message',  # long form text
}

new_nonempty = []
new_empty = 0
known_skipped = 0

for s in data['finalSettings']:
    key = s.get('setting', '')
    if not key or key in our_keys:
        continue
    if key in KNOWN_SKIPPED:
        known_skipped += 1
        continue
    val = s.get('value')
    if val in (None, '', False, 0, '0') or (isinstance(val, str) and val.lower() == 'false'):
        new_empty += 1
        continue
    new_nonempty.append((key, val))

api_total = len(data['finalSettings'])
print(f'MISP settings:             {api_total}')
print(f'Tracked in settings.yaml:  {len(our_keys)}')
print(f'Known-skipped (templates): {known_skipped}')
print(f'Untracked (empty/disabled):{new_empty}')
print(f'NEW with non-empty value:  {len(new_nonempty)}')

if new_nonempty:
    print()
    print('WARNING: The following settings have non-empty values but are')
    print('not tracked in settings.yaml. Review and add them:')
    print()
    for key, val in sorted(new_nonempty):
        display = str(val)[:70]
        print(f'  {key}: {display}')
    sys.exit(1)
else:
    print()
    print('OK: all settings with non-empty values are tracked')
    sys.exit(0)
" "$API_JSON" "$SETTINGS_YAML"
