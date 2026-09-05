#!/bin/bash
set -ev

export MACOSX_DEPLOYMENT_TARGET=10.13

export PY37BINDIR=/Library/Frameworks/Python.framework/Versions/3.7/bin/
echo osx build version is $DASH_ELECTRUM_VERSION

if [[ -n $GITHUB_REF ]]; then
    export PATH="$PY37BINDIR:$PATH"
    PIP_CMD="sudo -H $PY37BINDIR/python3 -m pip"
else
    export PATH=$PATH:$PY37BINDIR
    python3 -m virtualenv env
    source env/bin/activate
    PIP_CMD="pip"
fi


$PIP_CMD install --no-warn-script-location -U pip==24.0

PYTHON_BLS_WHL=python_bls-0.1.9-cp37-cp37m-macosx_10_6_intel.whl
PYTHON_BLS_SHA256=9c9842c2cebdcb095aa3b5cae087b0e8a06b1c0f66501491438625f85a33ff00
echo "${PYTHON_BLS_SHA256}  ${PYTHON_BLS_WHL}" > ${PYTHON_BLS_WHL}.sha256
curl -O -L https://files.pythonhosted.org/packages/5d/96/856dffe5f31ae604ba1a751c9e60dd7cf201cb4131946ddf05eac0b3852b/${PYTHON_BLS_WHL}
shasum -a256 -s -c ${PYTHON_BLS_WHL}.sha256
$PIP_CMD install --no-dependencies --no-warn-script-location ${PYTHON_BLS_WHL}
rm -f ${PYTHON_BLS_WHL} ${PYTHON_BLS_WHL}.sha256

$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-hw.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-binaries-mac.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -U x11_hash>=1.4

$PIP_CMD install --no-dependencies --no-warn-script-location -U \
    -r contrib/deterministic-build/requirements-build-mac.txt

if [[ -x /usr/local/bin/brew ]]; then
  export PATH="$(/usr/local/bin/brew --prefix gettext)/bin:$PATH"
elif command -v brew >/dev/null 2>&1; then
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
