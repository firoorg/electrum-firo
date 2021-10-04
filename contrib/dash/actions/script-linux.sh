#!/bin/bash
set -ev

mkdir -p dist
docker run --rm \
    -v $(pwd):/opt \
    -w /opt/ \
    -t zebralucky/electrum-dash-winebuild:Linux40x \
    /opt/contrib/build-linux/sdist/build.sh


sudo find . -name '*.po' -delete
sudo find . -name '*.pot' -delete


docker run --rm \
    -v $(pwd):/opt \
    -w /opt/contrib/build-linux/appimage \
    -t zebralucky/electrum-dash-winebuild:AppImage40x ./build.sh


BUILD_DIR=/root/build
TOR_PROXY_VERSION=0.4.5.7
TOR_PROXY_PATH=https://github.com/zebra-lucky/tor-proxy/releases/download
TOR_DIST=dist/tor-proxy-setup.exe

TOR_FILE=${TOR_PROXY_VERSION}/tor-proxy-${TOR_PROXY_VERSION}-win32-setup.exe
wget -O ${TOR_DIST} ${TOR_PROXY_PATH}/${TOR_FILE}
TOR_SHA=233ee2c8f4cbab6ffff74479156d91929564e7af8f9ff614e793f59fb51ac0f3
echo "$TOR_SHA  $TOR_DIST" > sha256.txt
shasum -a256 -s -c sha256.txt


export WINEARCH=win32
export WINEPREFIX=/root/.wine-32
export PYHOME=$WINEPREFIX/drive_c/Python38


ZBARW_PATH=https://github.com/zebra-lucky/zbarw/releases/download/20180620
ZBARW_FILE=zbarw-zbarcam-0.10-win32.zip
ZBARW_SHA=eed1af99d68a1f9eab975843071bf088735cb79bf3188d511d06a3f1b4e10243
wget ${ZBARW_PATH}/${ZBARW_FILE}
echo "$ZBARW_SHA  $ZBARW_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${ZBARW_FILE} && rm ${ZBARW_FILE} sha256.txt


docker run --rm \
    -e WINEARCH=$WINEARCH \
    -e WINEPREFIX=$WINEPREFIX \
    -e PYHOME=$PYHOME \
    -e BUILD_DIR=$BUILD_DIR \
    -v $(pwd):$BUILD_DIR \
    -v $(pwd):$WINEPREFIX/drive_c/electrum-dash \
    -w $BUILD_DIR \
    -t zebralucky/electrum-dash-winebuild:Wine41x \
    $BUILD_DIR/contrib/build-wine/build.sh


export WINEARCH=win64
export WINEPREFIX=/root/.wine-64
export PYHOME=$WINEPREFIX/drive_c/Python38


ZBARW_FILE=zbarw-zbarcam-0.10-win64.zip
ZBARW_SHA=7705dfd9a1c4b9d07c9ae11502dbe2dc305d08c884f0825b35d21b312316e162
wget ${ZBARW_PATH}/${ZBARW_FILE}
echo "$ZBARW_SHA  $ZBARW_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${ZBARW_FILE} && rm ${ZBARW_FILE} sha256.txt

rm ${TOR_DIST}
TOR_FILE=${TOR_PROXY_VERSION}/tor-proxy-${TOR_PROXY_VERSION}-win64-setup.exe
wget -O ${TOR_DIST} ${TOR_PROXY_PATH}/${TOR_FILE}
TOR_SHA=514387e3b45eccd9b98e95450ea201ced49886cc4f0980d4f0f6f7a4a51aebe9
echo "$TOR_SHA  $TOR_DIST" > sha256.txt
shasum -a256 -s -c sha256.txt
rm sha256.txt


docker run --rm \
    -e WINEARCH=$WINEARCH \
    -e WINEPREFIX=$WINEPREFIX \
    -e PYHOME=$PYHOME \
    -e BUILD_DIR=$BUILD_DIR \
    -v $(pwd):$BUILD_DIR \
    -v $(pwd):$WINEPREFIX/drive_c/electrum-dash \
    -w $BUILD_DIR \
    -t zebralucky/electrum-dash-winebuild:Wine41x \
    $BUILD_DIR/contrib/build-wine/build.sh
