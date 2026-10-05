#!/bin/bash

set -ev

source ./contrib/dash/travis/electrum_dash_version_env.sh;
echo wine build version is $DASH_ELECTRUM_VERSION

./contrib/make_locale

export ELECTRUM_COMMIT_HASH=$(git rev-parse HEAD)
if [ "$WINEARCH" = "win32" ] ; then
    export GCC_TRIPLET_HOST="i686-w64-mingw32"
elif [ "$WINEARCH" = "win64" ] ; then
    export GCC_TRIPLET_HOST="x86_64-w64-mingw32"
else
    fail "unexpected WINEARCH: $WINEARCH"
fi
export host_strip="${GCC_TRIPLET_HOST}-strip"

./contrib/build-wine/build_secp256k1.sh
./contrib/build-wine/build_libsparkmobile.sh
./contrib/build-wine/build_x11_hash.sh
./contrib/build-wine/build_pyinstaller.sh

mv $BUILD_DIR/zbarw $WINEPREFIX/drive_c/

cd $WINEPREFIX/drive_c/electrum-dash

rm -rf build
rm -rf dist/electrum-dash

cp contrib/build-wine/deterministic.spec .
cp contrib/dash/pyi_runtimehook.py .
cp contrib/dash/pyi_tctl_runtimehook.py .

wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements.txt
wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements-hw.txt
wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements-binaries.txt
wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements-build-wine.txt

wine pyinstaller --clean -y \
    --name electrum-firo-$DASH_ELECTRUM_VERSION.exe \
    deterministic.spec

SPARK_SMOKE_DIR=$(mktemp -d "$WINEPREFIX/drive_c/spark_smoke.XXXX")
cp "$WINEPREFIX/drive_c/libsparkmobile/electrum_libsparkmobile.dll" "$SPARK_SMOKE_DIR/"
wine python -c "import ctypes, sys; ctypes.CDLL(sys.argv[1]).isValidSparkAddress; print('libsparkmobile dll loads')" \
    "C:\\$(basename "$SPARK_SMOKE_DIR")\\electrum_libsparkmobile.dll" \
    || { echo "packaged libsparkmobile dll cannot be loaded"; exit 1; }
rm -rf "$SPARK_SMOKE_DIR"

if [[ $WINEARCH == win32 ]]; then
    NSIS_EXE="$WINEPREFIX/drive_c/Program Files/NSIS/makensis.exe"
else
    NSIS_EXE="$WINEPREFIX/drive_c/Program Files (x86)/NSIS/makensis.exe"
fi

wine "$NSIS_EXE" /NOCD -V3 \
    /DPRODUCT_VERSION=$DASH_ELECTRUM_VERSION \
    /DWINEARCH=$WINEARCH \
    contrib/build-wine/electrum-dash.nsi
