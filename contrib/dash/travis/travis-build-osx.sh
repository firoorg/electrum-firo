#!/bin/bash
set -ev

export MACOSX_DEPLOYMENT_TARGET=10.13

export PY37BINDIR=/Library/Frameworks/Python.framework/Versions/3.7/bin/
export PATH=$PATH:$PY37BINDIR
source ./contrib/dash/travis/electrum_firo_version_env.sh;
echo osx build version is $DASH_ELECTRUM_VERSION


cd build
if [[ -n $TRAVIS_TAG ]]; then
    BUILD_REPO_URL=https://github.com/akhavr/electrum-dash.git
    git clone --branch $TRAVIS_TAG $BUILD_REPO_URL electrum-dash
    PIP_CMD="sudo python3 -m pip"
else
    git clone .. electrum-dash
    python3 -m virtualenv env
    source env/bin/activate
    PIP_CMD="pip"
fi
cd electrum-dash


$PIP_CMD install --no-dependencies --no-warn-script-location -I \
    -r contrib/deterministic-build/requirements.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -I \
    -r contrib/deterministic-build/requirements-hw.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -I \
    -r contrib/deterministic-build/requirements-binaries-mac.txt
$PIP_CMD install --no-dependencies --no-warn-script-location -I x11_hash>=1.4

$PIP_CMD install --no-dependencies --no-warn-script-location -I \
    -r contrib/deterministic-build/requirements-build-mac.txt

export PATH="/usr/local/opt/gettext/bin:$PATH"
./contrib/make_locale
find . -name '*.po' -delete
find . -name '*.pot' -delete

cp contrib/osx/osx.spec .
cp contrib/dash/pyi_runtimehook.py .
cp contrib/dash/pyi_tctl_runtimehook.py .

pyinstaller --clean \
    -y \
    --name electrum-dash-$DASH_ELECTRUM_VERSION.bin \
    osx.spec

sudo hdiutil create -fs HFS+ -volname "Firo Electrum" \
    -srcfolder dist/Firo\ Electrum.app \
    dist/Firo-Electrum-$DASH_ELECTRUM_VERSION-macosx.dmg
