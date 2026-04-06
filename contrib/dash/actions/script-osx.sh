#!/bin/bash
set -ev

export MACOSX_DEPLOYMENT_TARGET=10.13

export PY37BINDIR=/Library/Frameworks/Python.framework/Versions/3.7/bin/
export PATH=$PATH:$PY37BINDIR
echo osx build version is $DASH_ELECTRUM_VERSION


if [[ -n $GITHUB_REF ]]; then
    PIP_CMD="sudo python3 -m pip"
else
    python3 -m virtualenv env
    source env/bin/activate
    PIP_CMD="pip"
fi


$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-hw.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-binaries-mac.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U x11_hash>=1.4

$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-build-mac.txt

if command -v brew >/dev/null 2>&1; then
  export PATH="$(brew --prefix gettext)/bin:$PATH"
else
  export PATH="/usr/local/opt/gettext/bin:$PATH"
fi

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
    dist/Firo-Electrum-$DASH_ELECTRUM_VERSION-macosx.dmg
