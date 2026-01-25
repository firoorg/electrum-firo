#!/bin/bash
set -ev

PYTHON_VERSION=$(python3 --version)
echo "Using system Python: $PYTHON_VERSION"

brew install gettext libtool automake pkg-config

LIBUSB_VER=1.0.24
LIBUSB_URI=https://github.com/libusb/libusb/releases/download
LIBUSB_SHA=7efd2685f7b327326dcfb85cee426d9b871fd70e22caa15bb68d595ce2a2b12a
LIBUSB_FILE=libusb-${LIBUSB_VER}.tar.bz2
echo "${LIBUSB_SHA}  ${LIBUSB_FILE}" > ${LIBUSB_FILE}.sha256
curl -O -L ${LIBUSB_URI}/v${LIBUSB_VER}/${LIBUSB_FILE}
tar -xjf ${LIBUSB_FILE}
shasum -a256 -s -c ${LIBUSB_FILE}.sha256
pushd libusb-${LIBUSB_VER}
./configure --disable-dependency-tracking --prefix=/opt/libusb
sudo env MACOSX_DEPLOYMENT_TARGET=11.0 make install
popd
sudo rm -rf libusb-${LIBUSB_VER}*
cp /opt/libusb/lib/libusb-1.*.dylib . || true

brew install secp256k1
cp /opt/homebrew/lib/libsecp256k1*.dylib . || true

if [[ -n $GITHUB_REF ]]; then
    echo "Building ZBar dylib..."
    rm -f libzbar.0.dylib
    export MACOSX_DEPLOYMENT_TARGET=11.0
    ./contrib/make_zbar.sh
    rm -rf contrib/zbar/
fi