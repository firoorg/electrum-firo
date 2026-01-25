#!/bin/bash
set -ev

export MACOSX_DEPLOYMENT_TARGET=11.0

echo osx build version is $DASH_ELECTRUM_VERSION

python3 -m venv env
source env/bin/activate
PIP_CMD="pip"

$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-hw.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-binaries-mac.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U x11_hash>=1.4
$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-build-mac.txt

export PATH="/opt/homebrew/bin:/usr/local/opt/gettext/bin:$PATH"
./contrib/make_locale
find . -name '*.po' -delete
find . -name '*.pot' -delete

cp contrib/osx/osx_actions.spec osx.spec
cp contrib/dash/pyi_runtimehook.py .
cp contrib/dash/pyi_tctl_runtimehook.py .

pyinstaller --clean \
    -y \
    --name electrum-firo-$DASH_ELECTRUM_VERSION.bin \
    osx.spec

sudo hdiutil create -fs HFS+ -volname "Firo Electrum" \
    -srcfolder dist/Firo\ Electrum.app \
    dist/Firo-Electrum-$DASH_ELECTRUM_VERSION