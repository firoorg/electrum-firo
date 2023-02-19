#!/bin/bash
set -ev

if [[ -n $GITHUB_ACTION ]]; then
    PYTHON_VERSION=3.7.9
    PYFTP=https://www.python.org/ftp/python/$PYTHON_VERSION
    PYPKG_NAME=python-$PYTHON_VERSION-macosx10.9.pkg
    PY_SHA256=bf54a14eef23467991e8c7a88c7307762e484c024a94ec1ee292ac1db3d41fc9
    echo "$PY_SHA256  $PYPKG_NAME" > $PYPKG_NAME.sha256
    curl -O $PYFTP/$PYPKG_NAME
    shasum -a256 -s -c $PYPKG_NAME.sha256
    sudo installer -pkg $PYPKG_NAME -target /
    rm $PYPKG_NAME $PYPKG_NAME.sha256
fi

LIBUSB_VER=1.0.24
LIBUSB_URI=https://github.com/libusb/libusb/releases/download
LIBUSB_SHA=7efd2685f7b327326dcfb85cee426d9b871fd70e22caa15bb68d595ce2a2b12a
LIBUSB_FILE=libusb-${LIBUSB_VER}.tar.bz2
echo "${LIBUSB_SHA}  ${LIBUSB_FILE}" > ${LIBUSB_FILE}.sha256
curl -O -L ${LIBUSB_URI}/v${LIBUSB_VER}/${LIBUSB_FILE}
tar -xzvf ${LIBUSB_FILE}
shasum -a256 -s -c ${LIBUSB_FILE}.sha256
pushd libusb-${LIBUSB_VER}
./configure --disable-dependency-tracking --prefix=/opt/libusb
sudo env MACOSX_DEPLOYMENT_TARGET=10.13 make install
popd
sudo rm -rf libusb-${LIBUSB_VER}*
cp /opt/libusb/lib/libusb-1.*.dylib .

LSECP256K1_PATH=https://github.com/zebra-lucky/secp256k1/
LSECP256K1_PATH=${LSECP256K1_PATH}releases/download/210521
LSECP256K1_FILE=libsecp256k1-210521-osx.tgz
LIB_SHA256=51c861bfb894ec520cc1ee0225fae00447aa86096782a1acd1fc6e338a576ea7
echo "$LIB_SHA256  $LSECP256K1_FILE" > $LSECP256K1_FILE.sha256
curl -O -L ${LSECP256K1_PATH}/${LSECP256K1_FILE}
shasum -a256 -s -c ${LSECP256K1_FILE}.sha256
tar -xzf ${LSECP256K1_FILE}
rm -f libsecp256k1.0.dylib
cp libsecp256k1/libsecp256k1.0.dylib .
rm -rf libsecp256k1/ ${LSECP256K1_FILE} ${LSECP256K1_FILE}.sha256

brew install gettext libtool automake pkg-config

if [[ -n $GITHUB_REF ]]; then
    echo "Building ZBar dylib..."
    rm -f libzbar.0.dylib
    export MACOSX_DEPLOYMENT_TARGET=10.13
    ./contrib/make_zbar.sh
    rm -rf contrib/zbar/
fi