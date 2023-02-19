#!/bin/bash
set -ev

if [[ $ELECTRUM_MAINNET == "true" ]] && [[ -z $IS_RELEASE ]]; then
    # do not build mainnet apk if is not release
    exit 0
fi

cd build
if [[ -n $TRAVIS_TAG ]]; then
    BUILD_REPO_URL=https://github.com/firstcryptoman/electrum-firo.git
    git clone --branch $TRAVIS_TAG $BUILD_REPO_URL electrum-firo
else
    git clone .. electrum-firo
fi


pushd electrum-firo
./contrib/make_locale
find . -name '*.po' -delete
find . -name '*.pot' -delete
popd

pushd electrum-firo/contrib/android
python3 -m virtualenv --python=python3 atlas_env
source atlas_env/bin/activate
pip install Kivy Pillow
make theming
deactivate
rm -rf atlas_env
popd

# patch buildozer to support APK_VERSION_CODE env
VERCODE_PATCH_PATH=/home/buildozer/build/contrib/dash/travis
VERCODE_PATCH="$VERCODE_PATCH_PATH/read_apk_version_code.patch"

DOCKER_CMD="pushd /opt/buildozer"
DOCKER_CMD="$DOCKER_CMD && patch -p0 < $VERCODE_PATCH && popd"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages"
DOCKER_CMD="$DOCKER_CMD && ./contrib/make_packages"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/bls_py"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/python_bls*"
DOCKER_CMD="$DOCKER_CMD && pushd contrib/deterministic-build/"
DOCKER_CMD="$DOCKER_CMD && python3 -m pip install --no-dependencies --user"
DOCKER_CMD="$DOCKER_CMD -r requirements-build-android.txt && popd"
DOCKER_CMD="$DOCKER_CMD && ./contrib/android/make_apk"

if [[ $ELECTRUM_MAINNET == "false" ]]; then
    DOCKER_CMD="$DOCKER_CMD release-testnet"
fi

sudo chown -R 1000 electrum-firo
docker run --rm \
    --env APP_ANDROID_ARCH=$APP_ANDROID_ARCH \
    --env APK_VERSION_CODE=$DASH_ELECTRUM_VERSION_CODE \
    -v $(pwd)/electrum-firo:/home/buildozer/build \
    -t zebralucky/electrum-dash-winebuild:Kivy40x bash -c \
    "$DOCKER_CMD"
