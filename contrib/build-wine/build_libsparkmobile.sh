#!/bin/bash

set -ev

echo "Building libsparkmobile for $WINEARCH."
export PROJ_ROOT=$WINEPREFIX/drive_c/electrum-dash
export DIST_DIR=$WINEPREFIX/drive_c/libsparkmobile

cd $PROJ_ROOT
source ./contrib/build_tools_util.sh
rm -f electrum_dash/electrum_libsparkmobile.dll electrum_dash/libelectrum_libsparkmobile.dll

export BUILD_FOR_SYSTEM_NAME=windows
export LIBSPARKMOBILE_CMAKE_EXTRA_ARGS="\
    -DCMAKE_SYSTEM_NAME=Windows \
    -DCMAKE_C_COMPILER=${GCC_TRIPLET_HOST}-gcc \
    -DCMAKE_CXX_COMPILER=${GCC_TRIPLET_HOST}-g++ \
    -DCMAKE_RC_COMPILER=${GCC_TRIPLET_HOST}-windres \
    -DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER \
    -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY \
    -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY \
    -DCMAKE_C_STANDARD_LIBRARIES=-lssp \
    -DCMAKE_CXX_STANDARD_LIBRARIES=-lssp"
./contrib/make_libsparkmobile.sh || fail "Could not build libsparkmobile."

DLL=electrum_dash/electrum_libsparkmobile.dll
[ -f "$DLL" ] || DLL=electrum_dash/libelectrum_libsparkmobile.dll
[ -f "$DLL" ] || fail "libsparkmobile dll not found after build."

$host_strip "$DLL" || true
mkdir -p $DIST_DIR
cp -fpv "$DLL" "$DIST_DIR/electrum_libsparkmobile.dll" || fail "Could not copy libsparkmobile dll."
rm -rf electrum_libsparkmobile/ "$DLL"
