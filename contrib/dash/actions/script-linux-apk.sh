#!/bin/bash
set -ev

source contrib/android/docker_env.sh

./contrib/make_locale
find . -name '*.po' -delete
find . -name '*.pot' -delete

pushd contrib/android
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

sudo chown -R 1000 .
docker run --rm \
    --env APP_ANDROID_ARCH=$APP_ANDROID_ARCH \
    --env APK_VERSION_CODE=$DASH_ELECTRUM_VERSION_CODE \
    -v $(pwd):/home/buildozer/build \
    -t $DOCKER_IMG_BUILD_ANDROID bash -c \
    "$DOCKER_CMD"
