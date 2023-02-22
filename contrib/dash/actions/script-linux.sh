#!/bin/bash
set -ev

source contrib/build-linux/appimage/docker_env.sh
source contrib/build-linux/sdist/docker_env.sh

mkdir -p dist

docker run --rm \
    -v $(pwd):/opt \
    -w /opt/ \
    -t $DOCKER_IMG_BUILD_SDIST \
    /opt/contrib/build-linux/sdist/build.sh

sudo find . -name '*.po' -delete
sudo find . -name '*.pot' -delete

docker run --rm \
    -v $(pwd):/opt \
    -w /opt/contrib/build-linux/appimage \
    -t $DOCKER_IMG_BUILD_APPIMAGE ./build.sh
