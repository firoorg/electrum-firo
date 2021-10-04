#!/bin/bash

set -xev

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_DIR="$(realpath $SCRIPT_DIR/../../)"
ACTIONS_DIR="$SCRIPT_DIR/actions"
TRAVIS_DIR="$SCRIPT_DIR/travis"
BUILD_DIST_DIR="$REPO_DIR/dist"
BUILD_BIN_DIR="$REPO_DIR/bin"
NAME=Firo-Electrum
TNAME=Firo-Electrum-Testnet
DEB_NAME=electrum-dash
APK_NAME=Electrum_DASH
APK_TNAME=Electrum_DASH_Testnet
UAPK_TAIL=release-unsigned.apk
APK_TAIL=release.apk


function build_sdist {
    sudo rm -rf build
    mkdir -p build
    $ACTIONS_DIR/install-linux.sh
    $ACTIONS_DIR/script-linux.sh
    cp ${BUILD_DIST_DIR}/${NAME}-${DASH_ELECTRUM_VERSION}.tar.gz \
        dist/
    cp ${BUILD_DIST_DIR}/${NAME}-${DASH_ELECTRUM_VERSION}.zip \
        dist/
    cp ${BUILD_DIST_DIR}/${NAME}-${DASH_ELECTRUM_VERSION}-x86_64.AppImage \
        dist/
    cp ${BUILD_DIST_DIR}/${NAME}-${DASH_ELECTRUM_VERSION}-setup-win32.exe \
        dist/
    cp ${BUILD_DIST_DIR}/${NAME}-${DASH_ELECTRUM_VERSION}-setup-win64.exe \
        dist/
    cp ${BUILD_DIST_DIR}/portable-electrum-firo-${DASH_ELECTRUM_VERSION}.exe \
        dist/${NAME}-${DASH_ELECTRUM_VERSION}-portable.exe
}

function build_ppa {
    # Build deb packages
    PEP440_PUBVER_PATTERN="^((\d+)!)?"
    PEP440_PUBVER_PATTERN=${PEP440_PUBVER_PATTERN}"(([0-9]+)(\.[0-9]+)*)"
    PEP440_PUBVER_PATTERN=${PEP440_PUBVER_PATTERN}"([a-zA-Z]+[0-9]+)?"
    PEP440_PUBVER_PATTERN=${PEP440_PUBVER_PATTERN}"((\.[a-zA-Z]+[0-9]+)*)$"
    if [[ ${DDASH_ELECTRUM_VERSION} =~ ${PEP440_PUBVER_PATTERN} ]]; then
        if [[ -n ${BASH_REMATCH[1]} ]]; then
            DEB_VERSION="${BASH_REMATCH[2]}:"
        fi
        DEB_VERSION="${DEB_VERSION}${BASH_REMATCH[3]}"
        if [[ -n ${BASH_REMATCH[6]} ]]; then
            DEB_VERSION="${DEB_VERSION}~${BASH_REMATCH[6]}"
        fi
        if [[ -n ${BASH_REMATCH[7]} ]]; then
            DEB_VERSION="${DEB_VERSION}${BASH_REMATCH[7]}"
        fi
        DEB_SERIES=("xenial" "bionic" "focal" "groovy" "hirsute")
        DEB_SER_VER=("16.04.1" "18.04.1" "20.04.1" "20.10.1" "21.04.1")

        pushd build
        sudo rm -rf electrum-dash
        cp ../dist/${NAME}-${DASH_ELECTRUM_VERSION}.tar.gz \
            ${DEB_NAME}_${DEB_VERSION}.orig.tar.gz
        tar xzf ${DEB_NAME}_${DEB_VERSION}.orig.tar.gz
        pushd ${NAME}-${DASH_ELECTRUM_VERSION}

        for ((i=0;i<${#DEB_SERIES[@]};i++)); do
            if [[ -f ~/pbuilder/${DEB_SERIES[i]}-base.tgz ]]; then
                PPA_VERSION=${DEB_VERSION}-0ppa1~ubuntu${DEB_SER_VER[i]}
                PPA_NAME=${DEB_NAME}_${PPA_VERSION}
                DEB_BUILD_DIR=~/pbuilder/${DEB_SERIES[i]}_result
                CHANGELOG_FIRST="${DEB_NAME} (${PPA_VERSION})"
                CHANGELOG_FIRST="${CHANGELOG_FIRST} ${DEB_SERIES[i]};"
                CHANGELOG_FIRST="${CHANGELOG_FIRST} urgency=medium"
                sed -i "1s/.*/$CHANGELOG_FIRST/" debian/changelog
                echo Building ${PPA_NAME}_all.deb python3-${PPA_NAME}_all.deb
                sudo debuild -i -us -uc -S
                pbuilder-dist ${DEB_SERIES[i]} ../${PPA_NAME}.dsc
                mkdir ../../dist/${DEB_SERIES[i]}
                cp ${DEB_BUILD_DIR}/${PPA_NAME}_all.deb \
                    ../../dist/${DEB_SERIES[i]}
                cp ${DEB_BUILD_DIR}/python3-${PPA_NAME}_all.deb \
                    ../../dist/${DEB_SERIES[i]}
            else
                echo ~/pbuilder/${DEB_SERIES[i]}-base.tgz not found
                echo Skip deb packages build for ${DEB_SERIES[i]}
            fi
        done

        popd
        popd
    else
        echo Version does not match PEP440 pubversion patter
        echo Skip deb packages build
    fi
}


function read_jks_storepass {
    KEYSTORE=~/.jks/keystore
    KEYSTORE_ARG="-keystore $KEYSTORE"
    KEYTOOL_CMD="keytool -list -storepass:env JKS_STOREPASS $KEYSTORE_ARG"
    while [[ -z $JKS_STOREPASS ]]; do
        echo -n Input $KEYSTORE keystore password:
        read -s JKS_STOREPASS
        echo
        export JKS_STOREPASS
        keytool_res=`$KEYTOOL_CMD`
        if [[ $? == 0 ]]; then
            break
        else
            echo Wrong password
            export JKS_STOREPASS=''
        fi
    done
    export JKS_KEYPASS=$JKS_STOREPASS
}


function build_apk {
    exit 0
    ./build/contrib/actions/before_install-linux-apk.sh
    export APP_ANDROID_ARCH=armeabi-v7a
    #export APP_ANDROID_ARCH=arm64-v8a

    # Build mainnet release apk
    if [[ -n $IS_RELEASE ]]; then
        sudo rm -rf build
        mkdir -p build && cp contrib/dash/travis/* ./build/
        export ELECTRUM_MAINNET=true
        ./build/travis-build-linux-apk.sh
        cp ${BUILD_BIN_DIR}/${APK_NAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL \
            ${BUILD_DIST_DIR}
    fi

    sudo rm -rf build
    mkdir -p build && cp contrib/dash/travis/* ./build/
    export ELECTRUM_MAINNET=false
    ./build/travis-build-linux-apk.sh
    cp ${BUILD_BIN_DIR}/${APK_TNAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL \
        ${BUILD_DIST_DIR}
}


function sign_apk {
    read_jks_storepass

    # Sign mainnet apk
    if [[ -n $IS_RELEASE ]]; then
        jarsigner -verbose \
            -tsa http://sha256timestamp.ws.symantec.com/sha256/timestamp \
            -sigalg SHA1withRSA -digestalg SHA1 \
            -sigfile dash-electrum \
            -keystore ~/.jks/keystore \
            -storepass:env JKS_STOREPASS \
            -keypass:env JKS_KEYPASS \
            dist/${APK_NAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL \
            electrum.dash.org

        zipalign -v 4 \
            dist/${APK_NAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL \
            dist/${NAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$APK_TAIL \

        rm dist/${APK_NAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL
    fi

    # Sign testnet apk
    jarsigner -verbose \
        -tsa http://sha256timestamp.ws.symantec.com/sha256/timestamp \
        -sigalg SHA1withRSA -digestalg SHA1 \
        -sigfile dash-electrum \
        -keystore ~/.jks/keystore \
        -storepass:env JKS_STOREPASS \
        -keypass:env JKS_KEYPASS \
        dist/${APK_TNAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL \
        electrum.dash.org

    zipalign -v 4 \
        dist/${APK_TNAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL \
        dist/${TNAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$APK_TAIL \

    rm dist/${APK_TNAME}-$DASH_ELECTRUM_APK_VERSION-$APP_ANDROID_ARCH-$UAPK_TAIL
}


function build_macos_dmg {
    $ACTIONS_DIR/install-osx.sh
    $ACTIONS_DIR/script-osx.sh
}

function build_linux {
    build_sdist
}


source contrib/dash/travis/electrum_dash_version_env.sh
if [[ -n $IS_RELEASE ]]; then
    echo electrum-dash version is $DASH_ELECTRUM_VERSION, release build
else
    echo electrum-dash version is $DASH_ELECTRUM_VERSION
fi
mkdir -p dist


if [[ ${OSTYPE} == "linux-gnu" ]]; then
    echo "Build for Linux/Windows/Android"
    build_linux $1
elif [[ ${OSTYPE} == "darwin"* ]]; then
    echo "Build for macOS"
    build_macos_dmg $1
else
    echo "Unknown OS: ${OSTYPE}"
    exit 1
fi
